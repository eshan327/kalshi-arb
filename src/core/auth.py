import os
import time
import base64
from dotenv import load_dotenv
from kalshi_python_sync.configuration import Configuration
from kalshi_python_sync import KalshiClient
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from core.config import KALSHI_ENV, API_BASE_URL

def _get_credentials() -> tuple[str, str]:
    """Helper to load keys."""

    load_dotenv()

    if KALSHI_ENV == "prod":
        key_id = os.getenv("KALSHI_PROD_KEY_ID")
        key_path = os.getenv("KALSHI_PROD_KEY_PATH")
    else:
        key_id = os.getenv("KALSHI_DEMO_KEY_ID")
        key_path = os.getenv("KALSHI_DEMO_KEY_PATH")

    if not key_id or not key_path:
        raise ValueError(f"Missing Kalshi {KALSHI_ENV} credentials.")

    normalized_key_path = os.path.expanduser(key_path)

    try:
        with open(normalized_key_path, "r", encoding="utf-8") as f:
            private_key_pem = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"Private key not found at {normalized_key_path}")
        
    return key_id, private_key_pem

def get_authenticated_client() -> KalshiClient:
    key_id, private_key_pem = _get_credentials()
    config = Configuration(host=API_BASE_URL)
    config.api_key_id = key_id
    config.private_key_pem = private_key_pem
    return KalshiClient(config)

def get_ws_auth_headers() -> dict[str, str]:
    """Generates cryptographic headers needed to open the WebSocket."""

    key_id, private_key_pem = _get_credentials()
    
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode('utf-8'), password=None
    )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TypeError("Expected RSA private key for Kalshi API signing.")

    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}GET/trade-api/ws/v2".encode('utf-8')
    
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )

    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode('utf-8'),
        "KALSHI-ACCESS-TIMESTAMP": timestamp
    }