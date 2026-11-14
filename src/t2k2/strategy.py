"""Strategy file loading, overbought detection and harvest sizing.

Everything here is pure: no network, so decisions are easy to test and reason about.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .market import pct_change, rsi, sma
from .models import Position


class StrategyError(Exception):
    """The strategy file is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OverboughtRules(_Strict):
    """When a holding counts as overbought. A ``None`` threshold disables that signal."""

    rsi_period: int = Field(14, ge=2)
    rsi_min: float | None = Field(70.0, ge=0, le=100)
    sma_period: int = Field(50, ge=2)
    min_pct_above_sma: float | None = 15.0
    run_up_days: int = Field(20, ge=1)
    min_run_up_pct: float | None = 20.0
    min_signals: int = Field(2, ge=1)

    @property
    def enabled_signals(self) -> int:
        """Number of signals with a threshold set."""
        thresholds = (self.rsi_min, self.min_pct_above_sma, self.min_run_up_pct)
        return sum(t is not None for t in thresholds)

    @property
    def history_needed(self) -> int:
        """Closes needed to compute every enabled signal."""
        return max(self.rsi_period + 1, self.sma_period, self.run_up_days + 1)

    @model_validator(mode="after")
    def _check_signals(self) -> OverboughtRules:
        if self.enabled_signals == 0:
            raise ValueError("enable at least one overbought signal")
        if self.min_signals > self.enabled_signals:
            raise ValueError(
                f"min_signals ({self.min_signals}) exceeds enabled signals "
                f"({self.enabled_signals})"
            )
        return self


class HarvestRules(_Strict):
    """How much of an overbought holding to sell, and how."""

    min_gain_pct: float = Field(25.0, ge=0)
    gain_fraction: float = Field(0.5, gt=0, le=1)
    max_position_sell_pct: float = Field(25.0, gt=0, le=100)
    min_order_value: float = Field(50.0, ge=0)
    order_type: Literal["limit", "market"] = "limit"
    limit_offset_pct: float = Field(-0.25, ge=-10, le=10)
    time_validity: Literal["DAY", "GOOD_TILL_CANCEL"] = "DAY"
    # Cancel T2-K2's own unfilled sell orders once they are this many days old (None: never)
    order_expiry_days: int | None = Field(None, ge=1)
    quantity_decimals: int = Field(2, ge=0, le=8)


class Strategy(_Strict):
    """The whole strategy file."""

    overbought: OverboughtRules = OverboughtRules()
    harvest: HarvestRules = HarvestRules()
    max_orders_per_run: int = Field(3, ge=0)
    exclude_tickers: frozenset[str] = frozenset()
    yahoo_symbols: dict[str, str] = Field(default_factory=dict)


