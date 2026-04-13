from __future__ import annotations

from dataclasses import dataclass

from core.market_profiles import MarketProfile, get_market_profile
from core.market_selection import apply_requested_asset_switch, get_active_asset, get_market_selection_state


@dataclass(frozen=True)
class ActiveAssetContext:
    profile: MarketProfile
    requested_asset: str | None
    options: list[str]


def get_active_asset_context() -> ActiveAssetContext:
    state = get_market_selection_state()
    active_asset = str(state.get("active_asset") or get_active_asset())
    profile = get_market_profile(active_asset)

    requested = state.get("requested_asset")
    requested_asset = str(requested) if isinstance(requested, str) else None

    options_raw = state.get("options")
    if isinstance(options_raw, list):
        options = [str(item) for item in options_raw]
    else:
        options = []

    return ActiveAssetContext(
        profile=profile,
        requested_asset=requested_asset,
        options=options,
    )


def apply_queued_asset_switch_and_get_context() -> tuple[bool, ActiveAssetContext]:
    switched = apply_requested_asset_switch() is not None
    return switched, get_active_asset_context()
