from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from app.services.binance_gateway import BinanceAPIError, BinanceGateway


def test_ed25519_signature_is_base64_and_query_encoded() -> None:
    private_key = Ed25519PrivateKey.generate()
    private_key_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("utf-8")
    gateway = BinanceGateway()
    signature = gateway.sign_request(
        private_key_pem,
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "newClientOrderId": "fib setup/1",
        },
    )
    assert isinstance(signature, str)
    assert "/" in signature or "+" in signature or "=" in signature
    assert gateway._build_query_string(
        {"newClientOrderId": "fib setup/1"}
    ) == "newClientOrderId=fib%20setup%2F1"


@pytest.mark.asyncio
async def test_signed_request_retries_once_after_timestamp_resync(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = BinanceGateway()
    credentials = SimpleNamespace(api_key="key", private_key_pem="private")
    request_calls: list[dict] = []
    sync_calls: list[str] = []

    async def fake_sync_server_time() -> int:
        sync_calls.append("sync")
        gateway.server_time_offset_ms = 4321
        return gateway.server_time_offset_ms

    async def fake_request(_method: str, _path: str, *, params: dict, headers: dict) -> SimpleNamespace:
        request_calls.append({"params": dict(params), "headers": dict(headers)})
        if len(request_calls) == 1:
            return SimpleNamespace(status_code=400, text='{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}')
        return SimpleNamespace(status_code=200, text='{"ok":true}', json=lambda: {"ok": True})

    monkeypatch.setattr(gateway, "sync_server_time", fake_sync_server_time)
    monkeypatch.setattr(gateway, "sign_request", lambda _key, params: f'signature-{params["timestamp"]}')
    monkeypatch.setattr(gateway.client, "request", fake_request)

    payload = await gateway.signed_request("GET", "/fapi/v3/positionRisk", params={"symbol": "BTCUSDT"}, credentials=credentials)

    assert payload == {"ok": True}
    assert sync_calls == ["sync"]
    assert len(request_calls) == 2
    assert request_calls[0]["headers"] == {"X-MBX-APIKEY": "key"}
    assert request_calls[0]["params"]["recvWindow"] == gateway.settings.binance_recv_window
    assert request_calls[1]["params"]["timestamp"] != request_calls[0]["params"]["timestamp"]
    assert request_calls[1]["params"]["signature"] == f'signature-{request_calls[1]["params"]["timestamp"]}'


@pytest.mark.asyncio
async def test_signed_request_does_not_retry_non_timestamp_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = BinanceGateway()
    credentials = SimpleNamespace(api_key="key", private_key_pem="private")
    sync_calls: list[str] = []

    async def fake_sync_server_time() -> int:
        sync_calls.append("sync")
        return gateway.server_time_offset_ms

    async def fake_request(_method: str, _path: str, *, params: dict, headers: dict) -> SimpleNamespace:
        return SimpleNamespace(status_code=400, text='{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}')

    monkeypatch.setattr(gateway, "sync_server_time", fake_sync_server_time)
    monkeypatch.setattr(gateway, "sign_request", lambda *_args, **_kwargs: "signature")
    monkeypatch.setattr(gateway.client, "request", fake_request)

    with pytest.raises(BinanceAPIError) as error:
        await gateway.signed_request("GET", "/fapi/v3/account", params={}, credentials=credentials)

    assert error.value.code == -2015
    assert sync_calls == []
