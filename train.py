"""
Training Script v2 — Addestramento su XAUUSD e BTCUSD (CFD)

Approccio ottimizzato:
1. Scarica dati storici H1 (2 anni) via Yahoo Finance diretta
2. Pre-calcola indicatori SMC in modo vettorizzato (molto più veloce)
3. Simula entrate/uscite candle-by-candle
4. Chiama on_trade_closed → aggiorna learned_rules.json dopo ogni trade
5. Loop notturno continuo (raddoppia training ogni ciclo con nuovi dati)

Eseguito in background per tutta la notte.
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from data.yahoo_fetcher import fetch_ohlcv
from agent.improver import init_trade_log, on_trade_closed, load_learned_rules
from agent.risk_manager import RiskManager, RiskParams

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("training.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("train")

# ─── Config ───────────────────────────────────────────────────────────────────

SYMBOLS = [
    {"mt5": "XAUUSD", "yf": "GC=F",   "name": "Gold CFD"},
    {"mt5": "BTCUSD", "yf": "BTC-USD", "name": "Bitcoin CFD"},
]

SL_ATR_MULT  = 1.5
TP_ATR_MULT  = 2.5
MIN_SCORE    = 0.25
LOOKBACK     = 50   # Candle precedenti per calcolo indicatori (50 è abbastanza per SMC)
INITIAL_BAL  = 10_000.0
RISK_PCT     = 1.0
NIGHT_LOOPS  = 8    # Quante passate stanotte (ogni passata = full backtest)


# ─── Indicatori vettorizzati ──────────────────────────────────────────────────

def compute_atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_swing_highs_lows(high: pd.Series, low: pd.Series, window: int = 5) -> tuple:
    """Swing highs e lows vettorizzati."""
    sh = (high == high.rolling(window * 2 + 1, center=True).max())
    sl = (low  == low.rolling(window * 2 + 1, center=True).min())
    return sh, sl


def compute_signals_vectorized(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcola tutti i segnali SMC in modo vettorizzato su tutto il dataset.
    Ritorna un DataFrame con colonne: score, direction, atr, patterns_str
    """
    out = pd.DataFrame(index=df.index)
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # ── ATR ──
    atr = compute_atr_series(df, 14)
    out["atr"] = atr

    # ── Trend: HH/HL vs LH/LL (rolling 20 candle) ──
    roll_high_max = high.rolling(20).max()
    roll_high_prev = high.rolling(20).max().shift(5)
    roll_low_min   = low.rolling(20).min()
    roll_low_prev  = low.rolling(20).min().shift(5)

    trend_up   = (roll_high_max > roll_high_prev) & (roll_low_min > roll_low_prev)
    trend_down = (roll_high_max < roll_high_prev) & (roll_low_min < roll_low_prev)

    # ── Swing high/low per BSL/SSL ──
    sh, sl_mask = compute_swing_highs_lows(high, low, window=5)
    # Ultimo swing high/low (rolling window 30 candle)
    bsl = high.where(sh).rolling(30, min_periods=1).max()
    ssl = low.where(sl_mask).rolling(30, min_periods=1).min()

    # ── Liquidity sweep ──
    swept_bsl = (high.rolling(5).max() >= bsl)
    swept_ssl = (low.rolling(5).min() <= ssl)

    # ── BOS: prezzo chiude oltre swing recente ──
    bos_bull = close > bsl.shift(1)
    bos_bear = close < ssl.shift(1)

    # ── Daily open (approssimato: apertura candle a inizio giornata) ──
    daily_open = close.resample("D").first().reindex(close.index, method="ffill")
    above_daily_open = close > daily_open

    # ── FVG vettorizzato: c1.high < c3.low (bullish) / c1.low > c3.high (bearish) ──
    c1_high = df["high"].shift(2)
    c3_low  = df["low"]
    c1_low  = df["low"].shift(2)
    c3_high = df["high"]
    fvg_bull = c1_high < c3_low   # bullish FVG
    fvg_bear = c1_low  > c3_high  # bearish FVG

    # ── Session (semplificato: ora UTC) ──
    hour = pd.to_datetime(df.index).hour
    session_london   = (hour >= 8)  & (hour < 13)
    session_ny       = (hour >= 13) & (hour < 22)
    session_asian    = ~(session_london | session_ny)

    # ── Calcolo score composito ──
    score = pd.Series(0.0, index=df.index)

    # Trend (±0.20)
    score += trend_up.astype(float)   * 0.20
    score -= trend_down.astype(float) * 0.20

    # BOS (±0.15)
    score += bos_bull.astype(float) * 0.15
    score -= bos_bear.astype(float) * 0.15

    # Liquidity sweep in London/NY (±0.30)
    score += (swept_ssl & (session_london | session_ny)).astype(float) * 0.30
    score -= (swept_bsl & (session_london | session_ny)).astype(float) * 0.30

    # FVG (±0.15)
    score += fvg_bull.astype(float) * 0.15
    score -= fvg_bear.astype(float) * 0.15

    # Above/below daily open (±0.10)
    score += (above_daily_open & trend_up).astype(float)   * 0.10
    score -= (~above_daily_open & trend_down).astype(float) * 0.10

    # Asian session riduzione ×0.4
    score = score.where(~session_asian, score * 0.4)

    out["score"] = score
    out["trend_up"]   = trend_up
    out["trend_down"] = trend_down
    out["swept_ssl"]  = swept_ssl
    out["swept_bsl"]  = swept_bsl
    out["bos_bull"]   = bos_bull
    out["bos_bear"]   = bos_bear
    out["fvg_bull"]   = fvg_bull
    out["fvg_bear"]   = fvg_bear
    out["session_london"] = session_london
    out["session_ny"]     = session_ny
    out["session_asian"]  = session_asian

    return out


