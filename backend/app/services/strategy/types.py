from dataclasses import dataclass, field

from app.models.enums import SignalDirection


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int | None = None
    is_closed: bool = True
    symbol: str | None = None

    @property
    def range_size(self) -> float:
        return max(self.high - self.low, 0.0)


@dataclass(frozen=True)
class SetupCandidate:
    symbol: str
    direction: SignalDirection
    entry_price: float
    stop_loss: float
    take_profit: float
    actual_rr: float
    net_r_multiple: float
    estimated_cost: float
    confirmation_score: int
    final_score: int
    rank_value: float
    setup_family: str
    setup_variant: str
    entry_style: str
    market_state: str
    execution_tier: str
    score_breakdown: dict[str, int] = field(default_factory=dict)
    reason_text: str = ""
    current_price: float = 0.0
    swing_origin: float = 0.0
    swing_terminus: float = 0.0
    timeframe: str = "15m"
    expiry_minutes: int = 45
    order_preview: dict = field(default_factory=dict)
    extra_context: dict = field(default_factory=dict)
    selection_context: dict = field(default_factory=dict)


def parse_klines(raw: list[list], *, symbol: str | None = None) -> list[Candle]:
    candles: list[Candle] = []
    for item in raw:
        close_time = int(item[6]) if len(item) > 6 else None
        is_closed = bool(item[11]) if len(item) > 11 else True
        candles.append(
            Candle(
                open_time=int(item[0]),
                open=float(item[1]),
                high=float(item[2]),
                low=float(item[3]),
                close=float(item[4]),
                volume=float(item[5]),
                close_time=close_time,
                is_closed=is_closed,
                symbol=symbol,
            )
        )
    return candles


def closed_candles(candles: list[Candle]) -> list[Candle]:
    if len(candles) <= 1:
        return []
    return [candle for candle in candles[:-1] if getattr(candle, "is_closed", True)]
