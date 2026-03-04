"""
Hybrid Analyzer — combines course knowledge + learned experience.

Decision pipeline:
1. Load course rules (static knowledge from scraper)
2. Load learned rules (dynamic, updated by improver after each trade)
3. Score market conditions against both layers
4. Use Claude AI to interpret ambiguous setups using both sources as context
5. Return a final signal with confidence score and active patterns
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
    direction: str          # "buy" | "sell" | "none"
    confidence: float       # 0.0 – 1.0
    patterns: list[str]     # active pattern keys
    reasoning: str          # human-readable explanation
    indicators_used: dict = field(default_factory=dict)


# ─── Knowledge Loading ────────────────────────────────────────────────────────

def _load_course_knowledge() -> dict:
    if config.COURSE_KNOWLEDGE_PATH.exists():
        with open(config.COURSE_KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    logger.warning("[Analyzer] course_knowledge.json not found — run scraper first")
    return {}


def _load_learned_rules() -> dict:
    if config.LEARNED_RULES_PATH.exists():
        with open(config.LEARNED_RULES_PATH, "r") as f:
            return json.load(f)
    return {"rules": []}


def _get_active_rules(learned: dict) -> list[dict]:
    """Return non-suspended rules sorted by weight descending."""
    rules = learned.get("rules", [])
    return sorted(
        [r for r in rules if not r.get("suspended", False)],
        key=lambda r: r.get("weight", 1.0),
        reverse=True,
    )


# ─── Rule-Based Scoring ───────────────────────────────────────────────────────

def _score_with_rules(snapshot: MarketSnapshot, course: dict, learned_rules: list[dict]) -> tuple[float, list[str]]:
    """
    Score market conditions using rule-based logic from course + learned experience.
    Returns (score, active_patterns) where score in [-1, 1]: positive=buy, negative=sell.
    """
    ind = snapshot.indicators
    if not ind:
        return 0.0, []

    score = 0.0
    patterns = []

    rsi = ind.get("rsi", 50)
    macd_diff = ind.get("macd_diff", 0)
    trend = ind.get("trend", "unknown")
    close = ind.get("current_close", 0)
    ema_20 = ind.get("ema_20", close)
    ema_50 = ind.get("ema_50", close)
    bb_upper = ind.get("bb_upper", close)
    bb_lower = ind.get("bb_lower", close)
    bb_middle = ind.get("bb_middle", close)
    recent_high = ind.get("recent_high", close)
    recent_low = ind.get("recent_low", close)

    # ── Course-derived rules (general technical analysis) ──

    # RSI oversold/overbought
    if rsi < 35:
        score += 0.3
        patterns.append("rsi_oversold")
    elif rsi > 65:
        score -= 0.3
        patterns.append("rsi_overbought")

    # MACD crossover
    if macd_diff > 0:
        score += 0.2
        patterns.append("macd_bullish")
    elif macd_diff < 0:
        score -= 0.2
        patterns.append("macd_bearish")

    # Trend alignment
    if trend == "up" and close > ema_20:
        score += 0.25
        patterns.append("trend_up_price_above_ema")
    elif trend == "down" and close < ema_20:
        score -= 0.25
        patterns.append("trend_down_price_below_ema")

    # Bollinger Band bounce
    if close <= bb_lower:
        score += 0.2
        patterns.append("bb_lower_bounce")
    elif close >= bb_upper:
        score -= 0.2
        patterns.append("bb_upper_bounce")

    # EMA crossover
    if ema_20 and ema_50:
        if ema_20 > ema_50:
            score += 0.15
            patterns.append("ema_20_above_50")
        else:
            score -= 0.15
            patterns.append("ema_20_below_50")

    # Support/Resistance proximity
    proximity_pct = 0.003  # 0.3%
    if abs(close - recent_low) / close < proximity_pct:
        score += 0.15
        patterns.append("near_support")
    if abs(close - recent_high) / close < proximity_pct:
        score -= 0.15
        patterns.append("near_resistance")

    # ── Apply learned rule weights ──
    pattern_weight_map = {r["pattern"]: r.get("weight", 1.0) for r in learned_rules}

    weighted_score = 0.0
    for pattern in patterns:
        w = pattern_weight_map.get(pattern, 1.0)
        base_contribution = score / len(patterns) if patterns else 0
        weighted_score += base_contribution * w

    # Normalize to [-1, 1]
    final_score = max(-1.0, min(1.0, weighted_score))
    return final_score, patterns


# ─── Claude AI Interpretation ─────────────────────────────────────────────────

def _ai_interpret(
    snapshot: MarketSnapshot,
    course: dict,
    learned_rules: list[dict],
    rule_score: float,
    patterns: list[str],
) -> str:
    """
    Use Claude to interpret the setup. Provides reasoning that incorporates
    course knowledge and learned experience.
    """
    if not config.ANTHROPIC_API_KEY:
        return "AI interpretation skipped (no API key)"

    # Build a compact course summary
    course_strategies = course.get("strategies", [])[:5]
    course_entry_rules = course.get("entry_rules", [])[:5]
    course_risk = course.get("risk_management", [])[:3]

    # Best performing patterns from learned experience
    best_learned = [r["pattern"] for r in learned_rules[:3] if r.get("win_rate", 0) > 0.5]

    prompt = f"""You are an expert Forex trading analyst. Analyze this trade setup and provide a brief, actionable assessment.

