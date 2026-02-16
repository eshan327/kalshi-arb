import os
from dotenv import load_dotenv
from kalshi_python_sync.configuration import Configuration
from kalshi_python_sync import KalshiClient
from core.config import KALSHI_ENV, API_BASE_URL 

def get_authenticated_client() -> KalshiClient:
    """
    Returns an authenticated KalshiClient based on .env variables
    """
    
    load_dotenv()
    
    if KALSHI_ENV == "prod":
        key_id = os.getenv("KALSHI_PROD_KEY_ID")
        key_path = os.getenv("KALSHI_PROD_KEY_PATH")
    else:
        key_id = os.getenv("KALSHI_DEMO_KEY_ID")
        key_path = os.getenv("KALSHI_DEMO_KEY_PATH")

    if not key_id or not key_path:
        raise ValueError(f"Missing Kalshi {KALSHI_ENV} credentials.")

    try:
        with open(key_path, "r") as f:
            private_key = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"Private key not found at {key_path}")

    # SDK configuration using the centralized URL
    config = Configuration(host=API_BASE_URL)
    config.api_key_id = key_id
    config.private_key_pem = private_key
    
    return KalshiClient(config)