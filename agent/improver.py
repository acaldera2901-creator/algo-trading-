"""
Auto-improvement module — learns trade-by-trade.

After every closed trade:
1. Records the outcome with market context
2. Updates rule weights in learned_rules.json
3. Strengthens patterns that work, weakens those that don't
4. Periodically runs hyperparameter tuning with optuna
5. Suggests updated risk parameters to RiskManager
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
from agent.risk_manager import RiskParams

logger = logging.getLogger(__name__)

LEARNED_RULES_PATH = config.LEARNED_RULES_PATH
TRADE_LOG_PATH = config.TRADE_LOG_PATH


# ─── SQLite Trade Log ─────────────────────────────────────────────────────────

def init_trade_log():
    """Create the trade log database if it doesn't exist."""
    TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(TRADE_LOG_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            direction TEXT,
            entry_price REAL,
            exit_price REAL,
            stop_loss REAL,
            take_profit REAL,
            position_size REAL,
            pnl REAL,
            pnl_pct REAL,
            outcome TEXT,
            confidence REAL,
            patterns TEXT,
            rsi REAL,
            macd_diff REAL,
            trend TEXT,
            atr REAL,
            entry_time TEXT,
            exit_time TEXT,
            duration_minutes REAL
        )
    """)
    conn.commit()
    conn.close()


def log_trade(trade: dict):
    """Insert a completed trade into the log."""
    conn = sqlite3.connect(str(TRADE_LOG_PATH))
    conn.execute("""
        INSERT INTO trades (
            symbol, direction, entry_price, exit_price, stop_loss, take_profit,
            position_size, pnl, pnl_pct, outcome, confidence, patterns,
            rsi, macd_diff, trend, atr, entry_time, exit_time, duration_minutes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade.get("symbol"), trade.get("direction"),
        trade.get("entry_price"), trade.get("exit_price"),
        trade.get("stop_loss"), trade.get("take_profit"),
        trade.get("position_size"), trade.get("pnl"), trade.get("pnl_pct"),
        trade.get("outcome"),      # "win" | "loss" | "breakeven"
        trade.get("confidence"), json.dumps(trade.get("patterns", [])),
        trade.get("rsi"), trade.get("macd_diff"), trade.get("trend"),
        trade.get("atr"), trade.get("entry_time"), trade.get("exit_time"),
        trade.get("duration_minutes"),
    ))
    conn.commit()
    conn.close()


