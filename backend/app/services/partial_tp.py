from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import SignalDirection
from app.services.binance_gateway import round_to_increment


@dataclass(frozen=True)
class PartialTakeProfitTargets:
    risk_distance: Decimal
    tp1_price: Decimal
    tp2_price: Decimal


@dataclass(frozen=True)
class PartialTakeProfitSplit:
    tp1_quantity: Decimal
    tp2_quantity: Decimal


def calculate_partial_take_profit_targets(
    *,
    direction: SignalDirection,
    entry_price: Decimal,
    stop_loss_price: Decimal,
) -> PartialTakeProfitTargets:
    risk_distance = abs(entry_price - stop_loss_price)
    if direction == SignalDirection.LONG:
        tp1_price = entry_price + (risk_distance * Decimal("1.5"))
        tp2_price = entry_price + (risk_distance * Decimal("3.0"))
    else:
        tp1_price = entry_price - (risk_distance * Decimal("1.5"))
        tp2_price = entry_price - (risk_distance * Decimal("3.0"))
    return PartialTakeProfitTargets(
        risk_distance=risk_distance,
        tp1_price=tp1_price,
        tp2_price=tp2_price,
    )


def split_partial_take_profit_quantity(
    *,
    total_quantity: Decimal,
    step_size: Decimal,
    min_qty: Decimal,
) -> PartialTakeProfitSplit | None:
    if total_quantity <= 0 or step_size <= 0:
        return None
    tp1_quantity = round_to_increment(total_quantity / Decimal("2"), step_size)
    tp2_quantity = total_quantity - tp1_quantity
    if tp1_quantity <= 0 or tp2_quantity <= 0:
        return None
    if tp1_quantity < min_qty or tp2_quantity < min_qty:
        return None
    return PartialTakeProfitSplit(
        tp1_quantity=tp1_quantity,
        tp2_quantity=tp2_quantity,
    )
