"""run_harvest with a fake broker and price source."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from t2k2.client import Trading212Error
from t2k2.harvest import (
    CANCEL_SETTLE_SECONDS,
    ORDER_SPACING_SECONDS,
    CancelStatus,
    HarvestReport,
    OrderStatus,
    Outcome,
    run_harvest,
)
from t2k2.market import MarketDataError
from t2k2.models import Order, Position
from t2k2.strategy import Action, HarvestRules, Strategy

from .payloads import PENDING_SELL, POSITIONS, api_sell, flat_closes, position, rising_closes


class FakeBroker:
    def __init__(
        self,
        positions: list[dict[str, object]],
        pending: list[dict[str, object]] | None = None,
        reject: set[str] | None = None,
        cancel_fails: set[int] | None = None,
    ) -> None:
        self.positions = [Position.model_validate(p) for p in positions]
        self.pending = [Order.model_validate(o) for o in pending or []]
        self.reject = reject or set()
        self.cancel_fails = cancel_fails or set()
        self.placed: list[tuple[str, Decimal, Decimal | None, str | None]] = []
        self.cancelled: list[int] = []
        self.calls: list[str] = []

    def get_positions(self) -> list[Position]:
        self.calls.append("positions")
        return self.positions

    def cancel_order(self, order_id: int) -> None:
        self.calls.append(f"cancel {order_id}")
        if order_id in self.cancel_fails:
            raise Trading212Error(404, "Order not found")
        self.cancelled.append(order_id)

    def get_pending_orders(self) -> list[Order]:
        return self.pending

    def place_limit_sell(
        self, ticker: str, quantity: Decimal, limit_price: Decimal, time_validity: str = "DAY"
    ) -> Order:
        return self._place(ticker, quantity, limit_price, time_validity)

    def place_market_sell(self, ticker: str, quantity: Decimal) -> Order:
        return self._place(ticker, quantity, None, None)

    def _place(
        self, ticker: str, quantity: Decimal, price: Decimal | None, validity: str | None
    ) -> Order:
        if ticker in self.reject:
            raise Trading212Error(400, "InsufficientResources")
        self.placed.append((ticker, quantity, price, validity))
        return Order(id=len(self.placed), ticker=ticker, quantity=-quantity, status="NEW")


class FakePrices:
    def __init__(self, closes: dict[str, list[float]]) -> None:
        self.closes = closes
        self.requested: list[str] = []

    def daily_closes(self, symbol: str) -> list[float]:
        self.requested.append(symbol)
        if symbol not in self.closes:
            raise MarketDataError(f"Yahoo returned no prices for {symbol}")
        return self.closes[symbol]


NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
PRICES = {"NVDA": rising_closes(), "VOD.L": rising_closes(start=80, end=104)}


def run(broker: FakeBroker, prices: FakePrices, strategy: Strategy | None = None,
        execute: bool = False, sleeps: list[float] | None = None) -> HarvestReport:
    return run_harvest(
        broker, prices, strategy or Strategy(), execute=execute,
        sleep=(sleeps if sleeps is not None else []).append, now=lambda: NOW,
    )


def by_ticker(report: HarvestReport) -> dict[str, Outcome]:
    return {o.decision.ticker: o for o in report.outcomes}


def test_dry_run_places_nothing() -> None:
    broker = FakeBroker(POSITIONS)
    prices = FakePrices(PRICES)
    report = run(broker, prices)
    outcomes = by_ticker(report)
    assert broker.placed == []
    assert outcomes["NVDA_US_EQ"].order_status is OrderStatus.DRY_RUN
    assert outcomes["VODl_EQ"].order_status is OrderStatus.DRY_RUN
    assert outcomes["AAPL_US_EQ"].decision.action is Action.HOLD
    # AAPL is below min_gain_pct, so its prices are never fetched
    assert prices.requested == ["NVDA", "VOD.L"]
    assert report.to_dict()["execute"] is False


def test_execute_places_orders_biggest_gain_first_and_spaces_them() -> None:
    broker = FakeBroker(POSITIONS)
    sleeps: list[float] = []
    report = run(broker, FakePrices(PRICES), execute=True, sleeps=sleeps)
    assert [p[0] for p in broker.placed] == ["NVDA_US_EQ", "VODl_EQ"]
    assert broker.placed[0][2] == Decimal("159.60")
    assert sleeps == [ORDER_SPACING_SECONDS]
    assert by_ticker(report)["NVDA_US_EQ"].order_id == 1
    assert report.failed_orders == 0


def test_max_orders_per_run_defers_the_rest() -> None:
    broker = FakeBroker(POSITIONS)
    report = run(broker, FakePrices(PRICES), Strategy(max_orders_per_run=1), execute=True)
    assert [p[0] for p in broker.placed] == ["NVDA_US_EQ"]
    assert by_ticker(report)["VODl_EQ"].order_status is OrderStatus.DEFERRED


def test_skips_pending_sells_and_excluded() -> None:
    broker = FakeBroker(POSITIONS, pending=[PENDING_SELL])
    strategy = Strategy(exclude_tickers=frozenset({"VODl_EQ"}))
    report = run(broker, FakePrices(PRICES), strategy, execute=True)
    outcomes = by_ticker(report)
    assert broker.placed == []
    assert "pending sell" in outcomes["NVDA_US_EQ"].decision.reason
    assert "excluded" in outcomes["VODl_EQ"].decision.reason


def test_market_data_failure_skips_only_that_ticker() -> None:
    broker = FakeBroker(POSITIONS)
    report = run(broker, FakePrices({"NVDA": rising_closes()}), execute=True)
    outcomes = by_ticker(report)
    assert outcomes["VODl_EQ"].decision.action is Action.SKIP
    assert [p[0] for p in broker.placed] == ["NVDA_US_EQ"]


def test_order_rejection_is_recorded_and_others_continue() -> None:
    broker = FakeBroker(POSITIONS, reject={"NVDA_US_EQ"})
    report = run(broker, FakePrices(PRICES), execute=True)
    outcomes = by_ticker(report)
    assert outcomes["NVDA_US_EQ"].order_status is OrderStatus.FAILED
    assert "InsufficientResources" in (outcomes["NVDA_US_EQ"].error or "")
    assert [p[0] for p in broker.placed] == ["VODl_EQ"]
    assert report.failed_orders == 1


def test_not_overbought_is_held() -> None:
    broker = FakeBroker([position("NVDA_US_EQ", 10, 100.0, 160.0)])
    report = run(broker, FakePrices({"NVDA": flat_closes()}), execute=True)
    assert broker.placed == []
    assert report.outcomes[0].decision.action is Action.HOLD


def test_positions_failure_propagates() -> None:
    class Down(FakeBroker):
        def get_positions(self) -> list[Position]:
            raise Trading212Error(503, "server_error after 4 attempts")

    with pytest.raises(Trading212Error):
        run(Down([]), FakePrices({}))


def test_report_dict_has_order_details() -> None:
    report = run(FakeBroker(POSITIONS), FakePrices(PRICES))
    rows = {r["ticker"]: r for r in report.to_dict()["decisions"]}
    assert rows["NVDA_US_EQ"]["order"]["status"] == "DRY_RUN"
    assert rows["NVDA_US_EQ"]["order"]["limitPrice"] == "159.60"
    assert "order" not in rows["AAPL_US_EQ"]


# NOW is Saturday 09:00; a weekly Tuesday 22:00 run is the realistic case, covered below.
EXPIRING = Strategy(
    harvest=HarvestRules(time_validity="GOOD_TILL_CANCEL", order_expiry_days=7)
)


def test_expired_api_sell_is_cancelled_then_ticker_re_evaluated() -> None:
    old = api_sell(7001, "NVDA_US_EQ", "2026-11-06T08:00:00Z")
    broker = FakeBroker(POSITIONS, pending=[old])
    sleeps: list[float] = []
    report = run(broker, FakePrices(PRICES), EXPIRING, execute=True, sleeps=sleeps)
    assert broker.cancelled == [7001]
    # Cancel happens before positions are read, so the freed shares count as sellable
    assert broker.calls[:2] == ["cancel 7001", "positions"]
    assert CANCEL_SETTLE_SECONDS in sleeps
    assert report.cancellations[0].status is CancelStatus.CANCELLED
    assert by_ticker(report)["NVDA_US_EQ"].order_status is OrderStatus.PLACED
    assert report.to_dict()["cancelledOrders"][0]["status"] == "CANCELLED"


def test_weekly_run_at_same_time_catches_last_weeks_order() -> None:
    # Placed Tuesday 22:00:40; next weekly run starts Tuesday 22:00:05, 35s short of 7 days
    order = api_sell(7002, "NVDA_US_EQ", "2026-11-10T22:00:40Z")
    broker = FakeBroker(POSITIONS, pending=[order])
    run_harvest(
        broker, FakePrices(PRICES), EXPIRING, execute=True, sleep=lambda _: None,
        now=lambda: datetime(2026, 11, 17, 22, 0, 5, tzinfo=UTC),
    )
    assert broker.cancelled == [7002]


def test_fresh_manual_and_inactive_orders_are_left_alone() -> None:
    pending = [
        api_sell(7003, "NVDA_US_EQ", "2026-11-12T08:00:00Z"),  # only 2 days old
        api_sell(7004, "VODl_EQ", "2026-10-01T08:00:00Z", initiated_from="WEB"),  # yours
        api_sell(7005, "AAPL_US_EQ", "2026-10-01T08:00:00Z", status="CANCELLING"),
    ]
    broker = FakeBroker(POSITIONS, pending=pending)
    report = run(broker, FakePrices(PRICES), EXPIRING, execute=True)
    assert broker.cancelled == []
    assert report.cancellations == []
    outcomes = by_ticker(report)
    assert "pending sell" in outcomes["NVDA_US_EQ"].decision.reason
    assert "pending sell" in outcomes["VODl_EQ"].decision.reason


def test_no_expiry_configured_never_cancels() -> None:
    broker = FakeBroker(POSITIONS, pending=[api_sell(7006, "NVDA_US_EQ", "2025-01-01T00:00:00Z")])
    run(broker, FakePrices(PRICES), Strategy(), execute=True)
    assert broker.cancelled == []


def test_dry_run_reports_cancellation_without_cancelling() -> None:
    broker = FakeBroker(POSITIONS, pending=[api_sell(7007, "NVDA_US_EQ", "2026-11-01T00:00:00Z")])
    report = run(broker, FakePrices(PRICES), EXPIRING)
    assert broker.cancelled == []
    assert report.cancellations[0].status is CancelStatus.DRY_RUN
    # Shows the order that would replace it
    assert by_ticker(report)["NVDA_US_EQ"].order_status is OrderStatus.DRY_RUN


def test_failed_cancel_keeps_ticker_skipped_and_counts_as_failure() -> None:
    broker = FakeBroker(
        POSITIONS,
        pending=[api_sell(7008, "NVDA_US_EQ", "2026-11-01T00:00:00Z")],
        cancel_fails={7008},
    )
    report = run(broker, FakePrices(PRICES), EXPIRING, execute=True)
    assert report.cancellations[0].status is CancelStatus.FAILED
    assert "pending sell" in by_ticker(report)["NVDA_US_EQ"].decision.reason
    assert [p[0] for p in broker.placed] == ["VODl_EQ"]
    assert report.failed_orders == 1
