import logging
import math
from dataclasses import dataclass
from datetime import datetime

from core.config import MARKET_ASSET_DEFAULT


@dataclass(frozen=True)
class MarketProfile:
    asset: str
    display_name: str
    index_id: str
    settlement_decimals_fallback: int
    high_frequency: bool = False
    settlement_window_seconds: int = 60

    @property
    def index_label(self) -> str:
        return f"CF Benchmarks {self.index_id}"

    @property
    def kalshi_series_ticker(self) -> str:
        return f"KX{self.asset}15M"


MARKET_PROFILES = {
    asset: MarketProfile(asset, name, index, decimals, fast)
    for asset, name, index, decimals, fast in (
        ("BTC", "Bitcoin", "BRTI", 2, True),
        ("ETH", "Ethereum", "ETHUSD_RTI", 2, True),
        ("SOL", "Solana", "SOLUSD_RTI", 4, True),
        ("XRP", "XRP", "XRPUSD_RTI", 4, True),
        ("DOGE", "Dogecoin", "DOGEUSD_RTI", 7, True),
        ("BNB", "BNB", "BNBUSD_RTI", 2, False),
        ("ADA", "Cardano", "ADAUSD_RTI", 4, False),
        ("NEAR", "NEAR", "NEARUSD_RTI", 4, False),
        ("BCH", "Bitcoin Cash", "BCHUSD_RTI", 2, False),
        ("HYPE", "Hyperliquid", "HYPEUSD_RTI", 4, False),
        ("TON", "Toncoin", "TONUSD_RTI", 4, False),
        ("ZEC", "Zcash", "ZECUSD_RTI", 4, False),
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


def extract_suggested_strike(market_info: dict) -> float | None:
    """Read structured exchange terms; missing terms must not become guessed strikes."""
    if not market_info:
        return None

    direct_keys = [
        "strike_price",
        "strike",
        "target_price",
        "floor_strike",
        "cap_strike",
    ]
    for key in direct_keys:
        value = market_info.get(key)
        try:
            number = float(value)
            if math.isfinite(number) and number > 0:
                return number
        except (TypeError, ValueError, OverflowError):
            pass

    return None


def extract_settlement_decimals(market_info: dict, fallback: int) -> int:
    custom_strike = market_info.get("custom_strike") if market_info else None
    value = (
        custom_strike.get("round_digits") if isinstance(custom_strike, dict) else None
    )
    try:
        return max(0, min(12, int(value)))
    except (TypeError, ValueError, OverflowError):
        return fallback


def parse_iso8601_to_epoch(value: str | None) -> float | None:
    if not isinstance(value, str) or not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
