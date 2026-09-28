"""Read-only Kalshi Perps REST and raw, sequence-checked WebSocket capture."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from contextlib import aclosing

from websockets.exceptions import ConnectionClosed, InvalidStatus

from core.config import API_BASE_URL, PERPS_API_BASE_URL, PERPS_WS_BASE_URL
from data.capture import FeedCapture
from data.kalshi_rest import _get_json, _pages
from data.kalshi_ws import command, connect

logger = logging.getLogger(__name__)
_PUBLIC_READS = re.compile(
    r"/(?:markets(?:/[^/]+(?:/(?:orderbook|candlesticks))?)?|trades|"
    r"exchange/status|risk_parameters|funding_rates/(?:historical|estimate))"
)
_PUBLIC_CHANNELS = ("orderbook_delta", "ticker", "trade")


def get_perps(path: str, **params) -> dict:
    """GET a margin-relative route (e.g. /balance); preserve fixed-point strings.

    Public routes need no key. All other reads use the configured environment's
    credentials. Pass the API's exact query names; None values are omitted.
    """
    if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", path):
        raise ValueError("Expected a margin-relative API path without a query string")
    query = {
        key: str(value).lower() if isinstance(value, bool) else value
        for key, value in params.items() if value is not None
    }
    return _get_json(
        f"{PERPS_API_BASE_URL}{path}",
        authenticated=_PUBLIC_READS.fullmatch(path) is None,
        params=query,
    )


def get_perps_limits() -> dict:
    """Perps limits live outside /margin, at /account/limits/perps."""
    return _get_json(f"{API_BASE_URL.rstrip('/')}/account/limits/perps", authenticated=True)


def get_perps_pages(path: str, key: str, **params) -> list[dict]:
    """Collect cursor-paginated trades, orders, fills, or order groups."""
    return _pages(path, key, params, lambda route, query: get_perps(route, **query))


async def _session(ticker: str, capture_path: str | None):
    async with await connect(url=PERPS_WS_BASE_URL) as ws:
        capture = FeedCapture(capture_path, f"perps-{time.time_ns()}")
        try:
            pending = set(_PUBLIC_CHANNELS)
            for channel in _PUBLIC_CHANNELS:
                await command(
                    ws, "subscribe", channels=[channel], market_tickers=[ticker],
                    **({"send_initial_snapshot": True} if channel == "ticker" else {}),
                )
            deadline = time.monotonic() + 10
            sequences = {}
            snapshot_sid = None
            while True:
                # Once subscribed and snapshotted, protocol ping/pong handles idle feeds.
                timeout = max(0, deadline - time.monotonic()) if pending or snapshot_sid is None else None
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if not isinstance(data, dict):
                    raise ValueError("Invalid Perps WebSocket message")
                capture.record(data)
                kind, msg = data.get("type"), data.get("msg", {})
                if kind == "error":
                    raise ValueError(f"Perps subscription rejected: {msg}")
                if kind == "subscribed":
                    pending.discard(msg["channel"])
                    continue
                if kind not in {"orderbook_snapshot", "orderbook_delta", "ticker", "trade"}:
                    continue
                if not isinstance(msg, dict) or msg.get("market_ticker") != ticker:
                    raise ValueError("Unexpected Perps market payload")
                sid, seq = data.get("sid"), data.get("seq")
                if kind != "ticker":
                    if type(sid) is not int or type(seq) is not int:
                        raise ConnectionError("Perps message missing subscription/sequence")
                    previous = sequences.get(sid)
                    if previous is not None:
                        if seq <= previous:
                            continue
                        if seq != previous + 1:
                            raise ConnectionError("Perps sequence gap; reconnecting for a new snapshot")
                    sequences[sid] = seq
                if kind == "orderbook_snapshot":
                    snapshot_sid = sid
                elif kind == "orderbook_delta" and sid != snapshot_sid:
                    raise ConnectionError("Perps delta arrived before its snapshot")
                yield data
        finally:
            capture.close()


async def perps_messages(ticker: str, capture_path: str | None = None):
    """Stream raw public data; each reconnect opens a new capture session.

    A gap terminates the session rather than yielding discontinuous book updates.
    Subscription errors, invalid data, and local file failures propagate to caller.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", ticker):
        raise ValueError("Invalid Perps market ticker")
    delay = 0.5
    while True:
        try:
            async with aclosing(_session(ticker, capture_path)) as session:
                async for data in session:
                    delay = 0.5
                    yield data
        except InvalidStatus as exc:
            if exc.response.status_code not in {429, 500, 502, 503, 504}:
                raise
            logger.warning("Perps handshake unavailable: %s", exc)
        except (ConnectionClosed, ConnectionError, TimeoutError) as exc:
            logger.warning("Perps reconnecting: %s", exc)
        await asyncio.sleep(delay * random.uniform(0.8, 1.2))
        delay = min(8.0, delay * 2)
