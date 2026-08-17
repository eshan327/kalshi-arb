import logging

from core.config import MARKET_ASSET_DEFAULT
from core.market_profiles import is_supported_asset, normalize_asset

logger = logging.getLogger(__name__)


def _active_asset() -> str:
    asset = normalize_asset(MARKET_ASSET_DEFAULT)
    if is_supported_asset(asset):
        return asset
    logger.warning(
        "Unsupported KALSHI_MARKET_ASSET=%s; falling back to BTC.", MARKET_ASSET_DEFAULT
    )
    return "BTC"


def get_active_asset() -> str:
    return _active_asset()