def get_recent_trades(n: int = 50) -> list[dict]:
    """Fetch the N most recent trades."""
    conn = sqlite3.connect(str(TRADE_LOG_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Rule Engine ──────────────────────────────────────────────────────────────

def load_learned_rules() -> dict:
    if LEARNED_RULES_PATH.exists():
        with open(LEARNED_RULES_PATH, "r") as f:
            return json.load(f)
    return {"version": 1, "last_updated": None, "rules": [], "risk_adjustments": {}, "performance_stats": {}}


def save_learned_rules(rules: dict):
    rules["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(LEARNED_RULES_PATH, "w") as f:
        json.dump(rules, f, indent=2)


def _find_or_create_rule(rules_list: list, pattern_key: str) -> dict:
    for r in rules_list:
        if r["pattern"] == pattern_key:
            return r
    new_rule = {
        "pattern": pattern_key,
        "weight": 1.0,
        "successes": 0,
        "failures": 0,
        "total": 0,
        "win_rate": 0.0,
        "suspended": False,
    }
    rules_list.append(new_rule)
    return new_rule


def update_rules_after_trade(trade: dict):
    """
    Core learning function — called after every closed trade.
    Updates weights of all patterns that were active in this trade.
    """
    learned = load_learned_rules()
    rules_list = learned.setdefault("rules", [])
    patterns = trade.get("patterns", [])
    outcome = trade.get("outcome", "loss")

    for pattern in patterns:
        rule = _find_or_create_rule(rules_list, pattern)
        rule["total"] += 1

        if outcome == "win":
            rule["successes"] += 1
            rule["weight"] = min(rule["weight"] * 1.15, 3.0)  # reinforce, cap at 3x
        elif outcome == "loss":
            rule["failures"] += 1
            rule["weight"] = max(rule["weight"] * 0.85, 0.1)  # weaken, floor at 0.1x

        rule["win_rate"] = rule["successes"] / rule["total"] if rule["total"] > 0 else 0.0

        # Suspend if consistently failing
        if rule["failures"] >= config.RULE_FAILURE_THRESHOLD and rule["win_rate"] < 0.3:
            if not rule["suspended"]:
                rule["suspended"] = True
                logger.warning(f"[Improver] Pattern '{pattern}' suspended (win rate {rule['win_rate']:.0%})")

        # Reinstate if recovered
        if rule["suspended"] and rule["win_rate"] >= 0.5 and rule["successes"] >= config.RULE_SUCCESS_THRESHOLD:
            rule["suspended"] = False
            logger.info(f"[Improver] Pattern '{pattern}' reinstated (win rate {rule['win_rate']:.0%})")

    # Update global performance stats
    recent = get_recent_trades(100)
    wins = [t for t in recent if t.get("outcome") == "win"]
    losses = [t for t in recent if t.get("outcome") == "loss"]
    pnls = [t.get("pnl", 0) for t in recent if t.get("pnl") is not None]

    stats = learned.setdefault("performance_stats", {})
    stats["total_trades"] = len(recent)
    stats["winning_trades"] = len(wins)
    stats["losing_trades"] = len(losses)
    stats["win_rate"] = len(wins) / len(recent) if recent else 0.0
    stats["avg_pnl"] = sum(pnls) / len(pnls) if pnls else 0.0

    # Best/worst patterns by win_rate
    active_rules = [r for r in rules_list if r["total"] >= 3]
    if active_rules:
        sorted_rules = sorted(active_rules, key=lambda r: r["win_rate"], reverse=True)
        stats["best_patterns"] = [r["pattern"] for r in sorted_rules[:3]]
        stats["worst_patterns"] = [r["pattern"] for r in sorted_rules[-3:]]

    save_learned_rules(learned)
    logger.info(
        f"[Improver] Rules updated | Win rate: {stats.get('win_rate', 0):.0%} "
        f"| Total trades: {stats.get('total_trades', 0)}"
    )


# ─── Risk Parameter Auto-Tuning ───────────────────────────────────────────────

def suggest_risk_adjustments(current_params: RiskParams) -> dict:
    """
    Analyze recent performance and suggest updated risk parameters.
    Returns dict of suggested changes (only what should change).
    """
    recent = get_recent_trades(config.MIN_TRADES_FOR_TUNING)
    if len(recent) < config.MIN_TRADES_FOR_TUNING:
        logger.info(f"[Improver] Not enough trades for tuning ({len(recent)}/{config.MIN_TRADES_FOR_TUNING})")
        return {}

    wins = [t for t in recent if t.get("outcome") == "win"]
    losses = [t for t in recent if t.get("outcome") == "loss"]
    win_rate = len(wins) / len(recent) if recent else 0

    suggestions = {}

    # If win rate is high, we can afford slightly more risk
    if win_rate >= 0.65:
        new_risk = min(current_params.risk_per_trade_pct * 1.1, 2.0)
        if new_risk != current_params.risk_per_trade_pct:
            suggestions["risk_per_trade_pct"] = round(new_risk, 2)

    # If win rate is low, reduce risk
    elif win_rate < 0.4:
        new_risk = max(current_params.risk_per_trade_pct * 0.8, 0.5)
        if new_risk != current_params.risk_per_trade_pct:
            suggestions["risk_per_trade_pct"] = round(new_risk, 2)

    # Adjust TP multiplier basato su RR realizzato
    # Non ridurre TP solo perché si finisce sempre in SL — è normale in un sistema trend-following
    # Riduci TP solo se il avg_win è molto inferiore all'avg_loss (RR inverso)
    if len(wins) >= 5 and len(losses) >= 5:
        avg_win_pnl  = sum(abs(t["pnl"]) for t in wins)   / len(wins)
        avg_loss_pnl = sum(abs(t["pnl"]) for t in losses) / len(losses)
        realized_rr  = avg_win_pnl / avg_loss_pnl if avg_loss_pnl > 0 else 1.0
        # Se RR realizzato è ok (>= 1.0) non toccare TP
        if realized_rr < 0.8:
            new_tp = max(current_params.tp_multiplier * 0.95, 1.8)  # floor a 1.8×ATR
            suggestions["tp_multiplier"] = round(new_tp, 2)
        elif realized_rr > 1.5 and win_rate >= 0.5:
            # Possiamo alzare TP se RR è alto e WR è buona
            new_tp = min(current_params.tp_multiplier * 1.05, 4.0)
            suggestions["tp_multiplier"] = round(new_tp, 2)

    if suggestions:
        logger.info(f"[Improver] Suggested risk adjustments: {suggestions}")

    return suggestions


def run_optuna_tuning(risk_params: RiskParams) -> Optional[dict]:
    """
    Use optuna to search for optimal risk parameters based on trade history.
    Runs only if enough trades are available.
    Returns best params or None.
    """
    recent = get_recent_trades(100)
    if len(recent) < config.MIN_TRADES_FOR_TUNING:
        return None

    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        pnls_by_trade = {t["id"]: t.get("pnl_pct", 0) for t in recent}

        def objective(trial):
            rp = trial.suggest_float("risk_per_trade_pct", 0.5, 2.0)
            sl_mult = trial.suggest_float("sl_multiplier", 1.0, 3.0)
            tp_mult = trial.suggest_float("tp_multiplier", 1.0, 4.0)
            conf_thresh = trial.suggest_float("confidence_threshold", 0.5, 0.8)

            filtered = [t for t in recent if (t.get("confidence") or 0) >= conf_thresh]
            if not filtered:
                return 0.0

            simulated_pnl = sum(t.get("pnl_pct", 0) * (rp / 1.0) for t in filtered)
            return simulated_pnl

        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials=50, show_progress_bar=False)

        best = study.best_params
        logger.info(f"[Improver] Optuna tuning complete: {best}")
        return best
    except Exception as e:
        logger.error(f"[Improver] Optuna error: {e}")
        return None


# ─── Main entry ───────────────────────────────────────────────────────────────

def on_trade_closed(trade: dict, risk_params: RiskParams):
    """
    Called by core.py after every trade closes.
    Runs the full learning cycle.
    """
    log_trade(trade)
    update_rules_after_trade(trade)
    suggestions = suggest_risk_adjustments(risk_params)
    if suggestions:
        risk_params.update(**suggestions)
        # Persist adjustments to learned_rules
        learned = load_learned_rules()
        learned["risk_adjustments"].update(suggestions)
        save_learned_rules(learned)
