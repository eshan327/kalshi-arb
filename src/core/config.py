import os

from dotenv import load_dotenv

load_dotenv()

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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


# Execution starts stopped; the operator chooses Paper or Live in the dashboard.
PAPER_STARTING_CASH_CENTS = max(
    100, _env_int("KALSHI_PAPER_STARTING_CASH_CENTS", 100_000)
)
EXECUTION_LOOP_INTERVAL_SEC = max(
    0.25, _env_float("KALSHI_EXECUTION_LOOP_INTERVAL_SEC", 1.0)
)
EXECUTION_EVENTS_MAXLEN = max(200, _env_int("KALSHI_EXECUTION_EVENTS_MAXLEN", 8_000))
EXECUTION_EVENTS_PATH = os.getenv(
    "KALSHI_EXECUTION_EVENTS_PATH", ".runtime/execution_events.jsonl"
)
EXECUTION_STATE_PATH = os.getenv(
    "KALSHI_EXECUTION_STATE_PATH", ".runtime/trading_state.json"
)
