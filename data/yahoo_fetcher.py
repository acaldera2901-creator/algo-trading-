"""
Yahoo Finance fetcher diretto — usa requests + API Yahoo v8.
Non richiede yfinance (evita il problema multitasking/protobuf).
"""

import logging
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Headers per sembrare un browser reale
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://finance.yahoo.com/",
}

INTERVAL_MAP = {
    "M1":  "1m",
    "M5":  "5m",
    "M15": "15m",
    "H1":  "1h",
    "H4":  "4h",
    "D1":  "1d",
}

# Massimo storico per intervallo Yahoo Finance
MAX_LOOKBACK_DAYS = {
    "1m":  7,
    "5m":  60,
    "15m": 60,
    "1h":  730,
    "4h":  730,
    "1d":  1825,
}


def fetch_ohlcv(symbol: str, interval: str = "1h", days: int = 730) -> pd.DataFrame:
    """
    Scarica dati OHLCV da Yahoo Finance v8 API.

    Args:
        symbol: Simbolo Yahoo (es. 'GC=F', 'BTC-USD', 'EURUSD=X')
        interval: '1m','5m','15m','1h','4h','1d'
        days: Quanti giorni di storia scaricare

    Returns:
        DataFrame con colonne [open, high, low, close, volume] e indice timestamp UTC
    """
    max_days = MAX_LOOKBACK_DAYS.get(interval, 730)
    days = min(days, max_days)

    now = int(time.time())
    start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "interval": interval,
        "period1": start,
        "period2": now,
        "includeAdjustedClose": True,
    }

    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        result = data.get("chart", {}).get("result")
        if not result:
            error = data.get("chart", {}).get("error", {})
            logger.error(f"Yahoo API error for {symbol}: {error}")
            return pd.DataFrame()

        chart = result[0]
        timestamps = chart.get("timestamp", [])
        indicators = chart.get("indicators", {})
        quote = indicators.get("quote", [{}])[0]

        if not timestamps or not quote:
            logger.error(f"Nessun dato Yahoo per {symbol}")
            return pd.DataFrame()

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
            "open":   quote.get("open",   [None] * len(timestamps)),
            "high":   quote.get("high",   [None] * len(timestamps)),
            "low":    quote.get("low",    [None] * len(timestamps)),
            "close":  quote.get("close",  [None] * len(timestamps)),
            "volume": quote.get("volume", [0]    * len(timestamps)),
        })
        df = df.set_index("timestamp")
        df = df.dropna(subset=["open", "high", "low", "close"])
        df["volume"] = df["volume"].fillna(0).astype(float)

        logger.info(f"[Yahoo] {len(df)} candle {interval} per {symbol} "
                    f"({df.index[0].date()} → {df.index[-1].date()})")
        return df

    except Exception as e:
        logger.error(f"[Yahoo] Fetch fallito per {symbol} {interval}: {e}")
        return pd.DataFrame()


def fetch_ohlcv_chunked(symbol: str, interval: str = "1h", total_days: int = 730) -> pd.DataFrame:
    """
    Scarica dati in chunks per aggirare limiti Yahoo (massimo ~730 giorni per 1h).
    Ritorna DataFrame unificato.
    """
    max_per_call = MAX_LOOKBACK_DAYS.get(interval, 730)
    if total_days <= max_per_call:
        return fetch_ohlcv(symbol, interval, total_days)

    # Scarica in più batch
    dfs = []
    remaining = total_days
    end_time = datetime.now(timezone.utc)

    while remaining > 0:
        chunk_days = min(remaining, max_per_call)
        start_time = end_time - timedelta(days=chunk_days)

        now = int(end_time.timestamp())
        start = int(start_time.timestamp())

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {"interval": interval, "period1": start, "period2": now}

        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("chart", {}).get("result")
            if result:
                chart = result[0]
                timestamps = chart.get("timestamp", [])
                quote = chart.get("indicators", {}).get("quote", [{}])[0]
                if timestamps and quote:
                    df_chunk = pd.DataFrame({
                        "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
                        "open":   quote.get("open",   []),
                        "high":   quote.get("high",   []),
                        "low":    quote.get("low",    []),
                        "close":  quote.get("close",  []),
                        "volume": quote.get("volume", []),
                    }).set_index("timestamp").dropna(subset=["close"])
                    dfs.append(df_chunk)
        except Exception as e:
            logger.error(f"Chunk fetch error: {e}")

        remaining -= chunk_days
        end_time = start_time - timedelta(hours=1)
        time.sleep(0.5)  # Rate limiting

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    logger.info(f"[Yahoo] Totale: {len(combined)} candle {interval} per {symbol}")
    return combined
