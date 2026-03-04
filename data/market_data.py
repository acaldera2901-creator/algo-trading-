"""
Market data ingestion module.
Fetches OHLCV data from OANDA or MT5 and computes SMC/ICT indicators aligned with the course.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class MarketSnapshot:
    symbol: str
    candles: pd.DataFrame          # columns: open, high, low, close, volume
    indicators: dict = field(default_factory=dict)
    smc: dict = field(default_factory=dict)    # SMC-specific data
    current_price: float = 0.0
    spread: float = 0.0
    session: str = ""              # asian | london | new_york | off


# ─── Session Detection ────────────────────────────────────────────────────────

def get_current_session() -> str:
    now = datetime.now(timezone.utc)
    hour = now.hour
    if 0 <= hour < 8:
        return "asian"
    elif 8 <= hour < 13:
        return "london"
    elif 13 <= hour < 22:
        return "new_york"
    return "off"


# ─── OANDA Fetcher ────────────────────────────────────────────────────────────

def _fetch_oanda(symbol, timeframe, count, api_key, account_id, env):
    try:
        from oandapyV20 import API
        from oandapyV20.endpoints.instruments import InstrumentsCandles
        client = API(access_token=api_key, environment=env)
        gran_map = {"M1": "M1", "M5": "M5", "M15": "M15", "H1": "H1", "H4": "H4", "D1": "D"}
        params = {"granularity": gran_map.get(timeframe, "H1"), "count": count, "price": "M"}
        r = InstrumentsCandles(instrument=symbol, params=params)
        client.request(r)
        rows = []
        for c in r.response["candles"]:
            if c["complete"]:
                mid = c["mid"]
                rows.append({
                    "timestamp": pd.to_datetime(c["time"]),
                    "open": float(mid["o"]), "high": float(mid["h"]),
                    "low": float(mid["l"]), "close": float(mid["c"]),
                    "volume": float(c["volume"]),
                })
        df = pd.DataFrame(rows).set_index("timestamp")
        logger.info(f"OANDA: {len(df)} candles for {symbol} {timeframe}")
        return df
    except Exception as e:
        logger.error(f"OANDA fetch error: {e}")
        return pd.DataFrame()


def _fetch_mt5(symbol, timeframe, count):
    try:
        import MetaTrader5 as mt5
        tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
                  "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1,
                  "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}
        rates = mt5.copy_rates_from_pos(symbol, tf_map.get(timeframe, mt5.TIMEFRAME_H1), 0, count)
        if rates is None:
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df.set_index("timestamp")[["open", "high", "low", "close", "tick_volume"]].rename(
            columns={"tick_volume": "volume"})
    except Exception as e:
        logger.error(f"MT5 fetch error: {e}")
        return pd.DataFrame()


def _fetch_yfinance(symbol, timeframe, count):
    try:
        import yfinance as yf
        tf_map = {"M1": "1m", "M5": "5m", "M15": "15m", "H1": "1h", "H4": "4h", "D1": "1d"}
        period_map = {"M1": "7d", "M5": "7d", "M15": "60d", "H1": "60d", "H4": "60d", "D1": "2y"}
        yf_symbol = symbol.replace("_", "=X") if "_" in symbol else symbol + "=X"
        df = yf.download(yf_symbol, period=period_map.get(timeframe, "60d"),
                         interval=tf_map.get(timeframe, "1h"), progress=False)
        df.columns = [c.lower() for c in df.columns]
        return df[["open", "high", "low", "close", "volume"]].tail(count)
    except Exception as e:
        logger.error(f"yfinance fetch error: {e}")
        return pd.DataFrame()


def _fetch_frankfurter(symbol: str, count: int) -> pd.DataFrame:
    """
    Fallback: fetch daily FX rates from frankfurter.app (free, no auth).
    Converts close-only data into OHLCV by estimating O/H/L from daily volatility.
    Used only when MT5/OANDA/yfinance are unavailable.
    """
    try:
        import requests
        from datetime import date, timedelta

        # Parse symbol: EURUSD → EUR/USD
        sym = symbol.replace("_", "").upper()
        if len(sym) == 6:
            base, quote = sym[:3], sym[3:]
        else:
            logger.error(f"Cannot parse symbol {symbol} for frankfurter")
            return pd.DataFrame()

        end = date.today()
        start = end - timedelta(days=max(count * 2, 90))
        url = f"https://api.frankfurter.app/{start}..{end}?from={base}&to={quote}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        rows = []
        rates_sorted = sorted(data["rates"].items())
        closes = [v[quote] for _, v in rates_sorted if quote in v]

        for i, (date_str, rate_dict) in enumerate(rates_sorted):
            if quote not in rate_dict:
                continue
            close = rate_dict[quote]
            # Estimate OHLCV from close using realistic daily volatility
            vol = abs(closes[i] - closes[i - 1]) if i > 0 else close * 0.003
            vol = max(vol, close * 0.001)
            rows.append({
                "timestamp": pd.Timestamp(date_str, tz="UTC"),
                "open": round(close - vol * 0.3, 5),
                "high": round(close + vol * 0.6, 5),
                "low": round(close - vol * 0.6, 5),
                "close": round(close, 5),
                "volume": 1000,
            })

        df = pd.DataFrame(rows).set_index("timestamp")
        df = df.tail(count)
        logger.info(f"[Frankfurter] {len(df)} daily candles for {symbol} (dev fallback)")
        return df
    except Exception as e:
        logger.error(f"Frankfurter fetch error: {e}")
        return pd.DataFrame()


# ─── SMC / ICT Calculations ───────────────────────────────────────────────────

def find_swing_points(df: pd.DataFrame, window: int = 5) -> dict:
    """Find swing highs and lows (BSL/SSL liquidity levels)."""
    highs = df["high"]
    lows = df["low"]
    n = len(df)

    swing_highs = []
    swing_lows = []

    for i in range(window, n - window):
        if highs.iloc[i] == highs.iloc[i - window:i + window + 1].max():
            swing_highs.append({"index": i, "price": highs.iloc[i], "time": df.index[i]})
        if lows.iloc[i] == lows.iloc[i - window:i + window + 1].min():
            swing_lows.append({"index": i, "price": lows.iloc[i], "time": df.index[i]})

    return {
        "swing_highs": swing_highs[-5:],   # last 5 swing highs (BSL)
        "swing_lows": swing_lows[-5:],     # last 5 swing lows (SSL)
        "last_bsl": swing_highs[-1]["price"] if swing_highs else None,
        "last_ssl": swing_lows[-1]["price"] if swing_lows else None,
    }


def detect_market_structure(df: pd.DataFrame, window: int = 5) -> dict:
    """
    Detect BOS (Break of Structure) and ChoCH (Change of Character).
    Returns trend direction and last structure event.
    """
    swings = find_swing_points(df, window)
    highs = [s["price"] for s in swings["swing_highs"]]
    lows = [s["price"] for s in swings["swing_lows"]]

    trend = "unknown"
    last_event = "none"

    if len(highs) >= 2 and len(lows) >= 2:
        # Uptrend: HH + HL
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            trend = "up"
            last_event = "HH_HL"
        # Downtrend: LH + LL
        elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            trend = "down"
            last_event = "LH_LL"
        # Potential ChoCH: was uptrend, now LL forming
        elif highs[-1] > highs[-2] and lows[-1] < lows[-2]:
            trend = "choch_bearish"
            last_event = "ChoCH_bearish"
        # Potential ChoCH: was downtrend, now HH forming
        elif highs[-1] < highs[-2] and lows[-1] > lows[-2]:
            trend = "choch_bullish"
            last_event = "ChoCH_bullish"

    # Check for BOS (price closing beyond last swing high/low)
    last_close = df["close"].iloc[-1]
    if highs and last_close > highs[-1]:
        last_event = "BOS_bullish"
    elif lows and last_close < lows[-1]:
        last_event = "BOS_bearish"

    return {
        "trend": trend,
        "last_event": last_event,
        "swing_highs": highs,
        "swing_lows": lows,
    }


def detect_fair_value_gaps(df: pd.DataFrame) -> list:
    """
    FVG (Fair Value Gap): 3-candle pattern where candle 1 high < candle 3 low (bullish)
    or candle 1 low > candle 3 high (bearish).
    """
    fvgs = []
    for i in range(2, len(df)):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]
        # Bullish FVG
        if c1["high"] < c3["low"]:
            fvgs.append({
                "type": "bullish",
                "top": c3["low"],
                "bottom": c1["high"],
                "time": df.index[i],
                "mitigated": False,
            })
        # Bearish FVG
        elif c1["low"] > c3["high"]:
            fvgs.append({
                "type": "bearish",
                "top": c1["low"],
                "bottom": c3["high"],
                "time": df.index[i],
                "mitigated": False,
            })

    # Mark mitigated FVGs (price has returned into the gap)
    current_price = df["close"].iloc[-1]
    for fvg in fvgs:
        if fvg["type"] == "bullish" and current_price <= fvg["top"]:
            fvg["mitigated"] = True
        elif fvg["type"] == "bearish" and current_price >= fvg["bottom"]:
            fvg["mitigated"] = True

    # Return only recent unmitigated FVGs (last 10)
    unmitigated = [f for f in fvgs if not f["mitigated"]][-10:]
    return unmitigated


def detect_order_blocks(df: pd.DataFrame) -> list:
    """
    Order Block: last bearish candle before a bullish impulse (bullish OB)
    or last bullish candle before a bearish impulse (bearish OB).
    """
    obs = []
    threshold = 0.002  # 0.2% move to qualify as impulse

    for i in range(1, len(df) - 1):
        curr = df.iloc[i]
        next_c = df.iloc[i + 1]
        prev = df.iloc[i - 1]

        # Bullish OB: bearish candle followed by strong bullish impulse
        if (curr["close"] < curr["open"] and  # bearish candle
                next_c["close"] > next_c["open"] and  # next is bullish
                (next_c["close"] - next_c["open"]) / curr["open"] > threshold):
            obs.append({
                "type": "bullish",
                "top": curr["open"],
                "bottom": curr["low"],
                "time": df.index[i],
            })

        # Bearish OB: bullish candle followed by strong bearish impulse
        elif (curr["close"] > curr["open"] and  # bullish candle
              next_c["close"] < next_c["open"] and  # next is bearish
              (next_c["open"] - next_c["close"]) / curr["open"] > threshold):
            obs.append({
                "type": "bearish",
                "top": curr["high"],
                "bottom": curr["open"],
                "time": df.index[i],
            })

    return obs[-5:]  # last 5 order blocks


def get_daily_bias(df_d1: pd.DataFrame) -> dict:
    """
    ICT Daily Bias: determines the expected direction for the day.
    Based on daily open price and recent D1 structure.
    """
    if df_d1.empty or len(df_d1) < 5:
        return {"bias": "neutral", "daily_open": None}

    daily_open = df_d1["open"].iloc[-1]
    current_close = df_d1["close"].iloc[-1]
    prev_high = df_d1["high"].iloc[-2]
    prev_low = df_d1["low"].iloc[-2]

    structure = detect_market_structure(df_d1)
    trend = structure["trend"]

    if trend in ("up", "choch_bullish") and current_close > daily_open:
        bias = "bullish"
    elif trend in ("down", "choch_bearish") and current_close < daily_open:
        bias = "bearish"
    elif current_close > daily_open and current_close > prev_high:
        bias = "bullish"
    elif current_close < daily_open and current_close < prev_low:
        bias = "bearish"
    else:
        bias = "neutral"

    return {
        "bias": bias,
        "daily_open": daily_open,
        "trend_d1": trend,
        "prev_high_d1": prev_high,
        "prev_low_d1": prev_low,
    }


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range."""
    if len(df) < period:
        return 0.0
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]


