import os

KALSHI_ENV = os.getenv("KALSHI_ENV", "demo").lower()

# Optional overrides (must include /trade-api/v2 for REST).
_DEFAULT_PROD_API = "https://external-api.kalshi.com/trade-api/v2"
_DEFAULT_DEMO_API = "https://external-api.demo.kalshi.co/trade-api/v2"
if KALSHI_ENV == "prod":
    API_BASE_URL = os.getenv("KALSHI_API_BASE_URL", _DEFAULT_PROD_API)
else:
    API_BASE_URL = os.getenv("KALSHI_API_BASE_URL", _DEFAULT_DEMO_API)

_DEFAULT_PROD_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
_DEFAULT_DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
if KALSHI_ENV == "prod":
    WS_BASE_URL = os.getenv("KALSHI_WS_BASE_URL", _DEFAULT_PROD_WS)
else:
    WS_BASE_URL = os.getenv("KALSHI_WS_BASE_URL", _DEFAULT_DEMO_WS)

# Market selection defaults
MARKET_ASSET_DEFAULT = os.getenv("KALSHI_MARKET_ASSET", "BTC").upper()
