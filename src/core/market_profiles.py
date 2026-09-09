from dataclasses import dataclass


@dataclass(frozen=True)
class MarketProfile:
    asset: str
    display_name: str
    index_id: str
    fallback_sigma_annual: float
    settlement_decimals_fallback: int
    high_frequency: bool = False
    settlement_window_seconds: int = 60

    @property
    def index_label(self) -> str:
        return f"CF Benchmarks {self.index_id}"

    @property
    def kalshi_series_ticker(self) -> str:
        return f"KX{self.asset}15M"

    @property
    def settlement_benchmark_label(self) -> str:
        return self.index_label

    @property
    def settlement_rule_text(self) -> str:
        return "The market's published rules and finalized Kalshi result govern settlement."


MARKET_PROFILES = {
    asset: MarketProfile(asset, name, index, sigma, decimals, fast)
    for asset, name, index, sigma, decimals, fast in (
        ("BTC", "Bitcoin", "BRTI", 0.55, 2, True),
        ("ETH", "Ethereum", "ETHUSD_RTI", 0.70, 2, True),
        ("SOL", "Solana", "SOLUSD_RTI", 0.90, 4, True),
        ("XRP", "XRP", "XRPUSD_RTI", 0.90, 4, True),
        ("DOGE", "Dogecoin", "DOGEUSD_RTI", 1.0, 7, True),
        ("BNB", "BNB", "BNBUSD_RTI", 0.75, 2, False),
        ("ADA", "Cardano", "ADAUSD_RTI", 0.90, 4, False),
        ("NEAR", "NEAR", "NEARUSD_RTI", 1.0, 4, False),
        ("BCH", "Bitcoin Cash", "BCHUSD_RTI", 0.80, 2, False),
        ("HYPE", "Hyperliquid", "HYPEUSD_RTI", 1.10, 4, False),
        ("TON", "Toncoin", "TONUSD_RTI", 0.90, 4, False),
        ("ZEC", "Zcash", "ZECUSD_RTI", 1.0, 4, False),
    )
}
_ASSET_ALIASES = {p.display_name.upper(): asset for asset, p in MARKET_PROFILES.items()}


def normalize_asset(asset: str | None) -> str:
    value = asset.upper().strip() if isinstance(asset, str) else ""
    return _ASSET_ALIASES.get(value, value)


def get_market_profile(asset: str) -> MarketProfile:
    normalized = normalize_asset(asset)
    if normalized not in MARKET_PROFILES:
        raise ValueError(f"Unsupported market asset '{asset}'.")
    return MARKET_PROFILES[normalized]


def get_supported_assets() -> list[str]:
    return list(MARKET_PROFILES)
