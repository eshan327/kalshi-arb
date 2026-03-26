import asyncio
import json
import time
from collections import deque
from threading import Lock
from typing import Optional
import websockets
from core.config import (
    ORDERBOOK_VIEW_DEPTH,
    RECONCILIATION_CONSECUTIVE_BREACHES,
    RECONCILIATION_TOP_N,
    SNAPSHOT_RECALIBRATION_SEC,
    WS_LOG_DEFAULT_LIMIT,
    WS_LOG_MAXLEN,
)
from data.kalshi_rest import get_market_orderbook, get_open_markets
from data.kalshi_ws import connect_and_subscribe
from engine.orderbook import OrderBook
from engine.reconciliation import compare_levels, is_reconciliation_breach

# Live orderbook instance (accessible by other modules)
live_book: Optional[OrderBook] = None
_live_market_info = {}
_ws_message_log = deque(maxlen=WS_LOG_MAXLEN)
_ws_log_lock = Lock()
_top10_impact_log = deque(maxlen=WS_LOG_MAXLEN)
_reconciliation_log = deque(maxlen=WS_LOG_MAXLEN)
_ws_stats = {
    "total_received": 0,
    "orderbook_delta_received": 0,
    "orderbook_delta_buffered": 0,
    "orderbook_delta_applied": 0,
    "orderbook_delta_stale_ignored": 0,
    "orderbook_delta_invalid_seq": 0,
    "orderbook_delta_seq_gap": 0,
    "ticker_received": 0,
    "snapshot_anchor_seen": 0,
}


def _count_incoming_message(msg_type: str) -> None:
    with _ws_log_lock:
        _ws_stats["total_received"] += 1
        if msg_type == "orderbook_delta":
            _ws_stats["orderbook_delta_received"] += 1
        elif msg_type == "ticker":
            _ws_stats["ticker_received"] += 1


def _record_ws_event(event_type: str, seq, payload: dict, status: str) -> None:
    entry = {
        "ts": time.time(),
        "type": event_type,
        "seq": seq,
        "status": status,
        "payload": payload,
    }
    with _ws_log_lock:
        _ws_message_log.append(entry)

        if event_type == "orderbook_delta":
            if status == "buffered":
                _ws_stats["orderbook_delta_buffered"] += 1
            elif status in {"applied", "applied_from_buffer"}:
                _ws_stats["orderbook_delta_applied"] += 1
            elif status in {"stale_ignored", "stale_buffer_ignored"}:
                _ws_stats["orderbook_delta_stale_ignored"] += 1
            elif status == "invalid_seq_ignored":
                _ws_stats["orderbook_delta_invalid_seq"] += 1
            elif status in {"seq_gap", "buffer_replay_gap"}:
                _ws_stats["orderbook_delta_seq_gap"] += 1

        if event_type == "orderbook_snapshot" and status == "anchor_seen":
            _ws_stats["snapshot_anchor_seen"] += 1


def _top10_signature(book: OrderBook) -> tuple:
    yes_bids, yes_asks, no_bids, no_asks = book.get_orderbook()

    def _norm(levels):
        return tuple((round(float(px), 2), float(qty)) for px, qty in levels[:ORDERBOOK_VIEW_DEPTH])

    return (
        _norm(yes_bids),
        _norm(yes_asks),
        _norm(no_bids),
        _norm(no_asks),
    )


def _record_top10_impact(seq: int, payload: dict, changed: bool) -> None:
    with _ws_log_lock:
        _top10_impact_log.append(
            {
                "ts": time.time(),
                "seq": seq,
                "changed": changed,
                "payload": payload,
            }
        )


def _record_reconciliation(event: dict) -> None:
    with _ws_log_lock:
        _reconciliation_log.append(event)


def _levels_from_rest_snapshot(book: OrderBook, snapshot: dict):
    yes_levels = snapshot.get("yes")
    no_levels = snapshot.get("no")

    if yes_levels is None or no_levels is None:
        yes_levels = snapshot.get("yes_dollars_fp", [])
        no_levels = snapshot.get("no_dollars_fp", [])

    yes_dict = {}
    no_dict = {}

    for level in yes_levels or []:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        px = book._normalize_price(level[0])
        qty = book._normalize_qty(level[1])
        if qty > 0:
            yes_dict[px] = qty

    for level in no_levels or []:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        px = book._normalize_price(level[0])
        qty = book._normalize_qty(level[1])
        if qty > 0:
            no_dict[px] = qty

    yes_bids = sorted(
        [(book._to_cents(p), q) for p, q in yes_dict.items()],
        key=lambda x: x[0],
        reverse=True,
    )
    no_bids = sorted(
        [(book._to_cents(p), q) for p, q in no_dict.items()],
        key=lambda x: x[0],
        reverse=True,
    )
    return yes_bids, no_bids


