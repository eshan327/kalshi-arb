import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional
import websockets
from core.config import (
    RECONCILIATION_CONSECUTIVE_BREACHES,
    RECONCILIATION_TOP_N,
    SNAPSHOT_RECALIBRATION_SEC,
)
from data.kalshi_rest import get_market_orderbook, get_open_markets
from data.kalshi_ws import connect_and_subscribe
from engine.book_microstructure import on_live_orderbook_update, reset_book_microstructure_for_new_market
from engine.live_pricing import reset_live_pricing_for_new_market
from engine.orderbook import OrderBook
from engine.reconciliation import compare_levels, is_reconciliation_breach
from engine.stream_metrics import (
    _count_incoming_message,
    _record_reconciliation,
    _record_top10_impact,
    _record_ws_event,
    _top10_signature,
    get_reconciliation_log,
    get_top10_impact_log,
    get_ws_message_log,
    get_ws_message_log_size,
    get_ws_processing_stats,
)

logger = logging.getLogger(__name__)
RECONNECT_DELAY_SEC = 5

# Live orderbook instance (accessible by other modules)
live_book: Optional[OrderBook] = None
_live_market_info: dict = {}
_live_market_info_lock = threading.Lock()
BufferedDelta = tuple[int, dict]
MIN_ACTIONABLE_PRICE_CENTS = 1.0
MAX_ACTIONABLE_PRICE_CENTS = 99.0


def _parse_iso8601_to_epoch(value: str | None) -> float | None:
    """Converts ISO8601 timestamps from Kalshi payloads to epoch seconds."""
    if not isinstance(value, str) or not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _select_target_market(markets: list[dict]) -> dict:
    """Selects the active market with the nearest future close time."""
    now = time.time()
    candidates = []

    for market in markets:
        close_ts = _parse_iso8601_to_epoch(market.get("close_time"))
        if close_ts is None or close_ts > now:
            rank = close_ts if close_ts is not None else float("inf")
            candidates.append((rank, market))

    if not candidates:
        return markets[0]

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _levels_from_rest_snapshot(book: OrderBook, snapshot: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
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
        if qty > 0 and px is not None:
            yes_dict[px] = qty

    for level in no_levels or []:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        px = book._normalize_price(level[0])
        qty = book._normalize_qty(level[1])
        if qty > 0 and px is not None:
            no_dict[px] = qty

    yes_bids = sorted([(book._to_cents(p), q) for p, q in yes_dict.items()], key=lambda x: x[0], reverse=True)
    no_bids = sorted([(book._to_cents(p), q) for p, q in no_dict.items()], key=lambda x: x[0], reverse=True)
    return yes_bids, no_bids


def _is_market_closed(market_close_ts: float | None) -> bool:
    return market_close_ts is not None and time.time() >= market_close_ts


def _replay_buffered_deltas(book: OrderBook, buffered_deltas: list[BufferedDelta]) -> int:
    applied = 0
    for buffered_seq, buffered_msg in sorted(buffered_deltas, key=lambda x: x[0]):
        before = _top10_signature(book)
        if book.apply_delta_with_seq(buffered_seq, buffered_msg):
            applied += 1
            after = _top10_signature(book)
            _record_top10_impact(buffered_seq, buffered_msg, before != after)
            on_live_orderbook_update(book)
            _record_ws_event("orderbook_delta", buffered_seq, buffered_msg, "applied_from_buffer")
        elif buffered_seq < (book.expected_seq or 0):
            _record_ws_event("orderbook_delta", buffered_seq, buffered_msg, "stale_buffer_ignored")
        else:
            _record_ws_event("orderbook_delta", buffered_seq, buffered_msg, "buffer_replay_gap")
            break
    return applied


def _try_bootstrap_from_rest(
    market_ticker: str,
    book: OrderBook,
    rest_snapshot_task: asyncio.Task,
    ws_snapshot_seq: int | None,
    buffered_deltas: list[BufferedDelta],
) -> tuple[bool, bool, float]:
    """Returns (bootstrapped, should_reconnect, recalibration_monotonic_ts)."""
    if not rest_snapshot_task.done() or ws_snapshot_seq is None:
        return False, False, time.monotonic()

    try:
        rest_snapshot = rest_snapshot_task.result()
    except Exception as exc:
        logger.warning("REST snapshot fetch failed (%s), reconnecting...", exc)
        return False, True, time.monotonic()

    rest_seq = book.load_rest_snapshot(rest_snapshot)
    seq_anchor = rest_seq if isinstance(rest_seq, int) else ws_snapshot_seq
    book.set_expected_seq(seq_anchor + 1)
    _record_ws_event("bootstrap", seq_anchor, {}, "rest_snapshot_loaded")

    applied = _replay_buffered_deltas(book, buffered_deltas)
    recalibration_ts = time.monotonic()
    on_live_orderbook_update(book)
    logger.info(
        "Bootstrap complete | anchor_seq=%s | buffered=%s | applied=%s",
        seq_anchor,
        len(buffered_deltas),
        applied,
    )

    if book.needs_resync:
        logger.warning("Bootstrap replay detected seq gap, reconnecting...")
        return True, True, recalibration_ts

    return True, False, recalibration_ts


async def _run_recalibration(
    market_ticker: str,
    book: OrderBook,
    consecutive_recon_breaches: int,
) -> tuple[int, str]:
    """Returns (updated_consecutive_breaches, action)."""
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

    return consecutive_recon_breaches, action


def get_live_book() -> Optional[OrderBook]:
    """Read-only accessor for the active Kalshi orderbook stream state."""
    return live_book


def get_live_market_info() -> dict:
    """Returns metadata for the currently tracked Kalshi market."""
    with _live_market_info_lock:
        return dict(_live_market_info)


def _is_actionable_display_level(level: tuple[float, float]) -> bool:
    price, _qty = level
    return MIN_ACTIONABLE_PRICE_CENTS <= float(price) <= MAX_ACTIONABLE_PRICE_CENTS


def _top_levels_for_display(levels: list[tuple[float, float]], depth: int) -> list[tuple[float, float]]:
    """Prefers 1-99c levels for display, but falls back to raw levels when needed."""
    if depth <= 0:
        return []

    filtered = [level for level in levels if _is_actionable_display_level(level)]
    selected = filtered if filtered else levels
    return selected[:depth]


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
        "yes_bids": _top_levels_for_display(yes_bids, depth),
        "yes_asks": _top_levels_for_display(yes_asks, depth),
        "no_bids": _top_levels_for_display(no_bids, depth),
        "no_asks": _top_levels_for_display(no_asks, depth),
    }


