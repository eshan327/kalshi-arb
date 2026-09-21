import asyncio
import json
import time
from datetime import UTC, datetime

import pytest

from data import account_state, streamer
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


@pytest.fixture
def account(monkeypatch):
    for key, value in {
        "_enabled": True,
        "_connected": True,
        "_needs_snapshot": False,
        "_revision": 0,
        "_positions": {},
        "_orders": {},
        "_pending": None,
        "_balance": {"balance": 10000, "portfolio_value": 0, "updated_ts": 100},
        "_refreshed": 100.0,
        "_dirty_since": 0.0,
        "_error": None,
    }.items():
        monkeypatch.setattr(account_state, key, value)
    monkeypatch.setattr(account_state.time, "time", lambda: 100.0)
    monkeypatch.setattr(account_state, "_watermark", lambda: 101.0)
    calls = []

    def request(method, path, **kwargs):
        calls.append(path)
        return {"balance": 9900, "portfolio_value": 100, "updated_ts": 101}

    monkeypatch.setattr(account_state, "_request", request)
    return calls


def test_streamed_fill_reconciles_without_repolling_positions(account, monkeypatch):
    monkeypatch.setattr(
        account_state, "get_positions", lambda: pytest.fail("redundant positions read")
    )
    account_state.begin_order("kalshi-algo-test", "TEST")
    with pytest.raises(RuntimeError, match="Previous order"):
        account_state.begin_order("kalshi-algo-duplicate", "TEST")
    fill = {
        "client_order_id": "kalshi-algo-test",
        "trade_id": "trade",
        "count_fp": "2.00",
        "post_position_fp": "2.00",
    }
    account_state.ingest("fill", fill)
    account_state.ingest("fill", fill)
    account_state.ingest(
        "market_position",
        {
            "market_ticker": "TEST",
            "position_fp": "2.00",
            "position_cost_dollars": "1.00",
        },
    )
    account_state.order_response({"fill_count": "2.00"})
    account_state.refresh()
    assert account_state.snapshot()["ready"]
    assert account_state.snapshot()["positions"][0]["position_fp"] == "2.00"
    assert account == ["/portfolio/balance"]


def test_uncertain_order_is_never_retried_or_cleared_on_absence(account, monkeypatch):
    account_state.begin_order("kalshi-algo-lost", "TEST")
    monkeypatch.setattr(account_state, "_request_orders_since", lambda _: [])
    account_state.refresh()
    assert not account_state.snapshot()["ready"]
    assert account_state._pending["client_order_id"] == "kalshi-algo-lost"
    assert account == []


def test_rest_response_cannot_overwrite_concurrent_stream_update(account, monkeypatch):
    account_state._needs_snapshot = True

    def positions():
        account_state.ingest(
            "market_position",
            {
                "market_ticker": "TEST",
                "position_fp": "3.00",
                "position_cost_dollars": "1.50",
            },
        )
        return []

    monkeypatch.setattr(account_state, "get_positions", positions)
    monkeypatch.setattr(account_state, "get_open_orders", list)
    account_state.refresh()
    assert account_state._positions["TEST"]["position_fp"] == "3.00"
    assert account_state._needs_snapshot


def test_disconnect_blocks_live_account(account):
    account_state.connection(False)
    with pytest.raises(RuntimeError):
        account_state.snapshot()


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
            assert sum(c["params"].get("channels") == ["fill"] for c in ws.sent) == 1
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


def test_paused_market_is_not_overwritten_by_fee_refresh(monkeypatch):
    profile = streamer.get_active_market_profile()
    ticker = profile.kalshi_series_ticker + "-TEST"
    monkeypatch.setattr(
        streamer,
        "_live_market_info",
        {"ticker": ticker, "status": "active", "event_ticker": "EVENT"},
    )
    monkeypatch.setattr(
        streamer, "get_series", lambda _: {"fee_type": "quadratic", "fee_multiplier": 1}
    )
    monkeypatch.setattr(streamer, "get_event", lambda _: {})
    monkeypatch.setattr(
        streamer, "get_open_markets", lambda _: pytest.fail("redundant discovery")
    )
    assert streamer._lifecycle(
        profile,
        "market_lifecycle_v2",
        {"market_ticker": ticker, "event_type": "deactivated"},
    )
    assert streamer._discover(profile)["status"] == "inactive"


def test_fee_override_uses_stream_payload_and_clear_uses_series(monkeypatch):
    profile = streamer.get_active_market_profile()
    monkeypatch.setattr(
        streamer,
        "_live_market_info",
        {
            "ticker": profile.kalshi_series_ticker + "-TEST",
            "event_ticker": "EVENT",
            "series_fee_policy": {"fee_type": "quadratic", "fee_multiplier": 1},
        },
    )
    streamer._lifecycle(
        profile,
        "event_fee_update",
        {
            "event_ticker": "EVENT",
            "fee_type_override": "quadratic",
            "fee_multiplier_override": 2,
        },
    )
    assert streamer.get_live_market_info()["fee_policy"]["fee_multiplier"] == 2
    streamer._lifecycle(
        profile,
        "event_fee_update",
        {
            "event_ticker": "EVENT",
            "fee_type_override": None,
            "fee_multiplier_override": None,
        },
    )
    assert streamer.get_live_market_info()["fee_policy"]["fee_multiplier"] == 1


def test_recovery_paginates_and_cancellation_routes_by_market(monkeypatch):
    from data import kalshi_trading

    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "DELETE":
            return {}
        if kwargs["params"].get("cursor"):
            return {"market_positions": [{"ticker": "B", "position_fp": "1"}]}
        return {
            "market_positions": [{"ticker": "A", "position_fp": "1"}],
            "cursor": "next",
        }

    monkeypatch.setattr(kalshi_trading, "_request", request)
    assert len(kalshi_trading.get_positions()) == 2
    kalshi_trading.cancel_order("id", "KXETH15M-TEST")
    assert calls[-1][2]["params"] == {"market_ticker": "KXETH15M-TEST"}


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


def test_manual_fee_lookup_does_not_guess_after_invalidation(monkeypatch):
    from trading import runtime

    monkeypatch.setattr(
        runtime, "get_live_market_info", lambda: {"fee_policy": {"ready": False}}
    )
    with pytest.raises(RuntimeError, match="fee policy"):
        runtime._current_fee_multiplier()