def build_patterns(row) -> list:
    """Ricostruisce lista pattern per una riga di signals."""
    patterns = []
    if row.get("trend_up"):   patterns.append("trend_up")
    if row.get("trend_down"): patterns.append("trend_down")
    if row.get("swept_ssl") and (row.get("session_london") or row.get("session_ny")):
        patterns.append("ssl_sweep_bullish_signal")
    if row.get("swept_bsl") and (row.get("session_london") or row.get("session_ny")):
        patterns.append("bsl_sweep_bearish_signal")
    if row.get("bos_bull"):   patterns.append("bos_bullish")
    if row.get("bos_bear"):   patterns.append("bos_bearish")
    if row.get("fvg_bull"):   patterns.append("price_at_bullish_fvg")
    if row.get("fvg_bear"):   patterns.append("price_at_bearish_fvg")
    if row.get("session_london"): patterns.append("session_london")
    elif row.get("session_ny"):   patterns.append("session_new_york")
    elif row.get("session_asian"): patterns.append("asian_session_caution")
    return patterns


# ─── Backtest ─────────────────────────────────────────────────────────────────

def run_backtest(symbol: str, df: pd.DataFrame, risk_params: RiskParams,
                 loop_num: int = 1) -> dict:
    """Esegue backtest vettorizzato su un simbolo."""
    logger.info(f"\n{'='*60}")
    logger.info(f"BACKTEST #{loop_num}: {symbol} — {len(df)} candle H1")
    logger.info(f"Periodo: {df.index[0].date()} → {df.index[-1].date()}")
    logger.info(f"{'='*60}")

    t0 = time.time()

    # Calcola tutti i segnali in una volta (vettorizzato)
    signals = compute_signals_vectorized(df)
    logger.info(f"Indicatori calcolati in {time.time()-t0:.1f}s")

    balance = INITIAL_BAL
    peak    = balance
    contract_size = config.SYMBOL_CONTRACT_SIZES.get(symbol.upper(), 100_000)

    trades = []
    open_trade = None  # dict con i dettagli della posizione aperta

    for i in range(LOOKBACK, len(df)):
        row = signals.iloc[i]
        candle = df.iloc[i]
        score = row["score"]
        atr   = row["atr"]

        high  = candle["high"]
        low   = candle["low"]
        close = candle["close"]

        # ── Gestione posizione aperta ──
        if open_trade:
            closed, exit_price, outcome = False, None, None

            if open_trade["direction"] == "buy":
                if low <= open_trade["sl"]:
                    closed, exit_price, outcome = True, open_trade["sl"], "loss"
                elif high >= open_trade["tp"]:
                    closed, exit_price, outcome = True, open_trade["tp"], "win"
            else:
                if high >= open_trade["sl"]:
                    closed, exit_price, outcome = True, open_trade["sl"], "loss"
                elif low <= open_trade["tp"]:
                    closed, exit_price, outcome = True, open_trade["tp"], "win"

            if closed:
                pnl = (exit_price - open_trade["entry"]) * open_trade["lots"] * contract_size
                if open_trade["direction"] == "sell":
                    pnl = -pnl
                pnl_pct = pnl / balance * 100
                balance += pnl
                if balance > peak:
                    peak = balance

                record = {
                    "symbol": symbol,
                    "direction": open_trade["direction"],
                    "entry_price": open_trade["entry"],
                    "exit_price": exit_price,
                    "stop_loss": open_trade["sl"],
                    "take_profit": open_trade["tp"],
                    "position_size": open_trade["lots"],
                    "pnl": round(pnl, 4),
                    "pnl_pct": round(pnl_pct, 4),
                    "outcome": outcome,
                    "confidence": abs(open_trade["score"]),
                    "patterns": open_trade["patterns"],
                    "trend": "up" if open_trade["score"] > 0 else "down",
                    "atr": open_trade["atr"],
                    "session": open_trade["session"],
                    "daily_bias": "bullish" if open_trade["score"] > 0 else "bearish",
                    "entry_time": open_trade["time"].isoformat(),
                    "exit_time":  df.index[i].isoformat(),
                }
                on_trade_closed(record, risk_params)
                trades.append(record)
                open_trade = None

                if len(trades) % 200 == 0:
                    wins = sum(1 for t in trades if t["outcome"] == "win")
                    wr   = wins / len(trades) * 100
                    logger.info(
                        f"[{symbol}] Ciclo #{loop_num} | Trade #{len(trades):4d} | "
                        f"WR {wr:4.1f}% | Balance ${balance:,.0f}"
                    )
            continue  # Non aprire con posizione aperta

        # ── Nuova entrata ──
        if abs(score) < MIN_SCORE or not atr or atr <= 0:
            continue
        if row["session_asian"]:
            continue

        direction = "buy" if score > 0 else "sell"
        sl_dist   = atr * SL_ATR_MULT
        tp_dist   = atr * TP_ATR_MULT

        if direction == "buy":
            sl = close - sl_dist
            tp = close + tp_dist
        else:
            sl = close + sl_dist
            tp = close - tp_dist

        risk_amount = balance * (RISK_PCT / 100)
        lots = max(round(risk_amount / (sl_dist * contract_size), 2), config.MIN_LOT_SIZE)
        lots = min(lots, config.MAX_LOT_SIZE)

        session = "london" if row["session_london"] else "new_york" if row["session_ny"] else "asian"
        patterns = build_patterns(row.to_dict())

        open_trade = {
            "direction": direction,
            "entry": close,
            "sl": sl,
            "tp": tp,
            "lots": lots,
            "score": score,
            "atr": atr,
            "session": session,
            "patterns": patterns,
            "time": df.index[i],
        }

    # ── Statistiche ──
    wins   = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    wr     = len(wins) / len(trades) * 100 if trades else 0
    total_pnl = sum(t["pnl"] for t in trades)
    avg_w     = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
    avg_l     = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    pf = abs(avg_w * len(wins)) / abs(avg_l * len(losses)) if losses and avg_l != 0 else float("inf")
    dd = (peak - balance) / peak * 100 if peak else 0

    logger.info(f"\n{'─'*50}")
    logger.info(f"RISULTATI {symbol} (ciclo #{loop_num}):")
    logger.info(f"  Trade:          {len(trades):4d}  (W:{len(wins)} L:{len(losses)})")
    logger.info(f"  Win rate:       {wr:5.1f}%")
    logger.info(f"  Profit factor:  {pf:.2f}")
    logger.info(f"  PnL totale:     ${total_pnl:+,.2f}")
    logger.info(f"  Balance finale: ${balance:,.0f}")
    logger.info(f"  Max Drawdown:   {dd:.1f}%")
    logger.info(f"  Tempo:          {time.time()-t0:.0f}s")
    logger.info(f"{'─'*50}")

    return {
        "symbol": symbol, "loop": loop_num,
        "total_trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": round(wr, 2), "profit_factor": round(pf, 2),
        "total_pnl": round(total_pnl, 2), "final_balance": round(balance, 2),
        "max_dd": round(dd, 2),
    }


