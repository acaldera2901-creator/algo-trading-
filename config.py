"""
Central configuration. All values come from .env file.
Override defaults by setting variables in .env
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
COURSE_KNOWLEDGE_PATH = KNOWLEDGE_DIR / "course_knowledge.json"
LEARNED_RULES_PATH = KNOWLEDGE_DIR / "learned_rules.json"
TRADE_LOG_PATH = BASE_DIR / "data" / "trade_log.db"

# ─── Anthropic ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"

# ─── Broker ───────────────────────────────────────────────────────────────────
BROKER = os.getenv("BROKER", "oanda").lower()  # "oanda" | "mt5"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# OANDA
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV = os.getenv("OANDA_ENV", "practice")  # "practice" | "live"

# MetaTrader 5
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
MT5_HOST = os.getenv("MT5_HOST", "localhost")   # rpyc bridge host
MT5_PORT = int(os.getenv("MT5_PORT", "18812"))  # rpyc bridge port

# MT5 Mac bridge (MQL5 EA + HTTP)
EA_BRIDGE_PORT = int(os.getenv("EA_BRIDGE_PORT", "8765"))  # HTTP bridge port

# ─── Risk Management (configurable, auto-tunable) ─────────────────────────────
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "10.0"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
SL_MULTIPLIER = float(os.getenv("SL_MULTIPLIER", "1.5"))
TP_MULTIPLIER = float(os.getenv("TP_MULTIPLIER", "2.0"))

# ─── Trading ──────────────────────────────────────────────────────────────────
SYMBOLS = os.getenv("SYMBOLS", "EUR_USD,GBP_USD,USD_JPY").split(",")
TIMEFRAME = os.getenv("TIMEFRAME", "H1")
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "3600"))

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ─── Self-improvement ─────────────────────────────────────────────────────────
MIN_TRADES_FOR_TUNING = int(os.getenv("MIN_TRADES_FOR_TUNING", "15"))
RULE_SUCCESS_THRESHOLD = int(os.getenv("RULE_SUCCESS_THRESHOLD", "3"))
RULE_FAILURE_THRESHOLD = int(os.getenv("RULE_FAILURE_THRESHOLD", "3"))
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.55"))

# ─── Strumenti CFD — dimensioni contratto ─────────────────────────────────────
# Usato dal RiskManager per calcolare il position sizing corretto per lotti MT5.
# 1 lotto XAUUSD = 100 oz → value = prezzo × 100 per pip monetario
# 1 lotto BTCUSD = 1 BTC  → valore alto, sizing conservativo necessario
SYMBOL_CONTRACT_SIZES: dict = {
    "XAUUSD":  100,
    "XAU_USD": 100,
    "BTCUSD":  1,
    "BTC_USD": 1,
    "ETHUSD":  1,
    "ETH_USD": 1,
    "EURUSD":  100_000,
    "EUR_USD": 100_000,
    "GBPUSD":  100_000,
    "GBP_USD": 100_000,
    "USDJPY":  100_000,
    "USD_JPY": 100_000,
    "USDCHF":  100_000,
    "AUDUSD":  100_000,
    "USDCAD":  100_000,
    "NZDUSD":  100_000,
}
DEFAULT_CONTRACT_SIZE = int(os.getenv("DEFAULT_CONTRACT_SIZE", "100000"))
MIN_LOT_SIZE = float(os.getenv("MIN_LOT_SIZE", "0.01"))
MAX_LOT_SIZE = float(os.getenv("MAX_LOT_SIZE", "5.0"))