async def _stream_with_sync(market_ticker: str, book: OrderBook, market_close_ts: float | None = None):
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
        if _is_market_closed(market_close_ts):
            logger.info("Market %s reached close time; rotating stream target...", market_ticker)
            return

        try:
            book.reset()
            buffered_deltas = []
            bootstrapped = False
            ws_snapshot_seq = None
            last_recalibration_at = time.monotonic()
            consecutive_recon_breaches = 0

            rest_snapshot_task = asyncio.create_task(asyncio.to_thread(get_market_orderbook, market_ticker))

            ws = await connect_and_subscribe(market_ticker)
            logger.info("Subscribed to %s. Bootstrapping from REST snapshot...", market_ticker)

            async for message in ws:
                if _is_market_closed(market_close_ts):
                    logger.info("Market %s reached close time; reconnect loop stopped.", market_ticker)
                    await ws.close()
                    return

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

                if msg_type == "orderbook_delta":
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
                        break

                    _record_ws_event(msg_type, seq, msg, "applied")
                    after = _top10_signature(book)
                    _record_top10_impact(seq, msg, before != after)
                    on_live_orderbook_update(book)

                elif msg_type == "subscribed":
                    logger.info("[SERVER] Subscription confirmed: %s", data.get("msg", {}).get("channel"))

                if book.needs_resync:
                    logger.warning("Resync triggered, reconnecting...")
                    break

                if not bootstrapped:
                    bootstrapped, reconnect_now, last_recalibration_at = _try_bootstrap_from_rest(
                        market_ticker,
                        book,
                        rest_snapshot_task,
                        ws_snapshot_seq,
                        buffered_deltas,
                    )
                    if reconnect_now:
                        break

                if bootstrapped and time.monotonic() - last_recalibration_at >= SNAPSHOT_RECALIBRATION_SEC:
                    consecutive_recon_breaches, action = await _run_recalibration(
                        market_ticker,
                        book,
                        consecutive_recon_breaches,
                    )
                    last_recalibration_at = time.monotonic()

                    if action == "trigger_resync":
                        logger.warning("Reconciliation drift threshold breached; forcing resync...")
                        break

            await ws.close()
            if not rest_snapshot_task.done():
                rest_snapshot_task.cancel()

        except (websockets.ConnectionClosed, ConnectionError, OSError) as exc:
            logger.warning("WebSocket dropped (%s), reconnecting in %ss", exc, RECONNECT_DELAY_SEC)

        await asyncio.sleep(RECONNECT_DELAY_SEC)


async def run_market_streamer():
    """Tracks the active 15-minute crypto market and rotates on close."""
    global live_book, _live_market_info
    current_market = None

    while True:
        logger.info("Fetching active KXBTC15M market to stream.")
        markets = get_open_markets("KXBTC15M")

        if not markets:
            logger.info("No active markets found. Retrying in %ss...", RECONNECT_DELAY_SEC)
            with _live_market_info_lock:
                _live_market_info.clear()
            await asyncio.sleep(RECONNECT_DELAY_SEC)
            continue

        selected_market = _select_target_market(markets)
        target_market = selected_market.get("ticker")

        if not target_market:
            logger.warning("No valid market ticker found. Retrying in %ss...", RECONNECT_DELAY_SEC)
            with _live_market_info_lock:
                _live_market_info.clear()
            await asyncio.sleep(RECONNECT_DELAY_SEC)
            continue

        if target_market != current_market:
            logger.info("Target market: %s", target_market)
            reset_live_pricing_for_new_market()
            reset_book_microstructure_for_new_market()
            current_market = target_market

        with _live_market_info_lock:
            _live_market_info.clear()
            _live_market_info.update(dict(selected_market))
        close_ts = _parse_iso8601_to_epoch(selected_market.get("close_time"))
        live_book = OrderBook(target_market)
        await _stream_with_sync(target_market, live_book, market_close_ts=close_ts)

        # Stream exits on market close/rotation event; immediately discover the next one.
        await asyncio.sleep(1)
