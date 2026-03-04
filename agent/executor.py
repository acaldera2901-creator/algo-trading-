"""
Order Executor — OANDA and MetaTrader5 support.

Handles:
- Opening market orders with SL and TP
- Closing positions
- Fetching account balance and open positions
- Dry-run mode (simulates without real orders)
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


# ─── OANDA Executor ───────────────────────────────────────────────────────────

class OANDAExecutor:
    def __init__(self, api_key: str, account_id: str, env: str = "practice"):
        self.account_id = account_id
        self.env = env
        self._client = None
        self._init_client(api_key)

    def _init_client(self, api_key: str):
        try:
            from oandapyV20 import API
            self._client = API(access_token=api_key, environment=self.env)
            logger.info(f"[Executor] OANDA client initialized ({self.env})")
        except Exception as e:
            logger.error(f"[Executor] OANDA init error: {e}")

    def get_account_balance(self) -> float:
        try:
            from oandapyV20.endpoints.accounts import AccountDetails
            r = AccountDetails(self.account_id)
            self._client.request(r)
            return float(r.response["account"]["balance"])
        except Exception as e:
            logger.error(f"[Executor] Balance fetch error: {e}")
            return 0.0

    def get_open_positions(self) -> list[Position]:
        try:
            from oandapyV20.endpoints.positions import OpenPositions
            r = OpenPositions(self.account_id)
            self._client.request(r)
            positions = []
            for p in r.response.get("positions", []):
                long = p.get("long", {})
                short = p.get("short", {})
                units_long = float(long.get("units", 0))
                units_short = float(short.get("units", 0))
                if units_long != 0:
                    positions.append(Position(
                        order_id=str(p["instrument"]),
                        symbol=p["instrument"],
                        direction="buy",
                        entry_price=float(long.get("averagePrice", 0)),
                        current_price=0.0,
                        stop_loss=0.0,
                        take_profit=0.0,
                        position_size=units_long,
                        unrealized_pnl=float(long.get("unrealizedPL", 0)),
                        open_time="",
                    ))
                if units_short != 0:
                    positions.append(Position(
                        order_id=str(p["instrument"]),
                        symbol=p["instrument"],
                        direction="sell",
                        entry_price=float(short.get("averagePrice", 0)),
                        current_price=0.0,
                        stop_loss=0.0,
                        take_profit=0.0,
                        position_size=abs(units_short),
                        unrealized_pnl=float(short.get("unrealizedPL", 0)),
                        open_time="",
                    ))
            return positions
        except Exception as e:
            logger.error(f"[Executor] Open positions error: {e}")
            return []

    def open_order(self, setup: TradeSetup) -> OrderResult:
        try:
            from oandapyV20.endpoints.orders import OrderCreate

            units = int(setup.position_size) if setup.direction == "buy" else -int(setup.position_size)
            order_data = {
                "order": {
                    "type": "MARKET",
                    "instrument": setup.symbol,
                    "units": str(units),
                    "stopLossOnFill": {"price": f"{setup.stop_loss:.5f}"},
                    "takeProfitOnFill": {"price": f"{setup.take_profit:.5f}"},
                    "timeInForce": "FOK",
                }
            }
            r = OrderCreate(self.account_id, data=order_data)
            self._client.request(r)
            order_fill = r.response.get("orderFillTransaction", {})
            fill_price = float(order_fill.get("price", setup.entry_price))
            order_id = order_fill.get("id", "unknown")
            logger.info(f"[Executor] Order filled: {setup.symbol} {setup.direction} @ {fill_price} id={order_id}")
            return OrderResult(success=True, order_id=order_id, fill_price=fill_price)
        except Exception as e:
            logger.error(f"[Executor] Order error: {e}")
            return OrderResult(success=False, order_id=None, fill_price=0.0, error=str(e))

    def close_position(self, symbol: str) -> OrderResult:
        try:
            from oandapyV20.endpoints.positions import PositionClose
            data = {"longUnits": "ALL", "shortUnits": "ALL"}
            r = PositionClose(self.account_id, symbol, data=data)
            self._client.request(r)
            logger.info(f"[Executor] Position closed: {symbol}")
            return OrderResult(success=True, order_id=None, fill_price=0.0)
        except Exception as e:
            logger.error(f"[Executor] Close error: {e}")
            return OrderResult(success=False, order_id=None, fill_price=0.0, error=str(e))


# ─── MT5 Executor ─────────────────────────────────────────────────────────────

class MT5Executor:
    def __init__(self, login: int, password: str, server: str):
        self._connected = False
        self._init(login, password, server)

    def _init(self, login: int, password: str, server: str):
        try:
            import MetaTrader5 as mt5
            if not mt5.initialize(login=login, password=password, server=server):
                logger.error(f"[Executor] MT5 init failed: {mt5.last_error()}")
                return
            self._connected = True
            logger.info("[Executor] MT5 connected")
        except Exception as e:
            logger.error(f"[Executor] MT5 import error: {e}")

    def get_account_balance(self) -> float:
        if not self._connected:
            return 0.0
        try:
            import MetaTrader5 as mt5
            info = mt5.account_info()
            return info.balance if info else 0.0
        except Exception:
            return 0.0

    def get_open_positions(self) -> list[Position]:
        if not self._connected:
            return []
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get()
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
            logger.error(f"[Executor] MT5 positions error: {e}")
            return []

    def open_order(self, setup: TradeSetup) -> OrderResult:
        if not self._connected:
            return OrderResult(success=False, order_id=None, fill_price=0.0, error="MT5 not connected")
        try:
            import MetaTrader5 as mt5
            order_type = mt5.ORDER_TYPE_BUY if setup.direction == "buy" else mt5.ORDER_TYPE_SELL
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": setup.symbol,
                "volume": round(setup.position_size / 100000, 2),  # convert units to lots
                "type": order_type,
                "sl": setup.stop_loss,
                "tp": setup.take_profit,
                "deviation": 20,
                "magic": 20240101,
                "comment": "algo-trading-agent",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"[Executor] MT5 order done: {result.order}")
                return OrderResult(success=True, order_id=str(result.order), fill_price=result.price)
            else:
                logger.error(f"[Executor] MT5 order failed: {result.comment}")
                return OrderResult(success=False, order_id=None, fill_price=0.0, error=result.comment)
        except Exception as e:
            logger.error(f"[Executor] MT5 open error: {e}")
            return OrderResult(success=False, order_id=None, fill_price=0.0, error=str(e))

    def close_position(self, symbol: str) -> OrderResult:
        if not self._connected:
            return OrderResult(success=False, order_id=None, fill_price=0.0, error="Not connected")
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get(symbol=symbol)
            if not positions:
                return OrderResult(success=True, order_id=None, fill_price=0.0)
            for p in positions:
                close_type = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
                tick = mt5.symbol_info_tick(symbol)
                price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": p.volume,
                    "type": close_type,
                    "position": p.ticket,
                    "price": price,
                    "deviation": 20,
                    "magic": 20240101,
                    "comment": "close",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                mt5.order_send(request)
            logger.info(f"[Executor] MT5 positions closed: {symbol}")
            return OrderResult(success=True, order_id=None, fill_price=0.0)
        except Exception as e:
            logger.error(f"[Executor] MT5 close error: {e}")
            return OrderResult(success=False, order_id=None, fill_price=0.0, error=str(e))


# ─── Dry-run Executor ─────────────────────────────────────────────────────────

class DryRunExecutor:
    """Simulates trades without real orders. For testing."""
    def get_account_balance(self) -> float:
        return 10000.0

    def get_open_positions(self) -> list[Position]:
        return []

    def open_order(self, setup: TradeSetup) -> OrderResult:
        logger.info(f"[DRY RUN] Would open: {setup.symbol} {setup.direction} size={setup.position_size:.0f} SL={setup.stop_loss:.5f} TP={setup.take_profit:.5f}")
        return OrderResult(success=True, order_id="DRY_RUN_001", fill_price=setup.entry_price)

    def close_position(self, symbol: str) -> OrderResult:
        logger.info(f"[DRY RUN] Would close: {symbol}")
        return OrderResult(success=True, order_id=None, fill_price=0.0)


# ─── Factory ──────────────────────────────────────────────────────────────────

def create_executor():
    """Create the appropriate executor based on config."""
    if config.DRY_RUN:
        logger.info("[Executor] DRY RUN mode — no real orders will be placed")
        return DryRunExecutor()

    if config.BROKER == "oanda":
        return OANDAExecutor(
            api_key=config.OANDA_API_KEY,
            account_id=config.OANDA_ACCOUNT_ID,
            env=config.OANDA_ENV,
        )
    elif config.BROKER == "mt5":
        return MT5Executor(
            login=config.MT5_LOGIN,
            password=config.MT5_PASSWORD,
            server=config.MT5_SERVER,
        )
    else:
        logger.warning(f"Unknown broker: {config.BROKER}, using dry run")
        return DryRunExecutor()
