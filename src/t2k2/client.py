"""Synchronous Trading212 client for positions, pending orders and sell orders.

API reference: https://docs.trading212.com/api

Retry policy: GET requests retry timeouts, network errors, 429 and 5xx. Order placement only
retries HTTP 429, where Trading212 rejected the request before acting on it. A timed-out or
failed POST is never retried, because the order may already exist and a retry could sell twice.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from decimal import Decimal
from types import TracebackType
from typing import Any

import httpx
import structlog
from pydantic import ValidationError

from .config import LIVE_BASE_URL
from .models import Order, Position

log = structlog.get_logger(__name__)

POSITIONS_PATH = "/api/v0/equity/positions"
ORDERS_PATH = "/api/v0/equity/orders"
LIMIT_ORDER_PATH = "/api/v0/equity/orders/limit"
MARKET_ORDER_PATH = "/api/v0/equity/orders/market"

# Upper bound on any single wait, so a bad header can't stall the process indefinitely.
MAX_WAIT_SECONDS = 120.0
# Added to server-provided waits, which are rounded to whole seconds.
RATE_LIMIT_MARGIN = 1.0

TIME_VALIDITIES = frozenset({"DAY", "GOOD_TILL_CANCEL"})


class Trading212Error(Exception):
    """A non-retryable or exhausted Trading212 API failure.

    Attributes:
        status_code: HTTP status code, or ``None`` for transport failures.
    """

    def __init__(self, status_code: int | None, message: str) -> None:
        self.status_code = status_code
        prefix = f"Trading212 API error {status_code}" if status_code else "Trading212 API error"
        super().__init__(f"{prefix}: {message}")


class _RetryableError(Exception):
    def __init__(self, status_code: int | None, reason: str, wait: float | None) -> None:
        self.status_code = status_code
        self.reason = reason
        self.wait = wait
        super().__init__(reason)


def _retry_wait(response: httpx.Response, now: float) -> float | None:
    """Seconds to wait before retrying, from ``Retry-After`` or ``x-ratelimit-reset``."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    reset = response.headers.get("x-ratelimit-reset")
    if reset is not None:
        try:
            return max(0.0, float(reset) - now)
        except ValueError:
            pass
    return None


