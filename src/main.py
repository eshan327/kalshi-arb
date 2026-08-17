import argparse
import os

from dotenv import load_dotenv

from core.market_profiles import (
    get_supported_assets,
    is_supported_asset,
    normalize_asset,
)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run one Kalshi 15-minute crypto bot.")
    parser.add_argument(
        "asset",
        nargs="?",
        default=os.getenv("KALSHI_MARKET_ASSET", "BTC"),
        help=f"crypto name or ticker ({', '.join(get_supported_assets())})",
    )
    args = parser.parse_args()
    asset = normalize_asset(args.asset)
    if not is_supported_asset(asset):
        parser.error(
            f"unsupported crypto '{args.asset}'; choose "
            f"{', '.join(get_supported_assets())}"
        )
    os.environ["KALSHI_MARKET_ASSET"] = asset
    from ui.web_app import run_web_app

    run_web_app()


if __name__ == "__main__":
    main()
