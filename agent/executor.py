"""
Order Executor — MetaTrader5 (Linux bridge via mt5linux), OANDA, Dry-run.

MT5 on Linux:
  Uses mt5linux which communicates with MT5 terminal running on Windows via rpyc.
  The Windows machine must run: python -c "from mt5linux import MetaTrader5; MetaTrader5().run_server()"
  Then set MT5_HOST in .env to the Windows machine IP (default: localhost for same-machine Wine).

Handles:
- Opening market orders with SL and TP
- Closing positions
- Fetching account balance and open positions
- Dry-run mode (no real orders)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
from agent.risk_manager import TradeSetup

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    fill_price: float
    error: Optional[str] = None


@dataclass
class Position:
    order_id: str
    symbol: str
    direction: str
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    unrealized_pnl: float
    open_time: str


# ─── MT5 Connector (Linux-compatible via mt5linux bridge) ────────────────────

class MT5Executor:
    """
    Connects to MetaTrader5 via mt5linux bridge.
    On Linux: requires MT5 terminal + rpyc server running on Windows host.
    Set MT5_HOST in .env (default 'localhost' for same machine Wine setup).
    """

    def __init__(self, login: int, password: str, server: str, host: str = "localhost", port: int = 18812):
        self._connected = False
        self._mt5 = None
        self._login = login
        self._password = password
        self._server = server
        self._host = host
        self._port = port
        self._init()

    def _init(self):
        try:
            from mt5linux import MetaTrader5
            self._mt5 = MetaTrader5(host=self._host, port=self._port)
            result = self._mt5.initialize(
                login=self._login,
                password=self._password,
                server=self._server,
            )
            if not result:
                err = self._mt5.last_error()
                logger.error(f"[MT5] Init failed: {err}")
                logger.error("[MT5] Ensure MT5 terminal is open and rpyc server is running on host")
                return
            info = self._mt5.account_info()
            if info:
                logger.info(f"[MT5] Connected: {info.login} | Balance: {info.balance} {info.currency}")
                self._connected = True
            else:
                logger.error("[MT5] Could not get account info after init")
        except Exception as e:
            logger.error(f"[MT5] Connection error: {e}")
            logger.error("[MT5] Start the rpyc server on your Windows/Wine MT5 machine first")

    def get_account_balance(self) -> float:
        if not self._connected:
            return 0.0
        try:
            info = self._mt5.account_info()
            return info.balance if info else 0.0
        except Exception:
            return 0.0

    def get_open_positions(self) -> list:
        if not self._connected:
            return []
        try:
            positions = self._mt5.positions_get()
            if not positions:
                return []
            result = []
            for p in positions:
                result.append(Position(
                    order_id=str(p.ticket),
                    symbol=p.symbol,
                    direction="buy" if p.type == 0 else "sell",
                    entry_price=p.price_open,
                    current_price=p.price_current,
                    stop_loss=p.sl,
                    take_profit=p.tp,
                    position_size=p.volume,
                    unrealized_pnl=p.profit,
                    open_time=str(p.time),
                ))
            return result
        except Exception as e:
            logger.error(f"[MT5] Positions error: {e}")
            return []

    def open_order(self, setup: TradeSetup) -> OrderResult:
        if not self._connected:
            return OrderResult(success=False, order_id=None, fill_price=0.0, error="MT5 not connected")
        try:
            mt5 = self._mt5
            ORDER_TYPE_BUY = 0
            ORDER_TYPE_SELL = 1
            TRADE_ACTION_DEAL = 1
            ORDER_TIME_GTC = 1
            ORDER_FILLING_IOC = 1
            TRADE_RETCODE_DONE = 10009

            order_type = ORDER_TYPE_BUY if setup.direction == "buy" else ORDER_TYPE_SELL
            tick = mt5.symbol_info_tick(setup.symbol)
            price = tick.ask if setup.direction == "buy" else tick.bid

            # Convert units to lots (1 standard lot = 100,000 units)
            volume = max(round(setup.position_size / 100000, 2), 0.01)

            request = {
                "action": TRADE_ACTION_DEAL,
                "symbol": setup.symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "sl": setup.stop_loss,
                "tp": setup.take_profit,
                "deviation": 20,
                "magic": 20240101,
                "comment": "algo-trading-agent",
                "type_time": ORDER_TIME_GTC,
                "type_filling": ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result.retcode == TRADE_RETCODE_DONE:
                logger.info(f"[MT5] Order filled: #{result.order} @ {result.price}")
                return OrderResult(success=True, order_id=str(result.order), fill_price=result.price)
            else:
                logger.error(f"[MT5] Order failed: {result.comment} (code {result.retcode})")
                return OrderResult(success=False, order_id=None, fill_price=0.0, error=result.comment)
        except Exception as e:
            logger.error(f"[MT5] Open order error: {e}")
            return OrderResult(success=False, order_id=None, fill_price=0.0, error=str(e))

    def close_position(self, symbol: str) -> OrderResult:
        if not self._connected:
            return OrderResult(success=False, order_id=None, fill_price=0.0, error="Not connected")
        try:
            mt5 = self._mt5
            TRADE_ACTION_DEAL = 1
            ORDER_TYPE_BUY = 0
            ORDER_TYPE_SELL = 1
            ORDER_TIME_GTC = 1
            ORDER_FILLING_IOC = 1

            positions = mt5.positions_get(symbol=symbol)
            if not positions:
                return OrderResult(success=True, order_id=None, fill_price=0.0)

            for p in positions:
                close_type = ORDER_TYPE_SELL if p.type == 0 else ORDER_TYPE_BUY
                tick = mt5.symbol_info_tick(symbol)
                price = tick.bid if close_type == ORDER_TYPE_SELL else tick.ask
                request = {
                    "action": TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": p.volume,
                    "type": close_type,
                    "position": p.ticket,
                    "price": price,
                    "deviation": 20,
                    "magic": 20240101,
                    "comment": "close",
                    "type_time": ORDER_TIME_GTC,
                    "type_filling": ORDER_FILLING_IOC,
                }
                mt5.order_send(request)
            logger.info(f"[MT5] Position closed: {symbol}")
            return OrderResult(success=True, order_id=None, fill_price=0.0)
        except Exception as e:
            logger.error(f"[MT5] Close error: {e}")
            return OrderResult(success=False, order_id=None, fill_price=0.0, error=str(e))

    def get_candles(self, symbol: str, timeframe_str: str, count: int) -> "pd.DataFrame":
        """Fetch OHLCV data directly from MT5."""
        import pandas as pd
        if not self._connected:
            return pd.DataFrame()
        try:
            tf_map = {
                "M1": 1, "M5": 5, "M15": 15, "M30": 30,
                "H1": 16385, "H4": 16388, "D1": 16408,
            }
            tf = tf_map.get(timeframe_str, 16385)
            rates = self._mt5.copy_rates_from_pos(symbol, tf, 0, count)
            if rates is None:
                return pd.DataFrame()
            df = pd.DataFrame(rates)
            df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
            return df.set_index("timestamp")[["open", "high", "low", "close", "tick_volume"]].rename(
                columns={"tick_volume": "volume"})
        except Exception as e:
            logger.error(f"[MT5] Candles error: {e}")
            return pd.DataFrame()


# ─── OANDA Executor ───────────────────────────────────────────────────────────

class OANDAExecutor:
    def __init__(self, api_key: str, account_id: str, env: str = "practice"):
        self.account_id = account_id
        self._client = None
        try:
            from oandapyV20 import API
            self._client = API(access_token=api_key, environment=env)
            logger.info(f"[OANDA] Client initialized ({env})")
        except Exception as e:
            logger.error(f"[OANDA] Init error: {e}")

    def get_account_balance(self) -> float:
        try:
            from oandapyV20.endpoints.accounts import AccountDetails
            r = AccountDetails(self.account_id)
            self._client.request(r)
            return float(r.response["account"]["balance"])
        except Exception as e:
            logger.error(f"[OANDA] Balance error: {e}")
            return 0.0

    def get_open_positions(self) -> list:
        return []

    def open_order(self, setup: TradeSetup) -> OrderResult:
        try:
            from oandapyV20.endpoints.orders import OrderCreate
            units = int(setup.position_size) if setup.direction == "buy" else -int(setup.position_size)
            order_data = {"order": {
                "type": "MARKET", "instrument": setup.symbol, "units": str(units),
                "stopLossOnFill": {"price": f"{setup.stop_loss:.5f}"},
                "takeProfitOnFill": {"price": f"{setup.take_profit:.5f}"},
                "timeInForce": "FOK",
            }}
            r = OrderCreate(self.account_id, data=order_data)
            self._client.request(r)
            fill = r.response.get("orderFillTransaction", {})
            return OrderResult(success=True, order_id=fill.get("id"), fill_price=float(fill.get("price", 0)))
        except Exception as e:
            return OrderResult(success=False, order_id=None, fill_price=0.0, error=str(e))

    def close_position(self, symbol: str) -> OrderResult:
        try:
            from oandapyV20.endpoints.positions import PositionClose
            r = PositionClose(self.account_id, symbol, data={"longUnits": "ALL", "shortUnits": "ALL"})
            self._client.request(r)
            return OrderResult(success=True, order_id=None, fill_price=0.0)
        except Exception as e:
            return OrderResult(success=False, order_id=None, fill_price=0.0, error=str(e))


# ─── Dry-run ──────────────────────────────────────────────────────────────────

class DryRunExecutor:
    def get_account_balance(self) -> float:
        return 10000.0

    def get_open_positions(self) -> list:
        return []

    def open_order(self, setup: TradeSetup) -> OrderResult:
        logger.info(
            f"[DRY RUN] {setup.symbol} {setup.direction.upper()} "
            f"size={setup.position_size:.2f} lots SL={setup.stop_loss:.5f} TP={setup.take_profit:.5f}"
        )
        return OrderResult(success=True, order_id="DRY-001", fill_price=setup.entry_price)

    def close_position(self, symbol: str) -> OrderResult:
        logger.info(f"[DRY RUN] Would close: {symbol}")
        return OrderResult(success=True, order_id=None, fill_price=0.0)


# ─── Factory ──────────────────────────────────────────────────────────────────

def create_executor():
    if config.BROKER == "ea_bridge":
        from agent.mt5_http_bridge import MT5BridgeServer, EABridgeExecutor
        server = MT5BridgeServer(port=config.EA_BRIDGE_PORT)
        server.start()
        return EABridgeExecutor(server)

    if config.DRY_RUN:
        logger.info("[Executor] DRY RUN mode — no real orders")
        return DryRunExecutor()

    if config.BROKER == "mt5":
        host = __import__("os").getenv("MT5_HOST", "localhost")
        port = int(__import__("os").getenv("MT5_PORT", "18812"))
        ex = MT5Executor(
            login=config.MT5_LOGIN,
            password=config.MT5_PASSWORD,
            server=config.MT5_SERVER,
            host=host,
            port=port,
        )
        if not ex._connected:
            logger.warning("[Executor] MT5 not connected — falling back to DRY RUN")
            return DryRunExecutor()
        return ex

    elif config.BROKER == "oanda":
        return OANDAExecutor(config.OANDA_API_KEY, config.OANDA_ACCOUNT_ID, config.OANDA_ENV)

    logger.warning(f"[Executor] Unknown broker '{config.BROKER}' — using DRY RUN")
    return DryRunExecutor()
