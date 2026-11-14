"""run_harvest with a fake broker and price source."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from t2k2.client import Trading212Error
from t2k2.harvest import ORDER_SPACING_SECONDS, HarvestReport, OrderStatus, Outcome, run_harvest
from t2k2.market import MarketDataError
from t2k2.models import Order, Position
from t2k2.strategy import Action, Strategy

from .payloads import PENDING_SELL, POSITIONS, flat_closes, position, rising_closes


class FakeBroker:
    def __init__(
        self,
        positions: list[dict[str, object]],
        pending: list[dict[str, object]] | None = None,
        reject: set[str] | None = None,
    ) -> None:
        self.positions = [Position.model_validate(p) for p in positions]
        self.pending = [Order.model_validate(o) for o in pending or []]
        self.reject = reject or set()
        self.placed: list[tuple[str, Decimal, Decimal | None, str | None]] = []

    def get_positions(self) -> list[Position]:
        return self.positions

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
