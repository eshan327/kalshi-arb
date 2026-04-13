from __future__ import annotations

from dataclasses import dataclass

from core.market_profiles import MarketProfile, get_market_profile


@dataclass(frozen=True)
class SettlementConfig:
    asset: str
    benchmark_label: str
    rule_text: str
    window_seconds: int


def get_settlement_config(asset_or_profile: str | MarketProfile) -> SettlementConfig:
    profile = (
        asset_or_profile
        if isinstance(asset_or_profile, MarketProfile)
        else get_market_profile(str(asset_or_profile))
    )
    return SettlementConfig(
        asset=profile.asset,
        benchmark_label=profile.settlement_benchmark_label,
        rule_text=profile.settlement_rule_text,
        window_seconds=int(profile.settlement_window_seconds),
    )
