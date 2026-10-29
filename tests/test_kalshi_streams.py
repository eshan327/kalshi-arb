import asyncio
import json
import time
from datetime import UTC, datetime

import pytest

from data import streamer


class FakeSocket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []
        self.sid = 0
        self.book_sid = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def recv(self):
        return json.dumps(await self.messages.get())

    async def send(self, raw):
        cmd = json.loads(raw)
        self.sent.append(cmd)
        if cmd["cmd"] == "subscribe":
            self.sid += 1
            channel = cmd["params"]["channels"][0]
            await self.messages.put(
                {
                    "id": cmd["id"],
                    "type": "subscribed",
                    "msg": {"channel": channel, "sid": self.sid},
                }
            )
            if channel == "orderbook_delta":
                self.book_sid = self.sid
                await self.snapshot(cmd["params"]["market_tickers"][0], 10)
        elif cmd["params"].get("action") == "get_snapshot":
            await self.snapshot(cmd["params"]["market_tickers"][0], 12)

    async def snapshot(self, ticker, seq):
        await self.messages.put(
            {
                "type": "orderbook_snapshot",
                "sid": self.book_sid,
                "seq": seq,
                "msg": {
                    "market_ticker": ticker,
                    "yes_dollars_fp": [["0.40", "2"]],
                    "no_dollars_fp": [["0.55", "3"]],
                },
            }
        )


async def until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Timed out")


def test_persistent_session_rotates_quiet_market_and_recovers_book_gap(monkeypatch):
    async def check():
        ws = FakeSocket()

        async def connect():
            return ws

        monkeypatch.setattr(streamer, "connect", connect)
        monkeypatch.setattr(streamer, "live_book", None)
        monkeypatch.setattr(streamer, "_live_market_info", {})
        close = time.time() + 0.4

        def discover(_):
            ticker, expiry = (
                ("OLD", close) if time.time() < close else ("NEW", close + 900)
            )
            return {
                "ticker": ticker,
                "status": "active",
                "close_time": datetime.fromtimestamp(expiry, UTC).isoformat(),
            }

        monkeypatch.setattr(streamer, "_discover", discover)
        monkeypatch.setattr(streamer, "get_recent_index_values", lambda _: [])
        task = asyncio.create_task(
            streamer._session(streamer.get_active_market_profile())
        )
        try:
            await until(
                lambda: (
                    streamer.live_book is not None and streamer.live_book.initialized
                )
            )
            await ws.messages.put(
                {
                    "type": "orderbook_delta",
                    "sid": ws.book_sid,
                    "seq": 12,
                    "msg": {
                        "market_ticker": "OLD",
                        "side": "yes",
                        "price_dollars": "0.40",
                        "delta_fp": "1",
                    },
                }
            )
            await until(
                lambda: any(
                    c["params"].get("action") == "get_snapshot" for c in ws.sent
                )
            )
            await until(
                lambda: (
                    streamer.live_book.initialized
                    and streamer.live_book.expected_seq == 13
                )
            )
            await until(
                lambda: (
                    streamer.live_book is not None
                    and streamer.live_book.market_ticker == "NEW"
                )
            )
            assert (
                sum(
                    c["params"].get("channels") == ["cfbenchmarks_value"]
                    for c in ws.sent
                )
                == 1
            )
            assert sum(c["params"].get("channels") == ["trade"] for c in ws.sent) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


def test_best_prices_handle_empty_sides_and_invalid_books():
    from data.orderbook import OrderBook

    book = OrderBook("TEST")
    assert book.get_best_prices() == (None, None, None, None)
    book.yes.update({35.0: 1, 40.1234: 2})
    assert book.get_best_prices() == (40.1234, None, None, 59.8766)
    book.no.update({45.0: 1, 50.1234: 2})
    assert book.get_best_prices() == (40.1234, 49.8766, 50.1234, 59.8766)
    book.yes.clear()
    assert book.get_best_prices() == (None, 49.8766, 50.1234, None)
    book.yes[49.8766] = 1
    assert book.get_best_prices() == (None, None, None, None)
    book.yes.clear()
    book.needs_resync = True
    assert book.get_best_prices() == (None, None, None, None)


