import os
import time
import base64
from pathlib import Path

from dotenv import load_dotenv
from kalshi_python_sync.configuration import Configuration
from kalshi_python_sync import KalshiClient
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from core.config import KALSHI_ENV, API_BASE_URL

# kalshi-arb/ (parent of src/) — relative key paths in .env resolve here, not from cwd
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_key_path(key_path: str) -> Path:
    p = Path(os.path.expanduser(key_path.strip()))
    if p.is_absolute():
        return p
    return (_PROJECT_ROOT / p).resolve()


def _normalize_key_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = raw.strip()
    if len(s) >= 2 and s[0] == "[" and s[-1] == "]":
        s = s[1:-1].strip()
    return s or None


def _normalize_pem(pem: str) -> str:
    # UTF-8 BOM breaks PEM parsing / signing if present
    return pem.lstrip("\ufeff").strip()


def _get_credentials() -> tuple[str, str]:
    """Helper to load keys."""

    load_dotenv()

    if KALSHI_ENV == "prod":
        key_id = _normalize_key_id(os.getenv("KALSHI_PROD_KEY_ID"))
        key_path = os.getenv("KALSHI_PROD_KEY_PATH")
    else:
        key_id = _normalize_key_id(os.getenv("KALSHI_DEMO_KEY_ID"))
        key_path = os.getenv("KALSHI_DEMO_KEY_PATH")

    if not key_id or not key_path:
        raise ValueError(f"Missing Kalshi {KALSHI_ENV} credentials.")

    resolved_key_path = _resolve_key_path(key_path)

    try:
        with open(resolved_key_path, "r", encoding="utf-8") as f:
            private_key_pem = _normalize_pem(f.read())
    except FileNotFoundError:
        raise FileNotFoundError(f"Private key not found at {resolved_key_path}")

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
    
    # Match kalshi_python_sync.auth.KalshiAuth (RSA-PSS SHA256, digest-length salt)
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode('utf-8'),
        "KALSHI-ACCESS-TIMESTAMP": timestamp
    }