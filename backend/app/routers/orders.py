from sqlalchemy import desc, select

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.models.enums import OrderStatus
from app.models.order import Order
from app.schemas.order import OrderRead

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=list[OrderRead])
async def list_orders(
    status: OrderStatus | None = None,
    symbol: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[OrderRead]:
    query = select(Order).where(Order.approved_by == "AUTO_MODE").order_by(desc(Order.created_at))
    if status:
        query = query.where(Order.status == status)
    if symbol:
        query = query.where(Order.symbol == symbol.upper())
    rows = (await session.execute(query)).scalars().all()
    return [OrderRead.model_validate(row) for row in rows]


@router.get("/active", response_model=list[OrderRead])
async def active_orders(session: AsyncSession = Depends(get_session)) -> list[OrderRead]:
    rows = (
        await session.execute(
            select(Order)
            .where(
                Order.approved_by == "AUTO_MODE",
                Order.status.in_([OrderStatus.SUBMITTING, OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION]),
            )
            .order_by(desc(Order.created_at))
        )
    ).scalars().all()
    return [OrderRead.model_validate(row) for row in rows]


@router.get("/{order_id}", response_model=OrderRead)
async def get_order(order_id: int, session: AsyncSession = Depends(get_session)) -> OrderRead:
    order = await session.get(Order, order_id)
    if order is None or order.approved_by != "AUTO_MODE":
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderRead.model_validate(order)
