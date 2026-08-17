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

    url = f"{API_BASE_URL}/markets?series_ticker={series_ticker}&status=open"
    return _get_json(url).get("markets", [])


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


def get_market_orderbook(market_ticker: str) -> dict[str, Any]:
    """Fetches the raw orderbook of a specific market."""

    url = f"{API_BASE_URL}/markets/{market_ticker}/orderbook"
    payload = _get_json(url)
    book = payload.get("orderbook_fp") or payload.get("orderbook") or {}
    if "yes_dollars" in book or "no_dollars" in book:
        return {
            "yes_dollars_fp": book.get("yes_dollars", []),
            "no_dollars_fp": book.get("no_dollars", []),
        }
    return book