def test_gold_recorder_subscribes_to_market_without_cf(monkeypatch):
    from core.markets import get_market_profile

    async def check():
        ws = FakeSocket()
        async def connect():
            return ws
        monkeypatch.setattr(streamer, "connect", connect)
        monkeypatch.setattr(streamer, "live_book", None)
        monkeypatch.setattr(streamer, "_live_market_info", {})
        monkeypatch.setattr(streamer, "_discover", lambda _: {
            "ticker": "KXGOLD15M-TEST", "status": "active",
            "close_time": datetime.fromtimestamp(time.time() + 900, UTC).isoformat(),
        })
        task = asyncio.create_task(streamer._session(get_market_profile("GOLD")))
        try:
            await until(lambda: streamer.live_book is not None and streamer.live_book.initialized)
            channels = [cmd["params"]["channels"][0] for cmd in ws.sent
                        if cmd["cmd"] == "subscribe"]
            assert channels == ["market_lifecycle_v2", "orderbook_delta", "trade"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.parametrize("failure", [PermissionError("capture denied"), OSError("disk full")])
def test_capture_failures_terminate_instead_of_reconnecting(monkeypatch, failure):
    calls = []

    async def fail(*args):
        calls.append(args)
        raise failure

    monkeypatch.setattr(streamer, "_session", fail)
    with pytest.raises(type(failure), match=str(failure)):
        asyncio.run(asyncio.wait_for(streamer.run_market_streamer("capture.jsonl"), 0.1))
    assert len(calls) == 1


def test_network_failures_still_reconnect(monkeypatch):
    calls = []

    async def session(*args):
        calls.append(args)
        if len(calls) == 1:
            raise ConnectionError("network disconnected")
        raise PermissionError("capture denied")

    async def no_wait(*args):
        pass

    monkeypatch.setattr(streamer, "_session", session)
    monkeypatch.setattr(streamer.asyncio, "sleep", no_wait)
    with pytest.raises(PermissionError, match="capture denied"):
        asyncio.run(streamer.run_market_streamer("capture.jsonl"))
    assert len(calls) == 2


def test_live_prediction_stream_ignores_stale_snapshots_and_wrong_subscriptions(monkeypatch):
    from core.markets import get_market_profile

    async def check():
        ws = FakeSocket()

        async def connect():
            return ws

        monkeypatch.setattr(streamer, "connect", connect)
        monkeypatch.setattr(streamer, "live_book", None)
        monkeypatch.setattr(streamer, "_live_market_info", {})
        monkeypatch.setattr(streamer, "_discover", lambda _: {
            "ticker": "TEST", "close_time": datetime.fromtimestamp(time.time() + 900, UTC).isoformat(),
        })
        task = asyncio.create_task(streamer._session(get_market_profile("GOLD")))
        try:
            await until(lambda: streamer.live_book is not None and streamer.live_book.initialized)
            await ws.messages.put({"type": "orderbook_delta", "sid": ws.book_sid, "seq": 11,
                                   "msg": {"market_ticker": "TEST", "side": "no",
                                           "price_dollars": "0.55", "delta_fp": "-3"}})
            await ws.snapshot("TEST", 10)
            await ws.messages.put({"type": "orderbook_snapshot", "sid": ws.book_sid + 100, "seq": 100,
                                   "msg": {"market_ticker": "TEST", "yes_dollars_fp": [["0.01", "1"]],
                                           "no_dollars_fp": [["0.55", "3"]]}})
            await ws.messages.put({"type": "orderbook_delta", "sid": ws.book_sid, "seq": 12,
                                   "msg": {"market_ticker": "TEST", "side": "yes",
                                           "price_dollars": "0.40", "delta_fp": "1"}})
            await until(lambda: streamer.live_book.expected_seq == 13)
            assert streamer.live_book.yes == {40.0: 3.0}
            assert streamer.live_book.no == {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())
