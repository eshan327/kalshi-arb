from dataclasses import dataclass


@dataclass(frozen=True)
class MarketProfile:
    asset: str
    display_name: str
    index_label: str
    kalshi_series_ticker: str
    settlement_window_seconds: int
    settlement_benchmark_label: str
    settlement_rule_text: str
    fallback_sigma_annual: float
    coinbase_product_id: str
    kraken_symbol: str
    gemini_symbol: str | None
    bitstamp_channel: str
    exchange_sources: tuple[str, ...]
    index_spacing_units: float | None
    index_tick_decimals: int
    settlement_decimals_fallback: int
    index_deviation_threshold: float
    index_erroneous_threshold: float
    index_stale_threshold_sec: int


_FOUR_VENUES = ("coinbase", "kraken", "gemini", "bitstamp")
_ASSET_DETAILS = {
    # display, benchmark, sigma, settlement decimals, index decimals, venues, D, P, stale
    "BTC": ("Bitcoin", "BRTI", 0.55, 2, 2, _FOUR_VENUES, 0.005, 0.05, 10),
    "ETH": ("Ethereum", "ETHUSD_RTI", 0.70, 2, 2, _FOUR_VENUES, 0.01, 0.05, 10),
    "SOL": ("Solana", "SOLUSD_RTI", 0.90, 4, 2, _FOUR_VENUES, 0.01, 0.05, 10),
    "XRP": (
        "XRP",
        "XRPUSD_RTI",
        0.90,
        4,
        5,
        ("coinbase", "kraken", "bitstamp"),
        0.01,
        0.10,
        10,
    ),
    "DOGE": ("Dogecoin", "DOGEUSD_RTI", 1.00, 7, 6, _FOUR_VENUES, 0.01, 0.10, 10),
    "BNB": ("BNB", "BNBUSD_RTI", 0.75, 2, 3, ("coinbase", "kraken"), 0.10, 0.10, 30),
    "ADA": (
        "Cardano",
        "ADAUSD_RTI",
        0.90,
        4,
        4,
        ("coinbase", "kraken"),
        0.01,
        0.05,
        30,
    ),
    "NEAR": ("NEAR", "NEARUSD_RTI", 1.00, 4, 2, ("coinbase", "kraken"), 0.01, 0.10, 30),
    "BCH": ("Bitcoin Cash", "BCHUSD_RTI", 0.80, 2, 2, _FOUR_VENUES, 0.01, 0.05, 30),
    "HYPE": (
        "Hyperliquid",
        "HYPEUSD_RTI",
        1.10,
        4,
        4,
        ("coinbase", "kraken", "bitstamp"),
        0.01,
        0.10,
        30,
    ),
    "TON": (
        "Toncoin",
        "TONUSD_RTI",
        0.90,
        4,
        6,
        ("coinbase", "kraken", "bitstamp"),
        0.01,
        0.10,
        30,
    ),
    "ZEC": ("Zcash", "ZECUSD_RTI", 1.00, 4, 4, ("coinbase", "kraken"), 0.01, 0.10, 30),
}


def _build_profile(asset: str, *details) -> MarketProfile:
    (
        display_name,
        benchmark,
        fallback_sigma,
        settlement_decimals,
        index_decimals,
        exchange_sources,
        deviation_threshold,
        erroneous_threshold,
        stale_threshold,
    ) = details
    return MarketProfile(
        asset=asset,
        display_name=display_name,
        index_label=f"{benchmark} Proxy",
        kalshi_series_ticker=f"KX{asset}15M",
        settlement_window_seconds=60,
        settlement_benchmark_label=f"CF Benchmarks {benchmark}",
        settlement_rule_text=(
            f"Resolution compares the final 60-second {benchmark} average with the "
            "previous 15-minute benchmark; YES resolves when it is at or above the target."
        ),
        fallback_sigma_annual=fallback_sigma,
        coinbase_product_id=f"{asset}-USD",
        kraken_symbol=f"{asset}/USD",
        gemini_symbol=f"{asset}USD" if "gemini" in exchange_sources else None,
        bitstamp_channel=f"order_book_{asset.lower()}usd",
        exchange_sources=exchange_sources,
        index_spacing_units=None,
        index_tick_decimals=index_decimals,
        settlement_decimals_fallback=settlement_decimals,
        index_deviation_threshold=deviation_threshold,
        index_erroneous_threshold=erroneous_threshold,
        index_stale_threshold_sec=stale_threshold,
    )


MARKET_PROFILES = {
    asset: _build_profile(asset, *details) for asset, details in _ASSET_DETAILS.items()
}
_ASSET_ALIASES = {
    profile.display_name.upper(): asset for asset, profile in MARKET_PROFILES.items()
}


def normalize_asset(asset: str | None) -> str:
    if not isinstance(asset, str):
        return ""
    normalized = asset.upper().strip()
    return _ASSET_ALIASES.get(normalized, normalized)


def is_supported_asset(asset: str | None) -> bool:
    return normalize_asset(asset) in MARKET_PROFILES


def get_market_profile(asset: str) -> MarketProfile:
    normalized = normalize_asset(asset)
    if normalized not in MARKET_PROFILES:
        raise ValueError(f"Unsupported market asset '{asset}'.")
    return MARKET_PROFILES[normalized]


def get_supported_assets() -> list[str]:
    return list(MARKET_PROFILES)