class Trading212Client:
    """Synchronous Trading212 client. Use as a context manager.

    Args:
        api_key: Trading212 API key.
        api_secret: Trading212 API secret.
        base_url: Trading212 live or demo base URL.
        timeout: Per-request timeout in seconds.
        max_attempts: Total attempts per request, including the first.
        backoff_base: Base for exponential backoff when no rate-limit header is given.
        sleep: Sleep function, injectable for tests.
        transport: Optional httpx transport, injectable for tests.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = LIVE_BASE_URL,
        timeout: float = 30.0,
        max_attempts: int = 4,
        backoff_base: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        encoded = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Basic {encoded}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._sleep = sleep

    def __enter__(self) -> Trading212Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    # ------------------------------------------------------------------ transport

    def _send_once(self, method: str, path: str, payload: dict[str, Any] | None) -> Any:
        idempotent = method == "GET"
        try:
            response = self._client.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            if idempotent:
                raise _RetryableError(None, "timeout", None) from exc
            raise Trading212Error(
                None, f"{method} {path} timed out; the order may have been placed, check open "
                "orders before re-running"
            ) from exc
        except httpx.TransportError as exc:
            if idempotent:
                raise _RetryableError(None, type(exc).__name__, None) from exc
            raise Trading212Error(None, f"{method} {path} failed ({type(exc).__name__})") from exc

        if response.status_code == 429:
            raise _RetryableError(429, "rate_limited", _retry_wait(response, time.time()))
        if response.status_code >= 500 and idempotent:
            raise _RetryableError(response.status_code, "server_error", None)
        if not response.is_success:
            # Trading212 error bodies are short codes such as "InsufficientResources".
            raise Trading212Error(response.status_code, response.text[:200] or "no body")
        try:
            return response.json()
        except ValueError as exc:
            raise Trading212Error(response.status_code, "response was not valid JSON") from exc

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._send_once(method, path, payload)
            except _RetryableError as exc:
                if attempt == self._max_attempts:
                    raise Trading212Error(
                        exc.status_code, f"{exc.reason} after {attempt} attempts"
                    ) from exc
                backoff = self._backoff_base**attempt
                # Rate-limit headers are whole seconds, so pad them; never wait less than the
                # backoff, or a "reset in 0s" header would retry immediately into another 429.
                wait = backoff if exc.wait is None else max(exc.wait + RATE_LIMIT_MARGIN, backoff)
                wait = min(wait, MAX_WAIT_SECONDS)
                log.warning(
                    "t212_request_retry",
                    method=method,
                    path=path,
                    reason=exc.reason,
                    status=exc.status_code,
                    attempt=attempt,
                    wait_seconds=round(wait, 1),
                )
                self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    def _get_list(self, path: str) -> list[dict[str, Any]]:
        data = self._request("GET", path)
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise Trading212Error(None, f"expected a JSON array of objects from {path}")
        return data

    # ------------------------------------------------------------------ reads

    def get_positions(self) -> list[Position]:
        """Fetch all open positions.

        Returns:
            Every open position.

        Raises:
            Trading212Error: On API failure or an unexpected payload.
        """
        try:
            return [Position.model_validate(p) for p in self._get_list(POSITIONS_PATH)]
        except ValidationError as exc:
            raise Trading212Error(None, f"unexpected position payload ({exc.error_count()} "
                                  "errors)") from exc

    def get_pending_orders(self) -> list[Order]:
        """Fetch all pending (not yet filled or cancelled) orders.

        Returns:
            Every pending order.

        Raises:
            Trading212Error: On API failure or an unexpected payload.
        """
        try:
            return [Order.model_validate(o) for o in self._get_list(ORDERS_PATH)]
        except ValidationError as exc:
            raise Trading212Error(None, f"unexpected order payload ({exc.error_count()} "
                                  "errors)") from exc

    # ------------------------------------------------------------------ orders

    def place_limit_sell(
        self,
        ticker: str,
        quantity: Decimal,
        limit_price: Decimal,
        time_validity: str = "DAY",
    ) -> Order:
        """Place a limit order to sell ``quantity`` shares.

        Args:
            ticker: Trading212 ticker.
            quantity: Shares to sell (positive).
            limit_price: Minimum price per share, in the instrument currency.
            time_validity: ``DAY`` or ``GOOD_TILL_CANCEL``.

        Returns:
            The order Trading212 created.

        Raises:
            ValueError: On a non-positive quantity or price, or unknown time validity.
            Trading212Error: If Trading212 rejects the order or can't be reached.
        """
        if limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if time_validity not in TIME_VALIDITIES:
            raise ValueError(f"time_validity must be one of {sorted(TIME_VALIDITIES)}")
        payload = {
            "ticker": ticker,
            "quantity": float(-_positive(quantity)),
            "limitPrice": float(limit_price),
            "timeValidity": time_validity,
        }
        log.info("t212_place_limit_sell", ticker=ticker, quantity=str(quantity),
                 limit_price=str(limit_price))
        return self._place(LIMIT_ORDER_PATH, payload)

    def place_market_sell(self, ticker: str, quantity: Decimal) -> Order:
        """Place a market order to sell ``quantity`` shares.

        Args:
            ticker: Trading212 ticker.
            quantity: Shares to sell (positive).

        Returns:
            The order Trading212 created.

        Raises:
            ValueError: On a non-positive quantity.
            Trading212Error: If Trading212 rejects the order or can't be reached.
        """
        payload = {"ticker": ticker, "quantity": float(-_positive(quantity))}
        log.info("t212_place_market_sell", ticker=ticker, quantity=str(quantity))
        return self._place(MARKET_ORDER_PATH, payload)

    def _place(self, path: str, payload: dict[str, Any]) -> Order:
        data = self._request("POST", path, payload)
        if not isinstance(data, dict):
            raise Trading212Error(None, f"expected a JSON object from {path}")
        # Some responses omit the ticker; we know what we asked for.
        data.setdefault("ticker", payload["ticker"])
        try:
            return Order.model_validate(data)
        except ValidationError as exc:
            raise Trading212Error(None, "order placed but the response was unexpected") from exc


def _positive(quantity: Decimal) -> Decimal:
    if quantity <= 0:
        raise ValueError("quantity must be positive; the client negates it for sells")
    return quantity
