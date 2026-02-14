import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

def get_open_markets(series_ticker: str) -> list:
    """Fetches open markets for a given series."""

    url = f"{BASE_URL}/markets?series_ticker={series_ticker}&status=open"
    response = requests.get(url)
    response.raise_for_status()
    return response.json().get('markets', [])

def get_market_orderbook(market_ticker: str) -> dict:
    """Fetches the raw orderbook of a specific market."""

    url = f"{BASE_URL}/markets/{market_ticker}/orderbook"
    response = requests.get(url)
    response.raise_for_status()
    return response.json().get('orderbook', {})