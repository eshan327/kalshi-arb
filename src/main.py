"""Record Kalshi market data and print a read-only pricing snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    from core.markets import get_supported_assets, normalize_asset

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "asset",
        nargs="?",
        default=os.getenv("KALSHI_MARKET_ASSET", "BTC"),
        type=normalize_asset,
        choices=get_supported_assets(),
    )
    parser.add_argument(
        "--capture-path", type=Path, default=Path(".runtime/market_data.jsonl")
    )
    parser.add_argument("--status-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if args.status_seconds <= 0:
        parser.error("--status-seconds must be positive")

    from core.auth import get_ws_auth_headers
    from core.markets import (
        extract_settlement_decimals,
        extract_suggested_strike,
        get_market_profile,
    )
    from data.benchmark import get_index_state
    from data.streamer import get_live_book, get_live_market_info, run_market_streamer
    from pricing.live_pricing import compute_live_pricing_snapshot

    get_ws_auth_headers()  # Fail before connecting if research credentials are absent.
    profile = get_market_profile(args.asset)

    async def report() -> None:
        while True:
            await asyncio.sleep(args.status_seconds)
            market = get_live_market_info()
            book = get_live_book()
            strike = extract_suggested_strike(market)
            try:
                pricing = compute_live_pricing_snapshot(
                    profile=profile,
                    strike=strike,
                    market_ticker=market.get("ticker"),
                    close_time_iso=market.get("close_time"),
                    settlement_decimals=extract_settlement_decimals(
                        market, profile.settlement_decimals_fallback
                    ),
                )
            except Exception as exc:
                pricing = {"ready": False, "reason": f"pricing error: {exc}"}
            bid, ask = (None, None)
            if book is not None and book.initialized and not book.needs_resync:
                bid, ask, _, _ = book.get_best_prices()
            print(
                json.dumps(
                    {
                        "time": datetime.now(UTC).isoformat(),
                        "ticker": market.get("ticker"),
                        "cf_price": get_index_state().get("price"),
                        "yes_bid_cents": bid,
                        "yes_ask_cents": ask,
                        "reference_yes_probability": pricing.get("p_model"),
                        "pricing_status": (
                            "ready" if pricing.get("ready") else pricing.get("reason")
                        ),
                    }
                ),
                flush=True,
            )

    async def run() -> None:
        await asyncio.gather(run_market_streamer(str(args.capture_path), profile), report())

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
