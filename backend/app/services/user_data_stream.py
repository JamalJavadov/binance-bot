import asyncio
import contextlib
import json
from typing import Any

from websockets import connect
from websockets.exceptions import ConnectionClosed

from app.core.logging import get_logger

logger = get_logger(__name__)


class UserDataStreamSupervisor:
    KEEPALIVE_INTERVAL_SECONDS = 25 * 60
    RECONNECT_DELAY_SECONDS = 5
    CREDENTIAL_POLL_SECONDS = 5
    RECEIVE_TIMEOUT_SECONDS = 1

    def __init__(self, gateway, session_factory, order_manager, lifecycle_monitor) -> None:
        self.gateway = gateway
        self.session_factory = session_factory
        self.order_manager = order_manager
        self.lifecycle_monitor = lifecycle_monitor
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def _set_primary_path_availability(self, *, available: bool, reason: str) -> None:
        callback = getattr(self.order_manager, "set_user_stream_primary_path_availability", None)
        if callable(callback):
            callback(available=available, reason=reason)

    async def _load_credentials(self):
        async with self.session_factory() as session:
            return await self.order_manager.get_credentials(session)

    @staticmethod
    def _parse_message(raw_message: Any) -> dict[str, Any] | None:
        if isinstance(raw_message, bytes):
            try:
                raw_message = raw_message.decode("utf-8")
            except Exception:
                return None
        if not isinstance(raw_message, str):
            return None
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    async def _wait_or_stop(self, timeout_seconds: float) -> bool:
        if self._stop.is_set():
            return True
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=timeout_seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _consume_stream(self, *, credentials, listen_key: str) -> None:
        ws_url = self.gateway.user_data_stream_ws_url(listen_key)
        logger.info("user_data_stream.connected", ws_url=ws_url)
        next_keepalive_at = asyncio.get_running_loop().time() + self.KEEPALIVE_INTERVAL_SECONDS
        async with connect(ws_url, ping_interval=20, ping_timeout=20, close_timeout=5) as websocket:
            while not self._stop.is_set():
                now = asyncio.get_running_loop().time()
                if now >= next_keepalive_at:
                    await self.gateway.keepalive_user_data_stream(credentials, listen_key)
                    next_keepalive_at = now + self.KEEPALIVE_INTERVAL_SECONDS
                try:
                    raw_message = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=self.RECEIVE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed:
                    break
                payload = self._parse_message(raw_message)
                if payload is None:
                    continue
                event_type = str(payload.get("e") or "").upper()
                normalized_event_type = event_type.replace("_", "")
                if normalized_event_type == "LISTENKEYEXPIRED":
                    raise RuntimeError("Binance user data stream listen key expired")
                if event_type in {"ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE"}:
                    await self.lifecycle_monitor.notify_exchange_event(payload)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                credentials = await self._load_credentials()
            except Exception as exc:
                self._set_primary_path_availability(available=False, reason="credentials_load_failed")
                logger.warning("user_data_stream.credentials_load_failed", error=str(exc))
                if await self._wait_or_stop(self.RECONNECT_DELAY_SECONDS):
                    break
                continue

            if credentials is None:
                self._set_primary_path_availability(available=False, reason="credentials_missing")
                if await self._wait_or_stop(self.CREDENTIAL_POLL_SECONDS):
                    break
                continue

            listen_key: str | None = None
            try:
                listen_key = await self.gateway.start_user_data_stream(credentials)
                self._set_primary_path_availability(available=True, reason="connected")
                await self._consume_stream(credentials=credentials, listen_key=listen_key)
            except Exception as exc:
                logger.warning("user_data_stream.disconnected", error=str(exc))
            finally:
                self._set_primary_path_availability(available=False, reason="disconnected")
                if listen_key is not None:
                    with contextlib.suppress(Exception):
                        await self.gateway.close_user_data_stream(credentials, listen_key)

            if await self._wait_or_stop(self.RECONNECT_DELAY_SECONDS):
                break

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
