import os
from dotenv import load_dotenv

load_dotenv()

# Valid modes: 'OBSERVE', 'PAPER', 'LIVE'
EXECUTION_MODE = os.getenv("KALSHI_EXECUTION_MODE", "OBSERVE").upper()

KALSHI_ENV = os.getenv("KALSHI_ENV", "demo").lower()

# Optional overrides if Kalshi changes hosts (must include /trade-api/v2 for REST)
_DEFAULT_PROD_API = "https://api.elections.kalshi.com/trade-api/v2"
_DEFAULT_DEMO_API = "https://demo-api.kalshi.co/trade-api/v2"
if KALSHI_ENV == "prod":
    API_BASE_URL = os.getenv("KALSHI_API_BASE_URL", _DEFAULT_PROD_API)
else:
    API_BASE_URL = os.getenv("KALSHI_API_BASE_URL", _DEFAULT_DEMO_API)

_DEFAULT_PROD_WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
_DEFAULT_DEMO_WS = "wss://demo-api.kalshi.co/trade-api/ws/v2"
if KALSHI_ENV == "prod":
    WS_BASE_URL = os.getenv("KALSHI_WS_BASE_URL", _DEFAULT_PROD_WS)
else:
    WS_BASE_URL = os.getenv("KALSHI_WS_BASE_URL", _DEFAULT_DEMO_WS)

# Flask app defaults
WEB_HOST = "127.0.0.1"
WEB_PORT = 5000

# Dashboard/view defaults
ORDERBOOK_VIEW_DEPTH = 10
WS_LOG_MAXLEN = 5000
WS_LOG_DEFAULT_LIMIT = 200

# Data/compute cadence defaults
BRTI_RECALC_INTERVAL_SEC = 1.0
SNAPSHOT_RECALIBRATION_SEC = 30.0

# Reconciliation policy defaults
RECONCILIATION_TOP_N = 10
RECONCILIATION_PRICE_TOL_CENTS = 0.01
RECONCILIATION_QTY_TOL = 1.0
RECONCILIATION_CONSECUTIVE_BREACHES = 3

# Market selection defaults
MARKET_ASSET_DEFAULT = os.getenv("KALSHI_MARKET_ASSET", "BTC").upper()
MARKET_SELECTION_STATE_PATH = os.getenv(
    "KALSHI_MARKET_SELECTION_STATE_PATH",
    ".runtime/market_selection.json",
)