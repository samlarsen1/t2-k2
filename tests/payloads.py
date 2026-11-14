"""Realistic Trading212 payloads (fictional account values) for tests."""

from __future__ import annotations

from typing import Any


def position(
    ticker: str,
    quantity: float,
    average_price: float,
    current_price: float,
    *,
    available: float | None = None,
    fx_rate: float = 1.0,
    currency: str = "USD",
) -> dict[str, Any]:
    """Build a ``GET /api/v0/equity/positions`` item. ``fx_rate`` converts to GBP."""
    value = quantity * current_price * fx_rate
    cost = quantity * average_price * fx_rate
    return {
        "instrument": {
            "ticker": ticker,
            "name": ticker.split("_")[0],
            "isin": "US0000000000",
            "currency": currency,
        },
        "createdAt": "2025-03-04T14:15:22.000Z",
        "quantity": quantity,
        "quantityAvailableForTrading": quantity if available is None else available,
        "quantityInPies": 0,
        "currentPrice": current_price,
        "averagePricePaid": average_price,
        "walletImpact": {
            "currency": "GBP",
            "totalCost": round(cost, 2),
            "currentValue": round(value, 2),
            "unrealizedProfitLoss": round(value - cost, 2),
            "fxImpact": 0,
        },
    }


POSITIONS: list[dict[str, Any]] = [
    # +60%: big winner
    position("NVDA_US_EQ", 10, 100.0, 160.0, fx_rate=0.8),
    # +30%: London listing, prices in pence
    position("VODl_EQ", 1000, 80.0, 104.0, fx_rate=0.01, currency="GBX"),
    # +5%: not enough gain
    position("AAPL_US_EQ", 5, 200.0, 210.0, fx_rate=0.8),
]

PENDING_SELL = {
    "id": 4501,
    "type": "LIMIT",
    "status": "NEW",
    "strategy": "QUANTITY",
    "side": "SELL",
    "quantity": -2.0,
    "filledQuantity": 0,
    "limitPrice": 170.0,
    "timeValidity": "DAY",
    "initiatedFrom": "WEB",
    "createdAt": "2026-11-13T09:01:00.000Z",
    "instrument": {"ticker": "NVDA_US_EQ", "name": "NVIDIA", "currency": "USD"},
}


def api_sell(
    order_id: int,
    ticker: str,
    created_at: str,
    *,
    initiated_from: str = "API",
    status: str = "NEW",
    order_type: str = "LIMIT",
) -> dict[str, Any]:
    """A pending sell order as ``GET /api/v0/equity/orders`` returns it."""
    return {
        "id": order_id,
        "type": order_type,
        "status": status,
        "side": "SELL",
        "quantity": -1.5,
        "filledQuantity": 0,
        "limitPrice": 160.0,
        "timeValidity": "GOOD_TILL_CANCEL",
        "initiatedFrom": initiated_from,
        "createdAt": created_at,
        "instrument": {"ticker": ticker, "currency": "USD"},
    }

PLACED_ORDER = {
    "id": 4502,
    "type": "LIMIT",
    "status": "NEW",
    "ticker": "NVDA_US_EQ",
    "quantity": -1.5,
    "limitPrice": 159.6,
    "timeValidity": "DAY",
    "initiatedFrom": "API",
    "createdAt": "2026-11-14T09:00:00.000Z",
}


def rising_closes(n: int = 260, start: float = 100.0, end: float = 160.0) -> list[float]:
    """Steady climb that ends with a sharp run-up: overbought on every signal."""
    base = [start + (end * 0.75 - start) * i / (n - 30) for i in range(n - 30)]
    last = base[-1]
    spurt = [last + (end - last) * (i + 1) / 30 for i in range(30)]
    return base + spurt


def flat_closes(n: int = 260, level: float = 100.0) -> list[float]:
    """Sideways prices with small alternating moves: not overbought."""
    return [level + (1.0 if i % 2 else -1.0) for i in range(n)]
