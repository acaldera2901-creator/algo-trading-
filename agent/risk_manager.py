"""
Risk Manager — fully configurable and auto-tunable.

Responsibilities:
- Calculate position size based on account balance and risk %
- Enforce max drawdown and max open positions limits
- Compute stop-loss and take-profit levels
- Expose all parameters for external override and auto-tuning
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class RiskParams:
    """All risk parameters in one place. Can be mutated by improver.py."""
    risk_per_trade_pct: float = field(default_factory=lambda: config.RISK_PER_TRADE_PCT)
    max_drawdown_pct: float = field(default_factory=lambda: config.MAX_DRAWDOWN_PCT)
    max_open_positions: int = field(default_factory=lambda: config.MAX_OPEN_POSITIONS)
    sl_multiplier: float = field(default_factory=lambda: config.SL_MULTIPLIER)
    tp_multiplier: float = field(default_factory=lambda: config.TP_MULTIPLIER)
    confidence_threshold: float = field(default_factory=lambda: config.CONFIDENCE_THRESHOLD)

    def update(self, **kwargs):
        """Update params from dict (used by improver)."""
        for k, v in kwargs.items():
            if hasattr(self, k) and v is not None:
                old = getattr(self, k)
                setattr(self, k, v)
                logger.info(f"[RiskManager] {k}: {old} → {v}")


@dataclass
class TradeSetup:
    symbol: str
    direction: str          # "buy" | "sell"
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float    # in units (OANDA) or lots (MT5)
    risk_amount: float      # in account currency
    risk_reward: float
    confidence: float


class RiskManager:
    def __init__(self, params: Optional[RiskParams] = None):
        self.params = params or RiskParams()

    def calculate_position_size(
        self,
        account_balance: float,
        entry_price: float,
        stop_loss: float,
        pip_value: float = 1.0,
    ) -> float:
        """
        Calculate units to trade so that the loss at SL == risk_per_trade_pct of balance.
        pip_value: value of 1 pip in account currency per unit (default 1.0 for OANDA)
        """
        risk_amount = account_balance * (self.params.risk_per_trade_pct / 100.0)
        sl_distance = abs(entry_price - stop_loss)
        if sl_distance == 0 or pip_value == 0:
            return 0.0
        units = risk_amount / (sl_distance * pip_value)
        return round(units, 0)

    def compute_sl_tp(
        self,
        direction: str,
        entry_price: float,
        atr: float,
    ) -> tuple[float, float]:
        """
        Compute stop-loss and take-profit using ATR and configured multipliers.
        direction: "buy" | "sell"
        """
        sl_distance = atr * self.params.sl_multiplier
        tp_distance = atr * self.params.tp_multiplier

        if direction == "buy":
            stop_loss = entry_price - sl_distance
            take_profit = entry_price + tp_distance
        else:
            stop_loss = entry_price + sl_distance
            take_profit = entry_price - tp_distance

        return round(stop_loss, 5), round(take_profit, 5)

    def build_trade_setup(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        atr: float,
        account_balance: float,
        confidence: float,
    ) -> Optional[TradeSetup]:
        """
        Full trade setup computation. Returns None if confidence is too low.
        """
        if confidence < self.params.confidence_threshold:
            logger.info(f"[RiskManager] Confidence {confidence:.2f} below threshold {self.params.confidence_threshold:.2f} — skip")
            return None

        stop_loss, take_profit = self.compute_sl_tp(direction, entry_price, atr)
        risk_amount = account_balance * (self.params.risk_per_trade_pct / 100.0)
        position_size = self.calculate_position_size(account_balance, entry_price, stop_loss)
        sl_dist = abs(entry_price - stop_loss)
        tp_dist = abs(entry_price - take_profit)
        rr = tp_dist / sl_dist if sl_dist > 0 else 0.0

        logger.info(
            f"[RiskManager] {symbol} {direction.upper()} | Entry={entry_price:.5f} "
            f"SL={stop_loss:.5f} TP={take_profit:.5f} | "
            f"Size={position_size:.0f} RR={rr:.2f}:1 Conf={confidence:.2f}"
        )

        return TradeSetup(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size=position_size,
            risk_amount=risk_amount,
            risk_reward=rr,
            confidence=confidence,
        )

    def check_drawdown(self, account_balance: float, peak_balance: float) -> bool:
        """Returns True if drawdown is within acceptable limits."""
        if peak_balance == 0:
            return True
        drawdown = (peak_balance - account_balance) / peak_balance * 100
        if drawdown >= self.params.max_drawdown_pct:
            logger.warning(f"[RiskManager] Max drawdown reached: {drawdown:.2f}% >= {self.params.max_drawdown_pct}%")
            return False
        return True

    def can_open_position(self, open_positions_count: int) -> bool:
        """Returns True if we can open another position."""
        if open_positions_count >= self.params.max_open_positions:
            logger.info(f"[RiskManager] Max open positions reached ({open_positions_count})")
            return False
        return True
