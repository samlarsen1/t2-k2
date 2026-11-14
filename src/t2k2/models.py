"""Pydantic models for the Trading212 payloads T2-K2 reads.

Unknown attributes are kept (``extra="allow"``) so API additions never break validation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Instrument(_Model):
    """A tradeable instrument as embedded in positions and orders."""

    ticker: str
    name: str | None = None
    isin: str | None = None
    # Instrument (quote) currency, e.g. "USD" or "GBX"
    currency: str | None = None


class WalletImpact(_Model):
    """A position's value and P/L expressed in the account currency."""

    currency: str
    current_value: Decimal = Field(alias="currentValue")
    total_cost: Decimal = Field(alias="totalCost")
    unrealized_profit_loss: Decimal = Field(alias="unrealizedProfitLoss")


class Position(_Model):
    """An open position. Prices are per share, in the instrument currency."""

    instrument: Instrument
    quantity: Decimal
    average_price: Decimal = Field(alias="averagePricePaid")
    current_price: Decimal = Field(alias="currentPrice")
    quantity_available_for_trading: Decimal | None = Field(
        None, alias="quantityAvailableForTrading"
    )
    wallet_impact: WalletImpact = Field(alias="walletImpact")

    @property
    def ticker(self) -> str:
        """Trading212 ticker, e.g. ``AAPL_US_EQ``."""
        return self.instrument.ticker

    @property
    def sellable_quantity(self) -> Decimal:
        """Shares that can be sold now (excludes shares held in pies or reserved by orders)."""
        if self.quantity_available_for_trading is None:
            return self.quantity
        return min(self.quantity, self.quantity_available_for_trading)

    @property
    def gain_pct(self) -> Decimal:
        """Price gain over the average price paid, in percent (instrument currency)."""
        if self.average_price <= 0:
            return Decimal(0)
        return (self.current_price - self.average_price) / self.average_price * 100


class Order(_Model):
    """A pending or newly placed order. Sell orders have a negative quantity."""

    id: int | None = None
    ticker: str
    type: str | None = None
    status: str | None = None
    side: str | None = None
    quantity: Decimal | None = None
    limit_price: Decimal | None = Field(None, alias="limitPrice")

    @model_validator(mode="before")
    @classmethod
    def _flatten_instrument(cls, data: Any) -> Any:
        # Newer payloads nest the ticker under "instrument"; older ones have it top level.
        if isinstance(data, dict) and "ticker" not in data:
            instrument = data.get("instrument")
            if isinstance(instrument, dict) and "ticker" in instrument:
                return {**data, "ticker": instrument["ticker"]}
        return data

    @property
    def is_sell(self) -> bool:
        """True for a sell order, judged by ``side`` or else a negative quantity."""
        if self.side:
            return self.side.upper() == "SELL"
        return self.quantity is not None and self.quantity < 0
