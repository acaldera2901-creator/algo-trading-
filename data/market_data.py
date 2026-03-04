"""
Market data ingestion module.
Fetches OHLCV data from OANDA or MT5, computes technical indicators.
"""

import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class Candle:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MarketSnapshot:
    symbol: str
    candles: pd.DataFrame          # columns: open, high, low, close, volume
    indicators: dict = field(default_factory=dict)
    current_price: float = 0.0
    spread: float = 0.0


# ─── OANDA Fetcher ────────────────────────────────────────────────────────────

def _fetch_oanda(symbol: str, timeframe: str, count: int, api_key: str, account_id: str, env: str) -> pd.DataFrame:
    try:
        from oandapyV20 import API
        from oandapyV20.endpoints.instruments import InstrumentsCandles

        client = API(access_token=api_key, environment=env)
        gran_map = {"M1": "M1", "M5": "M5", "M15": "M15", "H1": "H1", "H4": "H4", "D1": "D"}
        granularity = gran_map.get(timeframe, "H1")

        params = {"granularity": granularity, "count": count, "price": "M"}
        r = InstrumentsCandles(instrument=symbol, params=params)
        client.request(r)

        rows = []
        for c in r.response["candles"]:
            if c["complete"]:
                mid = c["mid"]
                rows.append({
                    "timestamp": pd.to_datetime(c["time"]),
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": float(c["volume"]),
                })
        df = pd.DataFrame(rows).set_index("timestamp")
        logger.info(f"OANDA: fetched {len(df)} candles for {symbol}")
        return df
    except Exception as e:
        logger.error(f"OANDA fetch error: {e}")
        return pd.DataFrame()


# ─── MT5 Fetcher ──────────────────────────────────────────────────────────────

def _fetch_mt5(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    try:
        import MetaTrader5 as mt5
        tf_map = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
        }
        tf = tf_map.get(timeframe, mt5.TIMEFRAME_H1)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None:
            logger.error(f"MT5: no data for {symbol}")
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("timestamp")[["open", "high", "low", "close", "tick_volume"]].rename(
            columns={"tick_volume": "volume"}
        )
        logger.info(f"MT5: fetched {len(df)} candles for {symbol}")
        return df
    except Exception as e:
        logger.error(f"MT5 fetch error: {e}")
        return pd.DataFrame()


# ─── Fallback: yfinance ───────────────────────────────────────────────────────

def _fetch_yfinance(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    try:
        import yfinance as yf
        tf_map = {"M1": "1m", "M5": "5m", "M15": "15m", "H1": "1h", "H4": "4h", "D1": "1d"}
        period_map = {"M1": "7d", "M5": "7d", "M15": "60d", "H1": "60d", "H4": "60d", "D1": "2y"}
        interval = tf_map.get(timeframe, "1h")
        period = period_map.get(timeframe, "60d")

        yf_symbol = symbol.replace("_", "=X") if "_" in symbol else symbol + "=X"
        df = yf.download(yf_symbol, period=period, interval=interval, progress=False)
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].tail(count)
        logger.info(f"yfinance: fetched {len(df)} candles for {symbol}")
        return df
    except Exception as e:
        logger.error(f"yfinance fetch error: {e}")
        return pd.DataFrame()


# ─── Indicators ───────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> dict:
    """Compute technical indicators on a OHLCV DataFrame."""
    if df.empty or len(df) < 20:
        return {}
    try:
        import ta
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        indicators = {}

        # Trend
        indicators["ema_20"] = ta.trend.ema_indicator(close, window=20).iloc[-1]
        indicators["ema_50"] = ta.trend.ema_indicator(close, window=50).iloc[-1] if len(df) >= 50 else None
        indicators["ema_200"] = ta.trend.ema_indicator(close, window=200).iloc[-1] if len(df) >= 200 else None

        # Momentum
        rsi = ta.momentum.RSIIndicator(close, window=14)
        indicators["rsi"] = rsi.rsi().iloc[-1]

        macd = ta.trend.MACD(close)
        indicators["macd"] = macd.macd().iloc[-1]
        indicators["macd_signal"] = macd.macd_signal().iloc[-1]
        indicators["macd_diff"] = macd.macd_diff().iloc[-1]

        # Volatility
        bb = ta.volatility.BollingerBands(close)
        indicators["bb_upper"] = bb.bollinger_hband().iloc[-1]
        indicators["bb_lower"] = bb.bollinger_lband().iloc[-1]
        indicators["bb_middle"] = bb.bollinger_mavg().iloc[-1]
        indicators["bb_width"] = indicators["bb_upper"] - indicators["bb_lower"]

        atr = ta.volatility.AverageTrueRange(high, low, close, window=14)
        indicators["atr"] = atr.average_true_range().iloc[-1]

        # Current price context
        indicators["current_close"] = close.iloc[-1]
        indicators["prev_close"] = close.iloc[-2]
        indicators["price_change_pct"] = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100

        # Support / Resistance (simple: recent swing highs/lows)
        window = min(20, len(df))
        indicators["recent_high"] = high.tail(window).max()
        indicators["recent_low"] = low.tail(window).min()

        # Trend direction
        if indicators["ema_20"] and indicators["ema_50"]:
            indicators["trend"] = "up" if indicators["ema_20"] > indicators["ema_50"] else "down"
        else:
            indicators["trend"] = "unknown"

        return indicators
    except Exception as e:
        logger.error(f"Indicator computation error: {e}")
        return {}


# ─── Public API ───────────────────────────────────────────────────────────────

def get_market_snapshot(
    symbol: str,
    timeframe: str = "H1",
    count: int = 200,
    broker: str = "oanda",
    **broker_kwargs,
) -> MarketSnapshot:
    """
    Fetch market data and compute indicators.
    Returns a MarketSnapshot ready for analysis.
    """
    df = pd.DataFrame()

    if broker == "oanda":
        df = _fetch_oanda(
            symbol, timeframe, count,
            broker_kwargs.get("api_key", ""),
            broker_kwargs.get("account_id", ""),
            broker_kwargs.get("env", "practice"),
        )
    elif broker == "mt5":
        df = _fetch_mt5(symbol, timeframe, count)

    if df.empty:
        logger.warning(f"Primary fetch failed, falling back to yfinance for {symbol}")
        df = _fetch_yfinance(symbol, timeframe, count)

    indicators = compute_indicators(df)
    current_price = df["close"].iloc[-1] if not df.empty else 0.0

    return MarketSnapshot(
        symbol=symbol,
        candles=df,
        indicators=indicators,
        current_price=current_price,
    )
