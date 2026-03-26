import os
from dotenv import load_dotenv

load_dotenv()

# Valid modes: 'OBSERVE', 'PAPER', 'LIVE'
EXECUTION_MODE = os.getenv("KALSHI_EXECUTION_MODE", "OBSERVE").upper()

KALSHI_ENV = os.getenv("KALSHI_ENV", "demo").lower()

if KALSHI_ENV == "prod":
    API_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
else:
    API_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

if KALSHI_ENV == "prod":
    WS_BASE_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
else:
    WS_BASE_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"

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