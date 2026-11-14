"""Daily price history (Yahoo Finance) and the indicators used to spot overbought holdings.

Indicators are ratios of closes from one source, so they don't depend on the quote unit
(Yahoo quotes LSE in pence, for example).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)

# Lower-case exchange code that Trading212 puts before "_EQ" → Yahoo Finance symbol suffix
EXCHANGE_SUFFIX: dict[str, str] = {
    "l": ".L",  # London
    "d": ".DE",  # Xetra
    "p": ".PA",  # Euronext Paris
    "a": ".AS",  # Euronext Amsterdam
    "e": ".MC",  # Madrid
    "m": ".MI",  # Milan
    "s": ".SW",  # SIX Swiss
    "b": ".BR",  # Euronext Brussels
}
# Country segment ("AAPL_US_EQ") → suffix
COUNTRY_SUFFIX: dict[str, str] = {"US": "", "CA": ".TO"}

_COUNTRY_RE = re.compile(r"^(?P<symbol>.+)_(?P<country>[A-Z]{2})_EQ$")
_EXCHANGE_RE = re.compile(r"^(?P<symbol>[A-Z0-9.]+?)(?P<exchange>[a-z])_EQ$")


class MarketDataError(Exception):
    """Price history couldn't be fetched or is unusable."""


class PriceHistory(Protocol):
    """Source of daily closing prices, oldest first."""

    def daily_closes(self, symbol: str) -> list[float]:
        """Return about a year of daily closes for a Yahoo Finance symbol, oldest first."""
        ...


def to_yahoo_symbol(t212_ticker: str, overrides: Mapping[str, str] | None = None) -> str:
    """Map a Trading212 ticker to a Yahoo Finance symbol.

    Args:
        t212_ticker: e.g. ``AAPL_US_EQ``, ``VODl_EQ``, ``SAPd_EQ``.
        overrides: Explicit Trading212 ticker → Yahoo symbol entries, checked first.

    Returns:
        e.g. ``AAPL``, ``VOD.L``, ``SAP.DE``.

    Raises:
        MarketDataError: If the ticker format isn't recognised and has no override.
    """
    if overrides and t212_ticker in overrides:
        return overrides[t212_ticker]
    match = _COUNTRY_RE.match(t212_ticker)
    if match and match["country"] in COUNTRY_SUFFIX:
        return match["symbol"].replace("_", "-") + COUNTRY_SUFFIX[match["country"]]
    match = _EXCHANGE_RE.match(t212_ticker)
    if match and match["exchange"] in EXCHANGE_SUFFIX:
        return match["symbol"].rstrip(".") + EXCHANGE_SUFFIX[match["exchange"]]
    raise MarketDataError(
        f"no Yahoo symbol for {t212_ticker}; add it under yahoo_symbols in the strategy file"
    )


class YahooPriceHistory:
    """Fetches daily closes from Yahoo Finance via yfinance.

    Args:
        period: yfinance history period, e.g. ``"1y"``.
    """

    def __init__(self, period: str = "1y") -> None:
        self._period = period

    def daily_closes(self, symbol: str) -> list[float]:
        """Return daily closes for ``symbol``, oldest first.

        Raises:
            MarketDataError: If Yahoo returns nothing or the request fails.
        """
        import yfinance as yf  # deferred: slow import, and tests never need it

        try:
            frame = yf.Ticker(symbol).history(
                period=self._period, interval="1d", auto_adjust=True
            )
        except Exception as exc:  # yfinance raises a wide, undocumented set of errors
            raise MarketDataError(f"Yahoo request for {symbol} failed ({type(exc).__name__})") \
                from exc
        if frame is None or frame.empty or "Close" not in frame:
            raise MarketDataError(f"Yahoo returned no prices for {symbol}")
        closes = [float(c) for c in frame["Close"].dropna().tolist()]
        if not closes:
            raise MarketDataError(f"Yahoo returned no prices for {symbol}")
        return closes


# ---------------------------------------------------------------------------- indicators


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder's Relative Strength Index of the latest close.

    Args:
        closes: Closes, oldest first.
        period: Smoothing period.

    Returns:
        RSI in 0-100, or ``None`` with fewer than ``period + 1`` closes.
    """
    if period < 1 or len(closes) < period + 1:
        return None
    deltas = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    avg_gain = sum(max(d, 0.0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0.0) for d in deltas[:period]) / period
    for delta in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def sma(closes: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` closes, or ``None`` without enough."""
    if period < 1 or len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def pct_change(closes: Sequence[float], days: int) -> float | None:
    """% change of the latest close over the last ``days`` sessions, or ``None``."""
    if days < 1 or len(closes) < days + 1 or closes[-(days + 1)] <= 0:
        return None
    return (closes[-1] / closes[-(days + 1)] - 1.0) * 100.0
