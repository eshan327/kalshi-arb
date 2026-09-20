import time
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode

import requests

from core.config import API_BASE_URL

HTTP_TIMEOUT_SEC = 10.0


def _get_json(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=HTTP_TIMEOUT_SEC)
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
        rows.extend(payload.get(key, []))
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
    by itself is incomplete. The private _data_tier marker is used only to route later
    candle/trade reads and is never sent back to Kalshi.
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

    merged: dict[str, dict[str, Any]] = {}
    for tier, rows in (("historical", archived), ("live", recent)):
        for raw in rows:
            ticker = str(raw.get("ticker") or "")
            if not ticker:
                continue
            market = dict(raw)
            market["_data_tier"] = tier
            merged[ticker] = market
    return list(merged.values())


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
    rows = _public_pages(
        "/historical/markets",
        "markets",
        {"series_ticker": series_ticker},
    )
    return [{**row, "_data_tier": "historical"} for row in rows]


def get_market_trades(
    *,
    ticker: str,
    min_ts: int | None = None,
    max_ts: int | None = None,
    include_block_trades: bool = False,
    historical: bool = False,
) -> list[dict[str, Any]]:
    """Fetch public trades from the correct live/archive tier."""
    params: dict[str, Any] = {"ticker": ticker}
    if min_ts is not None:
        params["min_ts"] = int(min_ts)
    if max_ts is not None:
        params["max_ts"] = int(max_ts)
    if not include_block_trades:
        params["is_block_trade"] = "false"
    path = "/historical/trades" if historical else "/markets/trades"
    return _public_pages(path, "trades", params)


def get_historical_trades(
    *,
    ticker: str,
    min_ts: int | None = None,
    max_ts: int | None = None,
    include_block_trades: bool = False,
) -> list[dict[str, Any]]:
    return get_market_trades(
        ticker=ticker,
        min_ts=min_ts,
        max_ts=max_ts,
        include_block_trades=include_block_trades,
        historical=True,
    )


def get_market_candlesticks(
    *,
    series_ticker: str,
    ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int = 1,
    historical: bool = False,
) -> list[dict[str, Any]]:
    """Fetch bid/ask/trade candles from the correct Kalshi data tier."""
    if period_interval not in {1, 60, 1440}:
        raise ValueError("period_interval must be 1, 60, or 1440")
    params = {
        "start_ts": int(start_ts),
        "end_ts": int(end_ts),
        "period_interval": int(period_interval),
    }
    if historical:
        path = f"/historical/markets/{ticker}/candlesticks"
    else:
        path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
    payload = _get_json(f"{API_BASE_URL}{path}?{urlencode(params)}")
    return payload.get("candlesticks", [])


def get_historical_candlesticks(
    *,
    ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int = 1,
) -> list[dict[str, Any]]:
    """Backward-compatible archived-candle helper."""
    return get_market_candlesticks(
        series_ticker="",
        ticker=ticker,
        start_ts=start_ts,
        end_ts=end_ts,
        period_interval=period_interval,
        historical=True,
    )
