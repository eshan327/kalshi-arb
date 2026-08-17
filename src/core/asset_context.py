from dataclasses import dataclass

from core.market_profiles import MarketProfile, get_market_profile
from core.market_selection import get_active_asset


@dataclass(frozen=True)
class ActiveAssetContext:
    profile: MarketProfile


def get_active_asset_context() -> ActiveAssetContext:
    return ActiveAssetContext(profile=get_market_profile(get_active_asset()))
