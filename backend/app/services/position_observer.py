from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.core.logging import get_logger
from app.models.enums import AuditLevel, OrderStatus, SignalDirection
from app.models.observed_position import ObservedPosition
from app.models.order import Order
from app.models.position_pnl_snapshot import PositionPnlSnapshot
from app.schemas.status import PortfolioSummaryResponse, PositionResponse
from app.services.audit import record_audit

logger = get_logger(__name__)


@dataclass(frozen=True)
class NormalizedObservedPosition:
    symbol: str
    position_side: str
    direction: SignalDirection
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    leverage: int | None
    unrealized_pnl: Decimal

    @property
    def key(self) -> tuple[str, str]:
        return (self.symbol, self.position_side)


class PositionObserver:
    def __init__(self, gateway, order_manager) -> None:
        self.gateway = gateway
        self.order_manager = order_manager
        self.last_synced_at: datetime | None = None

    @staticmethod
    def _decimal(value, *, default: str = "0") -> Decimal:
        try:
            return Decimal(str(value if value is not None else default))
        except Exception:
            return Decimal(default)

    @staticmethod
    def _normalize_position_side(raw_value: object) -> str:
        value = str(raw_value or "BOTH").strip().upper()
        return value or "BOTH"

    @classmethod
    def _normalize_payload(cls, payload: list[dict] | None) -> list[NormalizedObservedPosition]:
        rows: list[NormalizedObservedPosition] = []
        for item in payload or []:
            if not isinstance(item, dict):
                continue
            amount = cls._decimal(item.get("positionAmt"))
            if amount == 0:
                continue
            symbol = str(item.get("symbol") or "").upper()
            if not symbol:
                continue
            direction = SignalDirection.LONG if amount > 0 else SignalDirection.SHORT
            leverage_value = item.get("leverage")
            leverage = int(leverage_value) if leverage_value not in {None, ""} else None
            rows.append(
                NormalizedObservedPosition(
                    symbol=symbol,
                    position_side=cls._normalize_position_side(item.get("positionSide")),
                    direction=direction,
                    quantity=abs(amount),
                    entry_price=cls._decimal(item.get("entryPrice")),
                    mark_price=cls._decimal(item.get("markPrice")),
                    leverage=leverage,
                    unrealized_pnl=cls._decimal(item.get("unRealizedProfit")),
                )
            )
        return rows

    async def _positions_by_key(self, session) -> dict[tuple[str, str], ObservedPosition]:
        rows = (await session.execute(select(ObservedPosition))).scalars().all()
        return {(row.symbol.upper(), row.position_side.upper()): row for row in rows}

    async def _managed_orders_by_symbol_direction(self, session) -> dict[tuple[str, SignalDirection], Order]:
        rows = (
            await session.execute(
                select(Order)
                .where(Order.status.in_((OrderStatus.SUBMITTING, OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION)))
                .order_by(Order.id.desc())
            )
        ).scalars().all()
        status_priority = {
            OrderStatus.IN_POSITION: 3,
            OrderStatus.ORDER_PLACED: 2,
            OrderStatus.SUBMITTING: 1,
        }
        mapping: dict[tuple[str, SignalDirection], Order] = {}
        for row in rows:
            key = (row.symbol.upper(), row.direction)
            existing = mapping.get(key)
            if existing is None or status_priority.get(row.status, 0) > status_priority.get(existing.status, 0):
                mapping[key] = row
        return mapping

    async def list_open_positions(self, session) -> list[ObservedPosition]:
        return (
            await session.execute(
                select(ObservedPosition)
                .where(ObservedPosition.closed_at.is_(None))
                .order_by(ObservedPosition.symbol, ObservedPosition.position_side)
            )
        ).scalars().all()

    async def open_position_symbols(self, session) -> set[str]:
        rows = await self.list_open_positions(session)
        return {row.symbol.upper() for row in rows}

    async def portfolio_summary(self, session) -> PortfolioSummaryResponse:
        positions = await self.list_open_positions(session)
        winning = sum(1 for row in positions if Decimal(row.unrealized_pnl or 0) > 0)
        losing = sum(1 for row in positions if Decimal(row.unrealized_pnl or 0) < 0)
        total = sum((Decimal(row.unrealized_pnl or 0) for row in positions), start=Decimal("0"))
        return PortfolioSummaryResponse(
            open_position_count=len(positions),
            winning_position_count=winning,
            losing_position_count=losing,
            total_unrealized_pnl=float(total),
            last_synced_at=self.last_synced_at,
        )

    async def position_rows(self, session) -> list[PositionResponse]:
        positions = await self.list_open_positions(session)
        return [
            PositionResponse(
                symbol=row.symbol,
                position_side=row.position_side,
                direction=row.direction.value,
                position_amount=float(row.quantity),
                entry_price=float(row.entry_price),
                mark_price=float(row.mark_price),
                unrealized_pnl=float(row.unrealized_pnl),
                leverage=row.leverage,
                source_kind=row.source_kind,
                linked_order_id=row.linked_order_id,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
                closed_at=row.closed_at,
            )
            for row in positions
        ]

    async def sync_positions(self, session, *, now: datetime | None = None) -> None:
        synced_at = now or datetime.now(timezone.utc)
        credentials = await self.order_manager.get_credentials(session)
        if credentials is None:
            self.last_synced_at = synced_at
            return

        remote_positions = self._normalize_payload(await self.gateway.positions(credentials))
        positions_by_key = await self._positions_by_key(session)
        managed_orders = await self._managed_orders_by_symbol_direction(session)
        seen_keys: set[tuple[str, str]] = set()

        for remote_position in remote_positions:
            key = (remote_position.symbol.upper(), remote_position.position_side.upper())
            seen_keys.add(key)
            linked_order = managed_orders.get((remote_position.symbol.upper(), remote_position.direction))
            if linked_order is not None and linked_order.status != OrderStatus.IN_POSITION:
                try:
                    linked_order = await self.order_manager.sync_order(session, linked_order)
                except Exception as exc:
                    logger.warning(
                        "position_observer.linked_order_sync_failed",
                        order_id=linked_order.id,
                        symbol=linked_order.symbol,
                        error=str(exc),
                    )
                managed_orders[(remote_position.symbol.upper(), remote_position.direction)] = linked_order
            observed = positions_by_key.get(key)
            if observed is None:
                observed = ObservedPosition(
                    symbol=remote_position.symbol,
                    position_side=remote_position.position_side,
                    direction=remote_position.direction,
                    source_kind="APP_LINKED" if linked_order is not None else "EXTERNAL",
                    linked_order_id=linked_order.id if linked_order is not None else None,
                    quantity=remote_position.quantity,
                    entry_price=remote_position.entry_price,
                    mark_price=remote_position.mark_price,
                    leverage=remote_position.leverage,
                    unrealized_pnl=remote_position.unrealized_pnl,
                    first_seen_at=synced_at,
                    last_seen_at=synced_at,
                    closed_at=None,
                )
                session.add(observed)
                await session.flush()
                positions_by_key[key] = observed
            else:
                if observed.closed_at is not None:
                    observed.first_seen_at = synced_at
                observed.symbol = remote_position.symbol
                observed.position_side = remote_position.position_side
                observed.direction = remote_position.direction
                observed.source_kind = "APP_LINKED" if linked_order is not None else "EXTERNAL"
                observed.linked_order_id = linked_order.id if linked_order is not None else None
                observed.quantity = remote_position.quantity
                observed.entry_price = remote_position.entry_price
                observed.mark_price = remote_position.mark_price
                observed.leverage = remote_position.leverage
                observed.unrealized_pnl = remote_position.unrealized_pnl
                observed.last_seen_at = synced_at
                observed.closed_at = None

            session.add(
                PositionPnlSnapshot(
                    observed_position_id=observed.id,
                    captured_at=synced_at,
                    quantity=remote_position.quantity,
                    mark_price=remote_position.mark_price,
                    unrealized_pnl=remote_position.unrealized_pnl,
                )
            )

        for key, observed in positions_by_key.items():
            if observed.closed_at is not None or key in seen_keys:
                continue
            observed.closed_at = synced_at
            observed.last_seen_at = synced_at

            if observed.linked_order_id is None:
                continue

            linked_order = await session.get(Order, observed.linked_order_id)
            if linked_order is None or linked_order.status != OrderStatus.IN_POSITION:
                continue

            await self.order_manager.recover_closed_order(session, linked_order)
            if linked_order.status != OrderStatus.IN_POSITION:
                continue

            linked_order.status = OrderStatus.CLOSED_EXTERNALLY
            linked_order.close_type = "EXTERNAL"
            linked_order.closed_at = synced_at
            await record_audit(
                session,
                event_type="ORDER_CLOSED_EXTERNALLY",
                level=AuditLevel.WARNING,
                message=f"{linked_order.symbol} position closed outside the bot",
                symbol=linked_order.symbol,
                order_id=linked_order.id,
                signal_id=linked_order.signal_id,
            )

        self.last_synced_at = synced_at
