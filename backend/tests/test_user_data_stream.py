from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import app.services.user_data_stream as user_data_stream_module
from app.services.user_data_stream import UserDataStreamSupervisor


class DummySessionFactory:
    def __init__(self, credentials) -> None:
        self.credentials = credentials

    def __call__(self):
        credentials = self.credentials

        class _Context:
            async def __aenter__(self_inner):
                return SimpleNamespace(credentials=credentials)

            async def __aexit__(self_inner, exc_type, exc, tb) -> bool:
                return False

        return _Context()


class StubGateway:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def user_data_stream_ws_url(self, listen_key: str) -> str:
        return f"ws://example.test/{listen_key}"

    async def keepalive_user_data_stream(self, credentials, listen_key: str) -> dict:
        return {"listenKey": listen_key}

    async def start_user_data_stream(self, credentials) -> str:
        return "listen-key"

    async def close_user_data_stream(self, credentials, listen_key: str) -> dict:
        self.closed.append(listen_key)
        return {"listenKey": listen_key}


class StubOrderManager:
    def __init__(self) -> None:
        self.availability: list[tuple[bool, str]] = []

    def set_user_stream_primary_path_availability(self, *, available: bool, reason: str) -> None:
        self.availability.append((available, reason))

    async def get_credentials(self, session):
        return session.credentials


class StubLifecycleMonitor:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def notify_exchange_event(self, payload: dict) -> None:
        self.events.append(payload)


@pytest.mark.asyncio
async def test_user_data_stream_forwards_order_and_account_events(monkeypatch) -> None:
    gateway = StubGateway()
    order_manager = StubOrderManager()
    lifecycle_monitor = StubLifecycleMonitor()
    supervisor = UserDataStreamSupervisor(
        gateway,
        DummySessionFactory(SimpleNamespace(api_key="key", private_key_pem="private")),
        order_manager,
        lifecycle_monitor,
    )

    class FakeWebSocket:
        def __init__(self) -> None:
            self.calls = 0

        async def recv(self):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"e": "ORDER_TRADE_UPDATE", "o": {"s": "BCHUSDT"}})
            supervisor._stop.set()
            return json.dumps({"e": "ACCOUNT_UPDATE", "a": {"m": "ORDER"}})

    class FakeConnection:
        async def __aenter__(self):
            return FakeWebSocket()

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(user_data_stream_module, "connect", lambda *args, **kwargs: FakeConnection())

    await supervisor._consume_stream(credentials=SimpleNamespace(api_key="key", private_key_pem="private"), listen_key="listen-key")

    assert [payload["e"] for payload in lifecycle_monitor.events] == ["ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE"]


@pytest.mark.asyncio
async def test_user_data_stream_marks_primary_path_available_and_unavailable(monkeypatch) -> None:
    gateway = StubGateway()
    order_manager = StubOrderManager()
    lifecycle_monitor = StubLifecycleMonitor()
    supervisor = UserDataStreamSupervisor(
        gateway,
        DummySessionFactory(SimpleNamespace(api_key="key", private_key_pem="private")),
        order_manager,
        lifecycle_monitor,
    )

    async def fake_consume_stream(*, credentials, listen_key: str) -> None:
        supervisor._stop.set()

    monkeypatch.setattr(supervisor, "_consume_stream", fake_consume_stream)

    await supervisor._run()

    assert order_manager.availability[0] == (True, "connected")
    assert order_manager.availability[-1] == (False, "disconnected")
    assert gateway.closed == ["listen-key"]
