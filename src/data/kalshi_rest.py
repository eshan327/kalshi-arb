import time
from threading import Lock
from typing import Any
from urllib.parse import urlencode
from urllib.parse import urlparse

import requests

from core.config import API_BASE_URL
from core.auth import get_api_auth_headers

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


def _signed_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Authenticated read-only request; there is no order-capable client here."""
    sign_path = f"{urlparse(API_BASE_URL).path.rstrip('/')}{path}"
    response = _public_session.get(
        f"{API_BASE_URL}{path}",
        params=params,
        headers=get_api_auth_headers("GET", sign_path),
        timeout=HTTP_TIMEOUT_SEC,
    )
    response.raise_for_status()
    return response.json()


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


def get_settled_markets(
    series_ticker: str,
    *,
    min_close_ts: float | None = None,
    archival_cutoff_ts: float | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch the complete settled-market history across Kalshi's live and archive tiers.

    Kalshi partitions settled markets at a moving historical cutoff, so either endpoint
    by itself is incomplete. The live copy wins if a market appears in both.
    """
    recent_params: dict[str, Any] = {
        "series_ticker": series_ticker, "status": "settled"
    }
    if min_close_ts is not None:
        # Settlement cannot precede close, so this includes every eligible market.
        recent_params["min_settled_ts"] = int(min_close_ts) - 1
    recent = _public_pages(
        "/markets",
        "markets",
        recent_params,
    )
    archived = (
        [] if min_close_ts is not None and archival_cutoff_ts is not None
        and min_close_ts >= archival_cutoff_ts
        else _public_pages(
            "/historical/markets", "markets", {"series_ticker": series_ticker}
        )
    )

    return list({row["ticker"]: row for row in [*archived, *recent]}.values())


def get_historical_cutoff() -> dict[str, str]:
    """Each archived data type has its own moving cutoff."""
    return _get_json(f"{API_BASE_URL}/historical/cutoff")


def get_recent_index_values(index_id: str) -> list[dict]:
    return _signed_get(
        "/cfbenchmarks/values",
        {"id": index_id, "maxResolution": "PER_SECOND"},
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
    params: dict[str, Any] = {
        "id": index_id,
        "timespan": timespan,
        "timestamp": timestamp,
    }
    payload = _signed_get("/cfbenchmarks/history/values", params)
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
    cutoff_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Read the applicable tier(s), deduplicating at the moving boundary."""
    params = {
        "ticker": ticker,
        "min_ts": min_ts,
        "max_ts": max_ts,
        "is_block_trade": "false",
    }
    merged = {}
    if cutoff_ts is None or min_ts < cutoff_ts <= max_ts:
        paths = ("/historical/trades", "/markets/trades")
    elif max_ts < cutoff_ts:
        paths = ("/historical/trades",)
    else:
        paths = ("/markets/trades",)
    for path in paths:
        for trade in _public_pages(path, "trades", params):
            merged[trade["trade_id"]] = trade
    return list(merged.values())
