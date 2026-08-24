from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import threading
from typing import Optional

import websockets

from core.asset_context import get_active_market_profile
from data.kalshi_rest import get_open_markets
from data.kalshi_ws import connect_and_subscribe, request_orderbook_snapshot
from engine.live_pricing import reset_live_pricing_for_new_market
from engine.market_stream.discovery import (
    is_market_closed,
    parse_iso8601_to_epoch,
    select_target_market,
)
from engine.market_stream.display import top_levels_for_display
from engine.orderbook import OrderBook

logger = logging.getLogger(__name__)
RECONNECT_DELAY_SEC = 5
WS_RECONNECT_MIN_SEC = 0.5
WS_RECONNECT_MAX_SEC = 8.0
SNAPSHOT_RECOVERY_TIMEOUT_SEC = 3.0

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


def _set_live_market_info(profile, market: dict | None = None) -> None:
    with _live_market_info_lock:
        _live_market_info.clear()
        if isinstance(market, dict):
            _live_market_info.update(dict(market))
        _live_market_info.update(
            {
                "active_asset": profile.asset,
                "active_asset_display": profile.display_name,
                "active_series": profile.kalshi_series_ticker,
            }
        )


def get_live_orderbook_snapshot(depth: int = 10) -> dict:
    """Returns a serializable orderbook snapshot for UI consumers."""
    book = live_book
    if book is None or not book.initialized or book.needs_resync:
        return {
            "initialized": False,
            "market_ticker": None,
            "expected_seq": None,
            "last_update_ts": None,
            "yes_bids": [],
            "yes_asks": [],
            "no_bids": [],
            "no_asks": [],
        }

    read_depth = max(1, int(depth))
    # Pull a wider top-N slice, then apply actionable filtering for display.
    raw_depth = max(read_depth * 4, read_depth)
    yes_bids, yes_asks, no_bids, no_asks = book.get_orderbook_top_n(raw_depth)
    return {
        "initialized": True,
        "market_ticker": book.market_ticker,
        "expected_seq": book.expected_seq,
        "last_update_ts": book.last_update_ts,
        "yes_bids": top_levels_for_display(yes_bids, read_depth),
        "yes_asks": top_levels_for_display(yes_asks, read_depth),
        "no_bids": top_levels_for_display(no_bids, read_depth),
        "no_asks": top_levels_for_display(no_asks, read_depth),
    }


