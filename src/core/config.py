import os
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "y", "on"}:
        return True
    if val in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default

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

# Autonomous execution defaults
EXECUTION_ENABLED = _env_bool("KALSHI_EXECUTION_ENABLED", False)
_raw_execution_mode = os.getenv("KALSHI_EXECUTION_MODE", "paper").strip().lower() or "paper"
if _raw_execution_mode in {"observe", "paper", "live"}:
    EXECUTION_MODE = _raw_execution_mode
elif _raw_execution_mode in {"demo", "sim", "simulation"}:
    EXECUTION_MODE = "paper"
elif _raw_execution_mode in {"prod", "production"}:
    EXECUTION_MODE = "live"
else:
    EXECUTION_MODE = "paper"

EXECUTION_ALLOW_LIVE_IN_DEMO_ENV = _env_bool("KALSHI_EXECUTION_ALLOW_LIVE_IN_DEMO_ENV", False)
EXECUTION_POST_ONLY = _env_bool("KALSHI_EXECUTION_POST_ONLY", True)
EXECUTION_ORDER_TIME_IN_FORCE = (
    os.getenv("KALSHI_EXECUTION_ORDER_TIF", "good_till_canceled").strip().lower()
    or "good_till_canceled"
)
EXECUTION_LOOP_INTERVAL_SEC = max(0.25, _env_float("KALSHI_EXECUTION_LOOP_INTERVAL_SEC", 1.0))

# P(book) confirmation gate (P(model) remains the primary signal)
EXECUTION_REQUIRE_P_BOOK_CONFIRMATION = _env_bool(
    "KALSHI_EXECUTION_REQUIRE_P_BOOK_CONFIRMATION",
    True,
)
EXECUTION_P_BOOK_MIN_QUALITY = max(
    0.0,
    min(1.0, _env_float("KALSHI_EXECUTION_P_BOOK_MIN_QUALITY", 0.35)),
)
EXECUTION_P_BOOK_MAX_DIVERGENCE = max(
    0.01,
    min(0.49, _env_float("KALSHI_EXECUTION_P_BOOK_MAX_DIVERGENCE", 0.22)),
)

# Signal thresholds and sizing
EXECUTION_CONTRACT_SECONDS = max(60, _env_int("KALSHI_EXECUTION_CONTRACT_SECONDS", 900))
EXECUTION_MIN_CONFIDENCE = max(0.0, min(0.5, _env_float("KALSHI_EXECUTION_MIN_CONFIDENCE", 0.08)))
EXECUTION_MIN_EDGE_CENTS = max(0.01, _env_float("KALSHI_EXECUTION_MIN_EDGE_CENTS", 1.5))
EXECUTION_TARGET_EDGE_CENTS = max(
    EXECUTION_MIN_EDGE_CENTS,
    _env_float("KALSHI_EXECUTION_TARGET_EDGE_CENTS", 8.0),
)
EXECUTION_MIN_PROBABILITY = max(0.0, min(1.0, _env_float("KALSHI_EXECUTION_MIN_PROBABILITY", 0.08)))
EXECUTION_MAX_PROBABILITY = max(
    EXECUTION_MIN_PROBABILITY,
    min(1.0, _env_float("KALSHI_EXECUTION_MAX_PROBABILITY", 0.92)),
)
EXECUTION_MIN_TIMING_SCORE = max(0.0, min(1.0, _env_float("KALSHI_EXECUTION_MIN_TIMING_SCORE", 0.35)))

# Hard risk controls
EXECUTION_MAX_ORDER_CONTRACTS = max(1, _env_int("KALSHI_EXECUTION_MAX_ORDER_CONTRACTS", 10))
EXECUTION_MAX_MARKET_CONTRACTS = max(
    EXECUTION_MAX_ORDER_CONTRACTS,
    _env_int("KALSHI_EXECUTION_MAX_MARKET_CONTRACTS", 30),
)
EXECUTION_MAX_OPEN_ORDERS = max(1, _env_int("KALSHI_EXECUTION_MAX_OPEN_ORDERS", 4))
EXECUTION_MAX_DAILY_LOSS_CENTS = max(1, _env_int("KALSHI_EXECUTION_MAX_DAILY_LOSS_CENTS", 20_000))
EXECUTION_MIN_CASH_BUFFER_CENTS = max(0, _env_int("KALSHI_EXECUTION_MIN_CASH_BUFFER_CENTS", 5_000))

# Order management behavior
EXECUTION_ORDER_STALE_SEC = max(1.0, _env_float("KALSHI_EXECUTION_ORDER_STALE_SEC", 8.0))
EXECUTION_MAX_REPRICES = max(0, _env_int("KALSHI_EXECUTION_MAX_REPRICES", 2))

