from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from t2k2.models import Position
from t2k2.strategy import (
    Action,
    HarvestRules,
    Strategy,
    StrategyError,
    evaluate,
    load_strategy,
    size_order,
)

from .payloads import flat_closes, position, rising_closes

REPO_STRATEGY = Path(__file__).resolve().parents[1] / "config" / "strategy.yaml"


def make_position(**kwargs: float) -> Position:
    defaults: dict[str, float] = {"quantity": 10, "average_price": 100.0, "current_price": 160.0}
    defaults.update(kwargs)
    return Position.model_validate(
        position(
            "NVDA_US_EQ",
            defaults["quantity"],
            defaults["average_price"],
            defaults["current_price"],
            available=kwargs.get("available"),
        )
    )


def test_repo_strategy_file_is_valid() -> None:
    strategy = load_strategy(REPO_STRATEGY)
    assert strategy.overbought.min_signals == 2
    assert strategy.harvest.order_type == "limit"


def test_load_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text("harvest:\n  min_gain_pc: 10\n")
    with pytest.raises(StrategyError, match="min_gain_pc"):
        load_strategy(path)


def test_load_rejects_impossible_min_signals(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text("overbought:\n  min_run_up_pct: null\n  min_signals: 3\n")
    with pytest.raises(StrategyError, match="min_signals"):
        load_strategy(path)


def test_load_missing_and_bad_yaml(tmp_path: Path) -> None:
    with pytest.raises(StrategyError, match="can't read"):
        load_strategy(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("overbought: [unclosed\n")
    with pytest.raises(StrategyError, match="not valid YAML"):
        load_strategy(bad)


def test_overbought_winner_is_harvested() -> None:
    decision = evaluate(make_position(), rising_closes(), Strategy())
    assert decision.action is Action.HARVEST
    assert len(decision.signals) == 3
    assert decision.order is not None
    # Gain £480 of £1280 value; half the gain = 18.75% of the position → 1.87 shares
    assert decision.order.quantity == Decimal("1.87")
    # 160 x (1 - 0.25%) = 159.60
    assert decision.order.limit_price == Decimal("159.60")
    assert decision.order.time_validity == "DAY"


def test_small_gain_is_held_before_looking_at_prices() -> None:
    decision = evaluate(make_position(current_price=110.0), [], Strategy())
    assert decision.action is Action.HOLD
    assert "min_gain_pct" in decision.reason


def test_not_overbought_is_held() -> None:
    decision = evaluate(make_position(), flat_closes(), Strategy())
    assert decision.action is Action.HOLD
    assert "0/2 signals" in decision.reason


def test_short_history_is_skipped() -> None:
    decision = evaluate(make_position(), rising_closes()[-20:], Strategy())
    assert decision.action is Action.SKIP
    assert "need 50" in decision.reason


def test_size_order_caps_at_max_position_sell_pct() -> None:
    # +300% gain: selling all of it would be 75% of the position; capped at 25%
    order = size_order(
        make_position(current_price=400.0), HarvestRules(gain_fraction=1.0)
    )
    assert not isinstance(order, str)
    assert order.quantity == Decimal("2.50")


def test_size_order_respects_shares_in_pies() -> None:
    order = size_order(make_position(available=1.0), HarvestRules())
    assert not isinstance(order, str)
    assert order.quantity == Decimal("1.00")


def test_size_order_below_minimum_value() -> None:
    result = size_order(make_position(quantity=0.1), HarvestRules())
    assert isinstance(result, str)
    assert "min_order_value" in result


def test_size_order_market_has_no_limit_price() -> None:
    order = size_order(make_position(), HarvestRules(order_type="market"))
    assert not isinstance(order, str)
    assert order.limit_price is None


def test_fx_loss_wiping_out_gain_is_skipped() -> None:
    payload = position("NVDA_US_EQ", 10, 100.0, 160.0)
    payload["walletImpact"]["unrealizedProfitLoss"] = -5.0
    result = size_order(Position.model_validate(payload), HarvestRules())
    assert isinstance(result, str)
    assert "FX" in result
