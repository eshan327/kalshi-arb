import logging

from core.config import MARKET_ASSET_DEFAULT
from core.market_profiles import MarketProfile, get_market_profile

logger = logging.getLogger(__name__)


def get_active_market_profile() -> MarketProfile:
    try:
        return get_market_profile(MARKET_ASSET_DEFAULT)
    except ValueError:
        logger.warning(
            "Unsupported KALSHI_MARKET_ASSET=%s; falling back to BTC.",
            MARKET_ASSET_DEFAULT,
        )
        return get_market_profile("BTC")
