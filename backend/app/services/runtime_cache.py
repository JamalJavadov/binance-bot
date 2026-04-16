import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar


ValueT = TypeVar("ValueT")


@dataclass
class CacheEntry(Generic[ValueT]):
    value: ValueT
    expires_at: float


class AsyncTTLCache:
    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_or_set(
        self,
        key: str,
        *,
        ttl_seconds: float,
        factory: Callable[[], Awaitable[ValueT]],
    ) -> ValueT:
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.value

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._entries.get(key)
            if cached is not None and cached.expires_at > now:
                return cached.value

            value = await factory()
            self._entries[key] = CacheEntry(value=value, expires_at=now + ttl_seconds)
            return value