def get_live_book() -> Optional[OrderBook]:
    """Read-only accessor for the active Kalshi orderbook stream state."""
    return live_book


def get_live_market_info() -> dict:
    """Returns metadata for the currently tracked Kalshi market."""
    return dict(_live_market_info)


def get_live_orderbook_snapshot(depth: int = 10) -> dict:
    """Returns a JSON-serializable orderbook snapshot for UI/API consumers."""
    book = live_book
    if book is None or not book.initialized:
        return {
            "initialized": False,
            "market_ticker": None,
            "expected_seq": None,
            "yes_bids": [],
            "yes_asks": [],
            "no_bids": [],
            "no_asks": [],
        }

    yes_bids, yes_asks, no_bids, no_asks = book.get_orderbook()
    return {
        "initialized": True,
        "market_ticker": book.market_ticker,
        "expected_seq": book.expected_seq,
        "yes_bids": yes_bids[:depth],
        "yes_asks": yes_asks[:depth],
        "no_bids": no_bids[:depth],
        "no_asks": no_asks[:depth],
    }


def get_ws_message_log(limit: int = 200) -> list:
    """Returns newest websocket events for debugging and validation."""
    with _ws_log_lock:
        if limit <= 0:
            return []
        return list(_ws_message_log)[-limit:]


def get_top10_impact_log(limit: int = WS_LOG_DEFAULT_LIMIT) -> list:
    """Returns newest top-10 impact records for orderbook-focused verification."""
    with _ws_log_lock:
        if limit <= 0:
            return []
        return list(_top10_impact_log)[-limit:]


def get_ws_message_log_size() -> int:
    """Returns current number of retained websocket log entries."""
    with _ws_log_lock:
        return len(_ws_message_log)


def get_reconciliation_log(limit: int = WS_LOG_DEFAULT_LIMIT) -> list:
    """Returns newest reconciliation checks and resync decisions."""
    with _ws_log_lock:
        if limit <= 0:
            return []
        return list(_reconciliation_log)[-limit:]


def get_ws_processing_stats() -> dict:
    """Returns aggregate counters proving websocket events are processed end-to-end."""
    with _ws_log_lock:
        return dict(_ws_stats)


