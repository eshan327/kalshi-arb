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