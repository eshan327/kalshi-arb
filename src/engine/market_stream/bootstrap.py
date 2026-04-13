from __future__ import annotations

import asyncio
import logging
import time

from engine.book_microstructure import on_live_orderbook_update
from engine.orderbook import OrderBook
from engine.stream_metrics import _record_top10_impact, _record_ws_event, _top10_signature

logger = logging.getLogger(__name__)

BufferedDelta = tuple[int, dict]


def levels_from_rest_snapshot(book: OrderBook, snapshot: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
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


def replay_buffered_deltas(book: OrderBook, buffered_deltas: list[BufferedDelta]) -> int:
    applied = 0
    for buffered_seq, buffered_msg in sorted(buffered_deltas, key=lambda item: item[0]):
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


def try_bootstrap_from_rest(
    *,
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

    applied = replay_buffered_deltas(book, buffered_deltas)
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
