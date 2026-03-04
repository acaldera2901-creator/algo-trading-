"""
Hybrid Analyzer — SMC/ICT course logic + learned experience.

Decision pipeline (aligned with Space Traders Academy methodology):
1. Load course knowledge (SMC/ICT rules from spacetraders.it)
2. Load learned rules (dynamic, updated after each trade)
3. Apply SMC analysis: daily bias → structure → liquidity sweep → POI → IPA
4. Score confidence from rule weights (course + learned)
5. Use Claude AI to validate setup with full course + experience context
6. Return Signal with direction, confidence, active patterns
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import anthropic

import config
from data.market_data import MarketSnapshot

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    direction: str           # "buy" | "sell" | "none"
    confidence: float        # 0.0 – 1.0
    patterns: list           # active SMC pattern keys
    reasoning: str           # human-readable explanation
    indicators_used: dict = field(default_factory=dict)


# ─── Knowledge Loading ────────────────────────────────────────────────────────

def _load_course_knowledge() -> dict:
    if config.COURSE_KNOWLEDGE_PATH.exists():
        with open(config.COURSE_KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    logger.warning("[Analyzer] course_knowledge.json not found — run python main.py --scrape")
    return {}


def _load_learned_rules() -> dict:
    if config.LEARNED_RULES_PATH.exists():
        with open(config.LEARNED_RULES_PATH, "r") as f:
            return json.load(f)
    return {"rules": []}


def _get_active_rules(learned: dict) -> list:
    rules = learned.get("rules", [])
    return sorted(
        [r for r in rules if not r.get("suspended", False)],
        key=lambda r: r.get("weight", 1.0),
        reverse=True,
    )


# ─── SMC Rule-Based Scoring ───────────────────────────────────────────────────

def _score_smc(snapshot: MarketSnapshot, learned_rules: list) -> tuple:
    """
    Score market conditions using the SMC/ICT methodology from the course.
    Returns (score, active_patterns) where score ∈ [-1, 1].

    The course logic is:
      1. Check daily bias (bullish/bearish)
      2. Check market structure trend (BOS/ChoCH)
      3. Detect liquidity sweep (SLQ/BSL/SSL)
      4. Check for POI nearby (FVG/OB)
      5. Session filter (prefer London/NY)
    """
    ind = snapshot.indicators
    if not ind:
        return 0.0, []

    score = 0.0
    patterns = []
    close = ind.get("current_close", 0)

    # ── 1. Daily Bias (ICT Daily Bias) ──
    daily_bias = ind.get("daily_bias", "unknown")
    if daily_bias == "bullish":
        score += 0.25
        patterns.append("daily_bias_bullish")
    elif daily_bias == "bearish":
        score -= 0.25
        patterns.append("daily_bias_bearish")

    # ── 2. Market Structure (Trend Alignment) ──
    trend = ind.get("trend", "unknown")
    last_event = ind.get("last_bos_choch", "none")

    if trend == "up":
        score += 0.20
        patterns.append("trend_up")
    elif trend == "down":
        score -= 0.20
        patterns.append("trend_down")
    elif trend == "choch_bullish":
        score += 0.15
        patterns.append("choch_bullish")
    elif trend == "choch_bearish":
        score -= 0.15
        patterns.append("choch_bearish")

    if last_event == "BOS_bullish":
        score += 0.15
        patterns.append("bos_bullish")
    elif last_event == "BOS_bearish":
        score -= 0.15
        patterns.append("bos_bearish")

    # ── 3. Liquidity Sweep (SLQ/LIT signal — key course concept) ──
    swept_ssl = ind.get("swept_ssl", False)
    swept_bsl = ind.get("swept_bsl", False)
    session = ind.get("session", "")

    # Course rule: after sweep of SSL → expect bullish move (price hunts SSL then reverses)
    if swept_ssl and session in ("london", "new_york"):
        score += 0.30
        patterns.append("ssl_sweep_bullish_signal")
    # After sweep of BSL → expect bearish move
    elif swept_bsl and session in ("london", "new_york"):
        score -= 0.30
        patterns.append("bsl_sweep_bearish_signal")

    # ── 4. POI Nearby (FVG / Order Block) ──
    atr = ind.get("atr", 0)
    proximity_pips = atr * 0.5 if atr else 0.001

    # Bullish FVG nearby (price approaching bullish FVG from above)
    bullish_fvg = ind.get("nearest_bullish_fvg")
    if bullish_fvg and close <= bullish_fvg["top"] + proximity_pips:
        score += 0.20
        patterns.append("price_at_bullish_fvg")

    # Bearish FVG nearby
    bearish_fvg = ind.get("nearest_bearish_fvg")
    if bearish_fvg and close >= bearish_fvg["bottom"] - proximity_pips:
        score -= 0.20
        patterns.append("price_at_bearish_fvg")

    # Bullish Order Block nearby
    bull_ob = ind.get("nearest_bullish_ob")
    if bull_ob and bull_ob["bottom"] <= close <= bull_ob["top"] + proximity_pips:
        score += 0.20
        patterns.append("price_at_bullish_ob")

    # Bearish Order Block nearby
    bear_ob = ind.get("nearest_bearish_ob")
    if bear_ob and bear_ob["bottom"] - proximity_pips <= close <= bear_ob["top"]:
        score -= 0.20
        patterns.append("price_at_bearish_ob")

    # ── 5. Session Filter (course: trade London/NY, avoid Asian) ──
    if session == "asian":
        score *= 0.4  # significantly reduce confidence during Asian session
        patterns.append("asian_session_caution")
    elif session in ("london", "new_york"):
        patterns.append(f"session_{session}")

    # ── 6. Daily Open Price (ICT concept) ──
    above_daily_open = ind.get("above_daily_open")
    if above_daily_open is True and daily_bias == "bullish":
        score += 0.10
        patterns.append("above_daily_open_bullish")
    elif above_daily_open is False and daily_bias == "bearish":
        score -= 0.10
        patterns.append("below_daily_open_bearish")

    # ── Apply learned rule weights ──
    pattern_weight_map = {r["pattern"]: r.get("weight", 1.0) for r in learned_rules}
    weighted_score = 0.0
    if patterns:
        for pattern in patterns:
            w = pattern_weight_map.get(pattern, 1.0)
            # Each pattern contributes proportionally
            contribution = (score / len(patterns)) * w
            weighted_score += contribution
    else:
        weighted_score = score

    # Boost confidence if learned patterns are consistently winning
    winning_patterns = {r["pattern"] for r in learned_rules if r.get("win_rate", 0) >= 0.6}
    overlap = len(set(patterns) & winning_patterns)
    if overlap >= 2:
        weighted_score *= 1.1  # 10% boost for confirmed winning combination

    return max(-1.0, min(1.0, weighted_score)), patterns


# ─── Claude AI Interpretation ─────────────────────────────────────────────────

def _ai_interpret(
    snapshot: MarketSnapshot,
    course: dict,
    learned_rules: list,
    rule_score: float,
    patterns: list,
) -> str:
    """
    Use Claude to validate the setup using the course's SMC methodology
    and the learned experience from past trades.
    """
    if not config.ANTHROPIC_API_KEY:
        return "AI interpretation skipped (no API key configured)"

    # Key course rules for context
    entry_rules = course.get("entry_rules", [])
    session_rules = course.get("session_rules", {})
    concepts = course.get("concepts", {})

    # Best performing learned patterns
    best_learned = [
        f"{r['pattern']} (win rate {r.get('win_rate', 0):.0%})"
        for r in learned_rules[:5]
        if r.get("win_rate", 0) > 0.5
    ]

    # Compact indicator summary
    ind = snapshot.indicators
    ind_summary = {
        "daily_bias": ind.get("daily_bias"),
        "trend": ind.get("trend"),
        "last_event": ind.get("last_bos_choch"),
        "session": ind.get("session"),
        "swept_ssl": ind.get("swept_ssl"),
        "swept_bsl": ind.get("swept_bsl"),
        "atr": round(ind.get("atr", 0), 5),
        "current_price": ind.get("current_close"),
        "daily_open": ind.get("daily_open"),
        "bullish_fvg": str(ind.get("nearest_bullish_fvg", "none"))[:80],
        "bearish_fvg": str(ind.get("nearest_bearish_fvg", "none"))[:80],
        "bullish_ob": str(ind.get("nearest_bullish_ob", "none"))[:80],
        "bearish_ob": str(ind.get("nearest_bearish_ob", "none"))[:80],
    }

    prompt = f"""You are an expert Forex trader trained in SMC/ICT (Smart Money Concepts / ICT Inner Circle Trader methodology).