def compute_smc_indicators(df: pd.DataFrame, df_d1: pd.DataFrame = None) -> dict:
    """
    Full SMC indicator set aligned with the Space Traders Academy course.
    """
    if df.empty or len(df) < 10:
        return {}

    close = df["close"]
    high = df["high"]
    low = df["low"]

    ind = {}

    # Price
    ind["current_close"] = close.iloc[-1]
    ind["prev_close"] = close.iloc[-2]
    ind["atr"] = compute_atr(df)

    # Market structure
    structure = detect_market_structure(df)
    ind["trend"] = structure["trend"]
    ind["last_bos_choch"] = structure["last_event"]
    ind["swing_highs"] = structure["swing_highs"]
    ind["swing_lows"] = structure["swing_lows"]

    # Swing points (liquidity levels)
    swings = find_swing_points(df)
    ind["bsl"] = swings["last_bsl"]   # Buy Side Liquidity (above)
    ind["ssl"] = swings["last_ssl"]   # Sell Side Liquidity (below)

    # FVG (unmitigated)
    fvgs = detect_fair_value_gaps(df)
    ind["fvg_bullish"] = [f for f in fvgs if f["type"] == "bullish"]
    ind["fvg_bearish"] = [f for f in fvgs if f["type"] == "bearish"]
    ind["nearest_bullish_fvg"] = fvgs[-1] if fvgs and fvgs[-1]["type"] == "bullish" else None
    ind["nearest_bearish_fvg"] = fvgs[-1] if fvgs and fvgs[-1]["type"] == "bearish" else None

    # Order blocks
    obs = detect_order_blocks(df)
    ind["order_blocks"] = obs
    ind["nearest_bullish_ob"] = next((o for o in reversed(obs) if o["type"] == "bullish"), None)
    ind["nearest_bearish_ob"] = next((o for o in reversed(obs) if o["type"] == "bearish"), None)

    # Daily bias (if D1 data provided)
    if df_d1 is not None and not df_d1.empty:
        daily = get_daily_bias(df_d1)
        ind["daily_bias"] = daily["bias"]
        ind["daily_open"] = daily["daily_open"]
        ind["trend_d1"] = daily.get("trend_d1", "unknown")
    else:
        ind["daily_bias"] = "unknown"
        ind["daily_open"] = None

    # Session
    ind["session"] = get_current_session()

    # Liquidity sweep detection
    # Check if price recently swept a swing high or low (LIT / SLQ signal)
    last_20_high = high.tail(20).max()
    last_20_low = low.tail(20).min()
    bsl_val = ind["bsl"] or last_20_high
    ssl_val = ind["ssl"] or last_20_low

    recent_high = high.tail(5).max()
    recent_low = low.tail(5).min()

    ind["swept_bsl"] = recent_high >= bsl_val  # swept buy side liquidity
    ind["swept_ssl"] = recent_low <= ssl_val   # swept sell side liquidity

    # Price relative to daily open
    if ind["daily_open"]:
        ind["above_daily_open"] = ind["current_close"] > ind["daily_open"]
    else:
        ind["above_daily_open"] = None

    return ind


