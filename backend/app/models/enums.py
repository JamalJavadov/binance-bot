import enum


class ScanStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class TriggerType(str, enum.Enum):
    AUTO_MODE = "AUTO_MODE"


class ScanSymbolOutcome(str, enum.Enum):
    UNSUPPORTED = "UNSUPPORTED"
    NO_SETUP = "NO_SETUP"
    FILTERED_OUT = "FILTERED_OUT"
    CANDIDATE = "CANDIDATE"
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"


class SignalDirection(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStatus(str, enum.Enum):
    CANDIDATE = "CANDIDATE"
    QUALIFIED = "QUALIFIED"
    DISMISSED = "DISMISSED"
    APPROVED = "APPROVED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    ORDER_FAILED = "ORDER_FAILED"


class OrderStatus(str, enum.Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    SUBMITTING = "SUBMITTING"
    ORDER_PLACED = "ORDER_PLACED"
    IN_POSITION = "IN_POSITION"
    CLOSED_WIN = "CLOSED_WIN"
    CLOSED_LOSS = "CLOSED_LOSS"
    CLOSED_BY_BOT = "CLOSED_BY_BOT"
    CLOSED_EXTERNALLY = "CLOSED_EXTERNALLY"
    CANCELLED_BY_BOT = "CANCELLED_BY_BOT"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"


class AuditLevel(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
