import time
from functools import lru_cache
from typing import Any

import requests

from core.config import API_BASE_URL

HTTP_TIMEOUT_SEC = 10.0


def _get_json(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=HTTP_TIMEOUT_SEC)
    response.raise_for_status()
    return response.json()


def get_open_markets(series_ticker: str) -> list[dict[str, Any]]:
    """Fetches open markets for a series."""

    from urllib.parse import urlencode

    rows, cursor = [], None
    while True:
        params = {"series_ticker": series_ticker, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        payload = _get_json(f"{API_BASE_URL}/markets?{urlencode(params)}")
        rows.extend(payload.get("markets", []))
        next_cursor = payload.get("cursor")
        if not next_cursor:
            return rows
        if next_cursor == cursor:
            raise RuntimeError("Repeated market cursor")
        cursor = next_cursor


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