async def _stream_with_sync(
    market_ticker: str, book: OrderBook, market_close_ts: float | None = None
) -> None:
    """Maintain a sequence-aligned book and recover gaps on the same socket."""
    reconnect_delay = WS_RECONNECT_MIN_SEC
    while True:
        if is_market_closed(market_close_ts):
            logger.info(
                "Market %s reached close time; rotating stream target...", market_ticker
            )
            return

        ws = None
        try:
            book.reset()
            buffered_deltas: list[tuple[int, dict]] = []
            bootstrapped = False
            orderbook_sid: int | None = None
            snapshot_deadline: float | None = None

            ws = await connect_and_subscribe(market_ticker)
            logger.info("Subscribed to %s. Waiting for snapshot...", market_ticker)

            while True:
                timeout = (
                    max(0.0, snapshot_deadline - asyncio.get_running_loop().time())
                    if snapshot_deadline is not None
                    else None
                )
                try:
                    message = (
                        await asyncio.wait_for(ws.recv(), timeout)
                        if timeout is not None
                        else await ws.recv()
                    )
                except TimeoutError:
                    logger.warning(
                        "Orderbook snapshot recovery timed out for %s; reconnecting...",
                        market_ticker,
                    )
                    break

                if is_market_closed(market_close_ts):
                    logger.info(
                        "Market %s reached close time; reconnect loop stopped.",
                        market_ticker,
                    )
                    await ws.close()
                    return

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("Ignoring malformed Kalshi WebSocket message.")
                    continue
                if not isinstance(data, dict):
                    logger.warning("Ignoring non-object Kalshi WebSocket message.")
                    continue
                msg_type = data.get("type")
                seq = data.get("seq")
                raw_payload = data.get("msg")
                msg_payload = raw_payload if isinstance(raw_payload, dict) else {}

                if msg_type == "orderbook_snapshot":
                    if not isinstance(seq, int):
                        break
                    book.load_ws_snapshot(msg_payload, seq)
                    bootstrapped = True
                    for buffered_seq, buffered_msg in sorted(
                        buffered_deltas, key=lambda item: item[0]
                    ):
                        if book.apply_delta_with_seq(buffered_seq, buffered_msg):
                            continue
                        if buffered_seq < (book.expected_seq or 0):
                            continue
                        break
                    buffered_deltas.clear()
                    snapshot_deadline = None
                    if book.needs_resync:
                        break
                    reconnect_delay = WS_RECONNECT_MIN_SEC
                    continue

                if msg_type == "orderbook_delta":
                    msg = msg_payload

                    if not isinstance(seq, int):
                        continue

                    if not bootstrapped:
                        buffered_deltas.append((seq, msg))
                        continue

                    if not book.apply_delta_with_seq(seq, msg):
                        if seq < (book.expected_seq or 0):
                            continue
                        if orderbook_sid is None:
                            break
                        logger.warning(
                            "Orderbook sequence gap; requesting an in-band snapshot..."
                        )
                        book.reset()
                        buffered_deltas.clear()
                        bootstrapped = False
                        snapshot_deadline = (
                            asyncio.get_running_loop().time()
                            + SNAPSHOT_RECOVERY_TIMEOUT_SEC
                        )
                        await request_orderbook_snapshot(
                            ws, market_ticker, orderbook_sid
                        )
                        continue

                elif msg_type == "subscribed":
                    channel = msg_payload.get("channel")
                    sid = msg_payload.get("sid")
                    if channel == "orderbook_delta" and isinstance(sid, int):
                        orderbook_sid = sid
                    logger.info(
                        "[SERVER] Subscription confirmed: %s",
                        channel,
                    )

                elif msg_type == "error":
                    logger.warning("Kalshi WebSocket error: %s", msg_payload)
                    break

                if book.needs_resync:
                    if orderbook_sid is None:
                        break
                    logger.warning(
                        "Invalid orderbook state; requesting an in-band snapshot..."
                    )
                    book.reset()
                    buffered_deltas.clear()
                    bootstrapped = False
                    snapshot_deadline = (
                        asyncio.get_running_loop().time()
                        + SNAPSHOT_RECOVERY_TIMEOUT_SEC
                    )
                    await request_orderbook_snapshot(ws, market_ticker, orderbook_sid)

        except (websockets.ConnectionClosed, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning(
                "WebSocket dropped (%s); reconnecting...", exc
            )
        finally:
            if ws is not None:
                with contextlib.suppress(websockets.ConnectionClosed, OSError):
                    await ws.close()

        delay = reconnect_delay * random.uniform(0.8, 1.2)
        logger.info("Reconnecting WebSocket in %.2fs...", delay)
        await asyncio.sleep(delay)
        reconnect_delay = min(WS_RECONNECT_MAX_SEC, reconnect_delay * 2)


async def run_market_streamer() -> None:
    """Tracks the process asset's 15-minute crypto market and rotates on close."""
    global live_book, _live_market_info
    current_market = None

    while True:
        profile = get_active_market_profile()

        logger.info(
            "Fetching active %s 15m market to stream (%s).",
            profile.display_name,
            profile.kalshi_series_ticker,
        )
        markets = get_open_markets(profile.kalshi_series_ticker)

        if not markets:
            logger.info(
                "No active markets found. Retrying in %ss...", RECONNECT_DELAY_SEC
            )
            _set_live_market_info(profile)
            await asyncio.sleep(RECONNECT_DELAY_SEC)
            continue

        selected_market = select_target_market(markets)
        target_market = selected_market.get("ticker")

        if not target_market:
            logger.warning(
                "No valid market ticker found. Retrying in %ss...", RECONNECT_DELAY_SEC
            )
            _set_live_market_info(profile)
            await asyncio.sleep(RECONNECT_DELAY_SEC)
            continue

        if target_market != current_market:
            logger.info("Target market: %s", target_market)
            reset_live_pricing_for_new_market()
            current_market = target_market

        _set_live_market_info(profile, selected_market)

        close_ts = parse_iso8601_to_epoch(selected_market.get("close_time"))
        live_book = OrderBook(target_market)
        await _stream_with_sync(target_market, live_book, market_close_ts=close_ts)

        # Stream exits on close/rotation; immediately discover the next market.
        await asyncio.sleep(1)
