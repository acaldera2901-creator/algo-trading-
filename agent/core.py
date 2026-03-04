"""
Agent Core — main decision and execution loop.

Flow per iteration:
  For each symbol:
    1. Fetch market snapshot
    2. Analyze (course + experience hybrid)
    3. Check risk limits
    4. Execute order if signal is valid
    5. Monitor open positions for exit conditions
    6. On trade close → trigger improver
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import config
from agent.analyzer import analyze, Signal
from agent.executor import create_executor, Position
from agent.improver import init_trade_log, on_trade_closed
from agent.risk_manager import RiskManager, RiskParams, TradeSetup
from data.market_data import get_market_snapshot, MarketSnapshot

logger = logging.getLogger(__name__)


class TradingAgent:
    def __init__(self):
        self.executor = create_executor()
        self.risk_params = RiskParams()
        self.risk_manager = RiskManager(self.risk_params)
        self.peak_balance = self.executor.get_account_balance()
        self.open_trades: dict[str, dict] = {}  # symbol → trade metadata
        init_trade_log()
        logger.info("[Core] Trading agent initialized")
        logger.info(f"[Core] Mode: {'DRY RUN' if config.DRY_RUN else config.BROKER.upper()}")
        logger.info(f"[Core] Symbols: {config.SYMBOLS}")
        logger.info(f"[Core] Risk per trade: {self.risk_params.risk_per_trade_pct}%")

    def _broker_kwargs(self) -> dict:
        return {
            "api_key": config.OANDA_API_KEY,
            "account_id": config.OANDA_ACCOUNT_ID,
            "env": config.OANDA_ENV,
        }

    def _check_exit_conditions(self, snapshot: MarketSnapshot, position: Position) -> bool:
        """
        Check if an open position should be closed early (before SL/TP is hit).
        Returns True if we should close now.
        """
        ind = snapshot.indicators
        if not ind:
            return False

        direction = position.direction
        close = ind.get("current_close", 0)
        trend = ind.get("trend", "unknown")
        macd_diff = ind.get("macd_diff", 0)

        # Trend reversal against position
        if direction == "buy" and trend == "down" and macd_diff < -0.0002:
            logger.info(f"[Core] Trend reversal — closing {snapshot.symbol} buy early")
            return True
        if direction == "sell" and trend == "up" and macd_diff > 0.0002:
            logger.info(f"[Core] Trend reversal — closing {snapshot.symbol} sell early")
            return True

        return False

    def _build_trade_record(
        self,
        setup: TradeSetup,
        result,
        snapshot: MarketSnapshot,
        signal: Signal,
    ) -> dict:
        return {
            "symbol": setup.symbol,
            "direction": setup.direction,
            "entry_price": result.fill_price,
            "stop_loss": setup.stop_loss,
            "take_profit": setup.take_profit,
            "position_size": setup.position_size,
            "confidence": signal.confidence,
            "patterns": signal.patterns,
            "rsi": snapshot.indicators.get("rsi"),
            "macd_diff": snapshot.indicators.get("macd_diff"),
            "trend": snapshot.indicators.get("trend"),
            "atr": snapshot.indicators.get("atr"),
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "order_id": result.order_id,
        }

    def run_once(self):
        """Single iteration: analyze all symbols and manage open positions."""
        balance = self.executor.get_account_balance()
        open_positions = self.executor.get_open_positions()
        open_symbols = {p.symbol for p in open_positions}

        # Update peak balance
        if balance > self.peak_balance:
            self.peak_balance = balance

        # Safety: check drawdown
        if not self.risk_manager.check_drawdown(balance, self.peak_balance):
            logger.warning("[Core] Max drawdown reached — pausing all trading")
            return

        # Monitor and close positions if needed
        for position in open_positions:
            snapshot = get_market_snapshot(
                position.symbol,
                config.TIMEFRAME,
                broker=config.BROKER,
                **self._broker_kwargs(),
            )
            if self._check_exit_conditions(snapshot, position):
                result = self.executor.close_position(position.symbol)
                if result.success and position.symbol in self.open_trades:
                    trade_meta = self.open_trades.pop(position.symbol)
                    exit_price = snapshot.current_price
                    pnl = (exit_price - trade_meta["entry_price"]) * trade_meta["position_size"]
                    if trade_meta["direction"] == "sell":
                        pnl = -pnl
                    pnl_pct = pnl / balance * 100

                    trade_record = {
                        **trade_meta,
                        "exit_price": exit_price,
                        "exit_time": datetime.now(timezone.utc).isoformat(),
                        "pnl": round(pnl, 4),
                        "pnl_pct": round(pnl_pct, 4),
                        "outcome": "win" if pnl > 0 else "loss",
                    }
                    on_trade_closed(trade_record, self.risk_params)

        # Analyze each symbol and potentially open new positions
        for symbol in config.SYMBOLS:
            if symbol in open_symbols:
                continue  # already have a position
            if not self.risk_manager.can_open_position(len(open_positions)):
                break

            snapshot = get_market_snapshot(
                symbol,
                config.TIMEFRAME,
                broker=config.BROKER,
                **self._broker_kwargs(),
            )
            if snapshot.candles.empty:
                logger.warning(f"[Core] No data for {symbol}, skipping")
                continue

            signal = analyze(snapshot)

            if signal.direction == "none":
                logger.info(f"[Core] {symbol}: No signal")
                continue

            atr = snapshot.indicators.get("atr", 0)
            if atr == 0:
                logger.warning(f"[Core] {symbol}: ATR=0, skipping")
                continue

            setup = self.risk_manager.build_trade_setup(
                symbol=symbol,
                direction=signal.direction,
                entry_price=snapshot.current_price,
                atr=atr,
                account_balance=balance,
                confidence=signal.confidence,
            )

            if setup is None:
                continue

            result = self.executor.open_order(setup)
            if result.success:
                trade_record = self._build_trade_record(setup, result, snapshot, signal)
                self.open_trades[symbol] = trade_record
                logger.info(
                    f"[Core] ✓ Trade opened: {symbol} {signal.direction.upper()} "
                    f"@ {result.fill_price} | Reasoning: {signal.reasoning[:80]}"
                )

    def run_loop(self):
        """Continuous loop — runs run_once() every LOOP_INTERVAL_SECONDS."""
        logger.info(f"[Core] Starting trading loop (interval={config.LOOP_INTERVAL_SECONDS}s)")
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("[Core] Stopped by user")
                break
            except Exception as e:
                logger.error(f"[Core] Unexpected error: {e}", exc_info=True)
            time.sleep(config.LOOP_INTERVAL_SECONDS)
