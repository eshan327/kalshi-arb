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



def get_cfbenchmarks_history(
    index_id: str,
    *,
    timestamp: str,
    timespan: str = "HOUR",
    max_resolution: str | None = "PER_SECOND",
) -> list[dict]:
    """Fetch one fixed CF Benchmarks historical window through Kalshi's passthrough."""
    from data.kalshi_trading import _request

    params: dict[str, Any] = {
        "id": index_id,
        "timespan": timespan,
        "timestamp": timestamp,
    }
    if max_resolution:
        params["maxResolution"] = max_resolution
    payload = _request("GET", "/cfbenchmarks/history/values", params=params)
    data = payload.get("data", {})
    rows = data.get("payload", [])
    if isinstance(rows, dict):
        rows = rows.get("values", rows.get("data", []))
    if not isinstance(rows, list):
        raise ValueError("Unexpected CF Benchmarks history payload")
    return rows


def get_historical_markets(*, series_ticker: str) -> list[dict[str, Any]]:
    """Fetch every archived market for one Kalshi series."""
    from data.kalshi_trading import _pages

    return _pages(
        "/historical/markets",
        "markets",
        {"series_ticker": series_ticker},
    )


def get_historical_trades(
    *,
    ticker: str,
    min_ts: int | None = None,
    max_ts: int | None = None,
    include_block_trades: bool = False,
) -> list[dict[str, Any]]:
    """Fetch archived public trades for a market."""
    from data.kalshi_trading import _pages

    params: dict[str, Any] = {"ticker": ticker}
    if min_ts is not None:
        params["min_ts"] = int(min_ts)
    if max_ts is not None:
        params["max_ts"] = int(max_ts)
    if not include_block_trades:
        params["is_block_trade"] = False
    return _pages("/historical/trades", "trades", params)


def get_historical_candlesticks(
    *,
    ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int = 1,
) -> list[dict[str, Any]]:
    """Fetch archived bid/ask/trade candles. period_interval is minutes."""
    if period_interval not in {1, 60, 1440}:
        raise ValueError("period_interval must be 1, 60, or 1440")
    from data.kalshi_trading import _request

    payload = _request(
        "GET",
        f"/historical/markets/{ticker}/candlesticks",
        params={
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
            "period_interval": int(period_interval),
        },
    )
    return payload.get("candlesticks", [])
