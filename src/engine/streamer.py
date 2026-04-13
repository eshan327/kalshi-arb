from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Optional

import websockets

from core.asset_context import apply_queued_asset_switch_and_get_context
from core.config import (
    RECONCILIATION_CONSECUTIVE_BREACHES,
    RECONCILIATION_TOP_N,
    SNAPSHOT_RECALIBRATION_SEC,
)
from data.kalshi_rest import get_market_orderbook, get_open_markets
from data.kalshi_ws import connect_and_subscribe
from engine.book_microstructure import on_live_orderbook_update, reset_book_microstructure_for_new_market
from engine.live_pricing import reset_live_pricing_for_new_market
from engine.market_stream.bootstrap import BufferedDelta, try_bootstrap_from_rest
from engine.market_stream.discovery import is_market_closed, parse_iso8601_to_epoch, select_target_market
from engine.market_stream.display import top_levels_for_display
from engine.market_stream.reconciliation_runner import run_recalibration
from engine.orderbook import OrderBook
from engine.stream_metrics import (
    _count_incoming_message,
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


def get_live_book() -> Optional[OrderBook]:
    """Read-only accessor for the active Kalshi orderbook stream state."""
    return live_book


def get_live_market_info() -> dict:
    """Returns metadata for the currently tracked Kalshi market."""
    with _live_market_info_lock:
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

    read_depth = max(1, int(depth))
    # Pull a wider top-N slice, then apply actionable filtering for display.
    raw_depth = max(read_depth * 4, read_depth)
    if hasattr(book, "get_orderbook_top_n"):
        yes_bids, yes_asks, no_bids, no_asks = book.get_orderbook_top_n(raw_depth)
    else:
        yes_bids, yes_asks, no_bids, no_asks = book.get_orderbook()
    return {
        "initialized": True,
        "market_ticker": book.market_ticker,
        "expected_seq": book.expected_seq,
        "yes_bids": top_levels_for_display(yes_bids, read_depth),
        "yes_asks": top_levels_for_display(yes_asks, read_depth),
        "no_bids": top_levels_for_display(no_bids, read_depth),
        "no_asks": top_levels_for_display(no_asks, read_depth),
    }


async def _stream_with_sync(market_ticker: str, book: OrderBook, market_close_ts: float | None = None) -> None:
    """WS stream loop with REST snapshot bootstrap and sequence-safe delta replay."""
    while True:
        if is_market_closed(market_close_ts):
            logger.info("Market %s reached close time; rotating stream target...", market_ticker)
            return

        try:
            book.reset()
            buffered_deltas: list[BufferedDelta] = []
            bootstrapped = False
            ws_snapshot_seq: int | None = None
            last_recalibration_at = time.monotonic()
            consecutive_recon_breaches = 0

            rest_snapshot_task = asyncio.create_task(asyncio.to_thread(get_market_orderbook, market_ticker))

            ws = await connect_and_subscribe(market_ticker)
            logger.info("Subscribed to %s. Bootstrapping from REST snapshot...", market_ticker)

            async for message in ws:
                if is_market_closed(market_close_ts):
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
                    bootstrapped, reconnect_now, last_recalibration_at = try_bootstrap_from_rest(
                        book=book,
                        rest_snapshot_task=rest_snapshot_task,
                        ws_snapshot_seq=ws_snapshot_seq,
                        buffered_deltas=buffered_deltas,
                    )
                    if reconnect_now:
                        break

                if bootstrapped and time.monotonic() - last_recalibration_at >= SNAPSHOT_RECALIBRATION_SEC:
                    consecutive_recon_breaches, action = await run_recalibration(
                        market_ticker=market_ticker,
                        book=book,
                        consecutive_recon_breaches=consecutive_recon_breaches,
                        recon_top_n=RECONCILIATION_TOP_N,
                        recon_consecutive_breaches=RECONCILIATION_CONSECUTIVE_BREACHES,
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


async def run_market_streamer() -> None:
    """Tracks the active selected 15-minute crypto market and rotates on close."""
    global live_book, _live_market_info
    current_market = None

    while True:
        switched_asset, asset_context = apply_queued_asset_switch_and_get_context()
        profile = asset_context.profile

        if switched_asset:
            logger.info("Applied queued asset switch. Active asset is now %s.", profile.asset)
            current_market = None

        logger.info(
            "Fetching active %s 15m market to stream (%s).",
            profile.display_name,
            profile.kalshi_series_ticker,
        )
        markets = get_open_markets(profile.kalshi_series_ticker)

        if not markets:
            logger.info("No active markets found. Retrying in %ss...", RECONNECT_DELAY_SEC)
            with _live_market_info_lock:
                _live_market_info.clear()
                _live_market_info.update(
                    {
                        "active_asset": profile.asset,
                        "active_asset_display": profile.display_name,
                        "active_series": profile.kalshi_series_ticker,
                    }
                )
            await asyncio.sleep(RECONNECT_DELAY_SEC)
            continue

        selected_market = select_target_market(markets)
        target_market = selected_market.get("ticker")

        if not target_market:
            logger.warning("No valid market ticker found. Retrying in %ss...", RECONNECT_DELAY_SEC)
            with _live_market_info_lock:
                _live_market_info.clear()
                _live_market_info.update(
                    {
                        "active_asset": profile.asset,
                        "active_asset_display": profile.display_name,
                        "active_series": profile.kalshi_series_ticker,
                    }
                )
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
            _live_market_info.update(
                {
                    "active_asset": profile.asset,
                    "active_asset_display": profile.display_name,
                    "active_series": profile.kalshi_series_ticker,
                }
            )

        close_ts = parse_iso8601_to_epoch(selected_market.get("close_time"))
        live_book = OrderBook(target_market)
        await _stream_with_sync(target_market, live_book, market_close_ts=close_ts)

        # Stream exits on market close/rotation event; immediately discover the next one.
        await asyncio.sleep(1)
