from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

from sqlalchemy import desc, select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.audit_log import AuditLog
from app.models.credentials import ApiCredentials
from app.models.enums import AuditLevel, OrderStatus, ScanStatus, SignalDirection, SignalStatus, TriggerType
from app.models.observed_position import ObservedPosition
from app.models.order import Order
from app.models.scan_cycle import ScanCycle
from app.models.signal import Signal
from app.services.audit import record_audit
from app.services.binance_gateway import BinanceAPIError, BinanceGateway, LeverageBracket, SymbolFilters, round_to_increment
from app.services.notifier import Notifier
from app.services.order_sizing import calculate_position_size_usdt, calculate_stop_distance_pct
from app.services.partial_tp import calculate_partial_take_profit_targets, split_partial_take_profit_quantity
from app.services.runtime_cache import AsyncTTLCache
from app.services.settings import get_settings_map
from app.services.strategy.config import resolve_strategy_config
from app.services.strategy.statistics import CandidateStatsBucket, record_closed_trade_stat
from app.services.ws_manager import WebSocketManager

logger = get_logger(__name__)


class OrderApprovalExchangeError(RuntimeError):
    def __init__(self, *, detail: str, message: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.message = message

    def __str__(self) -> str:
        return f"{self.detail} {self.message}".strip()


@dataclass
class AccountSnapshot:
    wallet_balance: Decimal
    available_balance: Decimal
    reserve_balance: Decimal
    usable_balance: Decimal
    total_initial_margin: Decimal = Decimal("0")
    total_open_order_initial_margin: Decimal = Decimal("0")
    total_position_initial_margin: Decimal = Decimal("0")

    @classmethod
    def from_account_info(cls, account_info: dict[str, Any], *, reserve_fraction: Decimal) -> "AccountSnapshot":
        wallet_balance = Decimal(str(account_info.get("totalWalletBalance", "0")))
        available_balance = Decimal(str(account_info.get("availableBalance", wallet_balance)))
        reserve_balance = max(available_balance * reserve_fraction, Decimal("0"))
        usable_balance = max(available_balance - reserve_balance, Decimal("0"))
        return cls(
            wallet_balance=wallet_balance,
            available_balance=available_balance,
            reserve_balance=reserve_balance,
            usable_balance=usable_balance,
            total_initial_margin=Decimal(str(account_info.get("totalInitialMargin", "0"))),
            total_open_order_initial_margin=Decimal(str(account_info.get("totalOpenOrderInitialMargin", "0"))),
            total_position_initial_margin=Decimal(str(account_info.get("totalPositionInitialMargin", "0"))),
        )

    @classmethod
    def from_available_balance(cls, balance: Decimal, *, reserve_fraction: Decimal) -> "AccountSnapshot":
        available_balance = max(balance, Decimal("0"))
        reserve_balance = max(available_balance * reserve_fraction, Decimal("0"))
        usable_balance = max(available_balance - reserve_balance, Decimal("0"))
        return cls(
            wallet_balance=available_balance,
            available_balance=available_balance,
            reserve_balance=reserve_balance,
            usable_balance=usable_balance,
        )


@dataclass(frozen=True)
class SharedEntrySlotBudget:
    slot_cap: int
    active_entry_order_count: int
    remaining_entry_slots: int
    active_symbols: frozenset[str]
    deployable_equity: Decimal
    committed_initial_margin: Decimal
    remaining_deployable_equity: Decimal
    portfolio_budget: Decimal
    per_slot_budget: Decimal


@dataclass(frozen=True)
class RemoteOrderRef:
    order_id: str
    role: str
    kind: str


@dataclass(frozen=True)
class MarketStateValidation:
    execution_prices: dict[str, Decimal]
    error: str | None = None
    stale_reason: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ExchangeFillSnapshot:
    close_price: Decimal
    realized_pnl: Decimal
    filled_quantity: Decimal
    closed_at: datetime | None = None


@dataclass(frozen=True)
class EntryOrderState:
    remote_kind: str
    state: dict[str, Any]
    status: str | None
    algo_status: str | None = None
    actual_order_id: str | None = None
    actual_order_state: dict[str, Any] | None = None


@dataclass(frozen=True)
class EntryStateResolution:
    outcome: str
    entry_state: EntryOrderState | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthoritativeRecoveryResult:
    outcome: str
    quantity: Decimal = Decimal("0")
    entry_price: Decimal | None = None
    entry_state: EntryOrderState | None = None
    details: dict[str, Any] = field(default_factory=dict)


class OrderManager:
    RECOVERY_OUTCOME_RECOVERED = "recovered_live_exposure"
    RECOVERY_OUTCOME_CONFIRMED_NONE = "confirmed_no_exposure"
    RECOVERY_OUTCOME_INCONCLUSIVE = "authoritative_recovery_inconclusive"
    LOOKUP_OUTCOME_FOUND = "found"
    LOOKUP_OUTCOME_CONFIRMED_MISSING = "confirmed_missing"
    LOOKUP_OUTCOME_INCONCLUSIVE = "inconclusive"
    MAX_SHARED_ENTRY_ORDERS = 3
    BALANCE_RESERVE_FRACTION = Decimal("0.10")
    ENTRY_FEE_RATE = Decimal("0.0006")
    MIN_LIQUIDATION_GAP_PCT = Decimal("0.03")
    LIQUIDATION_BUFFER_MULTIPLE = Decimal("1.25")
    ENTRY_GTD_MIN_BUFFER_SECONDS = 601
    SUBMISSION_RECOVERY_GRACE = timedelta(minutes=2)
    ACTIVE_ORDER_STATUSES = (OrderStatus.SUBMITTING, OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION)
    READ_ACCOUNT_SNAPSHOT_TTL_SECONDS = 2.0
    USER_STREAM_STALE_MULTIPLIER = 3
    USER_STREAM_STALE_MIN_SECONDS = 120
    USER_STREAM_STALE_CONSECUTIVE_FAILURES = 2
    EXPLICIT_PENDING_CANCEL_REASONS = frozenset(
        {
            "expired",
            "regime_flipped",
            "setup_state_changed",
            "spread_filter_failed",
            "volatility_shock",
            "structure_invalidated",
            "correlation_conflict",
            "viability_lost",
        }
    )
    LEGACY_PENDING_CANCEL_REASON_MAP = {
        "aqrr_order_expired": "expired",
        "validity_window_expired": "expired",
        "expired": "expired",
        "aqrr_regime_flip": "regime_flipped",
        "aqrr_unstable_market_state": "regime_flipped",
        "regime_flipped": "regime_flipped",
        "auto_mode_reversed": "setup_state_changed",
        "auto_mode_rebalanced": "setup_state_changed",
        "position_opened_elsewhere": "setup_state_changed",
        "aqrr_breakout_failure_back_inside_range": "setup_state_changed",
        "auto_mode_too_far_from_mark": "setup_state_changed",
        "setup_state_changed": "setup_state_changed",
        "aqrr_spread_deteriorated": "spread_filter_failed",
        "spread_filter_failed": "spread_filter_failed",
        "aqrr_volatility_shock": "volatility_shock",
        "volatility_shock": "volatility_shock",
        "aqrr_invalidation_structure_break": "structure_invalidated",
        "structure_invalidated": "structure_invalidated",
        "aqrr_correlation_conflict": "correlation_conflict",
        "correlation_conflict": "correlation_conflict",
        "aqrr_score_viability_lost": "viability_lost",
        "auto_mode_invalidated": "viability_lost",
        "auto_mode_stopped": "viability_lost",
        "viability_lost": "viability_lost",
        "canceled": "setup_state_changed",
        "cancelled": "setup_state_changed",
    }

    def __init__(self, gateway: BinanceGateway, ws_manager: WebSocketManager, notifier: Notifier) -> None:
        self.gateway = gateway
        self.ws_manager = ws_manager
        self.notifier = notifier
        self._read_cache = AsyncTTLCache()
        self._order_update_integrity_failures: list[datetime] = []
        self._lifecycle_poll_seconds = max(int(get_settings().lifecycle_poll_seconds), 1)
        self._user_stream_health_started_at = datetime.now(timezone.utc)
        self._last_user_stream_order_update_at: datetime | None = None
        self._last_user_stream_account_update_at: datetime | None = None
        self._last_user_stream_account_snapshot: AccountSnapshot | None = None
        self._last_user_stream_event_at: datetime | None = None
        self._user_stream_primary_available = False
        self._user_stream_primary_reason = "not_started"
        self._pending_order_trade_update_events = 0
        self._pending_account_update_events = 0
        self._pending_user_stream_symbols: set[str] = set()
        self._pending_user_stream_position_symbols: set[str] = set()
        self._pending_user_stream_trade_execution = False
        self._pending_user_stream_account_refresh = False
        self._user_stream_stale_check_streak = 0

    async def _scan_cycle_id_for_signal(
        self,
        session,
        *,
        signal_id: int | None,
        signal: Signal | None = None,
    ) -> int | None:
        if signal is not None:
            return signal.scan_cycle_id
        if signal_id is None:
            return None
        related_signal = await session.get(Signal, signal_id)
        return related_signal.scan_cycle_id if related_signal is not None else None

    async def get_credentials(self, session) -> ApiCredentials | None:
        return (await session.execute(select(ApiCredentials).limit(1))).scalar_one_or_none()

    async def _latest_completed_scan_id(self, session) -> int | None:
        return (
            await session.execute(
                select(ScanCycle.id)
                .where(
                    ScanCycle.status == ScanStatus.COMPLETE,
                    ScanCycle.trigger_type == TriggerType.AUTO_MODE,
                )
                .order_by(desc(ScanCycle.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()

    async def get_account_snapshot(self, session, credentials: ApiCredentials | None) -> AccountSnapshot:
        if credentials is None:
            return AccountSnapshot.from_available_balance(Decimal("0"), reserve_fraction=self.BALANCE_RESERVE_FRACTION)
        account_info = await self.gateway.account_info(credentials)
        return AccountSnapshot.from_account_info(account_info, reserve_fraction=self.BALANCE_RESERVE_FRACTION)

    async def get_read_account_snapshot(self, session, credentials: ApiCredentials | None) -> AccountSnapshot:
        if credentials is None:
            return AccountSnapshot.from_available_balance(Decimal("0"), reserve_fraction=self.BALANCE_RESERVE_FRACTION)

        if (
            self._user_stream_primary_available
            and self._last_user_stream_account_snapshot is not None
            and self._user_stream_account_snapshot_is_fresh()
        ):
            return self._last_user_stream_account_snapshot

        credential_key = getattr(credentials, "api_key", "default")
        return await self._read_cache.get_or_set(
            f"account_snapshot:{credential_key}",
            ttl_seconds=self.READ_ACCOUNT_SNAPSHOT_TTL_SECONDS,
            factory=lambda: self.get_account_snapshot(session, credentials),
        )

    def _user_stream_stale_threshold_seconds(self) -> int:
        return max(
            self._lifecycle_poll_seconds * self.USER_STREAM_STALE_MULTIPLIER,
            self.USER_STREAM_STALE_MIN_SECONDS,
        )

    def _user_stream_account_snapshot_is_fresh(self) -> bool:
        if self._last_user_stream_account_update_at is None:
            return False
        age_seconds = (datetime.now(timezone.utc) - self._last_user_stream_account_update_at).total_seconds()
        return age_seconds <= self._user_stream_stale_threshold_seconds()

    def _mark_user_stream_order_update_heartbeat(self) -> None:
        self._last_user_stream_order_update_at = datetime.now(timezone.utc)
        self._user_stream_stale_check_streak = 0

    def set_user_stream_primary_path_availability(self, *, available: bool, reason: str | None = None) -> None:
        self._user_stream_primary_available = bool(available)
        self._user_stream_primary_reason = str(
            reason or ("event_stream_available" if available else "event_stream_unavailable")
        )
        if not self._user_stream_primary_available:
            self._user_stream_stale_check_streak = 0

    def _account_snapshot_from_user_stream_account_update(self, payload: dict[str, Any]) -> AccountSnapshot | None:
        account_payload = payload.get("a")
        if not isinstance(account_payload, dict):
            return None
        balances = account_payload.get("B")
        if not isinstance(balances, list):
            return None
        usdt_balance: dict[str, Any] | None = None
        for item in balances:
            if not isinstance(item, dict):
                continue
            asset = str(item.get("a") or "").upper()
            if asset == "USDT":
                usdt_balance = item
                break
        if usdt_balance is None:
            return None

        wallet_balance = self._decimal_from_payload(usdt_balance.get("wb"))
        if wallet_balance is None:
            return None
        available_balance = self._decimal_from_payload(usdt_balance.get("cw"))
        if available_balance is None:
            available_balance = wallet_balance

        total_position_initial_margin = Decimal("0")
        positions = account_payload.get("P")
        if isinstance(positions, list):
            for row in positions:
                if not isinstance(row, dict):
                    continue
                isolated_wallet = self._decimal_from_payload(row.get("iw"))
                if isolated_wallet is None:
                    continue
                total_position_initial_margin += abs(isolated_wallet)

        reserve_balance = max(available_balance * self.BALANCE_RESERVE_FRACTION, Decimal("0"))
        usable_balance = max(available_balance - reserve_balance, Decimal("0"))
        return AccountSnapshot(
            wallet_balance=wallet_balance,
            available_balance=available_balance,
            reserve_balance=reserve_balance,
            usable_balance=usable_balance,
            total_initial_margin=total_position_initial_margin,
            total_position_initial_margin=total_position_initial_margin,
            total_open_order_initial_margin=Decimal("0"),
        )

    def handle_user_stream_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_type = str(payload.get("e") or "").upper()
        if event_type not in {"ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE"}:
            return {"handled": False, "event_type": event_type}

        self.set_user_stream_primary_path_availability(available=True, reason="event_stream_live")
        now = datetime.now(timezone.utc)
        event_at = self._datetime_from_millis(payload.get("E")) or now
        self._last_user_stream_event_at = event_at

        if event_type == "ORDER_TRADE_UPDATE":
            self._mark_user_stream_order_update_heartbeat()
            self._pending_order_trade_update_events += 1
            order_payload = payload.get("o")
            if not isinstance(order_payload, dict):
                order_payload = {}
            symbol = str(order_payload.get("s") or "").upper()
            if symbol:
                self._pending_user_stream_symbols.add(symbol)
            execution_type = str(order_payload.get("x") or "")
            if execution_type.upper() == "TRADE":
                self._pending_user_stream_trade_execution = True
            return {
                "handled": True,
                "event_type": event_type,
                "symbol": symbol,
                "order_status": str(order_payload.get("X") or ""),
                "execution_type": execution_type,
            }

        self._last_user_stream_account_update_at = event_at
        self._pending_account_update_events += 1
        self._pending_user_stream_account_refresh = True
        account_snapshot = self._account_snapshot_from_user_stream_account_update(payload)
        if account_snapshot is not None:
            self._last_user_stream_account_snapshot = account_snapshot
        account_payload = payload.get("a")
        account_reason = None
        if isinstance(account_payload, dict):
            account_reason = str(account_payload.get("m") or "").strip() or None
            positions = account_payload.get("P")
            if isinstance(positions, list):
                for row in positions:
                    if not isinstance(row, dict):
                        continue
                    symbol = str(row.get("s") or "").upper()
                    if symbol:
                        self._pending_user_stream_position_symbols.add(symbol)
        return {
            "handled": True,
            "event_type": event_type,
            "reason": account_reason,
        }

    def consume_user_stream_supervision_events(self) -> dict[str, Any]:
        payload = {
            "order_trade_update_count": self._pending_order_trade_update_events,
            "account_update_count": self._pending_account_update_events,
            "prioritized_symbols": sorted(self._pending_user_stream_symbols | self._pending_user_stream_position_symbols),
            "position_symbols": sorted(self._pending_user_stream_position_symbols),
            "trade_execution_pending": self._pending_user_stream_trade_execution,
            "account_refresh_pending": self._pending_user_stream_account_refresh,
            "last_event_at": (
                None if self._last_user_stream_event_at is None else self._last_user_stream_event_at.isoformat()
            ),
            "last_order_update_at": (
                None
                if self._last_user_stream_order_update_at is None
                else self._last_user_stream_order_update_at.isoformat()
            ),
            "last_account_update_at": (
                None
                if self._last_user_stream_account_update_at is None
                else self._last_user_stream_account_update_at.isoformat()
            ),
        }
        self._pending_order_trade_update_events = 0
        self._pending_account_update_events = 0
        self._pending_user_stream_symbols.clear()
        self._pending_user_stream_position_symbols.clear()
        self._pending_user_stream_trade_execution = False
        self._pending_user_stream_account_refresh = False
        return payload

    async def user_data_stream_health(self, session, credentials: ApiCredentials | None) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        active_order_count = 0
        if credentials is not None:
            active_order_count = len(await self.active_entry_orders(session))
        required = credentials is not None and active_order_count > 0
        stale_threshold_seconds = self._user_stream_stale_threshold_seconds()
        stale_check_threshold = self.USER_STREAM_STALE_CONSECUTIVE_FAILURES

        if not self._user_stream_primary_available:
            self._user_stream_stale_check_streak = 0
            return {
                "healthy": True,
                "required": required,
                "mode": "polling_fallback",
                "health_reason": "event_stream_unavailable",
                "active_order_count": active_order_count,
                "last_order_update_at": (
                    None
                    if self._last_user_stream_order_update_at is None
                    else self._last_user_stream_order_update_at.isoformat()
                ),
                "last_account_update_at": (
                    None
                    if self._last_user_stream_account_update_at is None
                    else self._last_user_stream_account_update_at.isoformat()
                ),
                "last_event_at": (
                    None if self._last_user_stream_event_at is None else self._last_user_stream_event_at.isoformat()
                ),
                "heartbeat_age_seconds": (
                    None
                    if self._last_user_stream_order_update_at is None
                    else max(int((now - self._last_user_stream_order_update_at).total_seconds()), 0)
                ),
                "stale_threshold_seconds": stale_threshold_seconds,
                "stale_check_streak": 0,
                "stale_check_threshold": stale_check_threshold,
                "stream_primary_available": False,
                "stream_primary_reason": self._user_stream_primary_reason,
            }

        if not required:
            self._user_stream_stale_check_streak = 0
            return {
                "healthy": True,
                "required": False,
                "mode": "authoritative_order_update_liveness",
                "health_reason": "not_required",
                "active_order_count": active_order_count,
                "last_order_update_at": (
                    None
                    if self._last_user_stream_order_update_at is None
                    else self._last_user_stream_order_update_at.isoformat()
                ),
                "last_account_update_at": (
                    None
                    if self._last_user_stream_account_update_at is None
                    else self._last_user_stream_account_update_at.isoformat()
                ),
                "heartbeat_age_seconds": (
                    None
                    if self._last_user_stream_order_update_at is None
                    else max(int((now - self._last_user_stream_order_update_at).total_seconds()), 0)
                ),
                "stale_threshold_seconds": stale_threshold_seconds,
                "stale_check_streak": 0,
                "stale_check_threshold": stale_check_threshold,
                "stream_primary_available": True,
                "stream_primary_reason": self._user_stream_primary_reason,
            }

        reference_at = self._last_user_stream_order_update_at or self._user_stream_health_started_at
        heartbeat_age_seconds = max(int((now - reference_at).total_seconds()), 0)
        stale = heartbeat_age_seconds > stale_threshold_seconds
        if stale:
            self._user_stream_stale_check_streak += 1
        else:
            self._user_stream_stale_check_streak = 0
        healthy = self._user_stream_stale_check_streak < stale_check_threshold

        return {
            "healthy": healthy,
            "required": required,
            "mode": "authoritative_order_update_liveness",
            "health_reason": (
                "stale_order_update_liveness"
                if not healthy
                else ("awaiting_first_order_update" if self._last_user_stream_order_update_at is None else "healthy")
            ),
            "active_order_count": active_order_count,
            "last_order_update_at": (
                None
                if self._last_user_stream_order_update_at is None
                else self._last_user_stream_order_update_at.isoformat()
            ),
            "last_account_update_at": (
                None
                if self._last_user_stream_account_update_at is None
                else self._last_user_stream_account_update_at.isoformat()
            ),
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "stale_threshold_seconds": stale_threshold_seconds,
            "stale_check_streak": self._user_stream_stale_check_streak,
            "stale_check_threshold": stale_check_threshold,
            "stream_primary_available": True,
            "stream_primary_reason": self._user_stream_primary_reason,
        }

    async def order_update_integrity_state(self, session) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        lookback = timedelta(minutes=15)
        threshold = 3
        self._order_update_integrity_failures = [
            timestamp
            for timestamp in self._order_update_integrity_failures
            if now - timestamp <= lookback
        ]
        failure_count = len(self._order_update_integrity_failures)
        return {
            "healthy": failure_count < threshold,
            "failure_count": failure_count,
            "threshold": threshold,
            "lookback_minutes": int(lookback.total_seconds() // 60),
        }

    async def get_balance(self, session, credentials: ApiCredentials | None) -> Decimal:
        snapshot = await self.get_account_snapshot(session, credentials)
        return snapshot.available_balance

    async def active_entry_orders(self, session) -> list[Order]:
        return (
            await session.execute(
                select(Order).where(Order.status.in_(self.ACTIVE_ORDER_STATUSES))
            )
        ).scalars().all()

    @staticmethod
    def _managed_client_tag(order: Order, role: str) -> str:
        if order.id is None:
            raise ValueError("Order must have an id before generating client ids")
        return f"fbot.{order.id}.{role}"

    def _managed_entry_client_id(self, order: Order) -> str:
        return self._managed_client_tag(order, "entry")

    def _managed_tp_client_id(self, order: Order) -> str:
        return self._managed_client_tag(order, "tp")

    def _managed_tp1_client_id(self, order: Order) -> str:
        return self._managed_client_tag(order, "tp1")

    def _managed_tp2_client_id(self, order: Order) -> str:
        return self._managed_client_tag(order, "tp2")

    def _managed_sl_client_id(self, order: Order) -> str:
        return self._managed_client_tag(order, "sl")

    @staticmethod
    def _partial_tp_requested(settings_map: dict[str, str], *, approved_by: str) -> bool:
        return False

    @staticmethod
    def _partial_tp_enabled(order: Order) -> bool:
        return bool(getattr(order, "partial_tp_enabled", False))

    @staticmethod
    def _strategy_context_payload(order: Order) -> dict[str, Any]:
        return dict(getattr(order, "strategy_context", {}) or {})

    def _strategy_decimal(self, order: Order, key: str) -> Decimal | None:
        return self._decimal_from_payload(self._strategy_context_payload(order).get(key))

    def _update_strategy_context(self, order: Order, **values: str | None) -> None:
        context = self._strategy_context_payload(order)
        for key, value in values.items():
            if value is None:
                context.pop(key, None)
            else:
                context[key] = value
        order.strategy_context = context

    def _filled_entry_quantity(self, order: Order) -> Decimal:
        stored_quantity = self._strategy_decimal(order, "entry_filled_quantity")
        if stored_quantity is not None:
            return max(stored_quantity, Decimal("0"))

        if self._partial_tp_enabled(order) and order.tp1_filled_at is not None:
            tp1_quantity = self._decimal_from_payload(getattr(order, "tp_quantity_1", None))
            tp2_quantity = self._decimal_from_payload(getattr(order, "tp_quantity_2", None))
            if tp1_quantity is not None and tp2_quantity is not None:
                return max(tp1_quantity + tp2_quantity, Decimal("0"))

        if order.status == OrderStatus.IN_POSITION:
            live_quantity = self._decimal_from_payload(getattr(order, "remaining_quantity", None))
            if live_quantity is not None:
                return max(live_quantity, Decimal("0"))

        return Decimal("0")

    def _live_position_quantity(self, order: Order) -> Decimal:
        if order.status != OrderStatus.IN_POSITION:
            return Decimal("0")
        remaining_quantity = self._decimal_from_payload(getattr(order, "remaining_quantity", None))
        if remaining_quantity is not None:
            return max(remaining_quantity, Decimal("0"))
        stored_quantity = self._strategy_decimal(order, "protection_quantity")
        if stored_quantity is not None:
            return max(stored_quantity, Decimal("0"))
        if self._partial_tp_enabled(order) and order.tp1_filled_at is not None:
            tp2_quantity = self._decimal_from_payload(getattr(order, "tp_quantity_2", None))
            if tp2_quantity is not None:
                return max(tp2_quantity, Decimal("0"))
        return Decimal("0")

    def _protection_quantity(self, order: Order) -> Decimal:
        stored_quantity = self._strategy_decimal(order, "protection_quantity")
        if stored_quantity is not None:
            return max(stored_quantity, Decimal("0"))
        return Decimal("0")

    def _expected_protection_roles(self, order: Order) -> tuple[str, ...]:
        if self._partial_tp_enabled(order):
            return ("tp1", "tp2", "sl")
        return ("tp", "sl")

    @staticmethod
    def _protection_price_rounding(direction: SignalDirection) -> str:
        return ROUND_UP if direction == SignalDirection.LONG else ROUND_DOWN

    def _normalize_take_profit_price(
        self,
        *,
        direction: SignalDirection,
        filters: SymbolFilters,
        take_profit: Decimal,
    ) -> Decimal:
        return round_to_increment(
            take_profit,
            filters.tick_size,
            rounding=self._protection_price_rounding(direction),
        )

    @staticmethod
    def _valid_partial_take_profit_prices(
        *,
        direction: SignalDirection,
        entry_price: Decimal,
        tp1_price: Decimal,
        tp2_price: Decimal,
    ) -> bool:
        if direction == SignalDirection.LONG:
            return entry_price < tp1_price < tp2_price
        return tp2_price < tp1_price < entry_price

    @staticmethod
    def _datetime_from_millis(value: Any) -> datetime | None:
        if value in {None, ""}:
            return None
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _datetime_from_iso8601(value: Any) -> datetime | None:
        if value in {None, ""}:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _normalize_utc_datetime(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    def pending_entry_expiry(self, order: Order) -> datetime:
        order_expiry = self._normalize_utc_datetime(order.expires_at).astimezone(timezone.utc)
        strategy_context = self._strategy_context_payload(order)
        context_expiry_ms = self._datetime_from_millis(strategy_context.get("entry_expiry_epoch_ms"))
        context_expiry_iso = self._datetime_from_iso8601(strategy_context.get("entry_expiry_at"))

        candidates = [order_expiry]
        if context_expiry_ms is not None:
            candidates.append(context_expiry_ms)
        if context_expiry_iso is not None:
            candidates.append(context_expiry_iso)
        authoritative_expiry = min(candidates)
        expiry_drift_seconds = max(
            abs((candidate - authoritative_expiry).total_seconds())
            for candidate in candidates
        )
        if expiry_drift_seconds > 1:
            logger.warning(
                "order_manager.pending_expiry_metadata_drift",
                order_id=getattr(order, "id", None),
                symbol=getattr(order, "symbol", None),
                order_expires_at=order_expiry.isoformat(),
                context_entry_expiry_epoch_ms=str(strategy_context.get("entry_expiry_epoch_ms") or ""),
                context_entry_expiry_at=str(strategy_context.get("entry_expiry_at") or ""),
                authoritative_expires_at=authoritative_expiry.isoformat(),
            )
        if order_expiry != authoritative_expiry:
            order.expires_at = authoritative_expiry
        self._update_strategy_context(
            order,
            entry_expiry_at=authoritative_expiry.isoformat(),
            entry_expiry_epoch_ms=str(self._utc_timestamp_millis(authoritative_expiry)),
        )
        return authoritative_expiry

    def pending_entry_expired(self, order: Order, *, now: datetime | None = None) -> bool:
        reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return reference_now >= self.pending_entry_expiry(order)

    @classmethod
    def _normalize_pending_cancel_reason(cls, reason: str) -> str:
        normalized_reason = str(reason or "").strip().lower()
        if not normalized_reason:
            raise ValueError("Pending entry cancellation reason is required")
        if normalized_reason == "manual_cancel":
            return "manual_cancel"
        if normalized_reason.startswith("aqrr_kill_switch_"):
            return "viability_lost"
        mapped_reason = cls.LEGACY_PENDING_CANCEL_REASON_MAP.get(normalized_reason, normalized_reason)
        if mapped_reason not in cls.EXPLICIT_PENDING_CANCEL_REASONS:
            raise ValueError(f"Unsupported pending entry cancellation reason: {reason}")
        return mapped_reason

    @staticmethod
    def _utc_timestamp_millis(value: datetime) -> int:
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(normalized.astimezone(timezone.utc).timestamp() * 1000)

    def _entry_good_till_date_ms(self, *, expires_at: datetime, now: datetime | None = None) -> int | None:
        reference_now = now or datetime.now(timezone.utc)
        min_allowed = reference_now + timedelta(seconds=self.ENTRY_GTD_MIN_BUFFER_SECONDS)
        if expires_at <= min_allowed:
            return None
        return self._utc_timestamp_millis(expires_at)

    @staticmethod
    def _is_gtd_unsupported_error(exc: BinanceAPIError) -> bool:
        if exc.code in {-1100, -1102, -1116, -1130}:
            return True
        exchange_message = (exc.exchange_message or exc.raw_message or "").lower()
        return (
            "goodtilldate" in exchange_message
            or "timeinforce" in exchange_message
            or "gtd" in exchange_message
        )

    @staticmethod
    def _entry_quantity_step(filters: SymbolFilters) -> Decimal:
        lot_step = filters.step_size if filters.step_size > 0 else Decimal("0")
        market_step = (
            filters.market_step_size
            if filters.market_step_size is not None and filters.market_step_size > 0
            else Decimal("0")
        )
        if lot_step <= 0:
            return market_step if market_step > 0 else Decimal("0.001")
        if market_step <= 0:
            return lot_step
        return max(lot_step, market_step)

    @staticmethod
    def _entry_min_qty(filters: SymbolFilters) -> Decimal:
        market_min = (
            filters.market_min_qty
            if filters.market_min_qty is not None and filters.market_min_qty > 0
            else Decimal("0")
        )
        return max(filters.min_qty, market_min)

    @staticmethod
    def _entry_max_qty(filters: SymbolFilters) -> Decimal | None:
        candidates = [
            quantity
            for quantity in (
                filters.max_qty,
                filters.market_max_qty,
            )
            if quantity is not None and quantity > 0
        ]
        if not candidates:
            return None
        return min(candidates)

    def _entry_quantity_within_market_lot(self, *, filters: SymbolFilters, quantity: Decimal) -> bool:
        if quantity <= 0:
            return False
        min_qty = self._entry_min_qty(filters)
        if quantity < min_qty:
            return False
        max_qty = self._entry_max_qty(filters)
        if max_qty is not None and quantity > max_qty:
            return False
        return True

    @staticmethod
    def _percent_price_bounds(filters: SymbolFilters, *, mark_price: Decimal) -> tuple[Decimal, Decimal] | None:
        multiplier_up = filters.percent_price_multiplier_up
        multiplier_down = filters.percent_price_multiplier_down
        if (
            mark_price <= 0
            or multiplier_up is None
            or multiplier_down is None
            or multiplier_up <= 0
            or multiplier_down <= 0
        ):
            return None
        return (mark_price * multiplier_down, mark_price * multiplier_up)

    @classmethod
    def _submitting_order_is_stale(cls, order: Order, *, now: datetime) -> bool:
        reference = getattr(order, "updated_at", None) or getattr(order, "created_at", None)
        if reference is None:
            return True
        return now - reference >= cls.SUBMISSION_RECOVERY_GRACE

    @classmethod
    def _deployable_equity(cls, *, account_equity: Decimal) -> Decimal:
        return max(account_equity, Decimal("0")) * (Decimal("1") - cls.BALANCE_RESERVE_FRACTION)

    @staticmethod
    def _committed_initial_margin(snapshot: AccountSnapshot) -> Decimal:
        split_total = max(snapshot.total_open_order_initial_margin, Decimal("0")) + max(
            snapshot.total_position_initial_margin,
            Decimal("0"),
        )
        return max(max(snapshot.total_initial_margin, Decimal("0")), split_total)

    def build_shared_entry_slot_budget(
        self,
        *,
        available_balance: Decimal,
        account_equity: Decimal | None = None,
        committed_initial_margin: Decimal = Decimal("0"),
        active_entry_orders: list[Order] | None = None,
        active_entry_order_count: int | None = None,
    ) -> SharedEntrySlotBudget:
        slot_cap = self.MAX_SHARED_ENTRY_ORDERS
        effective_equity = account_equity if account_equity is not None else available_balance
        deployable_equity = self._deployable_equity(account_equity=effective_equity)
        remaining_deployable_equity = max(deployable_equity - max(committed_initial_margin, Decimal("0")), Decimal("0"))
        portfolio_budget = min(max(available_balance, Decimal("0")), remaining_deployable_equity)
        active_orders = list(active_entry_orders or [])
        active_count = max(active_entry_order_count if active_entry_order_count is not None else len(active_orders), 0)
        remaining_entry_slots = max(slot_cap - active_count, 0)
        per_slot_budget = (
            portfolio_budget / Decimal(remaining_entry_slots)
            if remaining_entry_slots > 0
            else Decimal("0")
        )
        active_symbols = frozenset(
            str(order.symbol).upper()
            for order in active_orders
            if getattr(order, "symbol", None)
        )
        return SharedEntrySlotBudget(
            slot_cap=slot_cap,
            active_entry_order_count=active_count,
            remaining_entry_slots=remaining_entry_slots,
            active_symbols=active_symbols,
            deployable_equity=deployable_equity,
            committed_initial_margin=max(committed_initial_margin, Decimal("0")),
            remaining_deployable_equity=remaining_deployable_equity,
            portfolio_budget=portfolio_budget,
            per_slot_budget=per_slot_budget,
        )

    async def get_shared_entry_slot_budget(
        self,
        session,
        *,
        account_snapshot: AccountSnapshot | None = None,
    ) -> SharedEntrySlotBudget:
        snapshot = account_snapshot
        if snapshot is None:
            credentials = await self.get_credentials(session)
            snapshot = await self.get_account_snapshot(session, credentials)
        active_orders = await self.active_entry_orders(session)
        return self.build_shared_entry_slot_budget(
            available_balance=snapshot.available_balance,
            account_equity=snapshot.wallet_balance,
            committed_initial_margin=self._committed_initial_margin(snapshot),
            active_entry_orders=active_orders,
        )

    def choose_leverage(self, sl_distance_pct: Decimal, leverage_cap: int) -> int:
        if sl_distance_pct <= Decimal("0.015"):
            return min(leverage_cap, 10)
        if sl_distance_pct <= Decimal("0.03"):
            return min(leverage_cap, 5)
        if sl_distance_pct <= Decimal("0.05"):
            return min(leverage_cap, 3)
        return 1

    @staticmethod
    def _bracket_for_notional(
        leverage_brackets: list[LeverageBracket] | None,
        *,
        notional: Decimal,
    ) -> LeverageBracket | None:
        if not leverage_brackets:
            return None
        normalized_notional = max(notional, Decimal("0"))
        for bracket in leverage_brackets:
            upper_bound = bracket.notional_cap
            if normalized_notional >= max(bracket.notional_floor, Decimal("0")) and (
                upper_bound <= 0 or normalized_notional < upper_bound
            ):
                return bracket
        return leverage_brackets[-1]

    @classmethod
    def _effective_maintenance_margin_ratio(
        cls,
        *,
        notional: Decimal,
        leverage_brackets: list[LeverageBracket] | None,
    ) -> Decimal:
        bracket = cls._bracket_for_notional(leverage_brackets, notional=notional)
        if bracket is None:
            return Decimal("0.004")
        ratio = max(bracket.maint_margin_ratio, Decimal("0"))
        if bracket.cum > 0 and notional > 0:
            ratio += bracket.cum / notional
        return min(ratio, Decimal("0.99"))

    def liquidation_price(
        self,
        entry_price: Decimal,
        leverage: int,
        direction: SignalDirection,
        *,
        notional: Decimal = Decimal("0"),
        leverage_brackets: list[LeverageBracket] | None = None,
    ) -> Decimal:
        maintenance_margin_rate = self._effective_maintenance_margin_ratio(
            notional=notional,
            leverage_brackets=leverage_brackets,
        )
        if direction == SignalDirection.LONG:
            return entry_price * (Decimal("1") - (Decimal("1") / leverage) + maintenance_margin_rate)
        return entry_price * (Decimal("1") + (Decimal("1") / leverage) - maintenance_margin_rate)

    def normalize_order_prices(
        self,
        *,
        filters: SymbolFilters,
        direction: SignalDirection,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
    ) -> dict[str, Decimal]:
        if direction == SignalDirection.LONG:
            return {
                "entry_price": round_to_increment(entry_price, filters.tick_size, rounding=ROUND_DOWN),
                "stop_loss": round_to_increment(stop_loss, filters.tick_size, rounding=ROUND_UP),
                "take_profit": round_to_increment(take_profit, filters.tick_size, rounding=ROUND_UP),
            }

        return {
            "entry_price": round_to_increment(entry_price, filters.tick_size, rounding=ROUND_UP),
            "stop_loss": round_to_increment(stop_loss, filters.tick_size, rounding=ROUND_UP),
            "take_profit": round_to_increment(take_profit, filters.tick_size, rounding=ROUND_DOWN),
        }

    @staticmethod
    def _decimal_string(value: Decimal) -> str:
        return format(value.normalize(), "f") if value != 0 else "0"

    @staticmethod
    def _decimal_from_payload(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    def _valid_execution_prices(
        self,
        *,
        direction: SignalDirection,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
    ) -> bool:
        if direction == SignalDirection.LONG:
            return stop_loss < entry_price < take_profit
        return take_profit < entry_price < stop_loss

    def _market_state_reason(
        self,
        *,
        entry_style: str,
        direction: SignalDirection,
        mark_price: Decimal,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
    ) -> str | None:
        if direction == SignalDirection.LONG:
            if mark_price <= stop_loss:
                return "stop_loss_crossed"
            if mark_price >= take_profit:
                return "take_profit_crossed"
            if entry_style == "STOP_ENTRY":
                if mark_price >= entry_price:
                    return "entry_crossed"
            elif mark_price <= entry_price:
                return "entry_crossed"
            return None

        if mark_price >= stop_loss:
            return "stop_loss_crossed"
        if mark_price <= take_profit:
            return "take_profit_crossed"
        if entry_style == "STOP_ENTRY":
            if mark_price <= entry_price:
                return "entry_crossed"
        elif mark_price >= entry_price:
            return "entry_crossed"
        return None

    def _market_state_message(
        self,
        *,
        symbol: str,
        entry_style: str,
        stale_reason: str,
        mark_price: Decimal,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
    ) -> str:
        if stale_reason == "stop_loss_crossed":
            summary = "the stop-loss would immediately trigger on Binance at the current mark price."
        elif stale_reason == "take_profit_crossed":
            summary = "the take-profit would immediately trigger on Binance at the current mark price."
        elif entry_style == "STOP_ENTRY":
            summary = "the entry stop would trigger immediately at the current mark price."
        else:
            summary = "the entry level has already been crossed and the pending LIMIT order would execute immediately."
        return (
            f"{symbol} order could not be placed because {summary} "
            f"Mark {self._decimal_string(mark_price)}, entry {self._decimal_string(entry_price)}, "
            f"stop-loss {self._decimal_string(stop_loss)}, take-profit {self._decimal_string(take_profit)}."
        )

    def _percent_price_filter_message(
        self,
        *,
        symbol: str,
        entry_price: Decimal,
        mark_price: Decimal,
        lower_bound: Decimal,
        upper_bound: Decimal,
    ) -> str:
        return (
            f"{symbol} order could not be placed because the entry price is outside Binance PERCENT_PRICE limits. "
            f"Entry {self._decimal_string(entry_price)}, mark {self._decimal_string(mark_price)}, "
            f"allowed range {self._decimal_string(lower_bound)} to {self._decimal_string(upper_bound)}."
        )

    def validate_market_state(
        self,
        *,
        symbol: str,
        filters: SymbolFilters,
        entry_style: str = "LIMIT_GTD",
        direction: SignalDirection,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
        mark_price: Decimal,
    ) -> MarketStateValidation:
        execution_prices = self.normalize_order_prices(
            filters=filters,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        normalized_entry = execution_prices["entry_price"]
        normalized_stop = execution_prices["stop_loss"]
        normalized_take_profit = execution_prices["take_profit"]
        if not self._valid_execution_prices(
            direction=direction,
            entry_price=normalized_entry,
            stop_loss=normalized_stop,
            take_profit=normalized_take_profit,
        ):
            return MarketStateValidation(
                execution_prices=execution_prices,
                error="invalid_execution_prices",
            )

        percent_bounds = self._percent_price_bounds(filters, mark_price=mark_price)
        if percent_bounds is not None:
            lower_bound, upper_bound = percent_bounds
            if normalized_entry < lower_bound or normalized_entry > upper_bound:
                return MarketStateValidation(
                    execution_prices=execution_prices,
                    error="percent_price_filter_failed",
                    message=self._percent_price_filter_message(
                        symbol=symbol,
                        entry_price=normalized_entry,
                        mark_price=mark_price,
                        lower_bound=lower_bound,
                        upper_bound=upper_bound,
                    ),
                )

        stale_reason = self._market_state_reason(
            entry_style=entry_style,
            direction=direction,
            mark_price=mark_price,
            entry_price=normalized_entry,
            stop_loss=normalized_stop,
            take_profit=normalized_take_profit,
        )
        if stale_reason is None:
            return MarketStateValidation(execution_prices=execution_prices)

        return MarketStateValidation(
            execution_prices=execution_prices,
            stale_reason=stale_reason,
            message=self._market_state_message(
                symbol=symbol,
                entry_style=entry_style,
                stale_reason=stale_reason,
                mark_price=mark_price,
                entry_price=normalized_entry,
                stop_loss=normalized_stop,
                take_profit=normalized_take_profit,
            ),
        )

    @staticmethod
    def _rounded_net_r_multiple(
        *,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
        estimated_cost: Decimal,
    ) -> Decimal:
        risk_distance = abs(entry_price - stop_loss)
        reward_distance = abs(take_profit - entry_price)
        if risk_distance <= 0:
            return Decimal("0")
        net_reward = reward_distance - estimated_cost
        net_risk = risk_distance + estimated_cost
        if net_reward <= 0 or net_risk <= 0:
            return Decimal("0")
        return net_reward / net_risk

    def _required_liquidation_gap_pct(self, *, entry_price: Decimal, stop_loss: Decimal) -> Decimal:
        stop_distance_pct = calculate_stop_distance_pct(
            entry_price=entry_price,
            stop_loss_price=stop_loss,
        )
        return max(self.MIN_LIQUIDATION_GAP_PCT, stop_distance_pct * self.LIQUIDATION_BUFFER_MULTIPLE)

    def _liquidation_safety(
        self,
        *,
        entry_price: Decimal,
        stop_loss: Decimal,
        direction: SignalDirection,
        leverage: int,
        notional: Decimal,
        leverage_brackets: list[LeverageBracket] | None,
    ) -> dict[str, Decimal | bool]:
        liq = self.liquidation_price(
            entry_price,
            leverage,
            direction,
            notional=notional,
            leverage_brackets=leverage_brackets,
        )
        required_gap = self._required_liquidation_gap_pct(entry_price=entry_price, stop_loss=stop_loss)
        gap_pct = abs(stop_loss - liq) / entry_price if entry_price else Decimal("0")
        maintenance_margin_ratio = self._effective_maintenance_margin_ratio(
            notional=notional,
            leverage_brackets=leverage_brackets,
        )
        return {
            "ok": gap_pct >= required_gap,
            "liquidation_price": liq,
            "liquidation_gap_pct": gap_pct,
            "required_gap_pct": required_gap,
            "maintenance_margin_ratio": maintenance_margin_ratio,
        }

    def _highest_safe_leverage(
        self,
        *,
        entry_price: Decimal,
        stop_loss: Decimal,
        direction: SignalDirection,
        leverage_cap: int,
        notional: Decimal,
        leverage_brackets: list[LeverageBracket] | None,
    ) -> int:
        for leverage in range(max(leverage_cap, 1), 0, -1):
            safety = self._liquidation_safety(
                entry_price=entry_price,
                stop_loss=stop_loss,
                direction=direction,
                leverage=leverage,
                notional=notional,
                leverage_brackets=leverage_brackets,
            )
            if bool(safety["ok"]):
                return leverage
        return 0

    def _max_allowed_leverage(
        self,
        *,
        leverage_brackets: list[LeverageBracket] | None,
        notional: Decimal,
        settings_cap: int,
    ) -> int:
        if not leverage_brackets:
            return max(settings_cap, 1)
        bracket = self._bracket_for_notional(leverage_brackets, notional=notional)
        if bracket is None:
            return max(1, settings_cap)
        return max(1, min(settings_cap, bracket.initial_leverage))

    def _max_affordable_quantity(
        self,
        *,
        budget: Decimal,
        mark_price: Decimal,
        leverage: int,
        step_size: Decimal,
    ) -> Decimal:
        if budget <= 0 or mark_price <= 0 or leverage <= 0:
            return Decimal("0")
        quantity = budget / (mark_price * ((Decimal("1") / Decimal(leverage)) + self.ENTRY_FEE_RATE))
        return round_to_increment(quantity, step_size)

    @staticmethod
    def _entry_fee_rate_for_style(config, *, entry_style: str) -> Decimal:
        if entry_style == "LIMIT_GTD":
            return config.maker_fee_rate
        return config.taker_fee_rate

    def _estimated_stop_execution_cost_per_unit(
        self,
        *,
        settings_map: dict[str, str],
        entry_style: str,
        entry_price: Decimal,
        stop_loss: Decimal,
        estimated_cost: Decimal | None = None,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        config = resolve_strategy_config(settings_map)
        entry_fee = entry_price * self._entry_fee_rate_for_style(config, entry_style=entry_style)
        exit_fee = stop_loss * config.taker_fee_rate
        entry_slippage = entry_price * (config.slippage_rate_floor / (Decimal("2") if entry_style == "LIMIT_GTD" else Decimal("1")))
        exit_slippage = stop_loss * (config.slippage_rate_floor / Decimal("2"))
        conservative_cost = entry_fee + exit_fee + entry_slippage + exit_slippage
        if estimated_cost is not None and estimated_cost > 0:
            conservative_cost = max(conservative_cost, estimated_cost)
        return conservative_cost, entry_fee, exit_fee, entry_slippage, exit_slippage

    def _build_preview_reason(
        self,
        *,
        status: str,
        available_balance: Decimal,
        slot_budget: Decimal,
        requested_initial_margin: Decimal,
        requested_entry_fee: Decimal,
        max_affordable_quantity: Decimal,
        filters: SymbolFilters,
        entry_notional: Decimal,
        requested_quantity: Decimal,
    ) -> str | None:
        if status == "not_affordable":
            return (
                f"available balance is ${available_balance:.2f}, so this entry slot is capped at "
                f"${slot_budget:.2f}. This order needs about ${requested_initial_margin:.2f} margin "
                f"plus ${requested_entry_fee:.2f} entry fee."
            )
        if status == "too_small_for_exchange":
            if requested_quantity <= 0:
                return "slot budget is too small to reach Binance minimum order size."
            min_qty = self._entry_min_qty(filters)
            if max_affordable_quantity < min_qty:
                return (
                    f"only {self._decimal_string(max_affordable_quantity)} fits within the entry slot budget, "
                    f"below Binance minimum quantity {self._decimal_string(min_qty)}."
                )
            return (
                f"only ${entry_notional:.2f} entry notional fits within the entry slot budget, "
                f"below Binance minimum notional ${filters.min_notional:.2f}."
            )
        if status == "too_large_for_exchange":
            max_qty = self._entry_max_qty(filters)
            if max_qty is not None:
                return (
                    f"required quantity {self._decimal_string(requested_quantity)} is above Binance MARKET_LOT_SIZE "
                    f"maximum {self._decimal_string(max_qty)}."
                )
            return "required quantity is above Binance maximum allowed order size."
        return None

    def _preview_payload(
        self,
        *,
        status: str,
        can_place: bool,
        auto_resized: bool,
        requested_quantity: Decimal,
        final_quantity: Decimal,
        max_affordable_quantity: Decimal,
        mark_price_used: Decimal,
        entry_notional: Decimal,
        required_initial_margin: Decimal,
        estimated_entry_fee: Decimal,
        estimated_exit_fee: Decimal,
        estimated_slippage_burden: Decimal,
        stop_risk_execution_cost: Decimal,
        available_balance: Decimal,
        reserve_balance: Decimal,
        usable_balance: Decimal,
        deployable_equity: Decimal,
        remaining_deployable_equity: Decimal,
        slot_budget: Decimal,
        risk_budget_usdt: Decimal,
        risk_usdt_at_stop: Decimal,
        recommended_leverage: int,
        liquidation_price: Decimal,
        liquidation_gap_pct: Decimal,
        required_liquidation_gap_pct: Decimal,
        maintenance_margin_ratio: Decimal,
        reason: str | None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "can_place": can_place,
            "auto_resized": auto_resized,
            "requested_quantity": self._decimal_string(requested_quantity),
            "final_quantity": self._decimal_string(final_quantity),
            "max_affordable_quantity": self._decimal_string(max_affordable_quantity),
            "mark_price_used": self._decimal_string(mark_price_used),
            "entry_notional": self._decimal_string(entry_notional),
            "required_initial_margin": self._decimal_string(required_initial_margin),
            "estimated_entry_fee": self._decimal_string(estimated_entry_fee),
            "estimated_exit_fee": self._decimal_string(estimated_exit_fee),
            "estimated_slippage_burden": self._decimal_string(estimated_slippage_burden),
            "stop_risk_execution_cost": self._decimal_string(stop_risk_execution_cost),
            "available_balance": self._decimal_string(available_balance),
            "reserve_balance": self._decimal_string(reserve_balance),
            "usable_balance": self._decimal_string(usable_balance),
            "deployable_equity": self._decimal_string(deployable_equity),
            "remaining_deployable_equity": self._decimal_string(remaining_deployable_equity),
            "slot_budget": self._decimal_string(slot_budget),
            "risk_budget_usdt": self._decimal_string(risk_budget_usdt),
            "risk_usdt_at_stop": self._decimal_string(risk_usdt_at_stop),
            "recommended_leverage": recommended_leverage,
            "liquidation_price": self._decimal_string(liquidation_price),
            "liquidation_gap_pct": self._decimal_string(liquidation_gap_pct),
            "required_liquidation_gap_pct": self._decimal_string(required_liquidation_gap_pct),
            "maintenance_margin_ratio": self._decimal_string(maintenance_margin_ratio),
            "reason": reason,
        }

    @staticmethod
    def risk_pct_of_wallet(*, available_balance: Decimal, risk_usdt_at_stop: Decimal) -> Decimal:
        if available_balance <= 0:
            return Decimal("0")
        return risk_usdt_at_stop * Decimal("100") / available_balance

    def build_execution_plan(
        self,
        *,
        symbol: str,
        balance: Decimal | None = None,
        account_snapshot: AccountSnapshot | None = None,
        settings_map: dict[str, str],
        filters: SymbolFilters,
        entry_style: str = "LIMIT_GTD",
        direction: SignalDirection,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
        mark_price: Decimal | None = None,
        leverage_brackets: list[LeverageBracket] | None = None,
        risk_budget_override_usdt: Decimal | None = None,
        target_risk_usdt_override: Decimal | None = None,
        estimated_cost: Decimal | None = None,
        use_stop_distance_position_sizing: bool = False,
    ) -> dict[str, Any]:
        effective_mark_price = mark_price or entry_price
        market_state = self.validate_market_state(
            symbol=symbol,
            filters=filters,
            entry_style=entry_style,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            mark_price=effective_mark_price,
        )
        normalized_prices = market_state.execution_prices
        normalized_entry = normalized_prices["entry_price"]
        normalized_stop = normalized_prices["stop_loss"]
        normalized_take_profit = normalized_prices["take_profit"]
        if market_state.error:
            return {"error": market_state.error, "market_state": market_state}
        config = resolve_strategy_config(settings_map)
        if estimated_cost is not None:
            rounded_net_r_multiple = self._rounded_net_r_multiple(
                entry_price=normalized_entry,
                stop_loss=normalized_stop,
                take_profit=normalized_take_profit,
                estimated_cost=estimated_cost,
            )
            if rounded_net_r_multiple < config.min_net_r_multiple:
                return {
                    "error": "net_r_multiple_below_min_after_rounding",
                    "market_state": market_state,
                    "rounded_net_r_multiple": rounded_net_r_multiple,
                }

        preview = self.build_preview(
            balance=balance,
            account_snapshot=account_snapshot,
            settings_map=settings_map,
            filters=filters,
            entry_style=entry_style,
            direction=direction,
            entry_price=normalized_entry,
            stop_loss=normalized_stop,
            take_profit=normalized_take_profit,
            mark_price=effective_mark_price,
            leverage_brackets=leverage_brackets,
            risk_budget_override_usdt=risk_budget_override_usdt,
            target_risk_usdt_override=target_risk_usdt_override,
            estimated_cost=estimated_cost,
            use_stop_distance_position_sizing=use_stop_distance_position_sizing,
        )
        return {
            **normalized_prices,
            "order_preview": preview,
            "market_state": market_state,
            "rounded_net_r_multiple": (
                self._rounded_net_r_multiple(
                    entry_price=normalized_entry,
                    stop_loss=normalized_stop,
                    take_profit=normalized_take_profit,
                    estimated_cost=estimated_cost or Decimal("0"),
                )
            ),
        }

    def _preview_error_message(self, symbol: str, execution: dict[str, Any]) -> str:
        error = execution.get("error")
        if error == "invalid_execution_prices":
            return (
                f"{symbol} order could not be placed because Binance tick-size rounding made the "
                "executable prices invalid."
            )
        if error == "net_r_multiple_below_min_after_rounding":
            rounded_net_r = Decimal(str(execution.get("rounded_net_r_multiple") or "0"))
            return (
                f"{symbol} order could not be placed because post-rounding AQRR net R fell below 3.0. "
                f"Rounded net R is {rounded_net_r:.4f}."
            )
        if error == "percent_price_filter_failed":
            market_state = execution.get("market_state")
            if market_state is not None and market_state.message is not None:
                return market_state.message
            return f"{symbol} order could not be placed because Binance PERCENT_PRICE constraints were violated."
        preview = execution.get("order_preview") or {}
        reason = preview.get("reason")
        if isinstance(reason, str) and reason.strip():
            return f"{symbol} order could not be placed because {reason}"
        return f"{symbol} order could not be placed because live margin checks failed."

    @staticmethod
    def _signal_metadata(signal: Signal) -> dict[str, Any]:
        extra_context = dict(signal.extra_context or {})
        return {
            "entry_style": getattr(signal, "entry_style", None) or extra_context.get("entry_style") or "LIMIT_GTD",
            "setup_family": getattr(signal, "setup_family", None) or extra_context.get("setup_family"),
            "setup_variant": getattr(signal, "setup_variant", None) or extra_context.get("setup_variant"),
            "market_state": getattr(signal, "market_state", None) or extra_context.get("market_state"),
            "execution_tier": getattr(signal, "execution_tier", None) or extra_context.get("execution_tier"),
            "score_band": getattr(signal, "score_band", None) or extra_context.get("score_band"),
            "volatility_band": getattr(signal, "volatility_band", None) or extra_context.get("volatility_band"),
            "stats_bucket_key": getattr(signal, "stats_bucket_key", None) or extra_context.get("stats_bucket_key"),
            "strategy_context": dict(getattr(signal, "strategy_context", None) or extra_context.get("strategy_context") or {}),
            "rank_value": getattr(signal, "rank_value", None) or extra_context.get("rank_value"),
            "net_r_multiple": getattr(signal, "net_r_multiple", None) or extra_context.get("net_r_multiple"),
            "estimated_cost": getattr(signal, "estimated_cost", None) or extra_context.get("estimated_cost"),
        }

    @staticmethod
    def _aqrr_reason_context(
        extra_context: dict[str, Any] | None,
        *,
        setup_family: str | None = None,
        entry_style: str | None = None,
    ) -> dict[str, Any]:
        context = dict(extra_context or {})
        raw_aqrr_reasons = [
            str(reason)
            for reason in (context.get("aqrr_raw_rejection_reasons") or context.get("raw_aqrr_reasons") or [])
            if isinstance(reason, str) and reason.strip()
        ]
        raw_aqrr_reason = str(
            context.get("aqrr_raw_rejection_reason")
            or context.get("raw_aqrr_reason")
            or ""
        ).strip() or None
        if raw_aqrr_reason is None and raw_aqrr_reasons:
            raw_aqrr_reason = raw_aqrr_reasons[0]

        payload: dict[str, Any] = {}
        if raw_aqrr_reason is not None:
            payload["raw_aqrr_reason"] = raw_aqrr_reason
        if raw_aqrr_reasons:
            payload["raw_aqrr_reasons"] = raw_aqrr_reasons
        aqrr_rejection_stage = str(context.get("aqrr_rejection_stage") or "").strip() or None
        if aqrr_rejection_stage is not None:
            payload["aqrr_rejection_stage"] = aqrr_rejection_stage
        resolved_setup_family = setup_family or context.get("setup_family")
        if resolved_setup_family:
            payload["setup_family"] = resolved_setup_family
        resolved_entry_style = entry_style or context.get("entry_style")
        if resolved_entry_style:
            payload["entry_style"] = resolved_entry_style
        return payload

    async def _signal_reason_context(
        self,
        session,
        *,
        signal: Signal | None = None,
        signal_id: int | None = None,
        setup_family: str | None = None,
        entry_style: str | None = None,
    ) -> dict[str, Any]:
        target_signal = signal
        if target_signal is None and signal_id is not None:
            target_signal = await session.get(Signal, signal_id)
        if target_signal is None:
            return self._aqrr_reason_context(
                None,
                setup_family=setup_family,
                entry_style=entry_style,
            )
        return self._aqrr_reason_context(
            dict(getattr(target_signal, "extra_context", {}) or {}),
            setup_family=setup_family or getattr(target_signal, "setup_family", None),
            entry_style=entry_style or getattr(target_signal, "entry_style", None),
        )

    @staticmethod
    def _order_stats_bucket(order: Order) -> CandidateStatsBucket | None:
        if (
            not order.stats_bucket_key
            or not order.setup_family
            or order.direction is None
            or not order.market_state
            or not order.score_band
            or not order.volatility_band
            or not order.execution_tier
        ):
            return None
        return CandidateStatsBucket(
            bucket_key=str(order.stats_bucket_key),
            setup_family=str(order.setup_family),
            direction=order.direction,
            market_state=str(order.market_state),
            score_band=str(order.score_band),
            volatility_band=str(order.volatility_band),
            execution_tier=str(order.execution_tier),
        )

    def _entry_order_params(
        self,
        *,
        signal: Signal,
        quantity: Decimal,
        side: str,
        entry_price: Decimal,
        entry_client_id: str,
        entry_style: str,
        expires_at: datetime,
        exchange_gtd_enabled: bool,
    ) -> dict[str, str]:
        params: dict[str, str] = {
            "symbol": signal.symbol,
            "side": side,
            "type": "LIMIT",
            "quantity": str(quantity),
            "price": str(entry_price),
            "newClientOrderId": entry_client_id,
        }
        if exchange_gtd_enabled:
            good_till_ms = self._entry_good_till_date_ms(expires_at=expires_at)
            if good_till_ms is not None:
                params["timeInForce"] = "GTD"
                params["goodTillDate"] = str(good_till_ms)
            else:
                params["timeInForce"] = "GTC"
        else:
            params["timeInForce"] = "GTC"
        return params

    def _stop_entry_algo_params(
        self,
        *,
        signal: Signal,
        quantity: Decimal,
        side: str,
        entry_price: Decimal,
        entry_client_id: str,
    ) -> dict[str, str]:
        return {
            "algoType": "CONDITIONAL",
            "symbol": signal.symbol,
            "side": side,
            "type": "STOP",
            "timeInForce": "GTC",
            "quantity": str(quantity),
            "triggerPrice": str(entry_price),
            "price": str(entry_price),
            "workingType": "MARK_PRICE",
            "clientAlgoId": entry_client_id,
        }

    @staticmethod
    def _entry_route(entry_style: str) -> str:
        return "algo" if entry_style == "STOP_ENTRY" else "standard"

    @staticmethod
    def _entry_endpoint_family(entry_style: str) -> str:
        return "algo_order" if entry_style == "STOP_ENTRY" else "order"

    @staticmethod
    def _entry_order_type(entry_style: str) -> str:
        return "STOP" if entry_style == "STOP_ENTRY" else "LIMIT"

    def _entry_submission_details(
        self,
        *,
        entry_style: str,
        entry_price: Decimal,
        quantity: Decimal,
        expires_at: datetime,
        exchange_gtd_enabled: bool,
        exchange_good_till_ms: int | None,
    ) -> dict[str, str]:
        details = {
            "order_route": self._entry_route(entry_style),
            "endpoint_family": self._entry_endpoint_family(entry_style),
            "order_type": self._entry_order_type(entry_style),
            "entry_style": entry_style,
            "price": self._decimal_string(entry_price),
            "quantity": self._decimal_string(quantity),
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
            "expires_at_epoch_ms": str(self._utc_timestamp_millis(expires_at)),
            "expiry_control": "exchange_gtd" if exchange_gtd_enabled else "internal_timer",
        }
        if exchange_good_till_ms is not None:
            details["exchange_good_till_ms"] = str(exchange_good_till_ms)
        if entry_style == "STOP_ENTRY":
            details["trigger_price"] = self._decimal_string(entry_price)
        return details

    def _shared_entry_slot_message(self, slot_cap: int) -> str:
        return f"All {slot_cap} shared entry slots are already in use by pending entry orders or open positions."

    def _shared_entry_symbol_message(self, symbol: str) -> str:
        return f"{symbol} already has an active entry order. Only one shared entry slot is allowed per coin."

    def _open_position_symbol_message(self, symbol: str) -> str:
        return f"{symbol} already has an open Binance position. New orders on that symbol are blocked until it closes naturally."

    async def open_position_symbols(self, session) -> set[str]:
        rows = (
            await session.execute(
                select(ObservedPosition.symbol).where(ObservedPosition.closed_at.is_(None))
            )
        ).scalars().all()
        return {str(row).upper() for row in rows}

    async def remote_open_position_symbols(self, credentials) -> set[str]:
        rows = await self.gateway.positions(credentials)
        symbols: set[str] = set()
        for item in rows:
            amount = self._decimal_from_payload(None if item is None else item.get("positionAmt"))
            if amount is None or amount == 0:
                continue
            symbol = str((item or {}).get("symbol") or "").upper()
            if symbol:
                symbols.add(symbol)
        return symbols

    async def _resolve_authoritative_live_position(
        self,
        credentials: ApiCredentials,
        order: Order,
    ) -> AuthoritativeRecoveryResult:
        try:
            rows = await self.gateway.positions(credentials)
        except Exception as exc:
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_INCONCLUSIVE,
                details={"position_lookup_error": str(exc)},
            )

        target_symbol = order.symbol.upper()
        matched_symbol = False
        for item in rows or []:
            symbol = str((item or {}).get("symbol") or "").upper()
            if symbol != target_symbol:
                continue
            matched_symbol = True
            amount = self._decimal_from_payload(None if item is None else item.get("positionAmt"))
            if amount is None or amount == 0:
                continue
            if order.direction == SignalDirection.LONG and amount < 0:
                continue
            if order.direction == SignalDirection.SHORT and amount > 0:
                continue
            entry_price = self._decimal_from_payload(None if item is None else item.get("entryPrice"))
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_RECOVERED,
                quantity=abs(amount),
                entry_price=entry_price,
                details={
                    "position_lookup_outcome": self.RECOVERY_OUTCOME_RECOVERED,
                    "position_symbol_matched": True,
                },
            )

        return AuthoritativeRecoveryResult(
            outcome=self.RECOVERY_OUTCOME_CONFIRMED_NONE,
            details={
                "position_lookup_outcome": self.RECOVERY_OUTCOME_CONFIRMED_NONE,
                "position_symbol_matched": matched_symbol,
            },
        )

    @staticmethod
    def _realized_pnl_for_close(*, order: Order, close_price: Decimal, quantity: Decimal) -> Decimal:
        if order.direction == SignalDirection.LONG:
            return (close_price - Decimal(order.entry_price)) * quantity
        return (Decimal(order.entry_price) - close_price) * quantity

    async def _close_linked_observed_position(self, session, *, order_id: int, closed_at: datetime) -> None:
        observed = (
            await session.execute(
                select(ObservedPosition)
                .where(
                    ObservedPosition.linked_order_id == order_id,
                    ObservedPosition.closed_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if observed is None:
            return
        observed.closed_at = closed_at
        observed.last_seen_at = closed_at

    async def get_live_signal_readiness(
        self,
        session,
        *,
        signal: Signal,
        settings_map: dict[str, str],
        account_snapshot: AccountSnapshot,
        filters_map: dict[str, SymbolFilters],
        leverage_brackets_map: dict[str, list[LeverageBracket]] | None = None,
        mark_prices_map: dict[str, dict] | None = None,
        credentials_available: bool,
        account_error: str | None = None,
        risk_budget_override_usdt: Decimal | None = None,
        target_risk_usdt_override: Decimal | None = None,
        use_stop_distance_position_sizing: bool = False,
    ) -> dict[str, Any]:
        filters = filters_map.get(signal.symbol)
        if filters is None:
            return {
                "mark_price": None,
                "order_preview": None,
                "can_open_now": False,
                "failure_reason": f"{signal.symbol} live Binance filters are unavailable right now.",
            }

        mark_payload = None
        try:
            mark_payload = await self.gateway.mark_price(signal.symbol)
        except Exception:
            mark_payload = None if mark_prices_map is None else mark_prices_map.get(signal.symbol)

        mark_price = self._decimal_from_payload(None if mark_payload is None else mark_payload.get("markPrice"))
        if mark_price is None:
            return {
                "mark_price": None,
                "order_preview": None,
                "can_open_now": False,
                "failure_reason": f"{signal.symbol} live Binance mark price is unavailable right now.",
            }

        slot_budget = await self.get_shared_entry_slot_budget(
            session,
            account_snapshot=account_snapshot,
        )
        open_position_symbols = await self.open_position_symbols(session)
        effective_risk_budget_override = (
            risk_budget_override_usdt
            if risk_budget_override_usdt is not None
            else slot_budget.per_slot_budget
        )
        metadata = self._signal_metadata(signal)

        execution = self.build_execution_plan(
            symbol=signal.symbol,
            account_snapshot=account_snapshot,
            settings_map=settings_map,
            filters=filters,
            entry_style=str(metadata["entry_style"]),
            direction=signal.direction,
            entry_price=Decimal(signal.entry_price),
            stop_loss=Decimal(signal.stop_loss),
            take_profit=Decimal(signal.take_profit),
            mark_price=mark_price,
            leverage_brackets=(leverage_brackets_map or {}).get(signal.symbol, []),
            risk_budget_override_usdt=effective_risk_budget_override,
            target_risk_usdt_override=target_risk_usdt_override,
            estimated_cost=Decimal(str(metadata["estimated_cost"] or "0")),
            use_stop_distance_position_sizing=use_stop_distance_position_sizing,
        )
        preview = execution.get("order_preview")
        failure_reason: str | None = None

        if account_error is not None:
            failure_reason = account_error
        elif not credentials_available:
            failure_reason = "API credentials are required before placing live orders."
        elif signal.symbol.upper() in open_position_symbols:
            failure_reason = self._open_position_symbol_message(signal.symbol)
        elif signal.symbol.upper() in slot_budget.active_symbols:
            failure_reason = self._shared_entry_symbol_message(signal.symbol)
        elif slot_budget.remaining_entry_slots <= 0:
            failure_reason = self._shared_entry_slot_message(slot_budget.slot_cap)
        elif execution.get("error"):
            failure_reason = self._preview_error_message(signal.symbol, execution)
        else:
            market_state = execution.get("market_state")
            if market_state is not None and market_state.stale_reason is not None and market_state.message is not None:
                failure_reason = market_state.message
            elif preview is not None and not preview["can_place"]:
                failure_reason = self._preview_error_message(signal.symbol, execution)

        if failure_reason is not None and preview is not None:
            preview = {**preview, "can_place": False, "reason": failure_reason}

        return {
            "mark_price": mark_price,
            "order_preview": preview,
            "can_open_now": failure_reason is None,
            "failure_reason": failure_reason,
        }

    def _format_exchange_message(self, exc: BinanceAPIError) -> str:
        if exc.code is not None and exc.exchange_message:
            return f"Binance error {exc.code}: {exc.exchange_message}"
        if exc.exchange_message:
            return exc.exchange_message
        if exc.code is not None:
            return f"Binance error {exc.code}"
        return exc.raw_message

    @staticmethod
    def _exchange_filter_rejection_reason(exc: BinanceAPIError) -> str | None:
        message = (exc.exchange_message or exc.raw_message or "").lower()
        if exc.code == -4164 or "min notional" in message or "notional" in message:
            return "min_notional"
        if "lot size" in message or "market_lot_size" in message or "quantity" in message and "precision" in message:
            return "lot_size"
        if "price filter" in message or "tick size" in message or "price precision" in message:
            return "price_filter"
        if "percent price" in message or "multiplier" in message and "price" in message:
            return "percent_price"
        if exc.code in {-2027, -2028, -2019} or "leverage" in message or "margin is insufficient" in message:
            return "leverage_bracket"
        return None

    @staticmethod
    def _execution_signature(
        *,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
        quantity: Decimal,
        leverage: int,
    ) -> tuple[str, str, str, str, int]:
        return (
            format(entry_price.normalize(), "f"),
            format(stop_loss.normalize(), "f"),
            format(take_profit.normalize(), "f"),
            format(quantity.normalize(), "f"),
            leverage,
        )

    @staticmethod
    def _hedge_mode_switch_detail(symbol: str) -> str:
        return (
            f"{symbol} order could not be placed because the Binance Futures account is in Hedge Mode and the bot "
            "could not switch it to One-way Mode while open orders or positions already exist. Switch the account "
            "to One-way Mode manually in Binance, clear conflicting open orders if needed, and try again."
        )

    async def _ensure_one_way_position_mode(self, credentials, *, symbol: str) -> None:
        should_change_mode = True
        try:
            should_change_mode = await self.gateway.get_position_mode(credentials)
        except Exception as exc:
            logger.warning("approve_signal.position_mode_probe_failed", symbol=symbol, error=str(exc))

        if not should_change_mode:
            return

        try:
            await self.gateway.change_position_mode(credentials, dual_side=False)
        except BinanceAPIError as exc:
            if exc.code == -4067:
                raise OrderApprovalExchangeError(
                    detail=self._hedge_mode_switch_detail(symbol),
                    message=self._format_exchange_message(exc),
                ) from exc
            raise

    async def _safe_recalculated_execution(
        self,
        session,
        *,
        credentials: ApiCredentials,
        signal: Signal,
        settings_map: dict[str, str],
        metadata: dict[str, Any],
        risk_budget_override_usdt: Decimal | None,
        target_risk_usdt_override: Decimal | None,
        use_stop_distance_position_sizing: bool,
        current_signature: tuple[str, str, str, str, int],
        exc: BinanceAPIError,
    ) -> dict[str, Any]:
        rejection_reason = self._exchange_filter_rejection_reason(exc)
        if rejection_reason is None:
            return {"reason": None, "retryable": False}

        refreshed_snapshot = await self.get_account_snapshot(session, credentials)
        refreshed_slot_budget = self.build_shared_entry_slot_budget(
            available_balance=refreshed_snapshot.available_balance,
            account_equity=refreshed_snapshot.wallet_balance,
            committed_initial_margin=self._committed_initial_margin(refreshed_snapshot),
            active_entry_order_count=0,
        )
        refreshed_risk_budget_override = (
            min(risk_budget_override_usdt, refreshed_slot_budget.per_slot_budget)
            if risk_budget_override_usdt is not None
            else refreshed_slot_budget.per_slot_budget
        )
        config = resolve_strategy_config(settings_map)
        refreshed_target_risk_override = (
            min(
                target_risk_usdt_override,
                refreshed_snapshot.available_balance * config.risk_per_trade_fraction,
            )
            if target_risk_usdt_override is not None
            else None
        )
        refreshed_exchange_info = await self.gateway.exchange_info()
        refreshed_filters = self.gateway.parse_symbol_filters(refreshed_exchange_info).get(signal.symbol)
        if refreshed_filters is None:
            return {
                "reason": rejection_reason,
                "retryable": False,
                "failure_reason": "filters_unavailable",
            }
        refreshed_brackets = (await self.gateway.leverage_brackets(credentials, signal.symbol)).get(signal.symbol, [])
        refreshed_mark_payload = await self.gateway.mark_price(signal.symbol)
        refreshed_mark_price = Decimal(
            str(refreshed_mark_payload.get("markPrice") or signal.current_price_at_signal or signal.entry_price)
        )
        execution = self.build_execution_plan(
            symbol=signal.symbol,
            account_snapshot=refreshed_snapshot,
            settings_map=settings_map,
            filters=refreshed_filters,
            entry_style=str(metadata["entry_style"]),
            direction=signal.direction,
            entry_price=Decimal(signal.entry_price),
            stop_loss=Decimal(signal.stop_loss),
            take_profit=Decimal(signal.take_profit),
            mark_price=refreshed_mark_price,
            leverage_brackets=refreshed_brackets,
            risk_budget_override_usdt=refreshed_risk_budget_override,
            target_risk_usdt_override=refreshed_target_risk_override,
            estimated_cost=Decimal(str(metadata["estimated_cost"] or "0")),
            use_stop_distance_position_sizing=use_stop_distance_position_sizing,
        )
        if execution.get("error"):
            return {
                "reason": rejection_reason,
                "retryable": False,
                "failure_reason": str(execution.get("error")),
                "execution": execution,
            }

        preview = execution["order_preview"]
        if not preview["can_place"]:
            return {
                "reason": rejection_reason,
                "retryable": False,
                "failure_reason": "preview_rejected",
                "execution": execution,
            }

        recalculated_signature = self._execution_signature(
            entry_price=execution["entry_price"],
            stop_loss=execution["stop_loss"],
            take_profit=execution["take_profit"],
            quantity=Decimal(preview["final_quantity"]),
            leverage=int(preview["recommended_leverage"]),
        )
        if recalculated_signature == current_signature:
            return {
                "reason": rejection_reason,
                "retryable": False,
                "failure_reason": "recalculated_plan_unchanged",
                "execution": execution,
            }

        return {
            "reason": rejection_reason,
            "retryable": True,
            "execution": execution,
            "account_snapshot": refreshed_snapshot,
            "filters": refreshed_filters,
            "leverage_brackets": refreshed_brackets,
            "mark_price": refreshed_mark_price,
        }

    @staticmethod
    def _cleanup_warning_text() -> str:
        return "Cleanup of previously created Binance orders may have failed, so check Binance manually."

    @staticmethod
    def _remote_id(value: Any) -> str | None:
        if value is None:
            return None
        remote_id = str(value).strip()
        return remote_id or None

    def _entry_state_update_time(self, entry_state: EntryOrderState) -> datetime | None:
        reference_payload = entry_state.actual_order_state or entry_state.state
        return self._datetime_from_millis(None if reference_payload is None else reference_payload.get("updateTime"))

    @staticmethod
    def _is_binance_order_missing_error(exc: Exception) -> bool:
        return isinstance(exc, BinanceAPIError) and exc.code == -2013

    async def _query_standard_entry_order_state_resolution(
        self,
        credentials: ApiCredentials,
        order: Order,
    ) -> EntryStateResolution:
        attempts: list[dict[str, Any]] = []
        if order.entry_order_id:
            try:
                state = await self.gateway.query_order(credentials, order.symbol, order.entry_order_id)
                return EntryStateResolution(
                    outcome=self.LOOKUP_OUTCOME_FOUND,
                    entry_state=EntryOrderState(
                        remote_kind="standard",
                        state=state,
                        status=str((state or {}).get("status") or "").upper() or None,
                    ),
                    details={"lookup_attempts": attempts},
                )
            except Exception as exc:
                attempts.append(
                    {
                        "lookup": "order_id",
                        "remote_order_id": order.entry_order_id,
                        "error": str(exc),
                    }
                )
                if not self._is_binance_order_missing_error(exc):
                    return EntryStateResolution(
                        outcome=self.LOOKUP_OUTCOME_INCONCLUSIVE,
                        details={"lookup_attempts": attempts},
                    )

        try:
            state = await self.gateway.query_order(
                credentials,
                order.symbol,
                None,
                orig_client_order_id=self._managed_entry_client_id(order),
            )
        except Exception as exc:
            attempts.append(
                {
                    "lookup": "client_order_id",
                    "client_order_id": self._managed_entry_client_id(order),
                    "error": str(exc),
                }
            )
            return EntryStateResolution(
                outcome=(
                    self.LOOKUP_OUTCOME_CONFIRMED_MISSING
                    if self._is_binance_order_missing_error(exc)
                    else self.LOOKUP_OUTCOME_INCONCLUSIVE
                ),
                details={"lookup_attempts": attempts},
            )

        resolved_order_id = self._remote_id(None if state is None else state.get("orderId"))
        if resolved_order_id:
            order.entry_order_id = resolved_order_id
        return EntryStateResolution(
            outcome=self.LOOKUP_OUTCOME_FOUND,
            entry_state=EntryOrderState(
                remote_kind="standard",
                state=state,
                status=str((state or {}).get("status") or "").upper() or None,
            ),
            details={"lookup_attempts": attempts},
        )

    async def _query_stop_entry_order_state_resolution(
        self,
        credentials: ApiCredentials,
        order: Order,
    ) -> EntryStateResolution:
        attempts: list[dict[str, Any]] = []
        algo_state: dict[str, Any] | None = None
        if order.entry_order_id:
            try:
                algo_state = await self.gateway.query_algo_order(credentials, order.entry_order_id)
            except Exception as exc:
                attempts.append(
                    {
                        "lookup": "algo_id",
                        "algo_id": order.entry_order_id,
                        "error": str(exc),
                    }
                )
                if not self._is_binance_order_missing_error(exc):
                    return EntryStateResolution(
                        outcome=self.LOOKUP_OUTCOME_INCONCLUSIVE,
                        details={"lookup_attempts": attempts},
                    )

        if algo_state is None:
            client_algo_id = self._managed_entry_client_id(order)
            try:
                algo_state = await self.gateway.query_algo_order(
                    credentials,
                    None,
                    client_algo_id=client_algo_id,
                )
            except Exception as exc:
                attempts.append(
                    {
                        "lookup": "client_algo_id",
                        "client_algo_id": client_algo_id,
                        "error": str(exc),
                    }
                )
                return EntryStateResolution(
                    outcome=(
                        self.LOOKUP_OUTCOME_CONFIRMED_MISSING
                        if self._is_binance_order_missing_error(exc)
                        else self.LOOKUP_OUTCOME_INCONCLUSIVE
                    ),
                    details={"lookup_attempts": attempts},
                )

        resolved_order_id = self._remote_id(None if algo_state is None else algo_state.get("algoId"))
        if resolved_order_id:
            order.entry_order_id = resolved_order_id

        algo_status = str((algo_state or {}).get("algoStatus") or "").upper() or None
        actual_order_id = self._remote_id(None if algo_state is None else algo_state.get("actualOrderId"))
        actual_order_state: dict[str, Any] | None = None
        actual_lookup_outcome = self.LOOKUP_OUTCOME_CONFIRMED_MISSING if actual_order_id is None else None
        normalized_status = algo_status
        if algo_status == "TRIGGERED":
            normalized_status = "NEW"
            if actual_order_id is not None:
                try:
                    actual_order_state = await self.gateway.query_order(credentials, order.symbol, actual_order_id)
                    actual_lookup_outcome = self.LOOKUP_OUTCOME_FOUND
                except Exception as exc:
                    actual_lookup_outcome = (
                        self.LOOKUP_OUTCOME_CONFIRMED_MISSING
                        if self._is_binance_order_missing_error(exc)
                        else self.LOOKUP_OUTCOME_INCONCLUSIVE
                    )
                    attempts.append(
                        {
                            "lookup": "actual_order_id",
                            "actual_order_id": actual_order_id,
                            "error": str(exc),
                        }
                    )
            if actual_order_state is not None:
                normalized_status = str(actual_order_state.get("status") or "").upper() or "NEW"
        elif algo_status in {"NEW", "ACCEPTED", "WORKING"}:
            normalized_status = "NEW"

        return EntryStateResolution(
            outcome=self.LOOKUP_OUTCOME_FOUND,
            entry_state=EntryOrderState(
                remote_kind="algo",
                state=algo_state or {},
                status=normalized_status,
                algo_status=algo_status,
                actual_order_id=actual_order_id,
                actual_order_state=actual_order_state,
            ),
            details={
                "lookup_attempts": attempts,
                "actual_order_lookup_outcome": actual_lookup_outcome,
            },
        )

    async def _query_entry_order_state_resolution(
        self,
        credentials: ApiCredentials,
        order: Order,
    ) -> EntryStateResolution:
        entry_style = str(getattr(order, "entry_style", None) or "LIMIT_GTD")
        if entry_style == "STOP_ENTRY":
            return await self._query_stop_entry_order_state_resolution(credentials, order)
        return await self._query_standard_entry_order_state_resolution(credentials, order)

    async def _query_entry_order_state(self, credentials: ApiCredentials, order: Order) -> EntryOrderState | None:
        resolution = await self._query_entry_order_state_resolution(credentials, order)
        return resolution.entry_state

    async def _query_managed_protective_order(
        self,
        credentials: ApiCredentials,
        order: Order,
        *,
        role: str,
        symbol: str | None = None,
    ) -> tuple[str, dict] | tuple[None, None]:
        if role not in {"tp", "tp1", "tp2", "sl"}:
            raise ValueError(f"Unsupported protection role: {role}")
        target_symbol = symbol or order.symbol
        if role == "tp1":
            remote_order_id = order.tp_order_1_id
            client_algo_id = self._managed_tp1_client_id(order)
        elif role in {"tp", "tp2"}:
            remote_order_id = order.tp_order_2_id or order.tp_order_id
            client_algo_id = self._managed_tp2_client_id(order) if self._partial_tp_enabled(order) or role == "tp2" else self._managed_tp_client_id(order)
        else:
            remote_order_id = order.sl_order_id
            client_algo_id = self._managed_sl_client_id(order)
        if remote_order_id:
            try:
                return "algo", await self.gateway.query_algo_order(credentials, remote_order_id)
            except BinanceAPIError:
                try:
                    return "standard", await self.gateway.query_order(credentials, target_symbol, remote_order_id)
                except BinanceAPIError:
                    pass
        try:
            state = await self.gateway.query_algo_order(credentials, None, client_algo_id=client_algo_id)
        except BinanceAPIError:
            return (None, None)
        resolved_order_id = self._remote_id(None if state is None else state.get("algoId"))
        if role == "tp1":
            order.tp_order_1_id = resolved_order_id or order.tp_order_1_id
        elif role in {"tp", "tp2"}:
            order.tp_order_id = resolved_order_id or order.tp_order_id
            order.tp_order_2_id = resolved_order_id or order.tp_order_2_id or order.tp_order_id
        else:
            order.sl_order_id = resolved_order_id or order.sl_order_id
        return "algo", state

    async def _recover_remote_order_refs(self, credentials: ApiCredentials, order: Order) -> dict[str, object]:
        recovered: dict[str, object] = {}
        entry_state = await self._query_entry_order_state(credentials, order)
        if entry_state is not None:
            recovered["entry"] = entry_state
        for role in self._expected_protection_roles(order):
            kind, protection_state = await self._query_managed_protective_order(credentials, order, role=role)
            if kind is not None and protection_state is not None:
                recovered[role] = protection_state
        return recovered

    def _known_remote_refs(self, order: Order) -> list[RemoteOrderRef]:
        remote_refs: list[RemoteOrderRef] = []
        known_pairs: set[tuple[str, str]] = set()

        def add_remote_ref(order_id: str | None, *, role: str, kind: str) -> None:
            if not order_id:
                return
            key = (order_id, role)
            if key in known_pairs:
                return
            known_pairs.add(key)
            remote_refs.append(RemoteOrderRef(order_id=order_id, role=role, kind=kind))

        if order.entry_order_id:
            add_remote_ref(
                order.entry_order_id,
                role="entry",
                kind="algo" if str(getattr(order, "entry_style", None) or "") == "STOP_ENTRY" else "standard",
            )
        if self._partial_tp_enabled(order):
            add_remote_ref(order.tp_order_1_id, role="tp1", kind="algo")
            add_remote_ref(order.tp_order_2_id or order.tp_order_id, role="tp2", kind="algo")
        else:
            add_remote_ref(order.tp_order_id, role="tp", kind="algo")
        add_remote_ref(order.sl_order_id, role="sl", kind="algo")
        return remote_refs

    async def _mark_submission_failed(
        self,
        session,
        *,
        order: Order,
        scan_cycle_id: int | None,
        reason: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        order.status = OrderStatus.CANCELLED_BY_BOT
        order.cancel_reason = reason
        order.cancelled_at = datetime.now(timezone.utc)
        payload = dict(details or {})
        for key, value in (
            await self._signal_reason_context(
                session,
                signal_id=order.signal_id,
                setup_family=str(getattr(order, "setup_family", None) or "") or None,
                entry_style=str(getattr(order, "entry_style", None) or "") or None,
            )
        ).items():
            payload.setdefault(key, value)
        payload.setdefault("reason", reason)
        await record_audit(
            session,
            event_type="ORDER_SUBMISSION_FAILED",
            level=AuditLevel.ERROR,
            message=message,
            order_id=order.id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            scan_cycle_id=scan_cycle_id,
            details=payload,
        )

    async def _record_authoritative_recovery_inconclusive(
        self,
        session,
        *,
        order: Order,
        scan_cycle_id: int | None,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = dict(details or {})
        for key, value in (
            await self._signal_reason_context(
                session,
                signal_id=order.signal_id,
                setup_family=str(getattr(order, "setup_family", None) or "") or None,
                entry_style=str(getattr(order, "entry_style", None) or "") or None,
            )
        ).items():
            payload.setdefault(key, value)
        payload["authoritative_recovery_outcome"] = self.RECOVERY_OUTCOME_INCONCLUSIVE
        self._order_update_integrity_failures.append(datetime.now(timezone.utc))
        await record_audit(
            session,
            event_type="ORDER_AUTHORITATIVE_RECOVERY_INCONCLUSIVE",
            level=AuditLevel.ERROR,
            message=message,
            order_id=order.id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            scan_cycle_id=scan_cycle_id,
            details=payload,
        )
        logger.error(
            "order_manager.authoritative_recovery_inconclusive",
            order_id=order.id,
            symbol=order.symbol,
            details=payload,
        )

    async def _attempt_authoritative_entry_recovery(
        self,
        session,
        *,
        credentials: ApiCredentials,
        order: Order,
        scan_cycle_id: int | None,
        entry_resolution: EntryStateResolution | None = None,
        protections_confirmed: bool | None = None,
        inconclusive_message: str,
        extra_details: dict[str, Any] | None = None,
    ) -> AuthoritativeRecoveryResult:
        recovery = await self._resolve_authoritative_entry_recovery(
            credentials,
            order,
            entry_resolution=entry_resolution,
        )
        merged_details = dict(extra_details or {})
        merged_details.update(recovery.details)
        if recovery.outcome == self.RECOVERY_OUTCOME_RECOVERED and recovery.entry_state is not None:
            await self._activate_entry_fill(
                session,
                credentials=credentials,
                order=order,
                entry_state=recovery.entry_state,
                scan_cycle_id=scan_cycle_id,
                protections_confirmed=protections_confirmed,
                authoritative_recovery_outcome=recovery.outcome,
            )
        elif recovery.outcome == self.RECOVERY_OUTCOME_INCONCLUSIVE:
            await self._record_authoritative_recovery_inconclusive(
                session,
                order=order,
                scan_cycle_id=scan_cycle_id,
                message=inconclusive_message,
                details=merged_details,
            )
        if merged_details != recovery.details:
            return AuthoritativeRecoveryResult(
                outcome=recovery.outcome,
                quantity=recovery.quantity,
                entry_price=recovery.entry_price,
                entry_state=recovery.entry_state,
                details=merged_details,
            )
        return recovery

    async def _reconcile_submitting_order(self, session, credentials: ApiCredentials, order: Order) -> None:
        scan_cycle_id = await self._scan_cycle_id_for_signal(session, signal_id=order.signal_id)
        entry_resolution = await self._query_entry_order_state_resolution(credentials, order)
        entry_state = entry_resolution.entry_state
        recovered_states: dict[str, object] = {}
        if entry_state is not None:
            recovered_states["entry"] = entry_state
        for role in self._expected_protection_roles(order):
            kind, protection_state = await self._query_managed_protective_order(credentials, order, role=role)
            if kind is not None and protection_state is not None:
                recovered_states[role] = protection_state
        now = datetime.now(timezone.utc)
        if entry_state is None:
            if self._submitting_order_is_stale(order, now=now):
                recovery = await self._attempt_authoritative_entry_recovery(
                    session,
                    credentials=credentials,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    entry_resolution=entry_resolution,
                    protections_confirmed=all(role in recovered_states for role in self._expected_protection_roles(order)),
                    inconclusive_message=f"{order.symbol} entry recovery remained inconclusive before stale submission cleanup",
                    extra_details={"recovered_roles": sorted(recovered_states)},
                )
                if recovery.outcome == self.RECOVERY_OUTCOME_CONFIRMED_NONE:
                    await self._mark_submission_failed(
                        session,
                        order=order,
                        scan_cycle_id=scan_cycle_id,
                        reason="submission_failed",
                        message=f"{order.symbol} order submission could not be recovered",
                        details={
                            "reason": "submission_failed",
                            "recovered_roles": sorted(recovered_states),
                            **recovery.details,
                        },
                    )
            return

        entry_status = str(entry_state.status or "").upper()
        protections_present = all(role in recovered_states for role in self._expected_protection_roles(order))

        if entry_status in {"PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED"}:
            activated = await self._activate_entry_fill(
                session,
                credentials=credentials,
                order=order,
                entry_state=entry_state,
                scan_cycle_id=scan_cycle_id,
                protections_confirmed=protections_present,
            )
            if activated:
                return
            if order.status != OrderStatus.SUBMITTING:
                return
            if entry_status in {"CANCELED", "EXPIRED"}:
                recovery = await self._attempt_authoritative_entry_recovery(
                    session,
                    credentials=credentials,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    entry_resolution=entry_resolution,
                    protections_confirmed=protections_present,
                    inconclusive_message=f"{order.symbol} entry recovery remained inconclusive before stale submission failure handling",
                    extra_details={
                        "entry_status": entry_status,
                        "entry_route": entry_state.remote_kind,
                        "algo_status": entry_state.algo_status,
                        "actual_order_id": entry_state.actual_order_id,
                    },
                )
                if recovery.outcome != self.RECOVERY_OUTCOME_CONFIRMED_NONE:
                    return
                await self._mark_submission_failed(
                    session,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    reason="submission_failed",
                    message=f"{order.symbol} order submission did not complete",
                    details={
                        "reason": "submission_failed",
                        **recovery.details,
                    },
                )
                return

        if entry_status == "NEW" and protections_present:
            order.status = OrderStatus.ORDER_PLACED
            order.placed_at = order.placed_at or self._entry_state_update_time(entry_state) or now
            return

        if entry_status == "REJECTED":
            recovery = await self._attempt_authoritative_entry_recovery(
                session,
                credentials=credentials,
                order=order,
                scan_cycle_id=scan_cycle_id,
                entry_resolution=entry_resolution,
                protections_confirmed=protections_present,
                inconclusive_message=f"{order.symbol} entry recovery remained inconclusive before submission rejection handling",
                extra_details={
                    "entry_status": entry_status,
                    "entry_route": entry_state.remote_kind,
                    "algo_status": entry_state.algo_status,
                    "actual_order_id": entry_state.actual_order_id,
                },
            )
            if recovery.outcome != self.RECOVERY_OUTCOME_CONFIRMED_NONE:
                return
            await self._mark_submission_failed(
                session,
                order=order,
                scan_cycle_id=scan_cycle_id,
                reason="submission_failed",
                message=f"{order.symbol} order submission did not complete",
                details={
                    "reason": "submission_failed",
                    **recovery.details,
                },
            )
            return

        if not self._submitting_order_is_stale(order, now=now):
            return

        recovery = await self._attempt_authoritative_entry_recovery(
            session,
            credentials=credentials,
            order=order,
            scan_cycle_id=scan_cycle_id,
            entry_resolution=entry_resolution,
            protections_confirmed=protections_present,
            inconclusive_message=f"{order.symbol} entry recovery remained inconclusive before stale submission cleanup",
            extra_details={
                "entry_status": entry_status or None,
                "entry_route": entry_state.remote_kind,
                "algo_status": entry_state.algo_status,
                "actual_order_id": entry_state.actual_order_id,
                "recovered_roles": sorted(recovered_states),
            },
        )
        if recovery.outcome != self.RECOVERY_OUTCOME_CONFIRMED_NONE:
            return

        cleanup_failures = await self._cleanup_remote_orders(credentials, order.symbol, self._known_remote_refs(order))
        await self._mark_submission_failed(
            session,
            order=order,
            scan_cycle_id=scan_cycle_id,
            reason="submission_failed",
            message=f"{order.symbol} order submission was only partially created and has been cancelled",
            details={
                "reason": "submission_failed",
                "cleanup_failures": cleanup_failures,
                **recovery.details,
            },
        )

    async def reconcile_managed_orders(self, session, *, approved_by: str | None = None) -> None:
        credentials = await self.get_credentials(session)
        if credentials is None:
            return
        query = select(Order).where(Order.status.in_(self.ACTIVE_ORDER_STATUSES))
        if approved_by is not None:
            query = query.where(Order.approved_by == approved_by)
        orders = (await session.execute(query.order_by(Order.id))).scalars().all()
        for order in orders:
            if order.status != OrderStatus.SUBMITTING:
                await self._recover_remote_order_refs(credentials, order)
                continue
            await self._reconcile_submitting_order(session, credentials, order)

    def _build_exchange_error(
        self,
        symbol: str,
        exc: BinanceAPIError,
        *,
        preview: dict[str, Any] | None = None,
        cleanup_failures: list[dict[str, str]] | None = None,
    ) -> OrderApprovalExchangeError:
        if exc.code == -2019 and preview is not None:
            detail = (
                f"{symbol} order could not be placed because live Binance margin only supports up to "
                f"{preview['max_affordable_quantity']} contracts at {preview['recommended_leverage']}x. "
                f"Available balance was ${preview['available_balance']}."
            )
        elif exc.code == -1111:
            detail = (
                f"{symbol} order could not be placed because the executable price precision "
                "does not match Binance tick-size rules."
            )
        elif exc.code == -4120:
            detail = (
                f"{symbol} order could not be placed because Binance now requires stop-style conditional orders "
                "to use the Algo Order API endpoints."
            )
        elif exc.code == -4067:
            detail = self._hedge_mode_switch_detail(symbol)
        else:
            detail = f"{symbol} order could not be placed because Binance rejected the request."
        if cleanup_failures:
            detail = f"{detail} {self._cleanup_warning_text()}"
        return OrderApprovalExchangeError(detail=detail, message=self._format_exchange_message(exc))

    async def _cancel_known_remote_order(
        self,
        credentials: ApiCredentials,
        symbol: str,
        remote_order: RemoteOrderRef,
    ) -> dict:
        if remote_order.kind == "algo":
            return await self.gateway.cancel_algo_order(credentials, remote_order.order_id)
        return await self.gateway.cancel_order(credentials, symbol, remote_order.order_id)

    async def _cleanup_remote_orders(
        self,
        credentials: ApiCredentials,
        symbol: str,
        remote_orders: list[RemoteOrderRef],
    ) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        for remote_order in remote_orders:
            try:
                await self._cancel_known_remote_order(credentials, symbol, remote_order)
            except Exception as exc:
                logger.warning(
                    "approve_signal.remote_cleanup_failed",
                    symbol=symbol,
                    order_id=remote_order.order_id,
                    role=remote_order.role,
                    kind=remote_order.kind,
                    error=str(exc),
                )
                failures.append(
                    {
                        "order_id": remote_order.order_id,
                        "role": remote_order.role,
                        "kind": remote_order.kind,
                        "error": str(exc),
                    }
                )
        return failures

    @staticmethod
    def _reason_details(
        *,
        reason: str,
        reason_context: dict[str, Any] | None = None,
        default_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        details = {"reason": reason}
        for context in (default_context, reason_context):
            if not context:
                continue
            for key, value in context.items():
                if value is None:
                    continue
                details[key] = value
        return details

    def _exchange_pending_cancel_reason(
        self,
        *,
        order: Order,
        exchange_status: str,
        entry_state: EntryOrderState,
    ) -> tuple[str, str]:
        normalized_status = str(exchange_status or "").upper()
        if normalized_status == "EXPIRED":
            return "expired", "exchange_status_expired"
        if normalized_status != "CANCELED":
            return self._normalize_pending_cancel_reason(normalized_status.lower()), "exchange_status_normalized"

        actual_status = str((entry_state.actual_order_state or {}).get("status") or "").upper() or None
        algo_status = str(entry_state.algo_status or "").upper() or None
        if actual_status == "EXPIRED":
            return "expired", "actual_order_status_expired"
        if algo_status == "EXPIRED":
            return "expired", "algo_status_expired"
        if self.pending_entry_expired(order):
            return "expired", "authoritative_local_expiry_elapsed"
        if actual_status in {"CANCELED", "CANCELLED"} or algo_status in {"CANCELED", "CANCELLED"}:
            return "setup_state_changed", "exchange_cancelled"
        return "setup_state_changed", "exchange_cancel_cause_unknown"

    async def _apply_uncertain_expired_terminal_resolution(
        self,
        session,
        *,
        credentials: ApiCredentials,
        order: Order,
        scan_cycle_id: int | None,
        reason_context: dict[str, Any] | None = None,
        default_reason_context: dict[str, Any] | None = None,
        message: str,
    ) -> None:
        entry_cancel_attempted = False
        entry_cancel_error: str | None = None
        try:
            entry_cancel_attempted = True
            await self._cancel_entry_order(credentials, order)
        except Exception as exc:
            entry_cancel_error = str(exc)
            logger.warning(
                "order_manager.expired_uncertain_entry_cancel_failed",
                order_id=order.id,
                symbol=order.symbol,
                error=entry_cancel_error,
            )

        order.status = OrderStatus.CANCELLED_BY_BOT
        order.cancel_reason = "expired"
        order.cancelled_at = datetime.now(timezone.utc)

        resolved_reason_context = dict(reason_context or {})
        resolved_reason_context.setdefault("authoritative_recovery_outcome", self.RECOVERY_OUTCOME_INCONCLUSIVE)
        resolved_reason_context.setdefault("expiry_resolution_path", "terminal_on_inconclusive_recovery")
        resolved_reason_context.setdefault("entry_cancel_attempted", entry_cancel_attempted)
        if entry_cancel_error is not None:
            resolved_reason_context.setdefault("entry_cancel_error", entry_cancel_error)

        await record_audit(
            session,
            event_type="ORDER_CANCELLED",
            level=AuditLevel.ERROR,
            message=message,
            order_id=order.id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            scan_cycle_id=scan_cycle_id,
            details=self._reason_details(
                reason="expired",
                reason_context=resolved_reason_context,
                default_context=default_reason_context,
            ),
        )

    async def _cancel_entry_order(self, credentials: ApiCredentials, order: Order) -> None:
        entry_style = str(getattr(order, "entry_style", None) or "LIMIT_GTD")
        if entry_style != "STOP_ENTRY":
            if order.entry_order_id:
                await self.gateway.cancel_order(credentials, order.symbol, order.entry_order_id)
            return

        entry_state = await self._query_entry_order_state(credentials, order)
        if entry_state is not None and entry_state.algo_status == "TRIGGERED" and entry_state.actual_order_id is not None:
            actual_status = str((entry_state.actual_order_state or {}).get("status") or "").upper()
            if actual_status in {"NEW", "PARTIALLY_FILLED"}:
                await self.gateway.cancel_order(credentials, order.symbol, entry_state.actual_order_id)
                return
        if order.entry_order_id:
            await self.gateway.cancel_algo_order(credentials, order.entry_order_id)

    async def _cancel_protective_order(self, credentials: ApiCredentials, symbol: str, remote_order_id: str) -> dict:
        try:
            return await self.gateway.cancel_algo_order(credentials, remote_order_id)
        except BinanceAPIError:
            return await self.gateway.cancel_order(credentials, symbol, remote_order_id)

    async def _query_protective_order(self, credentials: ApiCredentials, symbol: str, remote_order_id: str) -> tuple[str, dict]:
        try:
            return "algo", await self.gateway.query_algo_order(credentials, remote_order_id)
        except BinanceAPIError:
            return "standard", await self.gateway.query_order(credentials, symbol, remote_order_id)

    async def _record_protection_warning(
        self,
        session,
        *,
        order: Order,
        scan_cycle_id: int | None,
        protection: str,
        remote_order_id: str,
        algo_status: str,
    ) -> None:
        existing = (
            await session.execute(
                select(AuditLog.id)
                .where(AuditLog.order_id == order.id, AuditLog.event_type == "ORDER_PROTECTION_INACTIVE")
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        await record_audit(
            session,
            event_type="ORDER_PROTECTION_INACTIVE",
            level=AuditLevel.WARNING,
            message=f"{order.symbol} {protection} protection is inactive and needs manual review",
            order_id=order.id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            scan_cycle_id=scan_cycle_id,
            details={
                "protection": protection,
                "remote_order_id": remote_order_id,
                "algo_status": algo_status,
            },
        )

    def _entry_quantity_payload(self, entry_state: EntryOrderState) -> dict[str, Any]:
        return entry_state.actual_order_state or entry_state.state or {}

    def _entry_filled_quantity(self, entry_state: EntryOrderState) -> Decimal:
        payload = self._entry_quantity_payload(entry_state)
        filled_quantity = (
            self._decimal_from_payload(payload.get("executedQty"))
            or self._decimal_from_payload(payload.get("cumQty"))
            or self._decimal_from_payload(payload.get("cumBase"))
        )
        return max(filled_quantity or Decimal("0"), Decimal("0"))

    def _recovered_entry_state_from_authoritative_quantity(
        self,
        order: Order,
        *,
        live_quantity: Decimal,
        entry_price: Decimal | None,
        source_kind: str,
        source_entry_state: EntryOrderState | None = None,
    ) -> EntryOrderState:
        planned_quantity = self._decimal_from_payload(getattr(order, "quantity", None)) or Decimal("0")
        recovered_status = "PARTIALLY_FILLED"
        if planned_quantity > 0 and live_quantity >= planned_quantity:
            recovered_status = "FILLED"

        recovered_entry_price = entry_price if entry_price is not None and entry_price > 0 else Decimal(order.entry_price)
        recovered_payload = {
            "status": recovered_status,
            "executedQty": self._decimal_string(live_quantity),
            "avgPrice": self._decimal_string(recovered_entry_price),
            "orderId": (
                (source_entry_state.actual_order_id if source_entry_state is not None else None)
                or order.entry_order_id
            ),
            "updateTime": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        }
        base_state = dict((source_entry_state.state or {}) if source_entry_state is not None else {})
        base_state.update(recovered_payload)
        base_actual_state = dict(
            (source_entry_state.actual_order_state or source_entry_state.state or {})
            if source_entry_state is not None
            else {}
        )
        base_actual_state.update(recovered_payload)
        return EntryOrderState(
            remote_kind=source_kind if source_entry_state is None else source_entry_state.remote_kind,
            state=base_state,
            status=recovered_status,
            algo_status=None if source_entry_state is None else source_entry_state.algo_status,
            actual_order_id=(
                (source_entry_state.actual_order_id if source_entry_state is not None else None)
                or self._remote_id(base_actual_state.get("orderId"))
                or order.entry_order_id
            ),
            actual_order_state=base_actual_state,
        )

    def _entry_state_confirms_no_exposure(self, entry_state: EntryOrderState) -> bool:
        entry_status = str(entry_state.status or "").upper()
        if self._entry_filled_quantity(entry_state) > 0:
            return False
        if entry_status in {"REJECTED", "CANCELED", "EXPIRED"}:
            return True
        if entry_status != "NEW":
            return False
        if entry_state.remote_kind != "algo":
            return True
        algo_status = str(entry_state.algo_status or "").upper()
        if algo_status in {"NEW", "ACCEPTED", "WORKING"}:
            return True
        if algo_status != "TRIGGERED":
            return False
        actual_status = str((entry_state.actual_order_state or {}).get("status") or "").upper()
        return actual_status in {"NEW", "REJECTED", "CANCELED", "EXPIRED"}

    async def _resolve_authoritative_trade_fill(
        self,
        credentials: ApiCredentials,
        order: Order,
        *,
        actual_order_id: str | None,
    ) -> AuthoritativeRecoveryResult:
        if actual_order_id is None:
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_CONFIRMED_NONE,
                details={"trade_lookup_outcome": self.RECOVERY_OUTCOME_CONFIRMED_NONE},
            )

        try:
            trades = await self.gateway.account_trades(credentials, order.symbol, order_id=actual_order_id)
        except Exception as exc:
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_INCONCLUSIVE,
                details={
                    "trade_lookup_outcome": self.RECOVERY_OUTCOME_INCONCLUSIVE,
                    "trade_lookup_error": str(exc),
                    "trade_order_id": actual_order_id,
                },
            )

        total_qty = sum(
            (self._decimal_from_payload(trade.get("qty")) or Decimal("0") for trade in trades),
            start=Decimal("0"),
        )
        if total_qty <= 0:
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_CONFIRMED_NONE,
                details={
                    "trade_lookup_outcome": self.RECOVERY_OUTCOME_CONFIRMED_NONE,
                    "trade_order_id": actual_order_id,
                },
            )

        weighted_price = sum(
            (
                (self._decimal_from_payload(trade.get("price")) or Decimal("0"))
                * (self._decimal_from_payload(trade.get("qty")) or Decimal("0"))
                for trade in trades
            ),
            start=Decimal("0"),
        )
        entry_price = weighted_price / total_qty if weighted_price > 0 else None
        return AuthoritativeRecoveryResult(
            outcome=self.RECOVERY_OUTCOME_RECOVERED,
            quantity=total_qty,
            entry_price=entry_price,
            details={
                "trade_lookup_outcome": self.RECOVERY_OUTCOME_RECOVERED,
                "trade_order_id": actual_order_id,
            },
        )

    async def _resolve_authoritative_entry_recovery(
        self,
        credentials: ApiCredentials,
        order: Order,
        *,
        entry_resolution: EntryStateResolution | None = None,
    ) -> AuthoritativeRecoveryResult:
        resolved_entry = entry_resolution or await self._query_entry_order_state_resolution(credentials, order)
        entry_state = resolved_entry.entry_state
        details: dict[str, Any] = {
            "entry_lookup_outcome": resolved_entry.outcome,
            **resolved_entry.details,
        }
        if entry_state is not None:
            details["entry_status"] = str(entry_state.status or "").upper() or None
            details["entry_route"] = entry_state.remote_kind
            details["algo_status"] = entry_state.algo_status
            details["actual_order_id"] = entry_state.actual_order_id
            if self._entry_filled_quantity(entry_state) > 0:
                payload = self._entry_quantity_payload(entry_state)
                average_entry_price = (
                    self._decimal_from_payload(payload.get("avgPrice"))
                    or self._decimal_from_payload(payload.get("price"))
                    or Decimal(order.entry_price)
                )
                return AuthoritativeRecoveryResult(
                    outcome=self.RECOVERY_OUTCOME_RECOVERED,
                    quantity=self._entry_filled_quantity(entry_state),
                    entry_price=average_entry_price,
                    entry_state=entry_state,
                    details={
                        **details,
                        "authoritative_recovery_outcome": self.RECOVERY_OUTCOME_RECOVERED,
                        "recovery_source": "entry_state",
                    },
                )

        actual_order_id = None
        if entry_state is not None:
            payload = self._entry_quantity_payload(entry_state)
            actual_order_id = (
                entry_state.actual_order_id
                or self._remote_id(payload.get("orderId"))
                or order.entry_order_id
            )

        trade_resolution = await self._resolve_authoritative_trade_fill(
            credentials,
            order,
            actual_order_id=actual_order_id,
        )
        details.update(trade_resolution.details)
        if trade_resolution.outcome == self.RECOVERY_OUTCOME_RECOVERED:
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_RECOVERED,
                quantity=trade_resolution.quantity,
                entry_price=trade_resolution.entry_price,
                entry_state=self._recovered_entry_state_from_authoritative_quantity(
                    order,
                    live_quantity=trade_resolution.quantity,
                    entry_price=trade_resolution.entry_price,
                    source_kind=entry_state.remote_kind if entry_state is not None else "authoritative_trade",
                    source_entry_state=entry_state,
                ),
                details={
                    **details,
                    "authoritative_recovery_outcome": self.RECOVERY_OUTCOME_RECOVERED,
                    "recovery_source": "trade_fill",
                },
            )

        position_resolution = await self._resolve_authoritative_live_position(credentials, order)
        details.update(position_resolution.details)
        if position_resolution.outcome == self.RECOVERY_OUTCOME_RECOVERED:
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_RECOVERED,
                quantity=position_resolution.quantity,
                entry_price=position_resolution.entry_price,
                entry_state=self._recovered_entry_state_from_authoritative_quantity(
                    order,
                    live_quantity=position_resolution.quantity,
                    entry_price=position_resolution.entry_price,
                    source_kind="authoritative_position",
                    source_entry_state=entry_state,
                ),
                details={
                    **details,
                    "authoritative_recovery_outcome": self.RECOVERY_OUTCOME_RECOVERED,
                    "recovery_source": "position",
                },
            )

        entry_status = str(entry_state.status or "").upper() if entry_state is not None else None
        strong_entry_no_exposure = (
            entry_state is not None
            and self._entry_state_confirms_no_exposure(entry_state)
            and entry_status in {"REJECTED", "CANCELED", "EXPIRED"}
        )
        actual_order_confirmed_missing = (
            entry_state is not None
            and entry_state.remote_kind == "algo"
            and resolved_entry.details.get("actual_order_lookup_outcome") == self.LOOKUP_OUTCOME_CONFIRMED_MISSING
        )
        position_confirms_no_exposure = position_resolution.outcome == self.RECOVERY_OUTCOME_CONFIRMED_NONE
        if strong_entry_no_exposure or (
            position_confirms_no_exposure
            and (
                resolved_entry.outcome == self.LOOKUP_OUTCOME_CONFIRMED_MISSING
                or (entry_state is not None and self._entry_state_confirms_no_exposure(entry_state))
                or actual_order_confirmed_missing
            )
        ):
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_CONFIRMED_NONE,
                details={
                    **details,
                    "authoritative_recovery_outcome": self.RECOVERY_OUTCOME_CONFIRMED_NONE,
                },
            )

        if (
            resolved_entry.outcome == self.LOOKUP_OUTCOME_INCONCLUSIVE
            or trade_resolution.outcome == self.RECOVERY_OUTCOME_INCONCLUSIVE
            or position_resolution.outcome == self.RECOVERY_OUTCOME_INCONCLUSIVE
        ):
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_INCONCLUSIVE,
                details={
                    **details,
                    "authoritative_recovery_outcome": self.RECOVERY_OUTCOME_INCONCLUSIVE,
                },
            )

        return AuthoritativeRecoveryResult(
            outcome=self.RECOVERY_OUTCOME_CONFIRMED_NONE,
            details={
                **details,
                "authoritative_recovery_outcome": self.RECOVERY_OUTCOME_CONFIRMED_NONE,
            },
        )

    async def _resolve_authoritative_close_quantity(
        self,
        credentials: ApiCredentials,
        order: Order,
    ) -> AuthoritativeRecoveryResult:
        position_resolution = await self._resolve_authoritative_live_position(credentials, order)
        if position_resolution.outcome == self.RECOVERY_OUTCOME_RECOVERED:
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_RECOVERED,
                quantity=position_resolution.quantity,
                entry_price=position_resolution.entry_price,
                details={
                    **position_resolution.details,
                    "close_quantity_source": "position",
                },
            )

        entry_recovery = await self._resolve_authoritative_entry_recovery(credentials, order)
        if entry_recovery.outcome == self.RECOVERY_OUTCOME_RECOVERED:
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_RECOVERED,
                quantity=entry_recovery.quantity,
                entry_price=entry_recovery.entry_price,
                entry_state=entry_recovery.entry_state,
                details={
                    **position_resolution.details,
                    **entry_recovery.details,
                    "close_quantity_source": entry_recovery.details.get("recovery_source") or "entry_recovery",
                },
            )

        if (
            position_resolution.outcome == self.RECOVERY_OUTCOME_CONFIRMED_NONE
            and entry_recovery.outcome == self.RECOVERY_OUTCOME_CONFIRMED_NONE
        ):
            return AuthoritativeRecoveryResult(
                outcome=self.RECOVERY_OUTCOME_CONFIRMED_NONE,
                details={
                    **position_resolution.details,
                    **entry_recovery.details,
                    "close_quantity_source": "confirmed_no_exposure",
                },
            )

        return AuthoritativeRecoveryResult(
            outcome=self.RECOVERY_OUTCOME_INCONCLUSIVE,
            details={
                **position_resolution.details,
                **entry_recovery.details,
                "close_quantity_source": "inconclusive",
            },
        )

    async def _recover_entry_state_from_authoritative_exposure(
        self,
        credentials: ApiCredentials,
        order: Order,
    ) -> EntryOrderState | None:
        recovery = await self._resolve_authoritative_entry_recovery(credentials, order)
        if recovery.outcome != self.RECOVERY_OUTCOME_RECOVERED:
            return None
        return recovery.entry_state

    async def _entry_fill_details(
        self,
        credentials: ApiCredentials,
        order: Order,
        *,
        entry_state: EntryOrderState,
    ) -> tuple[Decimal, Decimal]:
        payload = self._entry_quantity_payload(entry_state)
        filled_quantity = self._entry_filled_quantity(entry_state)
        average_entry_price = (
            self._decimal_from_payload(payload.get("avgPrice"))
            or self._decimal_from_payload(payload.get("price"))
            or Decimal(order.entry_price)
        )
        actual_order_id = (
            entry_state.actual_order_id
            or self._remote_id(payload.get("orderId"))
            or order.entry_order_id
        )
        if actual_order_id:
            try:
                trades = await self.gateway.account_trades(credentials, order.symbol, order_id=actual_order_id)
            except Exception:
                trades = []
            if trades:
                total_qty = sum(
                    (self._decimal_from_payload(trade.get("qty")) or Decimal("0") for trade in trades),
                    start=Decimal("0"),
                )
                weighted_price = sum(
                    (
                        (self._decimal_from_payload(trade.get("price")) or Decimal("0"))
                        * (self._decimal_from_payload(trade.get("qty")) or Decimal("0"))
                        for trade in trades
                    ),
                    start=Decimal("0"),
                )
                if total_qty > 0:
                    filled_quantity = total_qty
                    average_entry_price = weighted_price / total_qty
        position_resolution = await self._resolve_authoritative_live_position(credentials, order)
        if position_resolution.outcome == self.RECOVERY_OUTCOME_RECOVERED:
            remote_quantity = position_resolution.quantity
            remote_entry_price = position_resolution.entry_price
            if remote_quantity > 0 and (filled_quantity <= 0 or remote_quantity != filled_quantity):
                filled_quantity = remote_quantity
                if remote_entry_price is not None and remote_entry_price > 0:
                    average_entry_price = remote_entry_price
        return max(filled_quantity, Decimal("0")), average_entry_price

    def _apply_entry_fill_state(
        self,
        order: Order,
        *,
        filled_quantity: Decimal,
        average_entry_price: Decimal,
    ) -> None:
        normalized_quantity = max(filled_quantity, Decimal("0"))
        if normalized_quantity <= 0:
            return
        normalized_entry_price = average_entry_price if average_entry_price > 0 else Decimal(order.entry_price)
        order.entry_price = normalized_entry_price
        order.remaining_quantity = normalized_quantity
        order.notional_value = normalized_quantity * normalized_entry_price
        if order.leverage > 0:
            order.position_margin = order.notional_value / Decimal(order.leverage)
        order.risk_usdt_at_stop = normalized_quantity * abs(normalized_entry_price - Decimal(order.stop_loss))
        self._update_strategy_context(
            order,
            entry_filled_quantity=self._decimal_string(normalized_quantity),
        )

    async def _minimum_viable_live_fill(
        self,
        order: Order,
        *,
        live_quantity: Decimal,
    ) -> tuple[bool, dict[str, str]]:
        exchange_info = await self.gateway.exchange_info()
        filters = self.gateway.parse_symbol_filters(exchange_info).get(order.symbol)
        if filters is None:
            return True, {"reason": "filters_unavailable"}

        normalized_quantity = round_to_increment(live_quantity, self._entry_quantity_step(filters))
        normalized_notional = normalized_quantity * Decimal(order.entry_price)
        viable = (
            normalized_quantity > 0
            and self._entry_quantity_within_market_lot(filters=filters, quantity=normalized_quantity)
            and normalized_notional >= filters.min_notional
        )
        return viable, {
            "reason": "minimum_viable_fill_check",
            "live_quantity": self._decimal_string(live_quantity),
            "normalized_quantity": self._decimal_string(normalized_quantity),
            "normalized_notional": self._decimal_string(normalized_notional),
            "min_notional": self._decimal_string(filters.min_notional),
            "step_size": self._decimal_string(self._entry_quantity_step(filters)),
            "min_qty": self._decimal_string(self._entry_min_qty(filters)),
        }

    def _protection_refs_present(self, order: Order) -> bool:
        return all(self._remote_order_id_for_role(order, role) for role in self._expected_protection_roles(order))

    async def _replace_protective_orders(
        self,
        credentials: ApiCredentials,
        order: Order,
        *,
        protection_quantity: Decimal,
    ) -> bool:
        exchange_info = await self.gateway.exchange_info()
        filters = self.gateway.parse_symbol_filters(exchange_info).get(order.symbol)
        if filters is None:
            return False

        normalized_quantity = round_to_increment(protection_quantity, self._entry_quantity_step(filters))
        if (
            normalized_quantity <= 0
            or not self._entry_quantity_within_market_lot(filters=filters, quantity=normalized_quantity)
            or (normalized_quantity * Decimal(order.entry_price)) < filters.min_notional
        ):
            return False

        exit_side = "SELL" if order.direction == SignalDirection.LONG else "BUY"
        cleanup_targets = [remote_order for remote_order in self._known_remote_refs(order) if remote_order.role != "entry"]
        if cleanup_targets:
            await self._cleanup_remote_orders(credentials, order.symbol, cleanup_targets)

        created_remote_orders: list[RemoteOrderRef] = []
        order.tp_order_id = None
        order.tp_order_1_id = None
        order.tp_order_2_id = None
        order.sl_order_id = None

        try:
            if self._partial_tp_enabled(order):
                partial_split = split_partial_take_profit_quantity(
                    total_quantity=normalized_quantity,
                    step_size=self._entry_quantity_step(filters),
                    min_qty=self._entry_min_qty(filters),
                )
                if partial_split is None or order.take_profit_1 is None or order.take_profit_2 is None:
                    return False
                order.tp_quantity_1 = partial_split.tp1_quantity
                order.tp_quantity_2 = partial_split.tp2_quantity

                tp1_order = await self.gateway.place_algo_order(
                    credentials,
                    {
                        "algoType": "CONDITIONAL",
                        "symbol": order.symbol,
                        "side": exit_side,
                        "type": "TAKE_PROFIT",
                        "quantity": str(partial_split.tp1_quantity),
                        "triggerPrice": str(order.take_profit_1),
                        "price": str(order.take_profit_1),
                        "reduceOnly": "true",
                        "workingType": "MARK_PRICE",
                        "clientAlgoId": self._managed_tp1_client_id(order),
                    },
                )
                tp1_order_id = self._remote_id(tp1_order.get("algoId"))
                if tp1_order_id is None:
                    raise ValueError("Binance partial take-profit order response did not include algoId")
                created_remote_orders.append(RemoteOrderRef(order_id=tp1_order_id, role="tp1", kind="algo"))
                order.tp_order_1_id = tp1_order_id

                tp2_order = await self.gateway.place_algo_order(
                    credentials,
                    {
                        "algoType": "CONDITIONAL",
                        "symbol": order.symbol,
                        "side": exit_side,
                        "type": "TAKE_PROFIT",
                        "quantity": str(partial_split.tp2_quantity),
                        "triggerPrice": str(order.take_profit_2),
                        "price": str(order.take_profit_2),
                        "reduceOnly": "true",
                        "workingType": "MARK_PRICE",
                        "clientAlgoId": self._managed_tp2_client_id(order),
                    },
                )
                tp2_order_id = self._remote_id(tp2_order.get("algoId"))
                if tp2_order_id is None:
                    raise ValueError("Binance second partial take-profit order response did not include algoId")
                created_remote_orders.append(RemoteOrderRef(order_id=tp2_order_id, role="tp2", kind="algo"))
                order.tp_order_2_id = tp2_order_id
                order.tp_order_id = tp2_order_id
            else:
                tp_order = await self.gateway.place_algo_order(
                    credentials,
                    {
                        "algoType": "CONDITIONAL",
                        "symbol": order.symbol,
                        "side": exit_side,
                        "type": "TAKE_PROFIT_MARKET",
                        "quantity": str(normalized_quantity),
                        "triggerPrice": str(order.take_profit),
                        "reduceOnly": "true",
                        "workingType": "MARK_PRICE",
                        "clientAlgoId": self._managed_tp_client_id(order),
                    },
                )
                tp_order_id = self._remote_id(tp_order.get("algoId"))
                if tp_order_id is None:
                    raise ValueError("Binance take-profit algo order response did not include algoId")
                created_remote_orders.append(RemoteOrderRef(order_id=tp_order_id, role="tp", kind="algo"))
                order.tp_order_id = tp_order_id

            sl_order = await self.gateway.place_algo_order(
                credentials,
                {
                    "algoType": "CONDITIONAL",
                    "symbol": order.symbol,
                    "side": exit_side,
                    "type": "STOP_MARKET",
                    "quantity": str(normalized_quantity),
                    "triggerPrice": str(order.stop_loss),
                    "reduceOnly": "true",
                    "workingType": "MARK_PRICE",
                    "clientAlgoId": self._managed_sl_client_id(order),
                },
            )
            sl_order_id = self._remote_id(sl_order.get("algoId"))
            if sl_order_id is None:
                raise ValueError("Binance stop-loss algo order response did not include algoId")
            created_remote_orders.append(RemoteOrderRef(order_id=sl_order_id, role="sl", kind="algo"))
            order.sl_order_id = sl_order_id
        except Exception:
            if created_remote_orders:
                await self._cleanup_remote_orders(credentials, order.symbol, created_remote_orders)
            return False

        self._update_strategy_context(
            order,
            protection_quantity=self._decimal_string(normalized_quantity),
        )
        return True

    async def _stop_loss_protection_confirmed(self, credentials: ApiCredentials, order: Order) -> bool:
        remote_kind, remote_state = await self._query_managed_protective_order(credentials, order, role="sl")
        if remote_kind is None or remote_state is None:
            return False
        if remote_kind == "standard":
            return str(remote_state.get("status") or "").upper() in {"NEW", "PARTIALLY_FILLED", "FILLED"}

        algo_status = str(remote_state.get("algoStatus") or "").upper()
        if algo_status in {"NEW", "ACCEPTED", "WORKING"}:
            return True
        if algo_status != "TRIGGERED":
            return False

        actual_order_id = self._remote_id(remote_state.get("actualOrderId"))
        if actual_order_id is None:
            return False
        try:
            actual_order_state = await self.gateway.query_order(credentials, order.symbol, actual_order_id)
        except BinanceAPIError:
            return False
        return str((actual_order_state or {}).get("status") or "").upper() in {"NEW", "PARTIALLY_FILLED", "FILLED"}

    async def _ensure_live_protections(
        self,
        credentials: ApiCredentials,
        order: Order,
        *,
        live_quantity: Decimal,
        force_confirm_stop_loss: bool,
    ) -> bool:
        expected_quantity = max(live_quantity, Decimal("0"))
        if expected_quantity <= 0:
            return False

        protections_ready = self._protection_refs_present(order)
        if not protections_ready or self._protection_quantity(order) != expected_quantity:
            if not await self._replace_protective_orders(
                credentials,
                order,
                protection_quantity=expected_quantity,
            ):
                return False
            force_confirm_stop_loss = True

        if force_confirm_stop_loss:
            return await self._stop_loss_protection_confirmed(credentials, order)
        return True

    async def _record_protection_failure_and_flatten(
        self,
        session,
        *,
        credentials: ApiCredentials,
        order: Order,
        scan_cycle_id: int | None,
        reason_context: dict[str, Any],
        reason: str = "protection_confirmation_failed",
        message: str | None = None,
    ) -> Order:
        await record_audit(
            session,
            event_type="ORDER_PROTECTION_FAILURE",
            level=AuditLevel.ERROR,
            message=message or f"{order.symbol} live exposure was flattened because stop-loss protection could not be confirmed",
            order_id=order.id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            scan_cycle_id=scan_cycle_id,
            details=self._reason_details(
                reason=reason,
                reason_context=reason_context,
                default_context=await self._signal_reason_context(
                    session,
                    signal_id=order.signal_id,
                    setup_family=str(getattr(order, "setup_family", None) or "") or None,
                    entry_style=str(getattr(order, "entry_style", None) or "") or None,
                ),
            ),
        )
        return await self._flatten_live_order(
            session,
            credentials=credentials,
            order=order,
            scan_cycle_id=scan_cycle_id,
            reason=reason,
            reason_context=reason_context,
        )

    async def _cancel_entry_remainder_if_partial(
        self,
        credentials: ApiCredentials,
        order: Order,
        *,
        entry_state: EntryOrderState | None = None,
    ) -> bool:
        resolved_entry_state = entry_state or await self._query_entry_order_state(credentials, order)
        if resolved_entry_state is None or str(resolved_entry_state.status or "").upper() != "PARTIALLY_FILLED":
            return False
        await self._cancel_entry_order(credentials, order)
        return True

    async def _sync_live_entry_state(
        self,
        session,
        credentials: ApiCredentials,
        order: Order,
        *,
        scan_cycle_id: int | None,
    ) -> None:
        entry_state = await self._query_entry_order_state(credentials, order)
        if entry_state is None:
            entry_state = await self._recover_entry_state_from_authoritative_exposure(credentials, order)
        if entry_state is None:
            return

        entry_status = str(entry_state.status or "").upper()
        if order.tp1_filled_at is None and Decimal(order.realized_pnl or 0) == 0 and entry_status in {"PARTIALLY_FILLED", "FILLED"}:
            previous_quantity = self._filled_entry_quantity(order)
            filled_quantity, average_entry_price = await self._entry_fill_details(
                credentials,
                order,
                entry_state=entry_state,
            )
            if filled_quantity <= 0:
                await self._record_protection_failure_and_flatten(
                    session,
                    credentials=credentials,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    reason="entry_fill_quantity_unconfirmed",
                    message=f"{order.symbol} live exposure was flattened because the filled entry quantity could not be confirmed",
                    reason_context={
                        "lifecycle_reason": "entry_fill_quantity_unconfirmed",
                        "entry_status": entry_status,
                        "actual_order_id": entry_state.actual_order_id,
                        "entry_route": entry_state.remote_kind,
                    },
                )
                return
            self._apply_entry_fill_state(
                order,
                filled_quantity=filled_quantity,
                average_entry_price=average_entry_price,
            )
            minimum_viable_fill, viability_details = await self._minimum_viable_live_fill(
                order,
                live_quantity=self._live_position_quantity(order),
            )
            if not minimum_viable_fill:
                await record_audit(
                    session,
                    event_type="ORDER_MINIMUM_VIABLE_FILL_CLOSED",
                    level=AuditLevel.WARNING,
                    message=f"{order.symbol} partial fill was closed because the live remainder was too small to protect safely",
                    order_id=order.id,
                    signal_id=order.signal_id,
                    symbol=order.symbol,
                    scan_cycle_id=scan_cycle_id,
                    details={
                        "entry_status": entry_status,
                        "filled_quantity": self._decimal_string(filled_quantity),
                        **viability_details,
                    },
                )
                await self._flatten_live_order(
                    session,
                    credentials=credentials,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    reason="minimum_viable_partial_fill",
                    reason_context={
                        "lifecycle_reason": "minimum_viable_partial_fill",
                        "entry_status": entry_status,
                        "filled_quantity": self._decimal_string(filled_quantity),
                        **viability_details,
                    },
                )
                return
            protections_ready = await self._ensure_live_protections(
                credentials,
                order,
                live_quantity=self._live_position_quantity(order),
                force_confirm_stop_loss=filled_quantity != previous_quantity,
            )
            if not protections_ready and order.status == OrderStatus.IN_POSITION:
                await self._record_protection_failure_and_flatten(
                    session,
                    credentials=credentials,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    reason_context={
                        "lifecycle_reason": "protection_confirmation_failed",
                        "entry_status": entry_status,
                        "filled_quantity": self._decimal_string(filled_quantity),
                        "actual_order_id": entry_state.actual_order_id,
                        "entry_route": entry_state.remote_kind,
                    },
                )
                return

        if entry_status == "PARTIALLY_FILLED" and self.pending_entry_expired(order):
            remainder_cancelled = await self._cancel_entry_remainder_if_partial(
                credentials,
                order,
                entry_state=entry_state,
            )
            if remainder_cancelled:
                await record_audit(
                    session,
                    event_type="ORDER_ENTRY_REMAINDER_CANCELLED",
                    message=f"{order.symbol} unfilled entry remainder cancelled at expiry",
                    symbol=order.symbol,
                    scan_cycle_id=scan_cycle_id,
                    order_id=order.id,
                    signal_id=order.signal_id,
                    details={
                        "reason": "entry_expired_after_partial_fill",
                        "filled_quantity": self._decimal_string(self._filled_entry_quantity(order)),
                        "remaining_live_quantity": self._decimal_string(self._live_position_quantity(order)),
                    },
                )

    async def _activate_entry_fill(
        self,
        session,
        *,
        credentials: ApiCredentials,
        order: Order,
        entry_state: EntryOrderState,
        scan_cycle_id: int | None,
        protections_confirmed: bool | None = None,
        authoritative_recovery_outcome: str | None = None,
    ) -> bool:
        entry_status = str(entry_state.status or "").upper()
        if entry_status not in {"PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED"}:
            return False

        now = datetime.now(timezone.utc)
        fill_time = self._entry_state_update_time(entry_state) or now
        filled_quantity, average_entry_price = await self._entry_fill_details(
            credentials,
            order,
            entry_state=entry_state,
        )
        if filled_quantity <= 0:
            if entry_status in {"CANCELED", "EXPIRED"}:
                return False
            await self._record_protection_failure_and_flatten(
                session,
                credentials=credentials,
                order=order,
                scan_cycle_id=scan_cycle_id,
                reason="entry_fill_quantity_unconfirmed",
                message=f"{order.symbol} live exposure was flattened because the filled entry quantity could not be confirmed",
                reason_context={
                    "lifecycle_reason": "entry_fill_quantity_unconfirmed",
                    "entry_status": entry_status,
                    "actual_order_id": entry_state.actual_order_id,
                    "entry_route": entry_state.remote_kind,
                },
            )
            return False

        normalized_quantity = max(filled_quantity, Decimal("0"))
        self._apply_entry_fill_state(
            order,
            filled_quantity=normalized_quantity,
            average_entry_price=average_entry_price,
        )

        order.status = OrderStatus.IN_POSITION
        order.placed_at = order.placed_at or fill_time
        first_trigger = order.triggered_at is None
        order.triggered_at = order.triggered_at or fill_time
        minimum_viable_fill, viability_details = await self._minimum_viable_live_fill(
            order,
            live_quantity=self._live_position_quantity(order),
        )
        if not minimum_viable_fill:
            await record_audit(
                session,
                event_type="ORDER_MINIMUM_VIABLE_FILL_CLOSED",
                level=AuditLevel.WARNING,
                message=f"{order.symbol} partial fill was closed because the live remainder was too small to protect safely",
                order_id=order.id,
                signal_id=order.signal_id,
                symbol=order.symbol,
                scan_cycle_id=scan_cycle_id,
                details={
                    "entry_status": entry_status,
                    "filled_quantity": self._decimal_string(normalized_quantity),
                    **viability_details,
                },
            )
            await self._flatten_live_order(
                session,
                credentials=credentials,
                order=order,
                scan_cycle_id=scan_cycle_id,
                reason="minimum_viable_partial_fill",
                reason_context={
                    "lifecycle_reason": "minimum_viable_partial_fill",
                    "entry_status": entry_status,
                    "filled_quantity": self._decimal_string(normalized_quantity),
                    **viability_details,
                },
            )
            return False

        protections_ready = await self._ensure_live_protections(
            credentials,
            order,
            live_quantity=self._live_position_quantity(order),
            force_confirm_stop_loss=True,
        )
        if not protections_ready:
            await self._record_protection_failure_and_flatten(
                session,
                credentials=credentials,
                order=order,
                scan_cycle_id=scan_cycle_id,
                reason_context={
                    "lifecycle_reason": "protection_confirmation_failed",
                    "entry_status": entry_status,
                    "filled_quantity": self._decimal_string(normalized_quantity),
                    "actual_order_id": entry_state.actual_order_id,
                    "entry_route": entry_state.remote_kind,
                    "protection_refs_present": self._protection_refs_present(order),
                },
            )
            return False

        total_entry_quantity = self._decimal_from_payload(getattr(order, "quantity", None)) or Decimal("0")
        remainder_quantity = max(total_entry_quantity - normalized_quantity, Decimal("0"))
        remainder_reason: str | None = None
        remainder_message: str | None = None
        entry_expired = self.pending_entry_expired(order, now=now)
        if remainder_quantity > 0:
            if entry_status == "PARTIALLY_FILLED" and entry_expired:
                remainder_reason = "entry_expired_after_partial_fill"
                remainder_message = f"{order.symbol} unfilled entry remainder expired after partial fill"
                try:
                    await self._cancel_entry_remainder_if_partial(credentials, order, entry_state=entry_state)
                except Exception as exc:
                    logger.warning(
                        "activate_entry_fill.entry_remainder_cancel_failed",
                        order_id=order.id,
                        symbol=order.symbol,
                        error=str(exc),
                    )
            elif entry_status == "CANCELED":
                remainder_reason = "entry_cancelled_after_partial_fill"
                remainder_message = f"{order.symbol} unfilled entry remainder was cancelled after partial fill"
            elif entry_status == "EXPIRED":
                remainder_reason = "entry_expired_after_partial_fill"
                remainder_message = f"{order.symbol} unfilled entry remainder expired after partial fill"

        reason_context = await self._signal_reason_context(
            session,
            signal_id=order.signal_id,
            setup_family=str(getattr(order, "setup_family", None) or "") or None,
            entry_style=str(getattr(order, "entry_style", None) or "") or None,
        )

        if remainder_reason is not None:
            await record_audit(
                session,
                event_type="ORDER_ENTRY_REMAINDER_CANCELLED",
                message=remainder_message,
                symbol=order.symbol,
                scan_cycle_id=scan_cycle_id,
                order_id=order.id,
                signal_id=order.signal_id,
                details={
                    "reason": remainder_reason,
                    "entry_status": entry_status,
                    "entry_route": entry_state.remote_kind,
                    "algo_status": entry_state.algo_status,
                    "actual_order_id": entry_state.actual_order_id,
                    "filled_quantity": self._decimal_string(normalized_quantity),
                    "entry_remainder_quantity": self._decimal_string(remainder_quantity),
                    "remaining_live_quantity": self._decimal_string(self._live_position_quantity(order)),
                    "entry_remainder_cancelled": True,
                    "authoritative_recovery_outcome": authoritative_recovery_outcome,
                    **reason_context,
                },
            )

        if not first_trigger:
            return True

        message = (
            f"{order.symbol} position partially opened"
            if remainder_quantity > 0
            else f"{order.symbol} position opened"
        )
        await record_audit(
            session,
            event_type="ORDER_TRIGGERED",
            message=message,
            symbol=order.symbol,
            scan_cycle_id=scan_cycle_id,
            order_id=order.id,
            signal_id=order.signal_id,
            details={
                "entry_status": entry_status,
                "entry_route": entry_state.remote_kind,
                "algo_status": entry_state.algo_status,
                "actual_order_id": entry_state.actual_order_id,
                "filled_quantity": self._decimal_string(normalized_quantity),
                "entry_remainder_cancelled": remainder_reason is not None,
                "entry_remainder_reason": remainder_reason,
                "entry_remainder_quantity": self._decimal_string(remainder_quantity),
                "authoritative_recovery_outcome": authoritative_recovery_outcome,
                **reason_context,
            },
        )
        await self.notifier.send(title="Order Triggered", message=message, sound="signal")
        return True

    async def _exchange_fill_snapshot(
        self,
        credentials: ApiCredentials,
        order: Order,
        *,
        actual_order_id: str | None,
        fallback_price: Decimal,
        fallback_quantity: Decimal | None = None,
        fallback_closed_at: datetime | None = None,
        order_state: dict | None = None,
    ) -> ExchangeFillSnapshot:
        close_price = fallback_price
        closed_at = fallback_closed_at
        trades: list[dict] = []
        close_status = ""
        default_filled_quantity = (
            fallback_quantity
            if fallback_quantity is not None
            else self._live_position_quantity(order)
        )

        if actual_order_id:
            if order_state is None:
                try:
                    order_state = await self.gateway.query_order(credentials, order.symbol, actual_order_id)
                except BinanceAPIError:
                    order_state = {}
            close_status = str(order_state.get("status") or "").upper()
            close_price = (
                self._decimal_from_payload(order_state.get("avgPrice"))
                or self._decimal_from_payload(order_state.get("price"))
                or fallback_price
            )
            closed_at = self._datetime_from_millis(order_state.get("updateTime")) or closed_at
            exchange_filled_quantity = (
                self._decimal_from_payload(order_state.get("executedQty"))
                or self._decimal_from_payload(order_state.get("cumQty"))
                or self._decimal_from_payload(order_state.get("cumBase"))
                or self._decimal_from_payload(order_state.get("origQty"))
            )
            if exchange_filled_quantity is not None and exchange_filled_quantity > 0:
                default_filled_quantity = exchange_filled_quantity
            trades = await self.gateway.account_trades(credentials, order.symbol, order_id=actual_order_id)

        if trades:
            realized_pnl = sum((self._decimal_from_payload(trade.get("realizedPnl")) or Decimal("0") for trade in trades), start=Decimal("0"))
            total_qty = sum((self._decimal_from_payload(trade.get("qty")) or Decimal("0") for trade in trades), start=Decimal("0"))
            weighted_close = sum(
                (
                    (self._decimal_from_payload(trade.get("price")) or Decimal("0"))
                    * (self._decimal_from_payload(trade.get("qty")) or Decimal("0"))
                    for trade in trades
                ),
                start=Decimal("0"),
            )
            if total_qty > 0:
                close_price = weighted_close / total_qty
            closed_at = max(
                (self._datetime_from_millis(trade.get("time")) or closed_at or datetime.now(timezone.utc) for trade in trades),
                default=closed_at,
            )
            return ExchangeFillSnapshot(
                close_price=close_price,
                realized_pnl=realized_pnl,
                filled_quantity=total_qty,
                closed_at=closed_at,
            )

        # Binance can report a filled protective exit before trade history or quantity fields
        # are visible. At that point the exit is already exchange-confirmed, so this fallback
        # is only used for close accounting, not for live close sizing.
        if actual_order_id and close_status == "FILLED" and default_filled_quantity <= 0:
            default_filled_quantity = self._live_position_quantity(order)

        return ExchangeFillSnapshot(
            close_price=close_price,
            realized_pnl=self._realized_pnl_for_close(
                order=order,
                close_price=close_price,
                quantity=default_filled_quantity,
            ),
            filled_quantity=default_filled_quantity,
            closed_at=closed_at,
        )

    def _filled_quantity_before_close(self, order: Order) -> Decimal:
        total_quantity = self._filled_entry_quantity(order)
        remaining_quantity = self._live_position_quantity(order)
        return max(total_quantity - remaining_quantity, Decimal("0"))

    def _combined_fill_snapshot(self, *, order: Order, fill_snapshot: ExchangeFillSnapshot) -> ExchangeFillSnapshot:
        previous_realized_pnl = Decimal(order.realized_pnl or 0)
        previous_close_price = Decimal(order.close_price) if order.close_price is not None else None
        previous_filled_quantity = self._filled_quantity_before_close(order)
        combined_filled_quantity = previous_filled_quantity + fill_snapshot.filled_quantity
        combined_close_price = fill_snapshot.close_price

        if (
            previous_close_price is not None
            and previous_filled_quantity > 0
            and fill_snapshot.filled_quantity > 0
            and combined_filled_quantity > 0
        ):
            combined_close_price = (
                (previous_close_price * previous_filled_quantity)
                + (fill_snapshot.close_price * fill_snapshot.filled_quantity)
            ) / combined_filled_quantity

        return ExchangeFillSnapshot(
            close_price=combined_close_price,
            realized_pnl=previous_realized_pnl + fill_snapshot.realized_pnl,
            filled_quantity=combined_filled_quantity,
            closed_at=fill_snapshot.closed_at,
        )

    @staticmethod
    def _remote_order_id_for_role(order: Order, role: str) -> str | None:
        if role == "tp1":
            return order.tp_order_1_id
        if role in {"tp", "tp2"}:
            return order.tp_order_2_id or order.tp_order_id
        if role == "sl":
            return order.sl_order_id
        return None

    async def _close_order_from_snapshot(
        self,
        session,
        *,
        order: Order,
        closed_status: OrderStatus,
        close_type: str,
        event_type: str,
        event_message: str,
        scan_cycle_id: int | None,
        fill_snapshot: ExchangeFillSnapshot,
        extra_details: dict[str, Any] | None = None,
        notify_title: str | None = None,
        notify_message: str | None = None,
        sound: str | None = None,
    ) -> None:
        combined_snapshot = self._combined_fill_snapshot(order=order, fill_snapshot=fill_snapshot)
        order.status = closed_status
        order.close_type = close_type
        order.closed_at = combined_snapshot.closed_at or datetime.now(timezone.utc)
        order.close_price = combined_snapshot.close_price
        order.realized_pnl = combined_snapshot.realized_pnl
        order.remaining_quantity = Decimal("0")
        await self._close_linked_observed_position(session, order_id=order.id, closed_at=order.closed_at)
        await record_closed_trade_stat(
            session,
            bucket=self._order_stats_bucket(order),
            closed_status=closed_status,
            closed_at=order.closed_at,
        )
        await record_audit(
            session,
            event_type=event_type,
            message=event_message,
            symbol=order.symbol,
            scan_cycle_id=scan_cycle_id,
            order_id=order.id,
            signal_id=order.signal_id,
            details={"close_type": close_type, **(extra_details or {})},
        )
        if notify_title and notify_message and sound:
            await self.notifier.send(title=notify_title, message=notify_message, sound=sound)

    async def _sync_partial_take_profit_fill(
        self,
        session,
        *,
        credentials: ApiCredentials,
        order: Order,
        scan_cycle_id: int | None,
    ) -> bool:
        if order.status != OrderStatus.IN_POSITION or not self._partial_tp_enabled(order) or order.tp1_filled_at is not None:
            return False

        remote_kind, remote_state = await self._query_managed_protective_order(credentials, order, role="tp1")
        if remote_kind is None or remote_state is None:
            return False

        actual_order_id: str | None = None
        actual_order_state: dict | None = None
        if remote_kind == "standard":
            if remote_state.get("status") != "FILLED":
                return False
            actual_order_id = self._remote_id(remote_state.get("orderId"))
            actual_order_state = remote_state
        else:
            actual_order_id = self._remote_id(remote_state.get("actualOrderId"))
            if actual_order_id is None:
                algo_status = str(remote_state.get("algoStatus") or "")
                if algo_status in {"CANCELED", "EXPIRED"}:
                    await self._record_protection_warning(
                        session,
                        order=order,
                        scan_cycle_id=scan_cycle_id,
                        protection="partial-take-profit",
                        remote_order_id=self._remote_order_id_for_role(order, "tp1") or "missing",
                        algo_status=algo_status,
                    )
                return False

            actual_order_state = await self.gateway.query_order(credentials, order.symbol, actual_order_id)
            if actual_order_state.get("status") != "FILLED":
                return False

        fill_snapshot = await self._exchange_fill_snapshot(
            credentials,
            order,
            actual_order_id=actual_order_id,
            fallback_price=Decimal(order.take_profit_1 or order.take_profit),
            fallback_quantity=Decimal(order.tp_quantity_1 or "0"),
            order_state=actual_order_state,
        )
        order.tp1_filled_at = fill_snapshot.closed_at or datetime.now(timezone.utc)
        order.realized_pnl = Decimal(order.realized_pnl or 0) + fill_snapshot.realized_pnl
        order.close_price = fill_snapshot.close_price
        if order.tp_quantity_2 is not None:
            order.remaining_quantity = Decimal(order.tp_quantity_2)
        else:
            order.remaining_quantity = max(self._filled_entry_quantity(order) - fill_snapshot.filled_quantity, Decimal("0"))
        await record_audit(
            session,
            event_type="ORDER_PARTIAL_TP_FILLED",
            message=f"{order.symbol} first take profit filled",
            symbol=order.symbol,
            scan_cycle_id=scan_cycle_id,
            order_id=order.id,
            signal_id=order.signal_id,
            details={
                "reason": "partial_tp_1_filled",
                "filled_quantity": self._decimal_string(fill_snapshot.filled_quantity),
                "remaining_quantity": self._decimal_string(Decimal(order.remaining_quantity or 0)),
                "close_price": self._decimal_string(fill_snapshot.close_price),
            },
        )
        await self.notifier.send(title="Partial Take Profit Hit", message=f"{order.symbol} locked in partial profit", sound="tp")
        return True

    async def _sync_protective_exit(
        self,
        session,
        *,
        credentials: ApiCredentials,
        order: Order,
        role: str,
        protection: str,
        close_type: str,
        fallback_close_price: Decimal,
        fallback_quantity: Decimal | None,
        scan_cycle_id: int | None,
    ) -> bool:
        if order.status != OrderStatus.IN_POSITION:
            return False

        remote_kind, remote_state = await self._query_managed_protective_order(credentials, order, role=role)
        if remote_kind is None or remote_state is None:
            if protection == "stop-loss":
                await self._record_protection_failure_and_flatten(
                    session,
                    credentials=credentials,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    reason="stop_loss_protection_unconfirmed",
                    message=f"{order.symbol} live exposure was flattened because stop-loss protection could not be confirmed",
                    reason_context={
                        "lifecycle_reason": "stop_loss_protection_unconfirmed",
                        "protection": protection,
                        "protection_role": role,
                    },
                )
                return True
            return False

        actual_order_id: str | None = None
        actual_order_state: dict | None = None
        if remote_kind == "standard":
            protection_status = str(remote_state.get("status") or "").upper()
            if protection_status == "FILLED":
                actual_order_id = self._remote_id(remote_state.get("orderId"))
                actual_order_state = remote_state
            elif protection == "stop-loss" and protection_status not in {"NEW", "PARTIALLY_FILLED"}:
                await self._record_protection_failure_and_flatten(
                    session,
                    credentials=credentials,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    reason="stop_loss_protection_inactive",
                    message=f"{order.symbol} live exposure was flattened because stop-loss protection became inactive",
                    reason_context={
                        "lifecycle_reason": "stop_loss_protection_inactive",
                        "protection": protection,
                        "protection_role": role,
                        "protection_status": protection_status,
                        "remote_kind": remote_kind,
                    },
                )
                return True
            else:
                return False
        else:
            algo_status = str(remote_state.get("algoStatus") or "").upper()
            if algo_status in {"NEW", "ACCEPTED", "WORKING"}:
                return False
            actual_order_id = self._remote_id(remote_state.get("actualOrderId"))
            if actual_order_id is None:
                if protection == "stop-loss" and algo_status in {"CANCELED", "EXPIRED"}:
                    await self._record_protection_failure_and_flatten(
                        session,
                        credentials=credentials,
                        order=order,
                        scan_cycle_id=scan_cycle_id,
                        reason="stop_loss_protection_inactive",
                        message=f"{order.symbol} live exposure was flattened because stop-loss protection became inactive",
                        reason_context={
                            "lifecycle_reason": "stop_loss_protection_inactive",
                            "protection": protection,
                            "protection_role": role,
                            "algo_status": algo_status,
                            "remote_kind": remote_kind,
                        },
                    )
                    return True
                if protection == "stop-loss":
                    await self._record_protection_failure_and_flatten(
                        session,
                        credentials=credentials,
                        order=order,
                        scan_cycle_id=scan_cycle_id,
                        reason="stop_loss_protection_unconfirmed",
                        message=f"{order.symbol} live exposure was flattened because stop-loss protection could not be confirmed",
                        reason_context={
                            "lifecycle_reason": "stop_loss_protection_unconfirmed",
                            "protection": protection,
                            "protection_role": role,
                            "algo_status": algo_status,
                            "remote_kind": remote_kind,
                        },
                    )
                    return True
                if algo_status in {"CANCELED", "EXPIRED"}:
                    await self._record_protection_warning(
                        session,
                        order=order,
                        scan_cycle_id=scan_cycle_id,
                        protection=protection,
                        remote_order_id=self._remote_order_id_for_role(order, role) or "missing",
                        algo_status=algo_status,
                    )
                return False

            try:
                actual_order_state = await self.gateway.query_order(credentials, order.symbol, actual_order_id)
            except Exception:
                if protection == "stop-loss":
                    await self._record_protection_failure_and_flatten(
                        session,
                        credentials=credentials,
                        order=order,
                        scan_cycle_id=scan_cycle_id,
                        reason="stop_loss_protection_unconfirmed",
                        message=f"{order.symbol} live exposure was flattened because stop-loss protection could not be confirmed",
                        reason_context={
                            "lifecycle_reason": "stop_loss_protection_unconfirmed",
                            "protection": protection,
                            "protection_role": role,
                            "algo_status": algo_status,
                            "actual_order_id": actual_order_id,
                            "remote_kind": remote_kind,
                        },
                    )
                    return True
                return False

            protection_status = str((actual_order_state or {}).get("status") or "").upper()
            if protection_status == "FILLED":
                pass
            elif protection == "stop-loss" and protection_status not in {"NEW", "PARTIALLY_FILLED"}:
                await self._record_protection_failure_and_flatten(
                    session,
                    credentials=credentials,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    reason="stop_loss_protection_inactive",
                    message=f"{order.symbol} live exposure was flattened because stop-loss protection became inactive",
                    reason_context={
                        "lifecycle_reason": "stop_loss_protection_inactive",
                        "protection": protection,
                        "protection_role": role,
                        "algo_status": algo_status,
                        "protection_status": protection_status,
                        "actual_order_id": actual_order_id,
                        "remote_kind": remote_kind,
                    },
                )
                return True
            else:
                return False

        try:
            await self._cancel_entry_remainder_if_partial(credentials, order)
        except Exception as exc:
            logger.warning(
                "sync_protective_exit.entry_remainder_cancel_failed",
                order_id=order.id,
                symbol=order.symbol,
                error=str(exc),
            )

        effective_fallback_quantity = fallback_quantity if fallback_quantity is not None and fallback_quantity > 0 else None
        if effective_fallback_quantity is None:
            close_quantity_resolution = await self._resolve_authoritative_close_quantity(credentials, order)
            if close_quantity_resolution.outcome == self.RECOVERY_OUTCOME_RECOVERED:
                effective_fallback_quantity = close_quantity_resolution.quantity
            elif close_quantity_resolution.outcome == self.RECOVERY_OUTCOME_INCONCLUSIVE:
                await self._record_authoritative_recovery_inconclusive(
                    session,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    message=f"{order.symbol} exit reconciliation could not confirm an authoritative close quantity",
                    details={
                        "lifecycle_reason": "authoritative_close_quantity_unconfirmed",
                        "close_type": close_type,
                        "protection": protection,
                        "protection_role": role,
                        **close_quantity_resolution.details,
                    },
                )
                return False
            else:
                effective_fallback_quantity = Decimal("0")
        fill_snapshot = await self._exchange_fill_snapshot(
            credentials,
            order,
            actual_order_id=actual_order_id,
            fallback_price=fallback_close_price,
            fallback_quantity=effective_fallback_quantity,
            order_state=actual_order_state,
        )
        combined_snapshot = self._combined_fill_snapshot(order=order, fill_snapshot=fill_snapshot)
        closed_status = self._closed_status_for_fill_snapshot(order, combined_snapshot)
        event_type = "ORDER_CLOSED_WIN" if closed_status == OrderStatus.CLOSED_WIN else "ORDER_CLOSED_LOSS"
        event_message = f"{order.symbol} closed at take profit" if close_type == "TP" else f"{order.symbol} closed at stop loss"
        notify_title = "Take Profit Hit" if close_type == "TP" else "Stop Loss Hit"
        notify_message = f"{order.symbol} closed in profit" if closed_status == OrderStatus.CLOSED_WIN else f"{order.symbol} closed in loss"
        sound = "tp" if close_type == "TP" else "sl"
        await self._close_order_from_snapshot(
            session,
            order=order,
            closed_status=closed_status,
            close_type=close_type,
            event_type=event_type,
            event_message=event_message,
            scan_cycle_id=scan_cycle_id,
            fill_snapshot=fill_snapshot,
            notify_title=notify_title,
            notify_message=notify_message,
            sound=sound,
        )
        return True

    async def _recover_external_fill_snapshot(self, credentials: ApiCredentials, order: Order) -> ExchangeFillSnapshot | None:
        if order.tp1_filled_at is not None:
            start_at = order.tp1_filled_at + timedelta(milliseconds=1)
        else:
            start_at = order.triggered_at or order.placed_at or getattr(order, "created_at", None)
        now = datetime.now(timezone.utc)
        start_window = max(
            start_at or (now - timedelta(days=6)),
            now - timedelta(days=6),
        )
        trades = await self.gateway.account_trades(
            credentials,
            order.symbol,
            start_time=int(start_window.timestamp() * 1000),
            end_time=int(now.timestamp() * 1000),
        )
        expected_close_side = "SELL" if order.direction == SignalDirection.LONG else "BUY"
        closing_trades = [
            trade
            for trade in trades
            if (self._decimal_from_payload(trade.get("realizedPnl")) or Decimal("0")) != 0
            and str(trade.get("side") or "").upper() == expected_close_side
            and (self._datetime_from_millis(trade.get("time")) or now) >= start_window
        ]
        if not closing_trades:
            return None
        total_qty = sum((self._decimal_from_payload(trade.get("qty")) or Decimal("0") for trade in closing_trades), start=Decimal("0"))
        if total_qty <= 0:
            return None
        weighted_close = sum(
            (
                (self._decimal_from_payload(trade.get("price")) or Decimal("0"))
                * (self._decimal_from_payload(trade.get("qty")) or Decimal("0"))
                for trade in closing_trades
            ),
            start=Decimal("0"),
        )
        realized_pnl = sum(
            ((self._decimal_from_payload(trade.get("realizedPnl")) or Decimal("0")) for trade in closing_trades),
            start=Decimal("0"),
        )
        closed_at = max(
            (self._datetime_from_millis(trade.get("time")) or now for trade in closing_trades),
            default=now,
        )
        return ExchangeFillSnapshot(
            close_price=weighted_close / total_qty,
            realized_pnl=realized_pnl,
            filled_quantity=total_qty,
            closed_at=closed_at,
        )

    async def _record_approval_failure(
        self,
        session,
        *,
        signal: Signal,
        message: str,
        level: AuditLevel = AuditLevel.WARNING,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = dict(details or {})
        for key, value in (
            await self._signal_reason_context(
                session,
                signal=signal,
                setup_family=getattr(signal, "setup_family", None),
                entry_style=getattr(signal, "entry_style", None),
            )
        ).items():
            payload.setdefault(key, value)
        await record_audit(
            session,
            event_type="ORDER_APPROVAL_FAILED",
            level=level,
            message=message,
            symbol=signal.symbol,
            scan_cycle_id=signal.scan_cycle_id,
            signal_id=signal.id,
            details=payload,
        )
        await session.commit()

    def build_preview(
        self,
        *,
        balance: Decimal | None = None,
        account_snapshot: AccountSnapshot | None = None,
        settings_map: dict[str, str],
        filters: SymbolFilters,
        entry_style: str = "LIMIT_GTD",
        direction: SignalDirection,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
        mark_price: Decimal | None = None,
        leverage_brackets: list[LeverageBracket] | None = None,
        risk_budget_override_usdt: Decimal | None = None,
        target_risk_usdt_override: Decimal | None = None,
        estimated_cost: Decimal | None = None,
        use_stop_distance_position_sizing: bool = False,
    ) -> dict[str, Any]:
        snapshot = account_snapshot or AccountSnapshot.from_available_balance(
            balance or Decimal("0"),
            reserve_fraction=self.BALANCE_RESERVE_FRACTION,
        )
        config = resolve_strategy_config(settings_map)
        effective_mark_price = mark_price or entry_price
        entry_step_size = self._entry_quantity_step(filters)
        leverage_cap = max(1, config.max_leverage)
        sl_distance_abs = abs(entry_price - stop_loss)
        slot_budget = self.build_shared_entry_slot_budget(
            available_balance=snapshot.available_balance,
            account_equity=snapshot.wallet_balance,
            committed_initial_margin=self._committed_initial_margin(snapshot),
            active_entry_order_count=0,
        )
        margin_budget_cap = (
            max(risk_budget_override_usdt, Decimal("0"))
            if risk_budget_override_usdt is not None
            else slot_budget.per_slot_budget
        )
        target_risk_usdt = (
            max(target_risk_usdt_override, Decimal("0"))
            if target_risk_usdt_override is not None
            else snapshot.available_balance * config.risk_per_trade_fraction
        )
        stop_risk_execution_cost, entry_fee_per_unit, exit_fee_per_unit, entry_slippage_per_unit, exit_slippage_per_unit = (
            self._estimated_stop_execution_cost_per_unit(
                settings_map=settings_map,
                entry_style=entry_style,
                entry_price=entry_price,
                stop_loss=stop_loss,
                estimated_cost=estimated_cost,
            )
        )
        stop_risk_distance_pct = (
            (sl_distance_abs + stop_risk_execution_cost) / entry_price
            if entry_price > 0
            else Decimal("0")
        )
        risk_budget_usdt = target_risk_usdt if use_stop_distance_position_sizing else margin_budget_cap
        affordable_budget_cap = min(snapshot.usable_balance, margin_budget_cap)

        def candidate_allowed(candidate_leverage: int, quantity: Decimal) -> bool:
            if quantity <= 0:
                return False
            allowed_cap = self._max_allowed_leverage(
                leverage_brackets=leverage_brackets,
                notional=quantity * effective_mark_price,
                settings_cap=leverage_cap,
            )
            return candidate_leverage <= allowed_cap

        if use_stop_distance_position_sizing:
            target_position_size_usdt = calculate_position_size_usdt(
                risk_budget_usdt=target_risk_usdt,
                stop_distance_pct=stop_risk_distance_pct,
            )
            requested_quantity = (
                round_to_increment(target_position_size_usdt / entry_price, entry_step_size)
                if target_position_size_usdt > 0 and entry_price > 0
                else Decimal("0")
            )
        else:
            requested_quantity = self._max_affordable_quantity(
                budget=affordable_budget_cap,
                mark_price=effective_mark_price,
                leverage=leverage_cap,
                step_size=entry_step_size,
            )

        requested_initial_margin = Decimal("0")
        requested_entry_fee = Decimal("0")
        max_affordable_quantity = Decimal("0")
        chosen_leverage = 1
        final_quantity = Decimal("0")
        requested_fits = False
        liquidation_blocked = False
        liquidation_safety = self._liquidation_safety(
            entry_price=entry_price,
            stop_loss=stop_loss,
            direction=direction,
            leverage=1,
            notional=max(filters.min_notional, self._entry_min_qty(filters) * max(effective_mark_price, Decimal("0"))),
            leverage_brackets=leverage_brackets,
        )

        for candidate_leverage in range(1, leverage_cap + 1):
            affordable_quantity = self._max_affordable_quantity(
                budget=affordable_budget_cap,
                mark_price=effective_mark_price,
                leverage=candidate_leverage,
                step_size=entry_step_size,
            )
            if candidate_allowed(candidate_leverage, affordable_quantity):
                max_affordable_quantity = max(max_affordable_quantity, affordable_quantity)
            if use_stop_distance_position_sizing:
                quantity = min(requested_quantity, affordable_quantity) if requested_quantity > 0 else Decimal("0")
            else:
                quantity = affordable_quantity
            if not candidate_allowed(candidate_leverage, quantity):
                continue
            entry_notional = quantity * entry_price
            if not self._entry_quantity_within_market_lot(filters=filters, quantity=quantity) or entry_notional < filters.min_notional:
                continue
            liquidation_safety = self._liquidation_safety(
                entry_price=entry_price,
                stop_loss=stop_loss,
                direction=direction,
                leverage=candidate_leverage,
                notional=entry_notional,
                leverage_brackets=leverage_brackets,
            )
            if not bool(liquidation_safety["ok"]):
                liquidation_blocked = True
                continue
            chosen_leverage = candidate_leverage
            final_quantity = quantity
            requested_fits = requested_quantity <= affordable_quantity if requested_quantity > 0 else True
            break

        leverage = chosen_leverage
        if requested_quantity > 0:
            requested_initial_margin = requested_quantity * effective_mark_price / Decimal(leverage)
            requested_entry_fee = requested_quantity * entry_fee_per_unit

        entry_notional = final_quantity * entry_price
        required_initial_margin = final_quantity * effective_mark_price / Decimal(leverage) if final_quantity > 0 else Decimal("0")
        estimated_entry_fee = final_quantity * entry_fee_per_unit
        estimated_exit_fee = final_quantity * exit_fee_per_unit
        estimated_slippage_burden = final_quantity * (entry_slippage_per_unit + exit_slippage_per_unit)
        stop_risk_execution_cost_total = final_quantity * stop_risk_execution_cost
        risk_usdt_at_stop = final_quantity * (sl_distance_abs + stop_risk_execution_cost)

        status = "affordable"
        auto_resized = False
        can_place = True
        max_qty = self._entry_max_qty(filters)

        if requested_quantity <= 0:
            status = "too_small_for_exchange"
            can_place = False
        elif max_qty is not None and requested_quantity > max_qty:
            status = "too_large_for_exchange"
            can_place = False
            auto_resized = False
        elif final_quantity <= 0:
            status = "not_affordable" if liquidation_blocked else "too_small_for_exchange"
            can_place = False
        elif not requested_fits:
            status = "resized_to_budget"
            auto_resized = True

        if final_quantity > 0 and (
            not self._entry_quantity_within_market_lot(filters=filters, quantity=final_quantity)
            or entry_notional < filters.min_notional
        ):
            if max_qty is not None and final_quantity > max_qty:
                status = "too_large_for_exchange"
            else:
                status = "too_small_for_exchange"
            can_place = False
            auto_resized = False

        if final_quantity > 0:
            liquidation_safety = self._liquidation_safety(
                entry_price=entry_price,
                stop_loss=stop_loss,
                direction=direction,
                leverage=leverage,
                notional=entry_notional,
                leverage_brackets=leverage_brackets,
            )

        reason = self._build_preview_reason(
            status=status,
            available_balance=snapshot.available_balance,
            slot_budget=margin_budget_cap,
            requested_initial_margin=requested_initial_margin,
            requested_entry_fee=requested_entry_fee,
            max_affordable_quantity=max_affordable_quantity,
            filters=filters,
            entry_notional=entry_notional,
            requested_quantity=requested_quantity,
        )
        if status == "not_affordable" and liquidation_blocked:
            reason = (
                f"{filters.symbol} order could not be placed because estimated liquidation at "
                f"{self._decimal_string(Decimal(liquidation_safety['liquidation_price']))} is too close to the stop-loss "
                f"after applying the current maintenance margin bracket."
            )

        return self._preview_payload(
            status=status,
            can_place=can_place,
            auto_resized=auto_resized,
            requested_quantity=requested_quantity,
            final_quantity=final_quantity,
            max_affordable_quantity=max_affordable_quantity,
            mark_price_used=effective_mark_price,
            entry_notional=entry_notional,
            required_initial_margin=required_initial_margin,
            estimated_entry_fee=estimated_entry_fee,
            estimated_exit_fee=estimated_exit_fee,
            estimated_slippage_burden=estimated_slippage_burden,
            stop_risk_execution_cost=stop_risk_execution_cost_total,
            available_balance=snapshot.available_balance,
            reserve_balance=snapshot.reserve_balance,
            usable_balance=snapshot.usable_balance,
            deployable_equity=slot_budget.deployable_equity,
            remaining_deployable_equity=slot_budget.remaining_deployable_equity,
            slot_budget=margin_budget_cap,
            risk_budget_usdt=risk_budget_usdt,
            risk_usdt_at_stop=risk_usdt_at_stop,
            recommended_leverage=leverage,
            liquidation_price=Decimal(liquidation_safety["liquidation_price"]),
            liquidation_gap_pct=Decimal(liquidation_safety["liquidation_gap_pct"]),
            required_liquidation_gap_pct=Decimal(liquidation_safety["required_gap_pct"]),
            maintenance_margin_ratio=Decimal(liquidation_safety["maintenance_margin_ratio"]),
            reason=reason,
        )

    async def approve_signal(
        self,
        session,
        *,
        signal_id: int,
        validity_hours: int | None = None,
        approved_by: str = "AUTO_MODE",
        risk_budget_override_usdt: Decimal | None = None,
        target_risk_usdt_override: Decimal | None = None,
        expires_at_override: datetime | None = None,
        settings_map_override: dict[str, str] | None = None,
        use_stop_distance_position_sizing: bool = False,
    ) -> Order:
        signal = await session.get(Signal, signal_id)
        if signal is None:
            raise ValueError("Signal not found")
        if signal.status in {SignalStatus.DISMISSED, SignalStatus.EXPIRED, SignalStatus.INVALIDATED}:
            raise ValueError("Signal is no longer actionable")
        latest_completed_scan_id = await self._latest_completed_scan_id(session)
        if latest_completed_scan_id is None or signal.scan_cycle_id != latest_completed_scan_id:
            raise ValueError("Only signals from the latest completed scan can be opened")

        credentials = await self.get_credentials(session)
        if credentials is None:
            raise ValueError("API credentials are required before placing live orders")

        settings_map = settings_map_override or await get_settings_map(session)
        metadata = self._signal_metadata(signal)
        account_snapshot = await self.get_account_snapshot(session, credentials)
        remote_open_position_symbols = await self.remote_open_position_symbols(credentials)
        slot_budget = await self.get_shared_entry_slot_budget(
            session,
            account_snapshot=account_snapshot,
        )
        effective_risk_budget_override = (
            risk_budget_override_usdt
            if risk_budget_override_usdt is not None
            else slot_budget.per_slot_budget
        )
        if signal.symbol.upper() in slot_budget.active_symbols:
            message = self._shared_entry_symbol_message(signal.symbol)
            await self._record_approval_failure(
                session,
                signal=signal,
                message=message,
                details={
                    "approved_by": approved_by,
                    "active_entry_order_count": slot_budget.active_entry_order_count,
                    "active_symbols": sorted(slot_budget.active_symbols),
                },
            )
            raise ValueError(message)
        if signal.symbol.upper() in remote_open_position_symbols:
            message = self._open_position_symbol_message(signal.symbol)
            await self._record_approval_failure(
                session,
                signal=signal,
                message=message,
                details={
                    "approved_by": approved_by,
                    "open_position_symbols": sorted(remote_open_position_symbols),
                },
            )
            raise ValueError(message)
        if slot_budget.remaining_entry_slots <= 0:
            message = self._shared_entry_slot_message(slot_budget.slot_cap)
            await self._record_approval_failure(
                session,
                signal=signal,
                message=message,
                details={
                    "approved_by": approved_by,
                    "active_entry_order_count": slot_budget.active_entry_order_count,
                    "slot_cap": slot_budget.slot_cap,
                },
            )
            raise ValueError(message)
        exchange_info = await self.gateway.exchange_info()
        filters = self.gateway.parse_symbol_filters(exchange_info)[signal.symbol]
        leverage_brackets = (await self.gateway.leverage_brackets(credentials, signal.symbol)).get(signal.symbol, [])
        mark_payload = await self.gateway.mark_price(signal.symbol)
        mark_price = Decimal(str(mark_payload.get("markPrice") or signal.current_price_at_signal or signal.entry_price))
        execution = self.build_execution_plan(
            symbol=signal.symbol,
            account_snapshot=account_snapshot,
            settings_map=settings_map,
            filters=filters,
            entry_style=str(metadata["entry_style"]),
            direction=signal.direction,
            entry_price=Decimal(signal.entry_price),
            stop_loss=Decimal(signal.stop_loss),
            take_profit=Decimal(signal.take_profit),
            mark_price=mark_price,
            leverage_brackets=leverage_brackets,
            risk_budget_override_usdt=effective_risk_budget_override,
            target_risk_usdt_override=target_risk_usdt_override,
            estimated_cost=Decimal(str(metadata["estimated_cost"] or "0")),
            use_stop_distance_position_sizing=use_stop_distance_position_sizing,
        )
        if execution.get("error"):
            raise ValueError(self._preview_error_message(signal.symbol, execution))
        preview = execution["order_preview"]
        if not preview["can_place"]:
            message = self._preview_error_message(signal.symbol, execution)
            await self._record_approval_failure(
                session,
                signal=signal,
                message=message,
                details={"order_preview": preview, "approved_by": approved_by},
            )
            raise ValueError(message)

        entry_price = execution["entry_price"]
        stop_loss = execution["stop_loss"]
        take_profit = execution["take_profit"]
        market_state = execution.get("market_state")
        if market_state is not None and market_state.stale_reason is not None and market_state.message is not None:
            await self._record_approval_failure(
                session,
                signal=signal,
                message=market_state.message,
                details={
                    "approved_by": approved_by,
                    "order_preview": preview,
                    "mark_price": self._decimal_string(mark_price),
                    "execution_prices": {
                        "entry_price": self._decimal_string(entry_price),
                        "stop_loss": self._decimal_string(stop_loss),
                        "take_profit": self._decimal_string(take_profit),
                    },
                    "stale_reason": market_state.stale_reason,
                },
            )
            raise ValueError(market_state.message)

        quantity = Decimal(preview["final_quantity"])
        position_margin = Decimal(preview["required_initial_margin"])
        notional_value = Decimal(preview["entry_notional"])
        risk_budget_usdt = Decimal(preview["risk_budget_usdt"])
        risk_usdt_at_stop = Decimal(preview["risk_usdt_at_stop"])
        risk_pct_of_wallet = self.risk_pct_of_wallet(
            available_balance=account_snapshot.available_balance,
            risk_usdt_at_stop=risk_usdt_at_stop,
        )
        leverage = int(preview["recommended_leverage"])
        partial_tp_requested = self._partial_tp_requested(settings_map, approved_by=approved_by)
        partial_tp_split = None
        partial_tp_enabled = False
        take_profit_1: Decimal | None = None
        take_profit_2: Decimal | None = None
        if partial_tp_requested:
            partial_tp_targets = calculate_partial_take_profit_targets(
                direction=signal.direction,
                entry_price=entry_price,
                stop_loss_price=stop_loss,
            )
            take_profit_1 = self._normalize_take_profit_price(
                direction=signal.direction,
                filters=filters,
                take_profit=partial_tp_targets.tp1_price,
            )
            take_profit_2 = self._normalize_take_profit_price(
                direction=signal.direction,
                filters=filters,
                take_profit=partial_tp_targets.tp2_price,
            )
            partial_tp_split = split_partial_take_profit_quantity(
                total_quantity=quantity,
                step_size=self._entry_quantity_step(filters),
                min_qty=self._entry_min_qty(filters),
            )
            partial_tp_enabled = partial_tp_split is not None and take_profit_1 is not None and take_profit_2 is not None and self._valid_partial_take_profit_prices(
                direction=signal.direction,
                entry_price=entry_price,
                tp1_price=take_profit_1,
                tp2_price=take_profit_2,
            )
            if not partial_tp_enabled:
                await record_audit(
                    session,
                    event_type="ORDER_PARTIAL_TP_FALLBACK",
                    level=AuditLevel.WARNING,
                    message=f"{signal.symbol} partial TP could not be split cleanly and fell back to single TP",
                    symbol=signal.symbol,
                    scan_cycle_id=signal.scan_cycle_id,
                    signal_id=signal.id,
                    details={
                        "reason": "partial_tp_split_unavailable_fallback",
                        "requested_quantity": self._decimal_string(quantity),
                        "step_size": self._decimal_string(filters.step_size),
                        "min_qty": self._decimal_string(filters.min_qty),
                    },
                )
                take_profit_1 = None
                take_profit_2 = None
                partial_tp_split = None
        if partial_tp_enabled and take_profit_2 is not None:
            take_profit = take_profit_2
            partial_market_state = self.validate_market_state(
                symbol=signal.symbol,
                filters=filters,
                entry_style=str(metadata["entry_style"]),
                direction=signal.direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                mark_price=mark_price,
            )
            if partial_market_state.error or (
                partial_market_state.stale_reason is not None and partial_market_state.message is not None
            ):
                message = (
                    partial_market_state.message
                    if partial_market_state.message is not None
                    else self._preview_error_message(
                        signal.symbol,
                        {"error": partial_market_state.error, "order_preview": preview},
                    )
                )
                await self._record_approval_failure(
                    session,
                    signal=signal,
                    message=message,
                    details={
                        "approved_by": approved_by,
                        "order_preview": preview,
                        "partial_tp_enabled": True,
                        "take_profit_1": self._decimal_string(take_profit_1 or Decimal("0")),
                        "take_profit_2": self._decimal_string(take_profit),
                    },
                )
                raise ValueError(message)

        side = "BUY" if signal.direction == SignalDirection.LONG else "SELL"
        exit_side = "SELL" if signal.direction == SignalDirection.LONG else "BUY"
        expires_at = expires_at_override or signal.expires_at or (
            datetime.now(timezone.utc) + timedelta(minutes=45)
        )
        expires_at = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=timezone.utc)
        expires_at = expires_at.astimezone(timezone.utc)
        entry_style = str(metadata["entry_style"])
        entry_gtd_requested = entry_style != "STOP_ENTRY"
        entry_good_till_ms = self._entry_good_till_date_ms(expires_at=expires_at) if entry_gtd_requested else None
        exchange_gtd_enabled = entry_good_till_ms is not None and entry_gtd_requested
        order = Order(
            signal_id=signal.id,
            symbol=signal.symbol,
            direction=signal.direction,
            leverage=leverage,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            rank_value=Decimal(str(metadata["rank_value"])) if metadata["rank_value"] is not None else None,
            net_r_multiple=Decimal(str(metadata["net_r_multiple"])) if metadata["net_r_multiple"] is not None else None,
            estimated_cost=Decimal(str(metadata["estimated_cost"])) if metadata["estimated_cost"] is not None else None,
            entry_style=metadata["entry_style"],
            setup_family=metadata["setup_family"],
            setup_variant=metadata["setup_variant"],
            market_state=metadata["market_state"],
            execution_tier=metadata["execution_tier"],
            score_band=metadata["score_band"],
            volatility_band=metadata["volatility_band"],
            stats_bucket_key=metadata["stats_bucket_key"],
            strategy_context=metadata["strategy_context"],
            partial_tp_enabled=partial_tp_enabled,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2 if partial_tp_enabled else None,
            tp_quantity_1=partial_tp_split.tp1_quantity if partial_tp_enabled and partial_tp_split is not None else None,
            tp_quantity_2=partial_tp_split.tp2_quantity if partial_tp_enabled and partial_tp_split is not None else None,
            quantity=quantity,
            position_margin=position_margin,
            notional_value=notional_value,
            rr_ratio=signal.rr_ratio,
            risk_budget_usdt=risk_budget_usdt,
            risk_usdt_at_stop=risk_usdt_at_stop,
            risk_pct_of_wallet=risk_pct_of_wallet,
            remaining_quantity=quantity,
            status=OrderStatus.SUBMITTING,
            expires_at=expires_at,
            approved_by=approved_by,
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        self._update_strategy_context(
            order,
            entry_expiry_at=expires_at.isoformat(),
            entry_expiry_epoch_ms=str(self._utc_timestamp_millis(expires_at)),
            entry_gtd_requested="true" if entry_gtd_requested else "false",
            entry_expiry_control="exchange_gtd" if exchange_gtd_enabled else "internal_timer",
            entry_exchange_good_till_ms=(str(entry_good_till_ms) if exchange_gtd_enabled and entry_good_till_ms is not None else None),
            entry_gtd_fallback_error=None,
        )
        await session.commit()

        entry_client_id = self._managed_entry_client_id(order)
        tp_client_id = self._managed_tp_client_id(order)
        tp1_client_id = self._managed_tp1_client_id(order)
        tp2_client_id = self._managed_tp2_client_id(order)
        sl_client_id = self._managed_sl_client_id(order)
        created_remote_orders: list[RemoteOrderRef] = []
        entry_submission_details = self._entry_submission_details(
            entry_style=entry_style,
            entry_price=entry_price,
            quantity=quantity,
            expires_at=expires_at,
            exchange_gtd_enabled=exchange_gtd_enabled,
            exchange_good_till_ms=entry_good_till_ms if exchange_gtd_enabled else None,
        )
        safe_recalc_attempted = False
        safe_recalc_reason: str | None = None

        try:
            await self._ensure_one_way_position_mode(credentials, symbol=signal.symbol)
            await self.gateway.change_margin_type(credentials, signal.symbol, "ISOLATED")
            await self.gateway.change_leverage(credentials, signal.symbol, leverage)

            current_signature = self._execution_signature(
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                quantity=quantity,
                leverage=leverage,
            )
            while True:
                try:
                    if entry_style == "STOP_ENTRY":
                        entry_order = await self.gateway.place_algo_order(
                            credentials,
                            self._stop_entry_algo_params(
                                signal=signal,
                                quantity=quantity,
                                side=side,
                                entry_price=entry_price,
                                entry_client_id=entry_client_id,
                            ),
                        )
                        entry_order_id = self._remote_id(entry_order.get("algoId"))
                        entry_remote_kind = "algo"
                    else:
                        try:
                            entry_order = await self.gateway.place_order(
                                credentials,
                                self._entry_order_params(
                                    signal=signal,
                                    quantity=quantity,
                                    side=side,
                                    entry_price=entry_price,
                                    entry_client_id=entry_client_id,
                                    entry_style=entry_style,
                                    expires_at=expires_at,
                                    exchange_gtd_enabled=exchange_gtd_enabled,
                                ),
                            )
                        except BinanceAPIError as exc:
                            if not exchange_gtd_enabled or not self._is_gtd_unsupported_error(exc):
                                raise
                            exchange_gtd_enabled = False
                            entry_submission_details = self._entry_submission_details(
                                entry_style=entry_style,
                                entry_price=entry_price,
                                quantity=quantity,
                                expires_at=expires_at,
                                exchange_gtd_enabled=False,
                                exchange_good_till_ms=None,
                            )
                            self._update_strategy_context(
                                order,
                                entry_expiry_control="internal_timer",
                                entry_exchange_good_till_ms=None,
                                entry_gtd_fallback_error=self._format_exchange_message(exc),
                            )
                            await session.commit()
                            entry_order = await self.gateway.place_order(
                                credentials,
                                self._entry_order_params(
                                    signal=signal,
                                    quantity=quantity,
                                    side=side,
                                    entry_price=entry_price,
                                    entry_client_id=entry_client_id,
                                    entry_style=entry_style,
                                    expires_at=expires_at,
                                    exchange_gtd_enabled=False,
                                ),
                            )
                        entry_order_id = self._remote_id(entry_order.get("orderId"))
                        entry_remote_kind = "standard"
                    break
                except BinanceAPIError as exc:
                    if safe_recalc_attempted:
                        raise
                    recalculated = await self._safe_recalculated_execution(
                        session,
                        credentials=credentials,
                        signal=signal,
                        settings_map=settings_map,
                        metadata=metadata,
                        risk_budget_override_usdt=effective_risk_budget_override,
                        target_risk_usdt_override=target_risk_usdt_override,
                        use_stop_distance_position_sizing=use_stop_distance_position_sizing,
                        current_signature=current_signature,
                        exc=exc,
                    )
                    safe_recalc_reason = recalculated.get("reason")
                    if not recalculated.get("retryable"):
                        if safe_recalc_reason is not None:
                            await record_audit(
                                session,
                                event_type="ORDER_SUBMISSION_RECALC_REJECTED",
                                level=AuditLevel.WARNING,
                                message=f"{signal.symbol} exchange rejection could not be safely recalculated",
                                symbol=signal.symbol,
                                scan_cycle_id=signal.scan_cycle_id,
                                order_id=order.id,
                                signal_id=signal.id,
                                details={
                                    "exchange_rejection_reason": safe_recalc_reason,
                                    "failure_reason": recalculated.get("failure_reason"),
                                    "binance_message": self._format_exchange_message(exc),
                                    "order_preview": (
                                        (recalculated.get("execution") or {}).get("order_preview")
                                        if isinstance(recalculated.get("execution"), dict)
                                        else preview
                                    ),
                                },
                            )
                            await session.commit()
                        raise

                    safe_recalc_attempted = True
                    execution = recalculated["execution"]
                    preview = execution["order_preview"]
                    account_snapshot = recalculated["account_snapshot"]
                    filters = recalculated["filters"]
                    leverage_brackets = recalculated["leverage_brackets"]
                    mark_price = recalculated["mark_price"]
                    entry_price = execution["entry_price"]
                    stop_loss = execution["stop_loss"]
                    take_profit = execution["take_profit"]
                    quantity = Decimal(preview["final_quantity"])
                    position_margin = Decimal(preview["required_initial_margin"])
                    notional_value = Decimal(preview["entry_notional"])
                    risk_budget_usdt = Decimal(preview["risk_budget_usdt"])
                    risk_usdt_at_stop = Decimal(preview["risk_usdt_at_stop"])
                    risk_pct_of_wallet = self.risk_pct_of_wallet(
                        available_balance=account_snapshot.available_balance,
                        risk_usdt_at_stop=risk_usdt_at_stop,
                    )
                    leverage = int(preview["recommended_leverage"])
                    order.leverage = leverage
                    order.entry_price = entry_price
                    order.stop_loss = stop_loss
                    order.take_profit = take_profit
                    order.quantity = quantity
                    order.remaining_quantity = quantity
                    order.position_margin = position_margin
                    order.notional_value = notional_value
                    order.risk_budget_usdt = risk_budget_usdt
                    order.risk_usdt_at_stop = risk_usdt_at_stop
                    order.risk_pct_of_wallet = risk_pct_of_wallet
                    signal.extra_context = {**signal.extra_context, "order_preview": preview}
                    current_signature = self._execution_signature(
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        quantity=quantity,
                        leverage=leverage,
                    )
                    entry_submission_details = self._entry_submission_details(
                        entry_style=entry_style,
                        entry_price=entry_price,
                        quantity=quantity,
                        expires_at=expires_at,
                        exchange_gtd_enabled=exchange_gtd_enabled,
                        exchange_good_till_ms=entry_good_till_ms if exchange_gtd_enabled else None,
                    )
                    await self.gateway.change_leverage(credentials, signal.symbol, leverage)
                    await record_audit(
                        session,
                        event_type="ORDER_SUBMISSION_RECALCULATED",
                        level=AuditLevel.INFO,
                        message=f"{signal.symbol} exchange rejection triggered one safe AQRR recalculation",
                        symbol=signal.symbol,
                        scan_cycle_id=signal.scan_cycle_id,
                        order_id=order.id,
                        signal_id=signal.id,
                        details={
                            "exchange_rejection_reason": safe_recalc_reason,
                            "order_preview": preview,
                            "binance_message": self._format_exchange_message(exc),
                        },
                    )
                    await session.commit()
            if entry_order_id is None:
                raise ValueError("Binance entry order response did not include a remote entry order id")
            created_remote_orders.append(RemoteOrderRef(order_id=entry_order_id, role="entry", kind=entry_remote_kind))
            order.entry_order_id = entry_order_id
            if entry_style != "STOP_ENTRY":
                self._update_strategy_context(
                    order,
                    entry_expiry_control="exchange_gtd" if exchange_gtd_enabled else "internal_timer",
                    entry_exchange_good_till_ms=(str(entry_good_till_ms) if exchange_gtd_enabled and entry_good_till_ms is not None else None),
                )
            await session.commit()
            if partial_tp_enabled and partial_tp_split is not None and take_profit_1 is not None and take_profit_2 is not None:
                tp1_order = await self.gateway.place_algo_order(
                    credentials,
                    {
                        "algoType": "CONDITIONAL",
                        "symbol": signal.symbol,
                        "side": exit_side,
                        "type": "TAKE_PROFIT",
                        "quantity": str(partial_tp_split.tp1_quantity),
                        "triggerPrice": str(take_profit_1),
                        "price": str(take_profit_1),
                        "reduceOnly": "true",
                        "workingType": "MARK_PRICE",
                        "clientAlgoId": tp1_client_id,
                    },
                )
                tp1_order_id = self._remote_id(tp1_order.get("algoId"))
                if tp1_order_id is None:
                    raise ValueError("Binance partial take-profit order response did not include algoId")
                created_remote_orders.append(RemoteOrderRef(order_id=tp1_order_id, role="tp1", kind="algo"))
                order.tp_order_1_id = tp1_order_id
                await session.commit()

                tp2_order = await self.gateway.place_algo_order(
                    credentials,
                    {
                        "algoType": "CONDITIONAL",
                        "symbol": signal.symbol,
                        "side": exit_side,
                        "type": "TAKE_PROFIT",
                        "quantity": str(partial_tp_split.tp2_quantity),
                        "triggerPrice": str(take_profit_2),
                        "price": str(take_profit_2),
                        "reduceOnly": "true",
                        "workingType": "MARK_PRICE",
                        "clientAlgoId": tp2_client_id,
                    },
                )
                tp2_order_id = self._remote_id(tp2_order.get("algoId"))
                if tp2_order_id is None:
                    raise ValueError("Binance partial take-profit order response did not include algoId")
                created_remote_orders.append(RemoteOrderRef(order_id=tp2_order_id, role="tp2", kind="algo"))
                order.tp_order_2_id = tp2_order_id
                order.tp_order_id = tp2_order_id
            else:
                tp_order = await self.gateway.place_algo_order(
                    credentials,
                    {
                        "algoType": "CONDITIONAL",
                        "symbol": signal.symbol,
                        "side": exit_side,
                        "type": "TAKE_PROFIT_MARKET",
                        "quantity": str(quantity),
                        "triggerPrice": str(take_profit),
                        "reduceOnly": "true",
                        "workingType": "MARK_PRICE",
                        "clientAlgoId": tp_client_id,
                    },
                )
                tp_order_id = self._remote_id(tp_order.get("algoId"))
                if tp_order_id is None:
                    raise ValueError("Binance take-profit algo order response did not include algoId")
                created_remote_orders.append(RemoteOrderRef(order_id=tp_order_id, role="tp", kind="algo"))
                order.tp_order_id = tp_order_id
            await session.commit()
            sl_order = await self.gateway.place_algo_order(
                credentials,
                {
                    "algoType": "CONDITIONAL",
                    "symbol": signal.symbol,
                    "side": exit_side,
                    "type": "STOP_MARKET",
                    "quantity": str(quantity),
                    "triggerPrice": str(stop_loss),
                    "reduceOnly": "true",
                    "workingType": "MARK_PRICE",
                    "clientAlgoId": sl_client_id,
                },
            )
            sl_order_id = self._remote_id(sl_order.get("algoId"))
            if sl_order_id is None:
                raise ValueError("Binance stop-loss algo order response did not include algoId")
            created_remote_orders.append(RemoteOrderRef(order_id=sl_order_id, role="sl", kind="algo"))
            order.sl_order_id = sl_order_id
            self._update_strategy_context(
                order,
                protection_quantity=self._decimal_string(quantity),
            )
        except OrderApprovalExchangeError as exc:
            recovery = await self._attempt_authoritative_entry_recovery(
                session,
                credentials=credentials,
                order=order,
                scan_cycle_id=signal.scan_cycle_id,
                protections_confirmed=False,
                inconclusive_message=f"{signal.symbol} authoritative recovery remained inconclusive after order submission exchange failure",
                extra_details={
                    "approved_by": approved_by,
                    "order_preview": preview,
                    "binance_message": exc.message,
                    "safe_recalc_attempted": safe_recalc_attempted,
                    "safe_recalc_reason": safe_recalc_reason,
                    **entry_submission_details,
                },
            )
            if recovery.outcome == self.RECOVERY_OUTCOME_CONFIRMED_NONE:
                await self._mark_submission_failed(
                    session,
                    order=order,
                    scan_cycle_id=signal.scan_cycle_id,
                    reason="submission_failed",
                    message=exc.message,
                    details={
                        "approved_by": approved_by,
                        "order_preview": preview,
                        "binance_message": exc.message,
                        "safe_recalc_attempted": safe_recalc_attempted,
                        "safe_recalc_reason": safe_recalc_reason,
                        **entry_submission_details,
                        **recovery.details,
                    },
                )
            await session.commit()
            raise
        except BinanceAPIError as exc:
            latest_preview = preview
            if exc.code == -2019:
                refreshed_snapshot = await self.get_account_snapshot(session, credentials)
                refreshed_mark = await self.gateway.mark_price(signal.symbol)
                refreshed_execution = self.build_execution_plan(
                    symbol=signal.symbol,
                    account_snapshot=refreshed_snapshot,
                    settings_map=settings_map,
                    filters=filters,
                    entry_style=str(metadata["entry_style"]),
                    direction=signal.direction,
                    entry_price=Decimal(signal.entry_price),
                    stop_loss=Decimal(signal.stop_loss),
                    take_profit=Decimal(signal.take_profit),
                    mark_price=Decimal(str(refreshed_mark.get("markPrice") or mark_price)),
                    leverage_brackets=leverage_brackets,
                    risk_budget_override_usdt=effective_risk_budget_override,
                    target_risk_usdt_override=target_risk_usdt_override,
                    estimated_cost=Decimal(str(metadata["estimated_cost"] or "0")),
                    use_stop_distance_position_sizing=use_stop_distance_position_sizing,
                )
                latest_preview = refreshed_execution.get("order_preview") or preview
            recovery = await self._attempt_authoritative_entry_recovery(
                session,
                credentials=credentials,
                order=order,
                scan_cycle_id=signal.scan_cycle_id,
                protections_confirmed=False,
                inconclusive_message=f"{signal.symbol} authoritative recovery remained inconclusive after Binance submission failure",
                extra_details={
                    "approved_by": approved_by,
                    "binance_code": exc.code,
                    "binance_message": exc.exchange_message,
                    "safe_recalc_attempted": safe_recalc_attempted,
                    "safe_recalc_reason": safe_recalc_reason,
                    "order_preview": latest_preview,
                    **entry_submission_details,
                },
            )
            if recovery.outcome == self.RECOVERY_OUTCOME_CONFIRMED_NONE:
                cleanup_failures = await self._cleanup_remote_orders(credentials, signal.symbol, created_remote_orders)
                await self._mark_submission_failed(
                    session,
                    order=order,
                    scan_cycle_id=signal.scan_cycle_id,
                    reason="submission_failed",
                    message=self._format_exchange_message(exc),
                    details={
                        "approved_by": approved_by,
                        "reason": "submission_failed",
                        "binance_code": exc.code,
                        "binance_message": exc.exchange_message,
                        "safe_recalc_attempted": safe_recalc_attempted,
                        "safe_recalc_reason": safe_recalc_reason,
                        "cleanup_failures": cleanup_failures,
                        "order_preview": latest_preview,
                        **entry_submission_details,
                        **recovery.details,
                    },
                )
            await session.commit()
            raise self._build_exchange_error(
                signal.symbol,
                exc,
                preview=latest_preview,
                cleanup_failures=(
                    cleanup_failures if recovery.outcome == self.RECOVERY_OUTCOME_CONFIRMED_NONE else None
                ),
            ) from exc
        except Exception as exc:
            recovery = await self._attempt_authoritative_entry_recovery(
                session,
                credentials=credentials,
                order=order,
                scan_cycle_id=signal.scan_cycle_id,
                protections_confirmed=False,
                inconclusive_message=f"{signal.symbol} authoritative recovery remained inconclusive after unexpected submission failure",
                extra_details={
                    "approved_by": approved_by,
                    "error": str(exc),
                    "safe_recalc_attempted": safe_recalc_attempted,
                    "safe_recalc_reason": safe_recalc_reason,
                    **entry_submission_details,
                },
            )
            if recovery.outcome == self.RECOVERY_OUTCOME_CONFIRMED_NONE:
                cleanup_failures = await self._cleanup_remote_orders(credentials, signal.symbol, created_remote_orders)
                await self._mark_submission_failed(
                    session,
                    order=order,
                    scan_cycle_id=signal.scan_cycle_id,
                    reason="submission_failed",
                    message=f"{signal.symbol} order submission failed",
                    details={
                        "approved_by": approved_by,
                        "reason": "submission_failed",
                        "cleanup_failures": cleanup_failures,
                        "error": str(exc),
                        "safe_recalc_attempted": safe_recalc_attempted,
                        "safe_recalc_reason": safe_recalc_reason,
                        **entry_submission_details,
                        **recovery.details,
                    },
                )
            await session.commit()
            raise

        order.status = OrderStatus.ORDER_PLACED
        order.placed_at = datetime.now(timezone.utc)
        signal.status = SignalStatus.APPROVED
        order_preview_payload = {
            **preview,
            "partial_tp_enabled": partial_tp_enabled,
            "entry_style": metadata["entry_style"],
            "take_profit_1": self._decimal_string(take_profit_1) if take_profit_1 is not None else None,
            "take_profit_2": self._decimal_string(Decimal(order.take_profit_2)) if order.take_profit_2 is not None else None,
            "tp_quantity_1": self._decimal_string(Decimal(order.tp_quantity_1)) if order.tp_quantity_1 is not None else None,
            "tp_quantity_2": self._decimal_string(Decimal(order.tp_quantity_2)) if order.tp_quantity_2 is not None else None,
        }
        signal.extra_context = {**signal.extra_context, "order_preview": order_preview_payload}
        await record_audit(
            session,
            event_type="ORDER_PLACED",
            message=f"{signal.symbol} order placed",
            symbol=signal.symbol,
            scan_cycle_id=signal.scan_cycle_id,
            signal_id=signal.id,
            details={
                "approved_by": approved_by,
                "entry_order_id": order.entry_order_id,
                "order_preview": order_preview_payload,
                **entry_submission_details,
            },
        )
        await session.commit()
        await session.refresh(order)
        await self.ws_manager.broadcast("order_status_change", {"order_id": order.id, "status": order.status.value})
        await self.notifier.send(title="Order Placed", message=f"{signal.symbol} {signal.direction.value} at {entry_price}", sound="placed")
        return order

    async def _flatten_live_order(
        self,
        session,
        *,
        credentials: ApiCredentials,
        order: Order,
        scan_cycle_id: int | None,
        reason: str,
        reason_context: dict[str, Any] | None = None,
    ) -> Order:
        default_reason_context = await self._signal_reason_context(
            session,
            signal_id=order.signal_id,
            setup_family=str(getattr(order, "setup_family", None) or "") or None,
            entry_style=str(getattr(order, "entry_style", None) or "") or None,
        )

        close_quantity_resolution = await self._resolve_authoritative_close_quantity(credentials, order)
        if close_quantity_resolution.outcome == self.RECOVERY_OUTCOME_INCONCLUSIVE:
            await self._record_authoritative_recovery_inconclusive(
                session,
                order=order,
                scan_cycle_id=scan_cycle_id,
                message=f"{order.symbol} live close quantity could not be confirmed authoritatively",
                details=self._reason_details(
                    reason="authoritative_close_quantity_unconfirmed",
                    reason_context={
                        **(reason_context or {}),
                        **close_quantity_resolution.details,
                        "lifecycle_reason": "authoritative_close_quantity_unconfirmed",
                    },
                    default_context=default_reason_context,
                ),
            )
            return order

        remaining_quantity = close_quantity_resolution.quantity
        if close_quantity_resolution.outcome == self.RECOVERY_OUTCOME_CONFIRMED_NONE or remaining_quantity <= 0:
            logger.warning(
                "flatten_live_order.no_authoritative_live_exposure",
                order_id=order.id,
                symbol=order.symbol,
                reason=reason,
                details=close_quantity_resolution.details,
            )
            return order

        filters = self.gateway.parse_symbol_filters(await self.gateway.exchange_info()).get(order.symbol)
        if filters is not None:
            remaining_quantity = round_to_increment(remaining_quantity, filters.step_size)
        if remaining_quantity <= 0:
            raise ValueError("Remaining position quantity is below the exchange step size")

        try:
            await self._cancel_entry_remainder_if_partial(credentials, order)
        except Exception as exc:
            logger.warning(
                "flatten_live_order.entry_remainder_cancel_failed",
                order_id=order.id,
                symbol=order.symbol,
                error=str(exc),
            )

        cleanup_failures: list[dict[str, str]] = []
        for remote_order in self._known_remote_refs(order):
            if remote_order.role == "entry":
                continue
            try:
                await self._cancel_known_remote_order(credentials, order.symbol, remote_order)
            except Exception as exc:
                logger.warning(
                    "flatten_live_order.remote_cleanup_failed",
                    symbol=order.symbol,
                    order_id=remote_order.order_id,
                    role=remote_order.role,
                    error=str(exc),
                )
                cleanup_failures.append(
                    {
                        "order_id": remote_order.order_id,
                        "role": remote_order.role,
                        "kind": remote_order.kind,
                        "error": str(exc),
                    }
                )

        close_side = "SELL" if order.direction == SignalDirection.LONG else "BUY"
        close_client_id = self._managed_client_tag(order, "close")
        close_order = await self.gateway.place_order(
            credentials,
            {
                "symbol": order.symbol,
                "side": close_side,
                "type": "MARKET",
                "quantity": self._decimal_string(remaining_quantity),
                "reduceOnly": "true",
                "newClientOrderId": close_client_id,
            },
        )
        actual_order_id = self._remote_id(close_order.get("orderId"))
        fill_snapshot = await self._exchange_fill_snapshot(
            credentials,
            order,
            actual_order_id=actual_order_id,
            fallback_price=Decimal(order.close_price or order.entry_price),
            fallback_quantity=remaining_quantity,
            order_state=close_order,
        )
        await self._close_order_from_snapshot(
            session,
            order=order,
            closed_status=OrderStatus.CLOSED_BY_BOT,
            close_type="BOT",
            event_type="ORDER_CLOSED_BY_BOT",
            event_message=f"{order.symbol} closed by AQRR protection",
            scan_cycle_id=scan_cycle_id,
            fill_snapshot=fill_snapshot,
            extra_details={
                "remote_close_order_id": actual_order_id,
                "cleanup_failures": cleanup_failures,
                **self._reason_details(
                    reason=reason,
                    reason_context=reason_context,
                    default_context=default_reason_context,
                ),
            },
            notify_title="Position Closed",
            notify_message=f"{order.symbol} closed by bot protection",
            sound="sl",
        )
        await session.commit()
        await session.refresh(order)
        await self.ws_manager.broadcast("order_status_change", {"order_id": order.id, "status": order.status.value})
        return order

    async def close_position(
        self,
        session,
        *,
        order_id: int,
        reason: str = "auto_mode_invalidated",
        reason_context: dict[str, Any] | None = None,
    ) -> Order:
        order = await session.get(Order, order_id)
        if order is None:
            raise ValueError("Order not found")

        synced_order = await self.sync_order(session, order)
        if synced_order.status != OrderStatus.IN_POSITION:
            return synced_order

        credentials = await self.get_credentials(session)
        if credentials is None:
            raise ValueError("API credentials missing")

        scan_cycle_id = await self._scan_cycle_id_for_signal(session, signal_id=synced_order.signal_id)
        try:
            await self._sync_live_entry_state(
                session,
                credentials,
                synced_order,
                scan_cycle_id=scan_cycle_id,
            )
        except Exception as exc:
            logger.warning(
                "close_position.live_entry_sync_failed",
                order_id=synced_order.id,
                symbol=synced_order.symbol,
                error=str(exc),
            )
        if synced_order.status != OrderStatus.IN_POSITION:
            return synced_order

        return await self._flatten_live_order(
            session,
            credentials=credentials,
            order=synced_order,
            scan_cycle_id=scan_cycle_id,
            reason=reason,
            reason_context=reason_context,
        )

    async def cancel_order(
        self,
        session,
        *,
        order_id: int,
        reason: str = "manual_cancel",
        reason_context: dict[str, Any] | None = None,
    ) -> Order:
        order = await session.get(Order, order_id)
        if order is None:
            raise ValueError("Order not found")
        if order.status != OrderStatus.ORDER_PLACED:
            raise ValueError("Only pending entry orders can be cancelled")
        resolved_reason = self._normalize_pending_cancel_reason(reason)
        scan_cycle_id = await self._scan_cycle_id_for_signal(session, signal_id=order.signal_id)
        credentials = await self.get_credentials(session)
        if credentials is None:
            raise ValueError("API credentials missing")
        default_reason_context = await self._signal_reason_context(
            session,
            signal_id=order.signal_id,
            setup_family=str(getattr(order, "setup_family", None) or "") or None,
            entry_style=str(getattr(order, "entry_style", None) or "") or None,
        )
        entry_resolution = await self._query_entry_order_state_resolution(credentials, order)
        recovery = await self._attempt_authoritative_entry_recovery(
            session,
            credentials=credentials,
            order=order,
            scan_cycle_id=scan_cycle_id,
            entry_resolution=entry_resolution,
            protections_confirmed=self._protection_refs_present(order),
            inconclusive_message=f"{order.symbol} pending entry cancellation was blocked because authoritative recovery remained inconclusive",
            extra_details={"cancel_reason": resolved_reason},
        )
        if recovery.outcome == self.RECOVERY_OUTCOME_RECOVERED or order.status != OrderStatus.ORDER_PLACED:
            await session.commit()
            await self.ws_manager.broadcast("order_status_change", {"order_id": order.id, "status": order.status.value})
            return order
        if recovery.outcome == self.RECOVERY_OUTCOME_INCONCLUSIVE:
            recovery_context = {
                "cancel_reason": resolved_reason,
                **(recovery.details or {}),
            }
            if resolved_reason == "expired" and self.pending_entry_expired(order):
                await self._apply_uncertain_expired_terminal_resolution(
                    session,
                    credentials=credentials,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    reason_context=recovery_context,
                    default_reason_context=default_reason_context,
                    message=f"{order.symbol} pending entry expired and was terminally cancelled because authoritative recovery remained inconclusive",
                )
                await session.commit()
                await self.ws_manager.broadcast("order_status_change", {"order_id": order.id, "status": order.status.value})
                return order
            await session.commit()
            return order
        resolved_reason_context = dict(reason_context or {})
        if resolved_reason != str(reason):
            resolved_reason_context.setdefault("legacy_reason", str(reason))
        resolved_reason_context.setdefault("authoritative_recovery_outcome", recovery.outcome)

        remote_orders = self._known_remote_refs(order)
        for remote_order in remote_orders:
            try:
                if remote_order.role == "entry":
                    await self._cancel_entry_order(credentials, order)
                else:
                    await self._cancel_protective_order(credentials, order.symbol, remote_order.order_id)
            except Exception as exc:
                logger.warning(
                    "cancel_order.remote_failed",
                    symbol=order.symbol,
                    order_id=remote_order.order_id,
                    role=remote_order.role,
                    error=str(exc),
                )

        order.status = OrderStatus.CANCELLED_BY_USER if resolved_reason == "manual_cancel" else OrderStatus.CANCELLED_BY_BOT
        order.cancel_reason = resolved_reason
        order.cancelled_at = datetime.now(timezone.utc)
        await record_audit(
            session,
            event_type="ORDER_CANCELLED",
            level=AuditLevel.INFO,
            message=f"{order.symbol} order cancelled",
            order_id=order.id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            scan_cycle_id=scan_cycle_id,
            details=self._reason_details(
                reason=resolved_reason,
                reason_context=resolved_reason_context,
                default_context=default_reason_context,
            ),
        )
        await session.commit()
        await self.ws_manager.broadcast("order_status_change", {"order_id": order.id, "status": order.status.value})
        return order

    def _closed_status_for_fill_snapshot(self, order: Order, fill_snapshot: ExchangeFillSnapshot) -> OrderStatus:
        if fill_snapshot.realized_pnl > 0:
            return OrderStatus.CLOSED_WIN
        if fill_snapshot.realized_pnl < 0:
            return OrderStatus.CLOSED_LOSS
        if order.direction == SignalDirection.LONG:
            return OrderStatus.CLOSED_WIN if fill_snapshot.close_price >= Decimal(order.entry_price) else OrderStatus.CLOSED_LOSS
        return OrderStatus.CLOSED_WIN if fill_snapshot.close_price <= Decimal(order.entry_price) else OrderStatus.CLOSED_LOSS

    async def recover_closed_order(self, session, order: Order) -> Order:
        synced_order = await self.sync_order(session, order)
        if synced_order.status != OrderStatus.IN_POSITION:
            return synced_order

        credentials = await self.get_credentials(session)
        if credentials is None:
            return synced_order

        fill_snapshot = await self._recover_external_fill_snapshot(credentials, synced_order)
        if fill_snapshot is None:
            return synced_order

        scan_cycle_id = await self._scan_cycle_id_for_signal(session, signal_id=synced_order.signal_id)
        closed_status = self._closed_status_for_fill_snapshot(
            synced_order,
            self._combined_fill_snapshot(order=synced_order, fill_snapshot=fill_snapshot),
        )
        await self._close_order_from_snapshot(
            session,
            order=synced_order,
            closed_status=closed_status,
            close_type="EXTERNAL",
            event_type="ORDER_CLOSED_WIN" if closed_status == OrderStatus.CLOSED_WIN else "ORDER_CLOSED_LOSS",
            event_message=(
                f"{synced_order.symbol} closed externally in profit"
                if closed_status == OrderStatus.CLOSED_WIN
                else f"{synced_order.symbol} closed externally in loss"
            ),
            scan_cycle_id=scan_cycle_id,
            fill_snapshot=fill_snapshot,
        )
        await session.commit()
        await self.ws_manager.broadcast("order_status_change", {"order_id": synced_order.id, "status": synced_order.status.value})
        return synced_order

    async def sync_order(self, session, order: Order) -> Order:
        credentials = await self.get_credentials(session)
        if credentials is None:
            return order
        scan_cycle_id = await self._scan_cycle_id_for_signal(session, signal_id=order.signal_id)
        entry_just_triggered = False

        if order.status == OrderStatus.SUBMITTING:
            previous_status = order.status
            await self._reconcile_submitting_order(session, credentials, order)
            await session.commit()
            if order.status != previous_status:
                await self.ws_manager.broadcast("order_status_change", {"order_id": order.id, "status": order.status.value})
            return order

        if order.status == OrderStatus.ORDER_PLACED:
            entry_resolution = await self._query_entry_order_state_resolution(credentials, order)
            entry_state = entry_resolution.entry_state
            if entry_state is None:
                recovery = await self._attempt_authoritative_entry_recovery(
                    session,
                    credentials=credentials,
                    order=order,
                    scan_cycle_id=scan_cycle_id,
                    entry_resolution=entry_resolution,
                    protections_confirmed=self._protection_refs_present(order),
                    inconclusive_message=f"{order.symbol} authoritative pending-entry recovery remained inconclusive during order sync",
                )
                if recovery.outcome == self.RECOVERY_OUTCOME_RECOVERED and order.status == OrderStatus.IN_POSITION:
                    entry_just_triggered = True
                await session.commit()
                await self.ws_manager.broadcast("order_status_change", {"order_id": order.id, "status": order.status.value})
                return order
            status = entry_state.status
            if status in {"PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED"}:
                activated = await self._activate_entry_fill(
                    session,
                    credentials=credentials,
                    order=order,
                    entry_state=entry_state,
                    scan_cycle_id=scan_cycle_id,
                    protections_confirmed=self._protection_refs_present(order),
                )
                if activated:
                    entry_just_triggered = True
                elif order.status == OrderStatus.ORDER_PLACED and status in {"CANCELED", "EXPIRED"}:
                    default_reason_context = await self._signal_reason_context(
                        session,
                        signal_id=order.signal_id,
                        setup_family=str(getattr(order, "setup_family", None) or "") or None,
                        entry_style=str(getattr(order, "entry_style", None) or "") or None,
                    )
                    recovery = await self._attempt_authoritative_entry_recovery(
                        session,
                        credentials=credentials,
                        order=order,
                        scan_cycle_id=scan_cycle_id,
                        entry_resolution=entry_resolution,
                        protections_confirmed=self._protection_refs_present(order),
                        inconclusive_message=f"{order.symbol} authoritative pending-entry recovery remained inconclusive before pending-entry cancellation",
                        extra_details={
                            "entry_status": status,
                            "entry_route": entry_state.remote_kind,
                            "algo_status": entry_state.algo_status,
                            "actual_order_id": entry_state.actual_order_id,
                        },
                    )
                    if recovery.outcome != self.RECOVERY_OUTCOME_CONFIRMED_NONE:
                        if (
                            recovery.outcome == self.RECOVERY_OUTCOME_INCONCLUSIVE
                            and (status == "EXPIRED" or self.pending_entry_expired(order))
                        ):
                            await self._apply_uncertain_expired_terminal_resolution(
                                session,
                                credentials=credentials,
                                order=order,
                                scan_cycle_id=scan_cycle_id,
                                reason_context={
                                    "exchange_entry_status": status,
                                    "entry_route": entry_state.remote_kind,
                                    "algo_status": entry_state.algo_status,
                                    "actual_order_id": entry_state.actual_order_id,
                                    **(recovery.details or {}),
                                },
                                default_reason_context=default_reason_context,
                                message=(
                                    f"{order.symbol} pending entry expired and was terminally cancelled during sync "
                                    "because authoritative recovery remained inconclusive"
                                ),
                            )
                        await session.commit()
                        await self.ws_manager.broadcast("order_status_change", {"order_id": order.id, "status": order.status.value})
                        return order
                    lifecycle_reason, exchange_cancel_cause = self._exchange_pending_cancel_reason(
                        order=order,
                        exchange_status=status,
                        entry_state=entry_state,
                    )
                    order.status = OrderStatus.CANCELLED_BY_BOT
                    order.cancel_reason = lifecycle_reason
                    order.cancelled_at = datetime.now(timezone.utc)
                    await record_audit(
                        session,
                        event_type="ORDER_CANCELLED",
                        level=AuditLevel.INFO,
                        message=f"{order.symbol} order cancelled",
                        order_id=order.id,
                        signal_id=order.signal_id,
                        symbol=order.symbol,
                        scan_cycle_id=scan_cycle_id,
                        details=self._reason_details(
                            reason=lifecycle_reason,
                            reason_context={
                                "authoritative_recovery_outcome": recovery.outcome,
                                "exchange_entry_status": status,
                                "exchange_cancel_cause": exchange_cancel_cause,
                                "entry_route": entry_state.remote_kind,
                                "algo_status": entry_state.algo_status,
                                "actual_order_id": entry_state.actual_order_id,
                            },
                            default_context=default_reason_context,
                        ),
                    )

        if order.status == OrderStatus.IN_POSITION:
            if not entry_just_triggered:
                try:
                    await self._sync_live_entry_state(
                        session,
                        credentials,
                        order,
                        scan_cycle_id=scan_cycle_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "sync_order.live_entry_sync_failed",
                        order_id=order.id,
                        symbol=order.symbol,
                        error=str(exc),
                    )
            if order.status != OrderStatus.IN_POSITION:
                await session.commit()
                await self.ws_manager.broadcast("order_status_change", {"order_id": order.id, "status": order.status.value})
                return order
            await self._sync_partial_take_profit_fill(
                session,
                credentials=credentials,
                order=order,
                scan_cycle_id=scan_cycle_id,
            )
            closed = await self._sync_protective_exit(
                session,
                credentials=credentials,
                order=order,
                role="tp2" if self._partial_tp_enabled(order) else "tp",
                protection="take-profit",
                close_type="TP",
                fallback_close_price=Decimal(order.take_profit_2 or order.take_profit),
                fallback_quantity=Decimal(order.tp_quantity_2) if order.tp_quantity_2 is not None else None,
                scan_cycle_id=scan_cycle_id,
            )
            if order.status == OrderStatus.IN_POSITION and not closed:
                await self._sync_protective_exit(
                    session,
                    credentials=credentials,
                    order=order,
                    role="sl",
                    protection="stop-loss",
                    close_type="SL",
                    fallback_close_price=order.stop_loss,
                    fallback_quantity=None,
                    scan_cycle_id=scan_cycle_id,
                )

        await session.commit()
        await self.ws_manager.broadcast("order_status_change", {"order_id": order.id, "status": order.status.value})
        return order

    async def cancel_sibling_pending_orders(self, session, active_order: Order) -> None:
        siblings = (
            await session.execute(
                select(Order).where(
                    Order.id != active_order.id,
                    Order.symbol == active_order.symbol,
                    Order.status == OrderStatus.ORDER_PLACED,
                )
            )
        ).scalars().all()
        for sibling in siblings:
            await self.cancel_order(session, order_id=sibling.id, reason="setup_state_changed")
