from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import UTC, datetime

from core.markets import get_active_market_profile, parse_iso8601_to_epoch
from data.capture import FeedCapture
from data.benchmark import (
    ingest_index,
    reset_tick_state,
    seed_history,
    set_index_connected,
)
from data.kalshi_rest import get_open_markets, get_recent_index_values
from data.kalshi_ws import command, connect, request_orderbook_snapshot
from data.orderbook import OrderBook
from pricing.live_pricing import reset_live_pricing_for_new_market

logger = logging.getLogger(__name__)
live_book: OrderBook | None = None
_live_market_info: dict = {}
_stream_epoch = 0


def get_live_book():
    return live_book


def get_live_market_info() -> dict:
    return dict(_live_market_info)


def _set_live_market_info(profile, market=None):
    _live_market_info.clear()
    _live_market_info.update(market or {})
    _live_market_info.update(
        active_asset=profile.asset,
        active_series=profile.kalshi_series_ticker,
    )


def _discover(profile) -> dict:
    markets = get_open_markets(profile.kalshi_series_ticker)
    now = time.time()
    markets = [
        market for market in markets
        if (parse_iso8601_to_epoch(market.get("close_time")) or 0) > now
    ]
    return (
        dict(min(markets, key=lambda m: parse_iso8601_to_epoch(m["close_time"])))
        if markets
        else {}
    )


def _lifecycle(profile, msg) -> bool:
    ticker = str(msg.get("market_ticker", ""))
    if not ticker.startswith(profile.kalshi_series_ticker + "-"):
        return False
    if ticker == _live_market_info.get("ticker"):
        _live_market_info.update(msg.get("additional_metadata") or {})
        if "close_ts" in msg:
            _live_market_info["close_time"] = datetime.fromtimestamp(
                msg["close_ts"], UTC
            ).isoformat()
        event = msg.get("event_type")
        if event in {"activated", "deactivated", "determined", "settled"}:
            _live_market_info["status"] = {
                "activated": "active",
                "deactivated": "inactive",
                "settled": "finalized",
            }.get(event, event)
        reset_live_pricing_for_new_market()
    return True


async def _seed_index(profile) -> None:
    try:
        rows = await asyncio.to_thread(get_recent_index_values, profile.index_id)
        seed_history(rows)
    except Exception as exc:
        logger.warning("Official history unavailable; warming up from stream: %s", exc)


