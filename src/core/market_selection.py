import json
import logging
import os
from threading import RLock

from core.config import MARKET_ASSET_DEFAULT, MARKET_SELECTION_STATE_PATH
from core.market_profiles import get_supported_assets, is_supported_asset, normalize_asset

logger = logging.getLogger(__name__)

_selection_lock = RLock()
_active_asset = "BTC"
_requested_asset: str | None = None


def _safe_default_asset() -> str:
    default_asset = normalize_asset(MARKET_ASSET_DEFAULT)
    if is_supported_asset(default_asset):
        return default_asset
    logger.warning("Unsupported KALSHI_MARKET_ASSET=%s; falling back to BTC.", MARKET_ASSET_DEFAULT)
    return "BTC"


def _state_file_path() -> str:
    return os.path.abspath(MARKET_SELECTION_STATE_PATH)


def _read_persisted_asset() -> str | None:
    path = _state_file_path()
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Failed to read market selection state (%s): %s", path, exc)
        return None

    asset = normalize_asset(payload.get("active_asset")) if isinstance(payload, dict) else ""
    if is_supported_asset(asset):
        return asset

    logger.warning("Ignoring invalid persisted market asset: %s", asset)
    return None


def _persist_active_asset(asset: str) -> None:
    path = _state_file_path()
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    payload = {"active_asset": asset}
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning("Failed to persist market selection state (%s): %s", path, exc)


def _initialize_selection_state() -> None:
    global _active_asset
    persisted = _read_persisted_asset()
    _active_asset = persisted or _safe_default_asset()
    _persist_active_asset(_active_asset)


_initialize_selection_state()


def get_supported_market_assets() -> list[str]:
    return get_supported_assets()


def get_active_asset() -> str:
    with _selection_lock:
        return _active_asset


def get_requested_asset() -> str | None:
    with _selection_lock:
        return _requested_asset


def get_market_selection_state() -> dict[str, str | None | list[str]]:
    with _selection_lock:
        return {
            "active_asset": _active_asset,
            "requested_asset": _requested_asset,
            "options": get_supported_assets(),
        }


def request_asset_switch(asset: str) -> dict[str, str | bool]:
    global _requested_asset
    normalized = normalize_asset(asset)
    if not is_supported_asset(normalized):
        return {
            "ok": False,
            "status": "invalid",
            "message": f"Unsupported asset '{asset}'.",
        }

    with _selection_lock:
        if normalized == _active_asset:
            _requested_asset = None
            return {
                "ok": True,
                "status": "already_active",
                "asset": normalized,
                "message": f"{normalized} is already active.",
            }

        _requested_asset = normalized

    return {
        "ok": True,
        "status": "pending",
        "asset": normalized,
        "message": (
            f"Switch to {normalized} queued. It will apply after current market rotation."
        ),
    }


def apply_requested_asset_switch() -> str | None:
    global _active_asset, _requested_asset
    with _selection_lock:
        if _requested_asset is None or _requested_asset == _active_asset:
            _requested_asset = None
            return None

        _active_asset = _requested_asset
        _requested_asset = None
        active = _active_asset

    _persist_active_asset(active)
    return active
