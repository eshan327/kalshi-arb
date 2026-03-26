import requests
from typing import Any
from core.config import API_BASE_URL

HTTP_TIMEOUT_SEC = 10.0


def _get_json(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=HTTP_TIMEOUT_SEC)
    response.raise_for_status()
    return response.json()

def get_open_markets(series_ticker: str) -> list[dict[str, Any]]:
    """Fetches open markets for a series."""

    url = f"{API_BASE_URL}/markets?series_ticker={series_ticker}&status=open"
    return _get_json(url).get('markets', [])

def get_market_orderbook(market_ticker: str) -> dict[str, Any]:
    """Fetches the raw orderbook of a specific market."""

    url = f"{API_BASE_URL}/markets/{market_ticker}/orderbook"
    return _get_json(url).get('orderbook', {})