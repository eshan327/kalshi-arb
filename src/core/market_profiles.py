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
    paxos_symbol: str | None
    index_spacing_units: int
    index_price_decimals: int
    index_deviation_threshold: float
    index_erroneous_threshold: float
    index_stale_threshold_sec: int


_ASSET_DETAILS = {
    "BTC": ("Bitcoin", "BRTI", 0.55, 2),
    "ETH": ("Ethereum", "ETHUSD_RTI", 0.70, 2),
    "SOL": ("Solana", "SOLUSD_RTI", 0.90, 4),
    "XRP": ("XRP", "XRPUSD_RTI", 0.90, 4),
    "DOGE": ("Dogecoin", "DOGEUSD_RTI", 1.00, 7),
    "BNB": ("BNB", "BNBUSD_RTI", 0.75, 2),
    "ADA": ("Cardano", "ADAUSD_RTI", 0.90, 4),
    "NEAR": ("NEAR", "NEARUSD_RTI", 1.00, 4),
    "BCH": ("Bitcoin Cash", "BCHUSD_RTI", 0.80, 2),
    "HYPE": ("Hyperliquid", "HYPEUSD_RTI", 1.10, 4),
    "TON": ("Toncoin", "TONUSD_RTI", 0.90, 4),
    "ZEC": ("Zcash", "ZECUSD_RTI", 1.00, 2),
}
_GEMINI_ASSETS = {
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "DOGE",
    "BNB",
    "BCH",
    "HYPE",
    "TON",
    "ZEC",
}


def _build_profile(
    asset: str, display_name: str, benchmark: str, fallback_sigma: float, decimals: int
) -> MarketProfile:
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
        gemini_symbol=f"{asset}USD" if asset in _GEMINI_ASSETS else None,
        bitstamp_channel=f"order_book_{asset.lower()}usd",
        paxos_symbol=f"{asset}USD" if asset in {"BTC", "ETH"} else None,
        index_spacing_units=1,
        index_price_decimals=decimals,
        index_deviation_threshold=0.005,
        index_erroneous_threshold=0.05,
        index_stale_threshold_sec=30,
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