# Paper-only high-activity profile (used for demo/presentation flows)
_raw_paper_activity_profile = os.getenv("KALSHI_PAPER_ACTIVITY_PROFILE", "high_activity").strip().lower()
if _raw_paper_activity_profile in {"high_activity", "balanced"}:
    PAPER_ACTIVITY_PROFILE = _raw_paper_activity_profile
else:
    PAPER_ACTIVITY_PROFILE = "high_activity"

PAPER_ACTIVITY_PROFILE_ENABLED = _env_bool("KALSHI_PAPER_ACTIVITY_PROFILE_ENABLED", True)
PAPER_ACTIVITY_MIN_CONFIDENCE = max(
    0.0,
    min(0.5, _env_float("KALSHI_PAPER_ACTIVITY_MIN_CONFIDENCE", 0.02)),
)
PAPER_ACTIVITY_MIN_EDGE_CENTS = max(
    0.01,
    _env_float("KALSHI_PAPER_ACTIVITY_MIN_EDGE_CENTS", 0.35),
)
PAPER_ACTIVITY_MIN_TIMING_SCORE = max(
    0.0,
    min(1.0, _env_float("KALSHI_PAPER_ACTIVITY_MIN_TIMING_SCORE", 0.0)),
)
PAPER_ACTIVITY_REQUIRE_P_BOOK_CONFIRMATION = _env_bool(
    "KALSHI_PAPER_ACTIVITY_REQUIRE_P_BOOK_CONFIRMATION",
    True,
)
PAPER_ACTIVITY_P_BOOK_MIN_QUALITY = max(
    0.0,
    min(1.0, _env_float("KALSHI_PAPER_ACTIVITY_P_BOOK_MIN_QUALITY", 0.10)),
)
PAPER_ACTIVITY_P_BOOK_MAX_DIVERGENCE = max(
    0.01,
    min(0.49, _env_float("KALSHI_PAPER_ACTIVITY_P_BOOK_MAX_DIVERGENCE", 0.45)),
)
PAPER_ACTIVITY_ALLOW_TAKER = _env_bool("KALSHI_PAPER_ACTIVITY_ALLOW_TAKER", True)
_raw_taker_edge = os.getenv("KALSHI_PAPER_ACTIVITY_TAKER_EDGE_CENTS")
if _raw_taker_edge is None:
    _raw_taker_edge = os.getenv("KALSHI_PAPER_ACTIVITY_AGGRESSIVE_EDGE_CENTS")
try:
    _parsed_taker_edge = float(_raw_taker_edge.strip()) if isinstance(_raw_taker_edge, str) else 1.15
except ValueError:
    _parsed_taker_edge = 1.15
PAPER_ACTIVITY_TAKER_EDGE_CENTS = max(0.01, _parsed_taker_edge)
# Backward compatible alias for older env naming.
PAPER_ACTIVITY_AGGRESSIVE_EDGE_CENTS = PAPER_ACTIVITY_TAKER_EDGE_CENTS
PAPER_ACTIVITY_ORDER_STALE_SEC = max(
    1.0,
    _env_float("KALSHI_PAPER_ACTIVITY_ORDER_STALE_SEC", 2.5),
)
PAPER_ACTIVITY_FORCE_FILL_PER_WINDOW = _env_bool(
    "KALSHI_PAPER_ACTIVITY_FORCE_FILL_PER_WINDOW",
    True,
)
PAPER_ACTIVITY_FALLBACK_TRIGGER_SEC = max(
    10.0,
    _env_float("KALSHI_PAPER_ACTIVITY_FALLBACK_TRIGGER_SEC", 120.0),
)
PAPER_ACTIVITY_FALLBACK_RETRY_SEC = max(
    0.25,
    _env_float("KALSHI_PAPER_ACTIVITY_FALLBACK_RETRY_SEC", 1.5),
)
PAPER_ACTIVITY_FALLBACK_MIN_EDGE_CENTS = max(
    0.0,
    _env_float("KALSHI_PAPER_ACTIVITY_FALLBACK_MIN_EDGE_CENTS", 0.05),
)
PAPER_ACTIVITY_FALLBACK_BYPASS_P_BOOK = _env_bool(
    "KALSHI_PAPER_ACTIVITY_FALLBACK_BYPASS_P_BOOK",
    True,
)