async def _session(profile, capture_path: str | None = None) -> None:
    global live_book, _stream_epoch
    async with await connect() as ws:
        if live_book is not None:
            live_book.reset()
        live_book = None
        _stream_epoch += 1
        set_index_connected(False)
        subscriptions: dict[str, int] = {}
        pending: dict[int, tuple[str, float]] = {}
        sequences: dict[int, int] = {}
        optional = {"cfbenchmarks_value_5hz", "market_lifecycle_v2", "trade"}

        async def subscribe(channel, **params):
            cid = await command(ws, "subscribe", channels=[channel], **params)
            pending[cid] = channel, time.monotonic() + 5

        await subscribe("cfbenchmarks_value", index_ids=[profile.index_id])
        if profile.high_frequency:
            await subscribe("cfbenchmarks_value_5hz", index_ids=[profile.index_id])
        await subscribe("market_lifecycle_v2")
        capture = FeedCapture(capture_path, f"{time.time_ns()}-{_stream_epoch}")
        history_task = asyncio.create_task(_seed_index(profile))
        discovery = asyncio.create_task(asyncio.to_thread(_discover, profile))
        discover_again = False
        next_discovery = time.monotonic() + 30
        snapshot_deadline = None
        receive = asyncio.create_task(ws.recv())
        try:
            while True:
                now = time.time()
                market = get_live_market_info()
                close = parse_iso8601_to_epoch(market.get("close_time"))
                if close is not None and now >= close and live_book is not None:
                    live_book.reset()
                    live_book = None
                    snapshot_deadline = None
                    _set_live_market_info(profile)
                    for channel in ("orderbook_delta", "trade"):
                        if channel in subscriptions:
                            await command(
                                ws,
                                "unsubscribe",
                                sids=[subscriptions.pop(channel)],
                            )
                    discover_again = True
                if discovery is not None and discovery.done():
                    try:
                        selected = discovery.result()
                        if discover_again:
                            # A lifecycle update arrived during the HTTP read. Fetch again
                            # rather than overwrite newer stream metadata with that response.
                            next_discovery = 0
                        elif selected:
                            ticker = selected["ticker"]
                            _set_live_market_info(profile, selected)
                            if live_book is None or live_book.market_ticker != ticker:
                                for channel in ("orderbook_delta", "trade"):
                                    if channel in subscriptions:
                                        await command(
                                            ws,
                                            "unsubscribe",
                                            sids=[subscriptions.pop(channel)],
                                        )
                                live_book = OrderBook(ticker)
                                reset_live_pricing_for_new_market()
                                capture.record({"type": "market_metadata", "msg": selected})
                                await subscribe(
                                    "orderbook_delta",
                                    market_tickers=[ticker],
                                    use_yes_price=True,
                                )
                                await subscribe("trade", market_tickers=[ticker])
                                snapshot_deadline = time.monotonic() + 5
                            next_discovery = time.monotonic() + 30
                        else:
                            next_discovery = time.monotonic() + 5
                    except Exception as exc:
                        logger.warning("Market metadata refresh: %s", exc)
                        next_discovery = time.monotonic() + 5
                    discovery = None
                    discover_again = False
                if discovery is None and (
                    discover_again or time.monotonic() >= next_discovery
                ):
                    discover_again = False
                    discovery = asyncio.create_task(
                        asyncio.to_thread(_discover, profile)
                    )
                if (
                    snapshot_deadline is not None
                    and time.monotonic() >= snapshot_deadline
                ):
                    raise TimeoutError("Orderbook snapshot deadline")
                for cid, (channel, deadline) in list(pending.items()):
                    if time.monotonic() >= deadline:
                        if channel in optional:
                            logger.error("Subscription %s unavailable", channel)
                            pending.pop(cid)
                        else:
                            raise TimeoutError(f"Subscription {channel} deadline")
                done, _ = await asyncio.wait({receive}, timeout=0.2)
                if not done:
                    continue
                message = receive.result()
                receive = asyncio.create_task(ws.recv())
                data = json.loads(message)
                kind, msg = data.get("type"), data.get("msg", {})
                if not isinstance(msg, dict):
                    raise ValueError("Invalid WebSocket payload")
                if kind in {"orderbook_snapshot", "orderbook_delta", "cfbenchmarks_value", "cfbenchmarks_value_5hz", "market_lifecycle_v2", "trade"}:
                    capture.record(data)
                if kind == "subscribed":
                    channel, sid = msg["channel"], msg["sid"]
                    subscriptions[channel] = sid
                    pending.pop(data.get("id"), None)
                    # Some servers omit the command ID on acknowledgements.
                    pending = {
                        cid: entry
                        for cid, entry in pending.items()
                        if entry[0] != channel
                    }
                    if channel in {"cfbenchmarks_value", "cfbenchmarks_value_5hz"}:
                        await command(ws, "update_subscription", sid=sid, action="indexlist")
                    continue
                if kind == "error":
                    channel, _ = pending.pop(data.get("id"), ("", 0))
                    if channel in optional:
                        logger.error("Optional channel %s: %s", channel, msg)
                        continue
                    raise ConnectionError(f"Kalshi stream error: {msg}")
                sid, seq = data.get("sid"), data.get("seq")
                if (
                    kind not in {"orderbook_snapshot", "orderbook_delta"}
                    and isinstance(sid, int)
                    and isinstance(seq, int)
                ):
                    expected = sequences.get(sid)
                    if expected is not None and seq <= expected:
                        continue
                    if expected is not None and seq != expected + 1:
                        raise ConnectionError("Stream sequence gap; reconciling")
                    sequences[sid] = seq
                if kind in {"cfbenchmarks_value_indexlist", "cfbenchmarks_value_5hz_indexlist"}:
                    if profile.index_id not in msg.get("index_ids", []):
                        if kind == "cfbenchmarks_value_indexlist":
                            raise ConnectionError(f"CF index {profile.index_id} unavailable")
                        logger.warning("CF 5 Hz index %s unavailable", profile.index_id)
                elif kind in {"orderbook_snapshot", "orderbook_delta"}:
                    book = live_book
                    if book is None or msg.get("market_ticker") != book.market_ticker:
                        continue
                    if kind == "orderbook_snapshot":
                        if not isinstance(seq, int):
                            raise ValueError("Snapshot has no sequence")
                        book.load_ws_snapshot(msg, seq)
                        snapshot_deadline = None
                        if book.needs_resync:
                            raise ValueError("Invalid snapshot")
                    elif book.initialized:
                        if not book.apply_delta_with_seq(seq, msg) or book.needs_resync:
                            if (
                                isinstance(seq, int)
                                and seq < (book.expected_seq or 0)
                                and not book.needs_resync
                            ):
                                continue
                            book.reset()
                            snapshot_deadline = time.monotonic() + 3
                            await request_orderbook_snapshot(
                                ws, book.market_ticker, subscriptions["orderbook_delta"]
                            )
                    # Snapshots are first by contract; recovery ignores deltas until snapshot.
                    elif snapshot_deadline is None:
                        raise ConnectionError("Delta without snapshot")
                elif kind in {"cfbenchmarks_value", "cfbenchmarks_value_5hz"}:
                    if ingest_index(kind, msg, profile.index_id):
                        if kind == "cfbenchmarks_value":
                            set_index_connected(True)
                elif kind == "market_lifecycle_v2":
                    if _lifecycle(profile, msg):
                        discover_again = True
        finally:
            receive.cancel()
            history_task.cancel()
            if discovery is not None:
                discovery.cancel()
            await asyncio.gather(
                receive,
                history_task,
                *([discovery] if discovery else []),
                return_exceptions=True,
            )
            capture.close()


async def run_market_streamer(capture_path: str | None = None, profile=None) -> None:
    profile = profile or get_active_market_profile()
    reset_tick_state(profile.asset)
    delay = 0.5
    while True:
        try:
            _set_live_market_info(profile)
            await _session(profile, capture_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Kalshi stream reconnecting: %s", exc)
        finally:
            if live_book:
                live_book.reset()
            set_index_connected(False)
        await asyncio.sleep(delay * random.uniform(0.8, 1.2))
        delay = min(8.0, delay * 2)