COURSE RULES (Space Traders Academy):
Entry rules: {entry_rules[:5]}
Session rules: {json.dumps(session_rules)}
Key concept: {concepts.get('sessions', {}).get('rules', [])[:3]}

LEARNED EXPERIENCE (patterns with >50% win rate from live trading):
{best_learned if best_learned else "No sufficient data yet — rely on course rules"}

CURRENT MARKET: {snapshot.symbol}
{json.dumps(ind_summary, indent=2)}

SMC Patterns detected: {patterns}
Rule-based score: {rule_score:.2f} (positive=bullish, negative=bearish)

Using the SMC/ICT methodology from the course:
1. Is this a VALID trade setup? (yes/partially/no)
2. Recommended direction: buy / sell / wait
3. Main risk that could invalidate this setup?
4. Does the current session support trading this setup?
Keep answer under 120 words."""

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=180,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"[Analyzer] Claude API error: {e}")
        return f"AI error: {e}"


# ─── Main Analyze Function ────────────────────────────────────────────────────

def analyze(snapshot: MarketSnapshot) -> Signal:
    """
    Main entry point. Applies full SMC analysis pipeline.
    Returns a Signal with direction, confidence, patterns, and reasoning.
    """
    course = _load_course_knowledge()
    learned = _load_learned_rules()
    active_rules = _get_active_rules(learned)

    # SMC scoring
    rule_score, patterns = _score_smc(snapshot, active_rules)

    # Determine direction
    if rule_score >= 0.25:
        direction = "buy"
    elif rule_score <= -0.25:
        direction = "sell"
    else:
        direction = "none"

    raw_confidence = abs(rule_score)

    # Boost if winning learned patterns overlap
    winning_patterns = {r["pattern"] for r in active_rules if r.get("win_rate", 0) > 0.6}
    overlap = len(set(patterns) & winning_patterns)
    raw_confidence = min(raw_confidence + overlap * 0.05, 1.0)

    # AI validation
    reasoning = _ai_interpret(snapshot, course, active_rules, rule_score, patterns)

    signal = Signal(
        direction=direction,
        confidence=round(raw_confidence, 3),
        patterns=patterns,
        reasoning=reasoning,
        indicators_used=snapshot.indicators,
    )

    logger.info(
        f"[Analyzer] {snapshot.symbol} | {direction.upper()} conf={signal.confidence:.2f} "
        f"| session={snapshot.session} | patterns={patterns}"
    )
    return signal
