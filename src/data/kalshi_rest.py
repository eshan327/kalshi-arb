import time
from functools import lru_cache
from threading import Lock
from typing import Any
from urllib.parse import urlencode

import requests

from core.config import API_BASE_URL

HTTP_TIMEOUT_SEC = 10.0
PUBLIC_MIN_INTERVAL_SEC = 0.22
PUBLIC_MAX_RETRIES = 6

_public_lock = Lock()
_public_last_request_monotonic = 0.0
_public_session = requests.Session()


def _get_json(url: str) -> dict[str, Any]:
    """GET public Kalshi data with conservative pacing and 429 retry handling."""
    global _public_last_request_monotonic
    for attempt in range(PUBLIC_MAX_RETRIES + 1):
        with _public_lock:
            now = time.monotonic()
            wait = PUBLIC_MIN_INTERVAL_SEC - (now - _public_last_request_monotonic)
            if wait > 0:
                time.sleep(wait)
            response = _public_session.get(url, timeout=HTTP_TIMEOUT_SEC)
            _public_last_request_monotonic = time.monotonic()

        if response.status_code != 429:
            response.raise_for_status()
            return response.json()

        if attempt >= PUBLIC_MAX_RETRIES:
            response.raise_for_status()

        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after is not None else 0.0
        except ValueError:
            delay = 0.0
        if delay <= 0:
            delay = min(8.0, 0.5 * (2**attempt))
        time.sleep(delay)

    raise RuntimeError("unreachable")


def _public_pages(path: str, key: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        query = {**params, "limit": 1000}
        if cursor:
            query["cursor"] = cursor
        payload = _get_json(f"{API_BASE_URL}{path}?{urlencode(query)}")
        page = payload[key]
        if not isinstance(page, list):
            raise ValueError(f"Invalid {key} response")
        rows.extend(page)
        next_cursor = payload.get("cursor")
        if not next_cursor:
            return rows
        if next_cursor == cursor or next_cursor in seen:
            raise RuntimeError(f"Repeated pagination cursor for {path}")
        seen.add(next_cursor)
        cursor = next_cursor


def get_open_markets(series_ticker: str) -> list[dict[str, Any]]:
    """Fetch open markets for a series."""
    return _public_pages(
        "/markets",
        "markets",
        {"series_ticker": series_ticker, "status": "open"},
    )


def get_settled_markets(series_ticker: str) -> list[dict[str, Any]]:
    """
    Fetch the complete settled-market history across Kalshi's live and archive tiers.

    Kalshi partitions settled markets at a moving historical cutoff, so either endpoint
    by itself is incomplete. The live copy wins if a market appears in both.
    """
    recent = _public_pages(
        "/markets",
        "markets",
        {"series_ticker": series_ticker, "status": "settled"},
    )
    archived = _public_pages(
        "/historical/markets",
        "markets",
        {"series_ticker": series_ticker},
    )

    return list({row["ticker"]: row for row in [*archived, *recent]}.values())


@lru_cache(maxsize=128)
def _get_cached(path: str, five_minute_bucket: int) -> dict[str, Any]:
    del five_minute_bucket
    return _get_json(f"{API_BASE_URL}{path}")


def get_series(series_ticker: str) -> dict[str, Any]:
    return _get_cached(f"/series/{series_ticker}", int(time.time() // 300)).get(
        "series", {}
    )


def get_event(event_ticker: str) -> dict[str, Any]:
    return _get_cached(f"/events/{event_ticker}", int(time.time() // 300)).get(
        "event", {}
    )


def get_market(market_ticker: str) -> dict[str, Any]:
    return _get_json(f"{API_BASE_URL}/markets/{market_ticker}").get("market", {})


def invalidate_metadata() -> None:
    _get_cached.cache_clear()


def get_recent_index_values(index_id: str) -> list[dict]:
    from data.kalshi_trading import _request

    return _request(
        "GET",
        "/cfbenchmarks/values",
        params={"id": index_id, "maxResolution": "PER_SECOND"},
    )["data"]["payload"]


def get_cfbenchmarks_history(
    index_id: str,
    *,
    timestamp: str,
    timespan: str = "HOUR",
) -> list[dict]:
    """Fetch one fixed CF Benchmarks historical window through Kalshi's passthrough.

    The CF /history/values endpoint returns the published historical ticks and does
    not expose the maxResolution selector used by the recent-values endpoints.
    """
    from data.kalshi_trading import _request

    params: dict[str, Any] = {
        "id": index_id,
        "timespan": timespan,
        "timestamp": timestamp,
    }
    payload = _request("GET", "/cfbenchmarks/history/values", params=params)
    rows = payload["data"]["payload"]
    if isinstance(rows, dict):
        rows = rows.get("values", rows.get("data", []))
    if not isinstance(rows, list):
        raise ValueError("Unexpected CF Benchmarks history payload")
    return rows


def get_market_trades(
    *,
    ticker: str,
    min_ts: int,
    max_ts: int,
) -> list[dict[str, Any]]:
    """Read both tiers and deduplicate at the moving archive boundary."""
    params = {
        "ticker": ticker,
        "min_ts": min_ts,
        "max_ts": max_ts,
        "is_block_trade": "false",
    }
    merged = {}
    for path in ("/historical/trades", "/markets/trades"):
        for trade in _public_pages(path, "trades", params):
            merged[trade["trade_id"]] = trade
    return list(merged.values())