# P(book) microstructure smoothing + cadence
P_BOOK_OBI_DEPTH = max(2, _env_int("KALSHI_P_BOOK_OBI_DEPTH", 10))
P_BOOK_MPP_WINDOW_SEC = max(3.0, _env_float("KALSHI_P_BOOK_MPP_WINDOW_SEC", 45.0))
P_BOOK_TRADE_WINDOW_SEC = max(3.0, _env_float("KALSHI_P_BOOK_TRADE_WINDOW_SEC", 120.0))
P_BOOK_MIN_SPREAD_CENTS = max(0.25, _env_float("KALSHI_P_BOOK_MIN_SPREAD_CENTS", 1.0))
P_BOOK_EMIT_INTERVAL_SEC = max(0.1, _env_float("KALSHI_P_BOOK_EMIT_INTERVAL_SEC", 0.5))
P_BOOK_FEATURE_EMA_ALPHA = max(
    0.01,
    min(1.0, _env_float("KALSHI_P_BOOK_FEATURE_EMA_ALPHA", 0.24)),
)
P_BOOK_PROB_EMA_ALPHA = max(
    0.01,
    min(1.0, _env_float("KALSHI_P_BOOK_PROB_EMA_ALPHA", 0.30)),
)
P_BOOK_OBI_CLIP = max(0.05, min(1.0, _env_float("KALSHI_P_BOOK_OBI_CLIP", 0.95)))
P_BOOK_TFI_CLIP = max(0.05, min(1.0, _env_float("KALSHI_P_BOOK_TFI_CLIP", 0.95)))
P_BOOK_MPP_CLIP = max(0.5, _env_float("KALSHI_P_BOOK_MPP_CLIP", 4.0))
P_BOOK_Z_CLIP = max(0.5, _env_float("KALSHI_P_BOOK_Z_CLIP", 6.0))
P_BOOK_MIN_TRADE_COUNT_FOR_QUALITY = max(
    1,
    _env_int("KALSHI_P_BOOK_MIN_TRADE_COUNT_FOR_QUALITY", 2),
)

# Paper simulator defaults
PAPER_SIM_STARTING_CASH_CENTS = max(1_000, _env_int("KALSHI_PAPER_SIM_STARTING_CASH_CENTS", 250_000))
PAPER_SIM_FILL_MODEL = os.getenv("KALSHI_PAPER_SIM_FILL_MODEL", "hybrid").strip().lower() or "hybrid"
PAPER_SIM_AGGRESSIVE_FILL_PROB = max(
    0.0,
    min(1.0, _env_float("KALSHI_PAPER_SIM_AGGRESSIVE_FILL_PROB", 0.96)),
)
PAPER_SIM_PASSIVE_BASE_FILL_PROB = max(
    0.0,
    min(1.0, _env_float("KALSHI_PAPER_SIM_PASSIVE_BASE_FILL_PROB", 0.15)),
)
PAPER_SIM_QUEUE_DECAY = max(0.0, _env_float("KALSHI_PAPER_SIM_QUEUE_DECAY", 0.22))
PAPER_SIM_QUEUE_AHEAD_FRACTION = max(
    0.0,
    _env_float("KALSHI_PAPER_SIM_QUEUE_AHEAD_FRACTION", 0.65),
)
PAPER_SIM_MIN_PARTIAL_FILL_FRACTION = max(
    0.05,
    min(1.0, _env_float("KALSHI_PAPER_SIM_MIN_PARTIAL_FILL_FRACTION", 0.25)),
)
PAPER_SIM_MAX_PARTIAL_FILL_FRACTION = max(
    PAPER_SIM_MIN_PARTIAL_FILL_FRACTION,
    min(1.0, _env_float("KALSHI_PAPER_SIM_MAX_PARTIAL_FILL_FRACTION", 0.9)),
)
PAPER_SIM_LATENCY_MS = max(0.0, _env_float("KALSHI_PAPER_SIM_LATENCY_MS", 70.0))
PAPER_SIM_PRICE_DRIFT_CENTS_PER_SEC = _env_float("KALSHI_PAPER_SIM_PRICE_DRIFT_CENTS_PER_SEC", 0.0)
PAPER_SIM_RANDOM_SEED = _env_int("KALSHI_PAPER_SIM_RANDOM_SEED", 17)

# Runtime telemetry + persistence
EXECUTION_EVENTS_MAXLEN = max(500, _env_int("KALSHI_EXECUTION_EVENTS_MAXLEN", 12_000))
EXECUTION_EVENTS_PATH = os.getenv(
    "KALSHI_EXECUTION_EVENTS_PATH",
    ".runtime/execution_events.jsonl",
)
EXECUTION_PERFORMANCE_PATH = os.getenv(
    "KALSHI_EXECUTION_PERFORMANCE_PATH",
    ".runtime/execution_performance.jsonl",
)

# Session exports
SESSION_EXPORT_ENABLED = _env_bool("KALSHI_SESSION_EXPORT_ENABLED", True)
SESSION_EXPORT_AUTO_ON_SHUTDOWN = _env_bool("KALSHI_SESSION_EXPORT_AUTO_ON_SHUTDOWN", True)
SESSION_EXPORT_DIR = os.getenv("KALSHI_SESSION_EXPORT_DIR", ".runtime/sessions")
SESSION_EXPORT_CHART_DPI = max(72, _env_int("KALSHI_SESSION_EXPORT_CHART_DPI", 110))
SESSION_EXPORT_MAX_ROWS = max(200, _env_int("KALSHI_SESSION_EXPORT_MAX_ROWS", 20_000))