"""
MT5 HTTP Bridge Server — receives data from MT5 EA (Mac/Windows) via HTTP.

The MQL5 EA (mt5_ea/AlgoTradingBridge.mq5) running in MetaTrader5 on Mac:
  - POSTs candle data every timer tick
  - POSTs account/position data
  - GETs pending trade commands

This server:
  - Stores incoming candle data in memory
  - Stores account and position state
  - Exposes /commands endpoint for the EA to poll
  - Is called by agent/core.py to get market snapshots and send orders

Run: python -m agent.mt5_http_bridge (starts in background thread automatically)
"""

import json
import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
import uuid

import pandas as pd

logger = logging.getLogger(__name__)


class BridgeState:
    """Shared state between HTTP server and trading agent."""

    def __init__(self):
        self._lock = threading.Lock()
        # Candle buffers per symbol: {symbol: deque of candle dicts}
        self.candles: dict = defaultdict(lambda: deque(maxlen=300))
        # Latest tick per symbol
        self.ticks: dict = {}
        # Account info
        self.account: dict = {"balance": 0.0, "equity": 0.0, "margin": 0.0, "free_margin": 0.0}
        # Open positions
        self.positions: list = []
        # Pending commands to send to EA
        self.pending_commands: list = []
        # Executed command results
        self.command_results: dict = {}
        # Connection status
        self.last_heartbeat: Optional[datetime] = None
        self.ea_online: bool = False

    def update_candles(self, symbol: str, candles: list):
        with self._lock:
            seen_times = {c["time"] for c in self.candles[symbol]}
            for c in candles:
                if c["time"] not in seen_times:
                    self.candles[symbol].append(c)
                    seen_times.add(c["time"])

    def get_candles_df(self, symbol: str, count: int = 200) -> pd.DataFrame:
        with self._lock:
            data = list(self.candles[symbol])
        if not data:
            return pd.DataFrame()
        data_sorted = sorted(data, key=lambda x: x["time"])
        df = pd.DataFrame(data_sorted)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]].tail(count)
        return df

    def update_account(self, info: dict):
        with self._lock:
            self.account.update(info)

    def update_positions(self, positions: list):
        with self._lock:
            self.positions = positions

    def update_tick(self, symbol: str, tick: dict):
        with self._lock:
            self.ticks[symbol] = tick

    def pop_commands(self) -> list:
        with self._lock:
            cmds = list(self.pending_commands)
            self.pending_commands.clear()
            return cmds

    def add_command(self, cmd: dict) -> str:
        cmd_id = str(uuid.uuid4())[:8]
        cmd["id"] = cmd_id
        with self._lock:
            self.pending_commands.append(cmd)
        return cmd_id

    def record_result(self, result: dict):
        with self._lock:
            self.command_results[result.get("id", "")] = result

    def set_heartbeat(self):
        with self._lock:
            self.last_heartbeat = datetime.now(timezone.utc)
            self.ea_online = True

    def is_online(self) -> bool:
        if not self.last_heartbeat:
            return False
        delta = (datetime.now(timezone.utc) - self.last_heartbeat).total_seconds()
        return delta < 60  # offline if no heartbeat for 60s


