from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from core.asset_context import get_active_market_profile
from core.config import RESEARCH_CAPTURE_HORIZON_SEC, RESEARCH_CAPTURE_PATH
from data import account_state
from data.kalshi_rest import (
    get_event,
    get_open_markets,
    get_recent_index_values,
    get_series,
    invalidate_metadata,
)
from data.kalshi_ws import command, connect, request_orderbook_snapshot
from engine.live_pricing import reset_live_pricing_for_new_market
from engine.market_stream.discovery import parse_iso8601_to_epoch, select_target_market
from engine.market_stream.display import top_levels_for_display
from engine.orderbook import OrderBook
from engine.updates import notify
from feeds.state.tick_store import (
    ingest_index,
    reset_tick_state,
    seed_history,
    set_index_connected,
)

logger = logging.getLogger(__name__)
live_book: OrderBook | None = None
_live_market_info: dict = {}
_live_market_info_lock = threading.Lock()
_market_results: dict[str, dict] = {}
_stream_epoch = 0


def _capture_research_event(
    kind: str,
    msg: dict,
    *,
    seq: int | None = None,
    sid: int | None = None,
    receipt_ts: float | None = None,
) -> None:
    """Persist raw late-market public data for forward microstructure research."""
    if not RESEARCH_CAPTURE_PATH:
        return
    market = get_live_market_info()
    ticker = str(market.get("ticker") or "")
    close_ts = parse_iso8601_to_epoch(market.get("close_time"))
    now = time.time() if receipt_ts is None else float(receipt_ts)
    if not ticker or close_ts is None:
        return
    seconds_to_expiry = close_ts - now
    if not 0 <= seconds_to_expiry <= RESEARCH_CAPTURE_HORIZON_SEC:
        return

    event = {
        "receipt_ts": now,
        "kind": str(kind),
        "seq": seq,
        "sid": sid,
        "market_ticker": ticker,
        "close_ts": close_ts,
        "seconds_to_expiry": seconds_to_expiry,
        "payload": msg,
    }
    try:
        path = Path(RESEARCH_CAPTURE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError as exc:  # pragma: no cover - research capture is best effort
        logger.warning("Could not persist research market-data event: %s", exc)


def get_live_book():
    return live_book


def get_live_market_info() -> dict:
    with _live_market_info_lock:
        return dict(_live_market_info)


def get_market_result(ticker: str) -> dict | None:
    with _live_market_info_lock:
        value = _market_results.get(ticker)
        return dict(value) if value else None


def get_stream_epoch() -> int:
    return _stream_epoch


def _set_live_market_info(profile, market=None):
    with _live_market_info_lock:
        _live_market_info.clear()
        _live_market_info.update(market or {})
        _live_market_info.update(
            active_asset=profile.asset,
            active_asset_display=profile.display_name,
            active_series=profile.kalshi_series_ticker,
        )


def get_live_orderbook_snapshot(depth: int = 10) -> dict:
    book = live_book
    if book is None or not book.initialized or book.needs_resync:
        return dict(
            initialized=False,
            market_ticker=None,
            expected_seq=None,
            last_update_ts=None,
            yes_bids=[],
            yes_asks=[],
            no_bids=[],
            no_asks=[],
        )
    levels = book.get_orderbook_top_n(max(1, depth) * 4)
    return dict(
        initialized=True,
        market_ticker=book.market_ticker,
        expected_seq=book.expected_seq,
        last_update_ts=book.last_verified_ts or book.last_update_ts,
        **{
            key: top_levels_for_display(value, depth)
            for key, value in zip(
                ("yes_bids", "yes_asks", "no_bids", "no_asks"), levels
            )
        },
    )


def _discover(profile) -> dict:
    market = get_live_market_info()
    if not market.get("ticker"):
        markets = get_open_markets(profile.kalshi_series_ticker)
        now = time.time()
        markets = [
            m
            for m in markets
            if (parse_iso8601_to_epoch(m.get("close_time")) or 0) > now
        ]
        if not markets:
            return {}
        market = dict(select_target_market(markets))
    series = get_series(profile.kalshi_series_ticker)
    event = market.get("_event_fee_override")
    if event is None:
        event = get_event(market["event_ticker"])
    market["series_fee_policy"] = {
        "fee_type": series.get("fee_type"),
        "fee_multiplier": series.get("fee_multiplier"),
    }
    market["fee_policy"] = _fees(series, event)
    return market


def _fees(series, event):
    fee_type = event.get("fee_type_override") or series.get("fee_type")
    multiplier = event.get("fee_multiplier_override")
    multiplier = series.get("fee_multiplier") if multiplier is None else multiplier
    ready = (
        fee_type in {"quadratic", "quadratic_with_maker_fees"}
        and isinstance(multiplier, (int, float))
        and multiplier > 0
    )
    return dict(fee_type=fee_type, fee_multiplier=multiplier, ready=ready)


def _lifecycle(profile, kind, msg) -> bool:
    ticker = str(msg.get("market_ticker", ""))
    with _live_market_info_lock:
        if kind == "event_fee_update":
            if msg.get("event_ticker") == _live_market_info.get("event_ticker"):
                _live_market_info["_event_fee_override"] = dict(msg)
                _live_market_info["fee_policy"] = _fees(
                    _live_market_info.get("series_fee_policy", {}), msg
                )
                invalidate_metadata()
                return True
            return False
        if not ticker.startswith(profile.kalshi_series_ticker + "-"):
            return False
        event = msg.get("event_type")
        if event in {"determined", "settled"}:
            if len(_market_results) >= 2000 and ticker not in _market_results:
                _market_results.pop(next(iter(_market_results)))
            _market_results[ticker] = {
                **_market_results.get(ticker, {}),
                **msg,
                "status": "finalized" if event == "settled" else "determined",
            }
        if ticker == _live_market_info.get("ticker"):
            _live_market_info.update(msg.get("additional_metadata") or {})
            _live_market_info.update(
                {
                    k: v
                    for k, v in msg.items()
                    if k not in {"market_ticker", "event_type", "additional_metadata"}
                }
            )
            if "close_ts" in msg:
                _live_market_info["close_time"] = datetime.fromtimestamp(
                    msg["close_ts"], UTC
                ).isoformat()
            if event in {"activated", "deactivated", "determined", "settled"}:
                _live_market_info["status"] = {
                    "activated": "active",
                    "deactivated": "inactive",
                    "settled": "finalized",
                }.get(event, event)
            reset_live_pricing_for_new_market()
        # Discard any in-flight metadata read when newer active-market state arrives.
        return ticker == _live_market_info.get("ticker") or not _live_market_info.get(
            "ticker"
        )


async def _seed_index(profile) -> None:
    try:
        rows = await asyncio.to_thread(get_recent_index_values, profile.index_id)
        seed_history(rows)
        notify()
    except Exception as exc:
        logger.warning("Official history unavailable; warming up from stream: %s", exc)


async def _session(profile) -> None:
    global live_book, _stream_epoch
    async with await connect() as ws:
        if live_book is not None:
            live_book.reset()
        live_book = None
        _stream_epoch += 1
        account_state.connection(False)
        set_index_connected(False)
        subscriptions: dict[str, int] = {}
        pending: dict[int, tuple[str, float]] = {}
        sequences: dict[int, int] = {}
        optional = {
            "cfbenchmarks_value_5hz",
            "fill",
            "market_positions",
            "user_orders",
            "trade",
        }

        async def subscribe(channel, **params):
            cid = await command(ws, "subscribe", channels=[channel], **params)
            pending[cid] = channel, time.monotonic() + 5

        await subscribe("cfbenchmarks_value", index_ids=[profile.index_id])
        if profile.high_frequency:
            await subscribe("cfbenchmarks_value_5hz", index_ids=[profile.index_id])
        for channel in (
            "market_lifecycle_v2",
            "fill",
            "market_positions",
            "user_orders",
        ):
            await subscribe(channel)
        history_task = asyncio.create_task(_seed_index(profile))
        discovery = asyncio.create_task(asyncio.to_thread(_discover, profile))
        discover_again = False
        next_discovery = time.monotonic() + 300
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
                    notify()
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
                                await subscribe(
                                    "orderbook_delta",
                                    market_tickers=[ticker],
                                    use_yes_price=True,
                                )
                                if RESEARCH_CAPTURE_PATH:
                                    await subscribe("trade", market_tickers=[ticker])
                                snapshot_deadline = time.monotonic() + 5
                            next_discovery = time.monotonic() + 300
                            notify()
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
                    if channel == "cfbenchmarks_value":
                        set_index_connected(True)
                    if all(
                        c in subscriptions
                        for c in ("fill", "market_positions", "user_orders")
                    ):
                        if channel in {"fill", "market_positions", "user_orders"}:
                            account_state.connection(True)
                    continue
                if kind == "error":
                    channel, _ = pending.pop(data.get("id"), ("", 0))
                    if channel in optional:
                        logger.error("Optional channel %s: %s", channel, msg)
                        continue
                    raise ConnectionError(f"Kalshi stream error: {msg}")
                sid, seq = data.get("sid"), data.get("seq")
                if kind in {
                    "orderbook_snapshot",
                    "orderbook_delta",
                    "cfbenchmarks_value",
                    "cfbenchmarks_value_5hz",
                    "market_lifecycle_v2",
                    "event_lifecycle",
                    "event_fee_update",
                    "trade",
                    "fill",
                    "market_position",
                    "user_order",
                }:
                    _capture_research_event(
                        kind,
                        msg,
                        seq=seq if isinstance(seq, int) else None,
                        sid=sid if isinstance(sid, int) else None,
                        receipt_ts=time.time(),
                    )
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
                if kind in {"orderbook_snapshot", "orderbook_delta"}:
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
                    notify()
                elif kind in {"cfbenchmarks_value", "cfbenchmarks_value_5hz"}:
                    if ingest_index(kind, msg, profile.index_id):
                        notify()
                elif kind in {"market_lifecycle_v2", "event_fee_update"}:
                    if _lifecycle(profile, kind, msg):
                        discover_again = True
                    notify()
                elif kind in {"fill", "market_position", "user_order"}:
                    account_state.ingest(kind, msg)
                    if kind == "fill":
                        from engine.trading.runtime import _emit_event

                        _emit_event("fill", "exchange_fill", fill=msg)
                # An unchanged sequenced book remains valid while the session is alive.
                if live_book and live_book.initialized and not live_book.needs_resync:
                    live_book.last_verified_ts = time.time()
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


async def run_market_streamer() -> None:
    profile = get_active_market_profile()
    reset_tick_state(profile.asset)
    delay = 0.5
    while True:
        try:
            _set_live_market_info(profile)
            await _session(profile)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Kalshi stream reconnecting: %s", exc)
        finally:
            if live_book:
                live_book.reset()
            set_index_connected(False)
            account_state.connection(False)
            notify()
        await asyncio.sleep(delay * random.uniform(0.8, 1.2))
        delay = min(8.0, delay * 2)