def load_strategy(path: Path) -> Strategy:
    """Load and validate a strategy YAML file.

    Args:
        path: Path to the YAML file.

    Returns:
        The validated strategy.

    Raises:
        StrategyError: If the file can't be read, isn't YAML, or fails validation.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StrategyError(f"can't read strategy file {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise StrategyError(f"strategy file {path} is not valid YAML") from exc
    try:
        return Strategy.model_validate(raw or {})
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise StrategyError(f"invalid strategy file {path}: {details}") from exc


# ---------------------------------------------------------------------------- decisions


class Action(StrEnum):
    """Outcome of evaluating one position."""

    HARVEST = "HARVEST"
    HOLD = "HOLD"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Indicators:
    """Indicator values for the latest close; ``None`` when history is too short."""

    rsi: float | None
    pct_above_sma: float | None
    run_up_pct: float | None


@dataclass(frozen=True)
class SellOrder:
    """A proposed sell order.

    Attributes:
        ticker: Trading212 ticker.
        quantity: Shares to sell (positive).
        limit_price: Limit price in the instrument currency, or ``None`` for a market order.
        time_validity: ``DAY`` or ``GOOD_TILL_CANCEL``.
        estimated_value: Approximate proceeds in the account currency.
    """

    ticker: str
    quantity: Decimal
    limit_price: Decimal | None
    time_validity: str
    estimated_value: Decimal


@dataclass(frozen=True)
class Decision:
    """What to do with one position, and why."""

    ticker: str
    action: Action
    reason: str
    gain_pct: Decimal
    indicators: Indicators | None = None
    signals: tuple[str, ...] = ()
    order: SellOrder | None = None


def compute_indicators(closes: Sequence[float], rules: OverboughtRules) -> Indicators:
    """Compute the indicators the overbought rules use."""
    average = sma(closes, rules.sma_period)
    pct_above = None
    if average is not None and average > 0:
        pct_above = (closes[-1] / average - 1.0) * 100.0
    return Indicators(
        rsi=rsi(closes, rules.rsi_period),
        pct_above_sma=pct_above,
        run_up_pct=pct_change(closes, rules.run_up_days),
    )


def overbought_signals(ind: Indicators, rules: OverboughtRules) -> tuple[str, ...]:
    """Return a description of each enabled signal that fires."""
    fired: list[str] = []
    if rules.rsi_min is not None and ind.rsi is not None and ind.rsi >= rules.rsi_min:
        fired.append(f"RSI{rules.rsi_period} {ind.rsi:.1f} >= {rules.rsi_min:g}")
    if (
        rules.min_pct_above_sma is not None
        and ind.pct_above_sma is not None
        and ind.pct_above_sma >= rules.min_pct_above_sma
    ):
        fired.append(
            f"{ind.pct_above_sma:.1f}% above SMA{rules.sma_period} "
            f">= {rules.min_pct_above_sma:g}%"
        )
    if (
        rules.min_run_up_pct is not None
        and ind.run_up_pct is not None
        and ind.run_up_pct >= rules.min_run_up_pct
    ):
        fired.append(
            f"{rules.run_up_days}d run-up {ind.run_up_pct:.1f}% >= {rules.min_run_up_pct:g}%"
        )
    return tuple(fired)


def size_order(position: Position, rules: HarvestRules) -> SellOrder | str:
    """Size a harvest sell order for ``position``.

    Sells ``gain_fraction`` of the unrealised gain (in the account currency), capped at
    ``max_position_sell_pct`` of the position and at the shares available to trade.

    Returns:
        The order, or a reason string if no valid order can be sized.
    """
    value = position.wallet_impact.current_value
    gain = position.wallet_impact.unrealized_profit_loss
    if value <= 0 or position.quantity <= 0:
        return "position has no value"
    if gain <= 0:
        return "no unrealised gain in account currency (FX loss offsets price gain)"

    target_fraction = gain * Decimal(str(rules.gain_fraction)) / value
    fraction = min(target_fraction, Decimal(str(rules.max_position_sell_pct)) / 100)
    step = Decimal(1).scaleb(-rules.quantity_decimals)
    quantity = min(position.quantity * fraction, position.sellable_quantity)
    quantity = quantity.quantize(step, rounding=ROUND_DOWN)
    if quantity <= 0:
        return "rounded sell quantity is zero (shares may be held in pies)"

    estimated_value = (value * quantity / position.quantity).quantize(Decimal("0.01"))
    if estimated_value < Decimal(str(rules.min_order_value)):
        return f"order value {estimated_value} below min_order_value {rules.min_order_value:g}"

    limit_price: Decimal | None = None
    if rules.order_type == "limit":
        offset = 1 + Decimal(str(rules.limit_offset_pct)) / 100
        limit_price = (position.current_price * offset).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    return SellOrder(
        ticker=position.ticker,
        quantity=quantity,
        limit_price=limit_price,
        time_validity=rules.time_validity,
        estimated_value=estimated_value,
    )


def check_gain(position: Position, strategy: Strategy) -> Decision | None:
    """Return a HOLD decision if the gain is below ``min_gain_pct``, else ``None``.

    Cheap to call before fetching price history, since most holdings stop here.
    """
    gain_pct = position.gain_pct.quantize(Decimal("0.01"))
    if gain_pct < Decimal(str(strategy.harvest.min_gain_pct)):
        return Decision(
            position.ticker, Action.HOLD,
            f"gain {gain_pct}% below min_gain_pct {strategy.harvest.min_gain_pct:g}%", gain_pct,
        )
    return None


def evaluate(position: Position, closes: Sequence[float], strategy: Strategy) -> Decision:
    """Decide whether to harvest ``position`` given its recent daily closes.

    Args:
        position: The Trading212 position.
        closes: Daily closes, oldest first.
        strategy: The strategy rules.

    Returns:
        A HARVEST decision with a sized order, or HOLD/SKIP with the reason.
    """
    held = check_gain(position, strategy)
    if held is not None:
        return held
    rules = strategy.overbought
    gain_pct = position.gain_pct.quantize(Decimal("0.01"))
    if len(closes) < rules.history_needed:
        return Decision(
            position.ticker, Action.SKIP,
            f"only {len(closes)} closes; need {rules.history_needed}", gain_pct,
        )

    indicators = compute_indicators(closes, rules)
    signals = overbought_signals(indicators, rules)
    if len(signals) < rules.min_signals:
        return Decision(
            position.ticker, Action.HOLD,
            f"not overbought ({len(signals)}/{rules.min_signals} signals)",
            gain_pct, indicators, signals,
        )

    order = size_order(position, strategy.harvest)
    if isinstance(order, str):
        return Decision(position.ticker, Action.SKIP, order, gain_pct, indicators, signals)
    return Decision(
        position.ticker, Action.HARVEST, "overbought: " + "; ".join(signals),
        gain_pct, indicators, signals, order,
    )
