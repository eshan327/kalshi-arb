import base64
import os
import time
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from core.config import KALSHI_ENV

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


@lru_cache(maxsize=1)
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


def get_api_auth_headers(method: str, path: str) -> dict[str, str]:
    """Sign a Kalshi API path without query parameters."""
    key_id, private_key_pem = _get_credentials()

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TypeError("Expected RSA private key for Kalshi API signing.")

    timestamp = str(int(time.time() * 1000))
    method = str(method).upper().strip()
    sign_path = str(path).split("?", 1)[0]
    message = f"{timestamp}{method}{sign_path}".encode("utf-8")

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
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


def get_ws_auth_headers() -> dict[str, str]:
    return get_api_auth_headers("GET", "/trade-api/ws/v2")
