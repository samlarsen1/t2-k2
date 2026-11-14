"""Trading212Client against mocked Trading212 responses."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from decimal import Decimal

import httpx
import pytest

from t2k2.client import (
    LIMIT_ORDER_PATH,
    MARKET_ORDER_PATH,
    ORDER_PATH,
    ORDERS_PATH,
    POSITIONS_PATH,
    Trading212Client,
    Trading212Error,
)

from .payloads import PENDING_SELL, PLACED_ORDER, POSITIONS

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler, sleeps: list[float] | None = None) -> Trading212Client:
    return Trading212Client(
        "key",
        "secret",
        max_attempts=3,
        sleep=(sleeps if sleeps is not None else []).append,
        transport=httpx.MockTransport(handler),
    )


def sequence(*items: httpx.Response | Exception) -> tuple[Handler, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    it: Iterator[httpx.Response | Exception] = iter(items)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    return handler, seen


def test_get_positions_sends_basic_auth_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == POSITIONS_PATH
        expected = base64.b64encode(b"key:secret").decode()
        assert request.headers["Authorization"] == f"Basic {expected}"
        return httpx.Response(200, json=POSITIONS)

    with make_client(handler) as client:
        positions = client.get_positions()
    assert [p.ticker for p in positions] == ["NVDA_US_EQ", "VODl_EQ", "AAPL_US_EQ"]
    assert positions[0].gain_pct == Decimal(60)
    assert positions[0].wallet_impact.unrealized_profit_loss == Decimal("480.0")


def test_get_pending_orders_reads_nested_ticker() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == ORDERS_PATH
        return httpx.Response(200, json=[PENDING_SELL])

    with make_client(handler) as client:
        [order] = client.get_pending_orders()
    assert order.ticker == "NVDA_US_EQ"
    assert order.is_sell


def test_get_retries_timeouts_then_succeeds() -> None:
    handler, seen = sequence(
        httpx.ReadTimeout("slow"),
        httpx.Response(503),
        httpx.Response(200, json=[]),
    )
    sleeps: list[float] = []
    with make_client(handler, sleeps) as client:
        assert client.get_positions() == []
    assert len(seen) == 3
    assert sleeps == [2.0, 4.0]


def test_get_honours_retry_after_on_429() -> None:
    handler, _ = sequence(
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json=[]),
    )
    sleeps: list[float] = []
    with make_client(handler, sleeps) as client:
        client.get_positions()
    assert sleeps == [8.0]


def test_get_gives_up_after_max_attempts() -> None:
    handler, _ = sequence(*(httpx.Response(500) for _ in range(3)))
    with make_client(handler) as client, pytest.raises(Trading212Error, match="3 attempts"):
        client.get_positions()


def test_client_error_is_not_retried() -> None:
    handler, seen = sequence(httpx.Response(403, text="Scope(portfolio) missing"))
    with make_client(handler) as client, pytest.raises(Trading212Error, match="403"):
        client.get_positions()
    assert len(seen) == 1


def test_unexpected_payload_shape() -> None:
    handler, _ = sequence(httpx.Response(200, json={"items": []}))
    with make_client(handler) as client, pytest.raises(Trading212Error, match="JSON array"):
        client.get_positions()


def test_invalid_position_payload() -> None:
    handler, _ = sequence(httpx.Response(200, json=[{"quantity": 1}]))
    with make_client(handler) as client, pytest.raises(Trading212Error, match="position"):
        client.get_positions()


def test_place_limit_sell_sends_negative_quantity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == LIMIT_ORDER_PATH
        assert json.loads(request.content) == {
            "ticker": "NVDA_US_EQ",
            "quantity": -1.5,
            "limitPrice": 159.6,
            "timeValidity": "DAY",
        }
        return httpx.Response(200, json=PLACED_ORDER)

    with make_client(handler) as client:
        order = client.place_limit_sell("NVDA_US_EQ", Decimal("1.5"), Decimal("159.60"))
    assert order.id == 4502
    assert order.is_sell


def test_place_market_sell() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == MARKET_ORDER_PATH
        assert json.loads(request.content) == {"ticker": "VODl_EQ", "quantity": -100.0}
        return httpx.Response(200, json={"id": 9, "type": "MARKET", "quantity": -100})

    with make_client(handler) as client:
        order = client.place_market_sell("VODl_EQ", Decimal(100))
    assert order.ticker == "VODl_EQ"


def test_order_post_is_not_retried_after_timeout() -> None:
    handler, seen = sequence(httpx.ReadTimeout("slow"))
    with make_client(handler) as client, pytest.raises(Trading212Error, match="check open"):
        client.place_market_sell("VODl_EQ", Decimal(1))
    assert len(seen) == 1


def test_order_post_is_not_retried_after_5xx() -> None:
    handler, seen = sequence(httpx.Response(502))
    with make_client(handler) as client, pytest.raises(Trading212Error, match="502"):
        client.place_market_sell("VODl_EQ", Decimal(1))
    assert len(seen) == 1


def test_order_post_retries_429() -> None:
    handler, seen = sequence(httpx.Response(429), httpx.Response(200, json=PLACED_ORDER))
    with make_client(handler) as client:
        client.place_limit_sell("NVDA_US_EQ", Decimal("1.5"), Decimal("159.6"))
    assert len(seen) == 2


def test_order_rejection_surfaces_body() -> None:
    handler, _ = sequence(httpx.Response(400, json={"code": "SellingEquityNotOwned"}))
    with make_client(handler) as client, pytest.raises(Trading212Error, match="NotOwned"):
        client.place_market_sell("VODl_EQ", Decimal(1))


@pytest.mark.parametrize("quantity", [Decimal(0), Decimal(-1)])
def test_rejects_non_positive_quantity(quantity: Decimal) -> None:
    handler, seen = sequence()
    with make_client(handler) as client, pytest.raises(ValueError):
        client.place_market_sell("VODl_EQ", quantity)
    assert seen == []


def test_rejects_bad_time_validity() -> None:
    handler, _ = sequence()
    with make_client(handler) as client, pytest.raises(ValueError, match="time_validity"):
        client.place_limit_sell("X_US_EQ", Decimal(1), Decimal(1), "FOREVER")


def test_cancel_order_sends_delete_and_accepts_empty_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == ORDER_PATH.format(order_id=4501)
        return httpx.Response(200)

    with make_client(handler) as client:
        client.cancel_order(4501)


def test_cancel_order_retries_timeout() -> None:
    handler, seen = sequence(httpx.ReadTimeout("slow"), httpx.Response(200))
    with make_client(handler) as client:
        client.cancel_order(4501)
    assert len(seen) == 2


def test_cancel_order_already_filled() -> None:
    handler, _ = sequence(httpx.Response(404, text="Order not found"))
    with make_client(handler) as client, pytest.raises(Trading212Error, match="404"):
        client.cancel_order(4501)


def test_pending_order_reads_origin_and_age() -> None:
    handler, _ = sequence(httpx.Response(200, json=[PENDING_SELL]))
    with make_client(handler) as client:
        [order] = client.get_pending_orders()
    assert order.initiated_from == "WEB"
    assert order.created_at is not None and order.created_at.year == 2026