def print_learned_rules_summary():
    """Stampa le regole apprese dal DB."""
    rules = load_learned_rules()
    active = sorted(
        [r for r in rules.get("rules", []) if r.get("total", 0) >= 5],
        key=lambda r: r["win_rate"], reverse=True
    )
    perf = rules.get("performance_stats", {})

    logger.info(f"\n{'='*60}")
    logger.info("REGOLE APPRESE (≥5 occorrenze):")
    logger.info(f"{'='*60}")
    for r in active[:12]:
        bar = "█" * int(r["win_rate"] * 10) + "░" * (10 - int(r["win_rate"] * 10))
        status = "[SOSPESA]" if r.get("suspended") else ""
        logger.info(
            f"  {bar} {r['win_rate']*100:5.1f}% | "
            f"W:{r['successes']:3d} L:{r['failures']:3d} | "
            f"w={r['weight']:.2f} | {r['pattern']} {status}"
        )

    logger.info(f"\n  DB totale:  {perf.get('total_trades', 0)} trade")
    logger.info(f"  Win rate:   {perf.get('win_rate', 0)*100:.1f}%")
    logger.info(f"  PnL medio:  {perf.get('avg_pnl', 0):.4f}")

    best = perf.get("best_patterns", [])
    worst = perf.get("worst_patterns", [])
    if best:
        logger.info(f"  Top:   {best}")
    if worst:
        logger.info(f"  Worst: {worst}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 60)
    logger.info("  TRAINING NOTTURNO — XAUUSD e BTCUSD (CFD)")
    logger.info(f"  Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"  Cicli pianificati: {NIGHT_LOOPS}")
    logger.info("=" * 60)

    init_trade_log()
    risk_params = RiskParams()
    all_results = []

    # Scarica dati una volta sola
    data = {}
    for sym in SYMBOLS:
        logger.info(f"Scaricando {sym['name']} ({sym['yf']}) H1...")
        df = fetch_ohlcv(sym["yf"], "1h", 730)
        if df.empty:
            logger.error(f"ERRORE: nessun dato per {sym['mt5']} — salto")
            continue
        data[sym["mt5"]] = {"config": sym, "df": df}
        logger.info(f"  OK: {len(df)} candle | prezzo: {df['close'].iloc[-1]:.2f}")

    if not data:
        logger.error("Nessun dato scaricato. Addestramento impossibile.")
        sys.exit(1)

    # Loop di training notturno
    for loop in range(1, NIGHT_LOOPS + 1):
        logger.info(f"\n{'#'*60}")
        logger.info(f"#  CICLO DI TRAINING {loop}/{NIGHT_LOOPS}")
        logger.info(f"#  {datetime.now().strftime('%H:%M:%S')}")
        logger.info(f"{'#'*60}")

        for sym_key, sym_data in data.items():
            result = run_backtest(
                symbol=sym_key,
                df=sym_data["df"],
                risk_params=risk_params,
                loop_num=loop,
            )
            all_results.append(result)

        # Aggiorna parametri di rischio dopo ogni ciclo
        from agent.improver import suggest_risk_adjustments
        suggestions = suggest_risk_adjustments(risk_params)
        if suggestions:
            risk_params.update(**suggestions)
            logger.info(f"Parametri aggiornati: {suggestions}")

        # Mostra regole apprese dopo ogni ciclo
        print_learned_rules_summary()

        # Pausa tra i cicli (5 minuti) per non sovraccaricare il sistema
        if loop < NIGHT_LOOPS:
            logger.info(f"\nPausa 5 min prima del ciclo {loop + 1}...")
            time.sleep(300)

    # ── Riepilogo finale ──
    logger.info(f"\n{'='*60}")
    logger.info("RIEPILOGO FINALE ADDESTRAMENTO NOTTURNO")
    logger.info(f"{'='*60}")

    total_sim_trades = sum(r["total_trades"] for r in all_results)
    logger.info(f"Trade simulati totali: {total_sim_trades:,}")

    # Per simbolo, media dei cicli
    for sym_key in data:
        sym_results = [r for r in all_results if r["symbol"] == sym_key]
        avg_wr = sum(r["win_rate"] for r in sym_results) / len(sym_results)
        avg_pf = sum(r["profit_factor"] for r in sym_results if r["profit_factor"] != float("inf")) / len(sym_results)
        total_pnl_all = sum(r["total_pnl"] for r in sym_results)
        logger.info(
            f"  {sym_key}: {len(sym_results)} cicli | "
            f"WR medio {avg_wr:.1f}% | PF medio {avg_pf:.2f} | "
            f"PnL totale simulato ${total_pnl_all:+,.0f}"
        )

    print_learned_rules_summary()
    logger.info(f"\n✓ Training notturno completato alle {datetime.now().strftime('%H:%M:%S')}")
    logger.info("  Sistema pronto per collegamento MT5 Demo domani mattina.")


if __name__ == "__main__":
    main()
