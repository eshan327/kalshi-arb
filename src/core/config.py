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