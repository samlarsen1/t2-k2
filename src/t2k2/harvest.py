"""Orchestrates a run: fetch holdings → evaluate each → place (or just report) sell orders."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

import structlog

from .client import Trading212Error
from .market import MarketDataError, PriceHistory, to_yahoo_symbol
from .models import Order, Position
from .strategy import Action, Decision, SellOrder, Strategy, check_gain, evaluate

log = structlog.get_logger(__name__)

# Trading212 allows one order placement per 2 seconds; stay just above it.
ORDER_SPACING_SECONDS = 2.5


class Broker(Protocol):
    """The Trading212 calls a run needs (``Trading212Client`` satisfies this)."""

    def get_positions(self) -> list[Position]: ...

    def get_pending_orders(self) -> list[Order]: ...

    def place_limit_sell(
        self, ticker: str, quantity: Decimal, limit_price: Decimal, time_validity: str = ...
    ) -> Order: ...

    def place_market_sell(self, ticker: str, quantity: Decimal) -> Order: ...


class OrderStatus(StrEnum):
    """What happened to a proposed sell order."""

    DRY_RUN = "DRY_RUN"
    PLACED = "PLACED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"  # over max_orders_per_run


@dataclass(frozen=True)
class Outcome:
    """A decision plus what happened to its order, if it had one."""

    decision: Decision
    order_status: OrderStatus | None = None
    order_id: int | None = None
    error: str | None = None


@dataclass
class HarvestReport:
    """Everything a run decided and did."""

    started_at: datetime
    execute: bool
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def failed_orders(self) -> int:
        """Number of orders Trading212 rejected or that couldn't be sent."""
        return sum(o.order_status is OrderStatus.FAILED for o in self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form of the report."""
        rows: list[dict[str, Any]] = []
        for o in self.outcomes:
            d = o.decision
            ind = d.indicators
            row: dict[str, Any] = {
                "ticker": d.ticker,
                "action": d.action.value,
                "reason": d.reason,
                "gainPct": str(d.gain_pct),
                "signals": list(d.signals),
                "rsi": None if ind is None or ind.rsi is None else round(ind.rsi, 2),
                "pctAboveSma": None
                if ind is None or ind.pct_above_sma is None
                else round(ind.pct_above_sma, 2),
                "runUpPct": None
                if ind is None or ind.run_up_pct is None
                else round(ind.run_up_pct, 2),
            }
            if d.order is not None:
                row["order"] = {
                    "quantity": str(d.order.quantity),
                    "limitPrice": None
                    if d.order.limit_price is None
                    else str(d.order.limit_price),
                    "timeValidity": d.order.time_validity,
                    "estimatedValue": str(d.order.estimated_value),
                    "status": o.order_status.value if o.order_status else None,
                    "id": o.order_id,
                    "error": o.error,
                }
            rows.append(row)
        return {
            "startedAt": self.started_at.isoformat(),
            "execute": self.execute,
            "decisions": rows,
        }


def run_harvest(
    broker: Broker,
    prices: PriceHistory,
    strategy: Strategy,
    execute: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> HarvestReport:
    """Evaluate every holding and harvest the overbought ones.

    Args:
        broker: Trading212 client.
        prices: Daily price history source.
        strategy: Overbought and harvest rules.
        execute: Place orders when True; otherwise only report what would be placed.
        sleep: Sleep function, injectable for tests.
        now: Clock, injectable for tests.

    Returns:
        The run report. Per-ticker market data failures and order rejections are recorded in
        it rather than raised, so one bad ticker never blocks the rest.

    Raises:
        Trading212Error: If positions or pending orders can't be fetched.
    """
    report = HarvestReport(started_at=now(), execute=execute)
    positions = broker.get_positions()
    pending_sells = {o.ticker for o in broker.get_pending_orders() if o.is_sell}
    log.info("harvest_start", positions=len(positions), pending_sells=len(pending_sells),
             execute=execute)

    decisions = [_decide(p, prices, strategy, pending_sells) for p in positions]
    harvests = sorted(
        (d for d in decisions if d.action is Action.HARVEST),
        key=lambda d: d.gain_pct,
        reverse=True,
    )
    to_place = harvests[: strategy.max_orders_per_run]
    deferred = harvests[strategy.max_orders_per_run :]

    placed_any = False
    for decision in to_place:
        order = decision.order
        if order is None:  # pragma: no cover - HARVEST decisions always carry an order
            continue
        if not execute:
            report.outcomes.append(Outcome(decision, OrderStatus.DRY_RUN))
            continue
        if placed_any:
            sleep(ORDER_SPACING_SECONDS)
        placed_any = True
        report.outcomes.append(_place(broker, decision, order))
    report.outcomes.extend(Outcome(d, OrderStatus.DEFERRED) for d in deferred)
    report.outcomes.extend(Outcome(d) for d in decisions if d.action is not Action.HARVEST)

    log.info(
        "harvest_done",
        harvest=len(harvests),
        placed=sum(o.order_status is OrderStatus.PLACED for o in report.outcomes),
        failed=report.failed_orders,
        deferred=len(deferred),
    )
    return report


def _decide(
    position: Position, prices: PriceHistory, strategy: Strategy, pending_sells: set[str]
) -> Decision:
    ticker = position.ticker
    gain_pct = position.gain_pct.quantize(Decimal("0.01"))
    if ticker in strategy.exclude_tickers:
        return Decision(ticker, Action.SKIP, "excluded in strategy file", gain_pct)
    if ticker in pending_sells:
        return Decision(ticker, Action.SKIP, "already has a pending sell order", gain_pct)
    held = check_gain(position, strategy)
    if held is not None:
        return held
    try:
        symbol = to_yahoo_symbol(ticker, strategy.yahoo_symbols)
        closes = prices.daily_closes(symbol)
    except MarketDataError as exc:
        log.warning("market_data_unavailable", ticker=ticker, error=str(exc))
        return Decision(ticker, Action.SKIP, f"market data unavailable: {exc}", gain_pct)
    decision = evaluate(position, closes, strategy)
    log.info("position_evaluated", ticker=ticker, action=decision.action.value,
             reason=decision.reason)
    return decision


def _place(broker: Broker, decision: Decision, order: SellOrder) -> Outcome:
    try:
        if order.limit_price is not None:
            placed = broker.place_limit_sell(
                order.ticker, order.quantity, order.limit_price, order.time_validity
            )
        else:
            placed = broker.place_market_sell(order.ticker, order.quantity)
    except (Trading212Error, ValueError) as exc:
        log.error("order_failed", ticker=order.ticker, error=str(exc))
        return Outcome(decision, OrderStatus.FAILED, error=str(exc))
    log.info("order_placed", ticker=order.ticker, order_id=placed.id, status=placed.status)
    return Outcome(decision, OrderStatus.PLACED, order_id=placed.id)
