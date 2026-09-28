import asyncio
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from core import auth
from data import kalshi_perps as perps, kalshi_ws


def test_signatures_and_websocket_handshake_use_full_perps_path(monkeypatch):
    monkeypatch.setattr(auth.time, "time", lambda: 123.0)
    for key in (ed25519.Ed25519PrivateKey.generate(), rsa.generate_private_key(public_exponent=65537, key_size=2048)):
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        monkeypatch.setattr(auth, "_get_credentials", lambda: ("test-key", pem))
        headers = auth.get_api_auth_headers("GET", "/trade-api/v2/margin/balance?ignored=true")
        signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        message = b"123000GET/trade-api/v2/margin/balance"
        if isinstance(key, ed25519.Ed25519PrivateKey):
            key.public_key().verify(signature, message)
        else:
            key.public_key().verify(signature, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())

    calls = []
    monkeypatch.setattr(kalshi_ws, "get_ws_auth_headers", lambda path: {"signed-path": path})

    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(kalshi_ws.websockets, "connect", connect)
    url = "wss://example.com/trade-api/ws/v2/margin"
    asyncio.run(kalshi_ws.connect(url=url))
    assert calls[0][1]["additional_headers"] == {"signed-path": "/trade-api/ws/v2/margin"}


def test_stream_snapshot_sequence_gap_and_capture_cleanup(monkeypatch, tmp_path):
    sent = []
    frames = [
        *[{"type": "subscribed", "msg": {"channel": channel, "sid": sid}} for sid, channel in enumerate(perps._PUBLIC_CHANNELS, 1)],
        {"type": "orderbook_snapshot", "sid": 1, "seq": 10, "msg": {"market_ticker": "BTC-PERP", "bid": [["65000.1234", "2.50"]], "ask": []}},
        {"type": "ticker", "sid": 2, "msg": {"market_ticker": "BTC-PERP", "price": "65000.1234", "ts_ms": 123456}},
        {"type": "orderbook_delta", "sid": 1, "seq": 11, "msg": {"market_ticker": "BTC-PERP", "price": "65000.1234", "delta": "-0.50", "side": "bid"}},
        {"type": "orderbook_delta", "sid": 1, "seq": 11, "msg": {"market_ticker": "BTC-PERP"}},
        {"type": "orderbook_delta", "sid": 1, "seq": 13, "msg": {"market_ticker": "BTC-PERP"}},
    ]

    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def send(self, message):
            sent.append(json.loads(message))
        async def recv(self):
            return json.dumps(frames.pop(0))

    async def connect(**kwargs):
        assert kwargs == {"url": perps.PERPS_WS_BASE_URL}
        return Socket()

    monkeypatch.setattr(perps, "connect", connect)
    path = tmp_path / "perps.jsonl"
    received = []

    async def run():
        with pytest.raises(ConnectionError, match="sequence gap"):
            async for data in perps._session("BTC-PERP", str(path)):
                received.append(data)

    asyncio.run(run())
    assert [row["type"] for row in received] == ["orderbook_snapshot", "ticker", "orderbook_delta"]
    assert received[0]["msg"]["bid"][0][0] == "65000.1234"
    assert [row["params"]["channels"][0] for row in sent] == list(perps._PUBLIC_CHANNELS)
    assert sent[1]["params"]["send_initial_snapshot"] is True
    capture = [json.loads(line) for line in path.read_text().splitlines()]
    assert capture[0]["data"]["type"] == "session_start"
    assert capture[-1]["data"]["type"] == "session_end"
    assert len({row["session"] for row in capture}) == 1
