import asyncio
import json
import time
from datetime import UTC, datetime

import pytest

from data import streamer
from data import benchmark as tick_store


def test_official_ticks_keep_one_second_history_and_reject_bad_values(monkeypatch):
    monkeypatch.setattr(tick_store.time, "time", lambda: 100.0)
    tick_store.reset_tick_state("BTC")
    tick_store.set_index_connected(True)
    slow = {
        "index_id": "BRTI",
        "data": json.dumps({"time": 99000, "value": "100"}),
        "avg_60s_data": {
            "value": "98",
            "window_size": 60,
            "window_start_ts_ms": 39000,
            "window_end_ts_exclusive": 99000,
        },
    }
    assert tick_store.ingest_index("cfbenchmarks_value", slow, "BRTI")
    for ts in (99200, 99400, 99600, 99800, 100000):
        assert tick_store.ingest_index(
            "cfbenchmarks_value_5hz",
            {"index_id": "BRTI", "source_ts_ms": ts, "value_usd": "101"},
            "BRTI",
        )
    assert len(tick_store.get_index_ticks()) == 1
    assert tick_store.get_index_state()["price"] == 101.0
    assert not tick_store.ingest_index("cfbenchmarks_value", slow, "BRTI")
    with pytest.raises(ValueError):
        tick_store.ingest_index(
            "cfbenchmarks_value_5hz",
            {"index_id": "BRTI", "source_ts_ms": 100200, "value_usd": "NaN"},
            "BRTI",
        )


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
                    "no_dollars_fp": [["0.60", "3"]],
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


def test_history_seed_never_replaces_live_averages_or_spot(monkeypatch):
    monkeypatch.setattr(tick_store.time, "time", lambda: 100.0)
    tick_store.reset_tick_state("BTC")
    tick_store.ingest_index(
        "cfbenchmarks_value",
        {
            "index_id": "BRTI",
            "data": json.dumps({"time": 99000, "value": "102"}),
            "avg_60s_data": {
                "value": "101",
                "window_size": 60,
                "window_start_ts_ms": 39000,
                "window_end_ts_exclusive": 99000,
            },
        },
        "BRTI",
    )
    tick_store.seed_history(
        [
            {"time": 98000, "value": "100"},
            {"time": 99000, "value": "101"},
            {"time": 99400, "value": "103"},
        ]
    )
    assert [t["price"] for t in tick_store.get_index_ticks()] == [100.0, 102.0]
    assert tick_store.get_index_state()["price"] == 102.0
    assert tick_store.get_index_ticks()[-1]["average"] == 101.0


def test_lifecycle_marks_market_inactive(monkeypatch):
    profile = streamer.get_active_market_profile()
    ticker = profile.kalshi_series_ticker + "-TEST"
    monkeypatch.setattr(
        streamer,
        "_live_market_info",
        {"ticker": ticker, "status": "active"},
    )
    assert streamer._lifecycle(
        profile,
        {"market_ticker": ticker, "event_type": "deactivated"},
    )
    assert streamer.get_live_market_info()["status"] == "inactive"


def test_reconnect_to_same_market_always_resubscribes(monkeypatch):
    async def check():
        from data.orderbook import OrderBook

        ws = FakeSocket()

        async def connect():
            return ws

        monkeypatch.setattr(streamer, "connect", connect)
        monkeypatch.setattr(streamer, "get_recent_index_values", lambda _: [])
        previous = OrderBook("SAME")
        previous.load_ws_snapshot(
            {"yes_dollars_fp": [["0.40", "2"]], "no_dollars_fp": [["0.60", "3"]]}, 10
        )
        monkeypatch.setattr(streamer, "live_book", previous)
        monkeypatch.setattr(streamer, "_live_market_info", {})
        monkeypatch.setattr(
            streamer,
            "_discover",
            lambda _: {
                "ticker": "SAME",
                "status": "active",
                "close_time": "2099-01-01T00:00:00Z",
            },
        )
        task = asyncio.create_task(
            streamer._session(streamer.get_active_market_profile())
        )
        try:
            await until(
                lambda: (
                    streamer.live_book is not None
                    and streamer.live_book is not previous
                    and streamer.live_book.initialized
                )
            )
            assert streamer.live_book is not previous
            assert not previous.initialized
            assert (
                sum(c["params"].get("channels") == ["orderbook_delta"] for c in ws.sent)
                == 1
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())
