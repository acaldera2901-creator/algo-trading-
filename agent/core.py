"""
Agent Core — main decision and execution loop.

Flow per iteration:
  For each symbol:
    1. Fetch market snapshot (via MT5 executor or yfinance fallback)
    2. Analyze (SMC course logic + learned experience hybrid)
    3. Check risk limits
    4. Execute order if signal is valid
    5. Monitor open positions for SMC-based exit conditions
    6. On trade close → trigger improver (trade-by-trade learning)
"""

import logging
import time
from datetime import datetime, timezone

import config
from agent.analyzer import analyze, Signal
from agent.executor import create_executor, MT5Executor, DryRunExecutor, Position
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
        self.open_trades: dict = {}  # symbol → trade metadata
        init_trade_log()

        logger.info("[Core] Trading agent initialized")
        logger.info(f"[Core] Mode: {'DRY RUN' if config.DRY_RUN else config.BROKER.upper()}")
        logger.info(f"[Core] Symbols: {config.SYMBOLS}")
        logger.info(f"[Core] Timeframe: {config.TIMEFRAME}")
        logger.info(f"[Core] Risk per trade: {self.risk_params.risk_per_trade_pct}%")
        logger.info(f"[Core] Confidence threshold: {self.risk_params.confidence_threshold}")

    def _fetch_snapshot(self, symbol: str) -> MarketSnapshot:
        """Fetch market data — uses MT5 executor if connected, else yfinance."""
        mt5_ex = self.executor if isinstance(self.executor, MT5Executor) else None
        return get_market_snapshot(
            symbol=symbol,
            timeframe=config.TIMEFRAME,
            count=200,
            broker=config.BROKER,
            mt5_executor=mt5_ex,
            api_key=config.OANDA_API_KEY,
            account_id=config.OANDA_ACCOUNT_ID,
            env=config.OANDA_ENV,
        )

    def _check_exit_conditions(self, snapshot: MarketSnapshot, position: Position) -> bool:
        """
        SMC-based early exit: close if market structure turns against position.
        Returns True if position should be closed now.
        """
        ind = snapshot.indicators
        if not ind:
            return False

        direction = position.direction
        trend = ind.get("trend", "unknown")
        last_event = ind.get("last_bos_choch", "none")
        daily_bias = ind.get("daily_bias", "unknown")

        # BOS against our position = structural invalidation
        if direction == "buy" and last_event == "BOS_bearish":
            logger.info(f"[Core] BOS bearish detected — closing {snapshot.symbol} BUY early")
            return True
        if direction == "sell" and last_event == "BOS_bullish":
            logger.info(f"[Core] BOS bullish detected — closing {snapshot.symbol} SELL early")
            return True

        # Daily bias flip against position
        if direction == "buy" and daily_bias == "bearish" and trend == "down":
            logger.info(f"[Core] Daily bias flipped bearish — closing {snapshot.symbol} BUY")
            return True
        if direction == "sell" and daily_bias == "bullish" and trend == "up":
            logger.info(f"[Core] Daily bias flipped bullish — closing {snapshot.symbol} SELL")
            return True

        return False

    def _build_trade_record(
        self,
        setup: TradeSetup,
        result,
        snapshot: MarketSnapshot,
        signal: Signal,
    ) -> dict:
        ind = snapshot.indicators
        return {
            "symbol": setup.symbol,
            "direction": setup.direction,
            "entry_price": result.fill_price,
            "stop_loss": setup.stop_loss,
            "take_profit": setup.take_profit,
            "position_size": setup.position_size,
            "confidence": signal.confidence,
            "patterns": signal.patterns,
            "trend": ind.get("trend"),
            "atr": ind.get("atr"),
            "session": ind.get("session"),
            "daily_bias": ind.get("daily_bias"),
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "order_id": result.order_id,
        }

    def run_once(self):
        """Single iteration: analyze all symbols and manage open positions."""
        balance = self.executor.get_account_balance()
        if balance <= 0:
            logger.warning("[Core] Balance is 0 or unavailable — skipping iteration")
            return

        open_positions = self.executor.get_open_positions()
        open_symbols = {p.symbol for p in open_positions}

        if balance > self.peak_balance:
            self.peak_balance = balance

        # Safety: check drawdown
        if not self.risk_manager.check_drawdown(balance, self.peak_balance):
            logger.warning("[Core] Max drawdown reached — pausing trading")
            return

        # Monitor open positions for early exit
        for position in open_positions:
            snapshot = self._fetch_snapshot(position.symbol)
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

        # Analyze each symbol
        for symbol in config.SYMBOLS:
            if symbol in open_symbols:
                logger.info(f"[Core] {symbol}: already in position, skipping")
                continue
            if not self.risk_manager.can_open_position(len(open_positions)):
                logger.info("[Core] Max positions reached")
                break

            snapshot = self._fetch_snapshot(symbol)
            if snapshot.candles.empty:
                logger.warning(f"[Core] {symbol}: no market data, skipping")
                continue

            signal = analyze(snapshot)
            logger.info(
                f"[Core] {symbol} | {signal.direction.upper()} conf={signal.confidence:.2f} "
                f"session={snapshot.session} | {signal.patterns}"
            )

            if signal.direction == "none":
                continue

            atr = snapshot.indicators.get("atr", 0)
            if not atr or atr == 0:
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
                    f"[Core] ✓ {symbol} {signal.direction.upper()} @ {result.fill_price} "
                    f"SL={setup.stop_loss:.5f} TP={setup.take_profit:.5f} | {signal.reasoning[:80]}"
                )

    def run_loop(self):
        """Continuous loop — runs run_once() every LOOP_INTERVAL_SECONDS."""
        logger.info(f"[Core] Starting loop (interval={config.LOOP_INTERVAL_SECONDS}s)")
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("[Core] Stopped by user")
                break
            except Exception as e:
                logger.error(f"[Core] Unexpected error: {e}", exc_info=True)
            time.sleep(config.LOOP_INTERVAL_SECONDS)
