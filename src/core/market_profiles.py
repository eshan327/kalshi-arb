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
    gemini_symbol: str
    bitstamp_channel: str
    paxos_symbol: str
    index_spacing_units: int
    index_deviation_threshold: float
    index_erroneous_threshold: float
    index_stale_threshold_sec: int


MARKET_PROFILES: dict[str, MarketProfile] = {
    "BTC": MarketProfile(
        asset="BTC",
        display_name="Bitcoin",
        index_label="BRTI Proxy",
        kalshi_series_ticker="KXBTC15M",
        settlement_window_seconds=60,
        settlement_benchmark_label="CF Benchmarks BRTI",
        settlement_rule_text=(
            "Resolution uses the average of the final 60 RTI/BRTI prints before expiry; "
            "YES resolves when the average is at or above strike."
        ),
        fallback_sigma_annual=0.55,
        coinbase_product_id="BTC-USD",
        kraken_symbol="BTC/USD",
        gemini_symbol="BTCUSD",
        bitstamp_channel="order_book_btcusd",
        paxos_symbol="BTCUSD",
        index_spacing_units=1,
        index_deviation_threshold=0.005,
        index_erroneous_threshold=0.05,
        index_stale_threshold_sec=30,
    ),
    "ETH": MarketProfile(
        asset="ETH",
        display_name="Ethereum",
        index_label="ETHUSD_RTI Proxy",
        kalshi_series_ticker="KXETH15M",
        settlement_window_seconds=60,
        settlement_benchmark_label="CF Benchmarks ETHUSD_RTI",
        settlement_rule_text=(
            "Resolution uses the simple average of the final 60 ETHUSD_RTI prints before expiry; "
            "YES resolves when the average is at or above strike."
        ),
        fallback_sigma_annual=0.70,
        coinbase_product_id="ETH-USD",
        kraken_symbol="ETH/USD",
        gemini_symbol="ETHUSD",
        bitstamp_channel="order_book_ethusd",
        paxos_symbol="ETHUSD",
        index_spacing_units=1,
        index_deviation_threshold=0.005,
        index_erroneous_threshold=0.05,
        index_stale_threshold_sec=30,
    ),
}


def normalize_asset(asset: str | None) -> str:
    if not isinstance(asset, str):
        return ""
    return asset.upper().strip()


def is_supported_asset(asset: str | None) -> bool:
    return normalize_asset(asset) in MARKET_PROFILES


def get_market_profile(asset: str) -> MarketProfile:
    normalized = normalize_asset(asset)
    if normalized not in MARKET_PROFILES:
        raise ValueError(f"Unsupported market asset '{asset}'.")
    return MARKET_PROFILES[normalized]


def get_supported_assets() -> list[str]:
    return list(MARKET_PROFILES.keys())
