import argparse
import os

from core.market_profiles import (
    get_supported_assets,
    normalize_asset,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Kalshi 15-minute crypto bot.")
    parser.add_argument(
        "asset",
        nargs="?",
        default=os.getenv("KALSHI_MARKET_ASSET", "BTC"),
        type=normalize_asset,
        choices=get_supported_assets(),
        help=f"crypto name or ticker ({', '.join(get_supported_assets())})",
    )
    args = parser.parse_args()
    os.environ["KALSHI_MARKET_ASSET"] = args.asset
    from reflex.reflex import cli

    cli.main(args=["run"], prog_name="reflex")


if __name__ == "__main__":
    main()