async def _stream_with_sync(market_ticker: str, book: OrderBook):
    """
    Connects WS, receives snapshot, then applies sequential deltas.
    On seq gap or disconnect: reconnect for a fresh snapshot.

    Bootstrapping flow:
    1. Connect WS and begin buffering orderbook_delta messages
    2. Fetch REST snapshot in parallel
    3. Anchor sequence and replay buffered deltas in-order
    4. Continue live delta application until disconnect/seq gap
    """

    while True:
        try:
            book.reset()
            buffered_deltas = []
            bootstrapped = False
            ws_snapshot_seq = None
            delta_count = 0
            last_recalibration_at = time.monotonic()
            consecutive_recon_breaches = 0

            rest_snapshot_task = asyncio.create_task(
                asyncio.to_thread(get_market_orderbook, market_ticker)
            )

            ws = await connect_and_subscribe(market_ticker)
            print(f"  --> Subscribed to {market_ticker}. Bootstrapping from REST snapshot...")

            async for message in ws:
                data = json.loads(message)
                msg_type = data.get("type")
                seq = data.get("seq")
                msg_payload = data.get("msg", {})

                _count_incoming_message(msg_type or "unknown")

                _record_ws_event(msg_type or "unknown", seq, msg_payload, "received")

                if msg_type == "orderbook_snapshot":
                    if isinstance(seq, int):
                        ws_snapshot_seq = seq
                        _record_ws_event(msg_type, seq, msg_payload, "anchor_seen")
                    continue

                elif msg_type == "orderbook_delta":
                    msg = msg_payload

                    if not isinstance(seq, int):
                        _record_ws_event(msg_type, seq, msg, "invalid_seq_ignored")
                        continue

                    if not bootstrapped:
                        buffered_deltas.append((seq, msg))
                        _record_ws_event(msg_type, seq, msg, "buffered")
                        continue

                    before = _top10_signature(book)
                    if not book.apply_delta_with_seq(seq, msg):
                        if seq < (book.expected_seq or 0):
                            _record_ws_event(msg_type, seq, msg, "stale_ignored")
                            continue
                        _record_ws_event(msg_type, seq, msg, "seq_gap")
                        break  # seq gap — reconnect

                    _record_ws_event(msg_type, seq, msg, "applied")

                    delta_count += 1
                    after = _top10_signature(book)
                    changed = before != after
                    _record_top10_impact(seq, msg, changed)

                elif msg_type == "ticker":
                    pass

                elif msg_type == "subscribed":
                    print(f"  [SERVER] Subscription confirmed: {data.get('msg', {}).get('channel')}")

                # Check if resync needed after processing
                if book.needs_resync:
                    print("  --> Resync triggered, reconnecting...")
                    break

                if not bootstrapped and rest_snapshot_task.done() and ws_snapshot_seq is not None:
                    try:
                        rest_snapshot = rest_snapshot_task.result()
                    except Exception as e:
                        print(f"  --> REST snapshot fetch failed ({e}), reconnecting...")
                        break

                    rest_seq = book.load_rest_snapshot(rest_snapshot)
                    seq_anchor = rest_seq if isinstance(rest_seq, int) else ws_snapshot_seq
                    book.set_expected_seq(seq_anchor + 1)
                    _record_ws_event("bootstrap", seq_anchor, {}, "rest_snapshot_loaded")

                    applied = 0
                    for buffered_seq, buffered_msg in sorted(buffered_deltas, key=lambda x: x[0]):
                        before = _top10_signature(book)
                        if book.apply_delta_with_seq(buffered_seq, buffered_msg):
                            applied += 1
                            after = _top10_signature(book)
                            _record_top10_impact(buffered_seq, buffered_msg, before != after)
                            _record_ws_event(
                                "orderbook_delta", buffered_seq, buffered_msg, "applied_from_buffer"
                            )
                        elif buffered_seq < (book.expected_seq or 0):
                            _record_ws_event(
                                "orderbook_delta", buffered_seq, buffered_msg, "stale_buffer_ignored"
                            )
                        else:
                            _record_ws_event(
                                "orderbook_delta", buffered_seq, buffered_msg, "buffer_replay_gap"
                            )
                            break

                    bootstrapped = True
                    delta_count += applied
                    last_recalibration_at = time.monotonic()
                    print(
                        f"  --> Bootstrap complete | anchor_seq={seq_anchor} | "
                        f"buffered={len(buffered_deltas)} | applied={applied}"
                    )

                    if book.needs_resync:
                        print("  --> Bootstrap replay detected seq gap, reconnecting...")
                        break

                if bootstrapped and time.monotonic() - last_recalibration_at >= SNAPSHOT_RECALIBRATION_SEC:
                    recal_snapshot = await asyncio.to_thread(get_market_orderbook, market_ticker)
                    rest_yes_bids, rest_no_bids = _levels_from_rest_snapshot(book, recal_snapshot)
                    live_yes_bids, _, live_no_bids, _ = book.get_orderbook()

                    metrics = {
                        "yes": compare_levels(live_yes_bids, rest_yes_bids, RECONCILIATION_TOP_N),
                        "no": compare_levels(live_no_bids, rest_no_bids, RECONCILIATION_TOP_N),
                    }

                    breach = is_reconciliation_breach(metrics)
                    if breach:
                        consecutive_recon_breaches += 1
                    else:
                        consecutive_recon_breaches = 0

                    action = "none"
                    if consecutive_recon_breaches >= RECONCILIATION_CONSECUTIVE_BREACHES:
                        action = "trigger_resync"
                        book.needs_resync = True

                    _record_reconciliation(
                        {
                            "ts": time.time(),
                            "market_ticker": market_ticker,
                            "breach": breach,
                            "consecutive_breaches": consecutive_recon_breaches,
                            "action": action,
                            "metrics": metrics,
                        }
                    )

                    _record_ws_event(
                        "recalibration",
                        recal_snapshot.get("seq", recal_snapshot.get("sequence")),
                        {
                            "breach": breach,
                            "consecutive_breaches": consecutive_recon_breaches,
                            "action": action,
                        },
                        "rest_snapshot_reconciled",
                    )
                    last_recalibration_at = time.monotonic()

                    if action == "trigger_resync":
                        print("  --> Reconciliation drift threshold breached; forcing resync...")
                        break

            await ws.close()
            if not rest_snapshot_task.done():
                rest_snapshot_task.cancel()

        except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
            print(f"  --> WebSocket dropped ({e}), reconnecting in 5s")

        await asyncio.sleep(5)


async def run_market_streamer():
    """Finds an active 15-minute crypto market and starts the sync loop."""
    global live_book, _live_market_info

    print("Fetching active KXBTC15M market to stream.")
    markets = get_open_markets("KXBTC15M")

    if not markets:
        print("No active markets found.")
        return

    target_market = markets[0]['ticker']
    _live_market_info = dict(markets[0])
    print(f"  --> Target market: {target_market}")

    live_book = OrderBook(target_market)
    await _stream_with_sync(target_market, live_book)