# Global bridge state singleton
bridge_state = BridgeState()


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silence default HTTP logs

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        try:
            return json.loads(body)
        except Exception:
            return {}

    def _respond(self, status: int, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/commands":
            cmds = bridge_state.pop_commands()
            self._respond(200, cmds)
        elif self.path == "/status":
            self._respond(200, {
                "online": bridge_state.is_online(),
                "account": bridge_state.account,
                "symbols": list(bridge_state.candles.keys()),
                "positions": len(bridge_state.positions),
            })
        else:
            self._respond(404, {"error": "Not found"})

    def do_POST(self):
        data = self._read_body()
        path = self.path

        if path == "/candles":
            symbol = data.get("symbol", "")
            candles = data.get("candles", [])
            if symbol and candles:
                bridge_state.update_candles(symbol, candles)
                logger.debug(f"[Bridge] Received {len(candles)} candles for {symbol}")
            self._respond(200, {"ok": True})

        elif path == "/tick":
            symbol = data.get("symbol", "")
            if symbol:
                bridge_state.update_tick(symbol, data)
            self._respond(200, {"ok": True})

        elif path == "/account":
            bridge_state.update_account(data)
            self._respond(200, {"ok": True})

        elif path == "/positions":
            bridge_state.update_positions(data.get("positions", []))
            self._respond(200, {"ok": True})

        elif path == "/heartbeat":
            bridge_state.set_heartbeat()
            logger.info(f"[Bridge] EA heartbeat — account: {data.get('account')}")
            self._respond(200, {"ok": True, "server": "alive"})

        elif path == "/command_result":
            bridge_state.record_result(data)
            success = data.get("success", False)
            logger.info(f"[Bridge] Command result: {data.get('action')} {data.get('symbol')} -> {'OK' if success else 'FAIL'}")
            self._respond(200, {"ok": True})

        else:
            self._respond(404, {"error": "Not found"})


class MT5BridgeServer:
    """HTTP server that runs in a background thread."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host = host
        self.port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._server = HTTPServer((self.host, self.port), BridgeHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"[Bridge] HTTP server started on {self.host}:{self.port}")
        logger.info(f"[Bridge] Waiting for MT5 EA connection...")

    def stop(self):
        if self._server:
            self._server.shutdown()


# ─── Executor interface (used by agent/executor.py) ─────────────────────────

class EABridgeExecutor:
    """
    Executor that sends orders via the HTTP bridge to MT5 EA on Mac.
    Used as a drop-in replacement for MT5Executor.
    """

    def __init__(self, server: MT5BridgeServer):
        self.server = server
        self.state = bridge_state

    def get_account_balance(self) -> float:
        return self.state.account.get("balance", 0.0)

    def get_open_positions(self):
        from agent.executor import Position
        result = []
        for p in self.state.positions:
            result.append(Position(
                order_id=str(p.get("ticket", "")),
                symbol=p.get("symbol", ""),
                direction=p.get("type", "buy"),
                entry_price=p.get("open_price", 0.0),
                current_price=p.get("open_price", 0.0),
                stop_loss=p.get("sl", 0.0),
                take_profit=p.get("tp", 0.0),
                position_size=p.get("volume", 0.0),
                unrealized_pnl=p.get("profit", 0.0),
                open_time="",
            ))
        return result

    def open_order(self, setup):
        from agent.executor import OrderResult
        import config

        # Convert units to lots (1 lot = 100,000 units)
        volume = max(round(setup.position_size / 100000, 2), 0.01)

        cmd = {
            "action": setup.direction,
            "symbol": setup.symbol,
            "volume": volume,
            "sl": round(setup.stop_loss, 5),
            "tp": round(setup.take_profit, 5),
        }

        if config.DRY_RUN:
            logger.info(f"[DRY RUN via EA Bridge] Would send: {cmd}")
            return OrderResult(success=True, order_id="DRY-EA-001", fill_price=setup.entry_price)

        if not self.state.is_online():
            logger.warning("[EABridge] MT5 EA not online — order not sent")
            return OrderResult(success=False, order_id=None, fill_price=0.0, error="EA offline")

        cmd_id = self.state.add_command(cmd)
        logger.info(f"[EABridge] Order queued: {setup.symbol} {setup.direction} {volume} lots (id={cmd_id})")
        return OrderResult(success=True, order_id=cmd_id, fill_price=setup.entry_price)

    def close_position(self, symbol: str):
        from agent.executor import OrderResult
        import config

        if config.DRY_RUN:
            logger.info(f"[DRY RUN via EA Bridge] Would close: {symbol}")
            return OrderResult(success=True, order_id=None, fill_price=0.0)

        if not self.state.is_online():
            return OrderResult(success=False, order_id=None, fill_price=0.0, error="EA offline")

        cmd_id = self.state.add_command({"action": "close", "symbol": symbol, "volume": 0})
        logger.info(f"[EABridge] Close queued: {symbol}")
        return OrderResult(success=True, order_id=cmd_id, fill_price=0.0)

    def get_candles_df(self, symbol: str, count: int = 200) -> pd.DataFrame:
        return self.state.get_candles_df(symbol, count)

    def is_online(self) -> bool:
        return self.state.is_online()
