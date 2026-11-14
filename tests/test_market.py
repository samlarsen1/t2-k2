from __future__ import annotations

import pytest

from t2k2.market import MarketDataError, pct_change, rsi, sma, to_yahoo_symbol


@pytest.mark.parametrize(
    ("ticker", "symbol"),
    [
        ("AAPL_US_EQ", "AAPL"),
        ("BRK_B_US_EQ", "BRK-B"),
        ("SHOP_CA_EQ", "SHOP.TO"),
        ("VODl_EQ", "VOD.L"),
        ("SAPd_EQ", "SAP.DE"),
        ("MCp_EQ", "MC.PA"),
        ("ASMLa_EQ", "ASML.AS"),
        ("NG.l_EQ", "NG.L"),
    ],
)
def test_to_yahoo_symbol(ticker: str, symbol: str) -> None:
    assert to_yahoo_symbol(ticker) == symbol


def test_override_wins() -> None:
    assert to_yahoo_symbol("VWRPl_EQ", {"VWRPl_EQ": "VWRP.L"}) == "VWRP.L"


def test_unknown_format() -> None:
    with pytest.raises(MarketDataError, match="yahoo_symbols"):
        to_yahoo_symbol("WEIRD")


def test_rsi_extremes_and_short_history() -> None:
    assert rsi([float(i) for i in range(30)]) == 100.0
    assert rsi([float(30 - i) for i in range(30)]) == pytest.approx(0.0)
    assert rsi([1.0] * 30) == 50.0
    assert rsi([1.0, 2.0], period=14) is None


def test_rsi_known_value() -> None:
    # Alternating +2/-1 moves: avg gain 1, avg loss 0.5 over the seed window → RSI 66.7
    closes = [100.0]
    for i in range(14):
        closes.append(closes[-1] + (2.0 if i % 2 == 0 else -1.0))
    assert rsi(closes, 14) == pytest.approx(100 - 100 / 3)


def test_sma_and_pct_change() -> None:
    closes = [1.0, 2.0, 3.0, 4.0]
    assert sma(closes, 2) == 3.5
    assert sma(closes, 5) is None
    assert pct_change(closes, 3) == pytest.approx(300.0)
    assert pct_change(closes, 4) is None