# ─── Public API ───────────────────────────────────────────────────────────────

def get_market_snapshot(
    symbol: str,
    timeframe: str = "H1",
    count: int = 200,
    broker: str = "oanda",
    mt5_executor=None,
    **broker_kwargs,
) -> MarketSnapshot:
    """
    Fetch market data and compute SMC indicators.
    Also fetches D1 data for daily bias calculation.
    If mt5_executor is provided (MT5Executor instance), fetches candles directly from it.
    """
    df = pd.DataFrame()
    df_d1 = pd.DataFrame()

    if broker == "mt5" and mt5_executor is not None:
        # Use MT5 executor directly (Linux bridge via mt5linux)
        df = mt5_executor.get_candles(symbol, timeframe, count)
        df_d1 = mt5_executor.get_candles(symbol, "D1", 30) if not df.empty else pd.DataFrame()
    elif broker == "oanda":
        kw = {"api_key": broker_kwargs.get("api_key", ""),
              "account_id": broker_kwargs.get("account_id", ""),
              "env": broker_kwargs.get("env", "practice")}
        df = _fetch_oanda(symbol, timeframe, count, **kw)
        df_d1 = _fetch_oanda(symbol, "D1", 30, **kw) if not df.empty else pd.DataFrame()

    if df.empty:
        # Fallback chain: yfinance → frankfurter (daily, dev only)
        logger.warning(f"Primary fetch failed, trying yfinance for {symbol}")
        yf_symbol = symbol.replace("_", "")
        df = _fetch_yfinance(yf_symbol, timeframe, count)
        df_d1 = _fetch_yfinance(yf_symbol, "D1", 30) if not df.empty else pd.DataFrame()

    if df.empty:
        logger.warning(f"yfinance failed, using frankfurter.app daily fallback for {symbol}")
        df = _fetch_frankfurter(symbol, count)
        df_d1 = _fetch_frankfurter(symbol, 30) if not df.empty else pd.DataFrame()

    smc = compute_smc_indicators(df, df_d1)
    current_price = df["close"].iloc[-1] if not df.empty else 0.0

    return MarketSnapshot(
        symbol=symbol,
        candles=df,
        indicators=smc,
        smc=smc,
        current_price=current_price,
        session=get_current_session(),
    )
