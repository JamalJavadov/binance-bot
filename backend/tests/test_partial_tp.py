from decimal import Decimal

from app.models.enums import SignalDirection
from app.services.partial_tp import calculate_partial_take_profit_targets, split_partial_take_profit_quantity


def test_calculate_partial_take_profit_targets_uses_risk_distance() -> None:
    plan = calculate_partial_take_profit_targets(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("95"),
    )

    assert plan.risk_distance == Decimal("5")
    assert plan.tp1_price == Decimal("107.5")
    assert plan.tp2_price == Decimal("115.0")


def test_split_partial_take_profit_quantity_returns_none_when_min_qty_blocks_halves() -> None:
    split = split_partial_take_profit_quantity(
        total_quantity=Decimal("0.099"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.06"),
    )

    assert split is None


def test_split_partial_take_profit_quantity_splits_on_exchange_step_size() -> None:
    split = split_partial_take_profit_quantity(
        total_quantity=Decimal("0.099"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
    )

    assert split is not None
    assert split.tp1_quantity == Decimal("0.049")
    assert split.tp2_quantity == Decimal("0.050")