COURSE KNOWLEDGE:
- Strategies: {course_strategies}
- Entry rules: {course_entry_rules}
- Risk rules: {course_risk}

LEARNED EXPERIENCE (best performing patterns so far):
{best_learned}

CURRENT MARKET ({snapshot.symbol}):
- Indicators: {json.dumps(snapshot.indicators, default=str, indent=2)[:800]}
- Active patterns detected: {patterns}
- Rule-based score: {rule_score:.2f} (positive=bullish, negative=bearish)

Given the course rules AND the learned experience, assess:
1. Is this a valid trade setup? (yes/no)
2. Direction: buy, sell, or wait?
3. Key risk: what could invalidate this setup?
Keep your answer under 150 words."""

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"[Analyzer] Claude API error: {e}")
        return f"AI error: {e}"


# ─── Main Analyze Function ────────────────────────────────────────────────────

def analyze(snapshot: MarketSnapshot) -> Signal:
    """
    Main entry point. Returns a Signal with direction, confidence, and reasoning.
    Combines course rules + learned experience + AI interpretation.
    """
    course = _load_course_knowledge()
    learned = _load_learned_rules()
    active_rules = _get_active_rules(learned)

    # Score with rule-based engine
    rule_score, patterns = _score_with_rules(snapshot, course, active_rules)

    # Determine direction from score
    if rule_score >= 0.25:
        direction = "buy"
    elif rule_score <= -0.25:
        direction = "sell"
    else:
        direction = "none"

    # Confidence: map score magnitude to [0, 1]
    raw_confidence = abs(rule_score)

    # Boost confidence if learned rules strongly agree
    winning_patterns = {r["pattern"] for r in active_rules if r.get("win_rate", 0) > 0.6}
    overlap = len(set(patterns) & winning_patterns)
    if overlap > 0:
        raw_confidence = min(raw_confidence + overlap * 0.05, 1.0)

    # AI interpretation for context
    reasoning = _ai_interpret(snapshot, course, active_rules, rule_score, patterns)

    signal = Signal(
        direction=direction,
        confidence=round(raw_confidence, 3),
        patterns=patterns,
        reasoning=reasoning,
        indicators_used=snapshot.indicators,
    )

    logger.info(
        f"[Analyzer] {snapshot.symbol} → {direction.upper()} "
        f"conf={signal.confidence:.2f} patterns={patterns}"
    )
    return signal
