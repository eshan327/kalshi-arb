"""Discover Kalshi Perps markets, read account state, or record a public feed."""

import argparse
import asyncio
import json
import logging
from urllib.parse import urlparse

from core.auth import get_ws_auth_headers
from core.config import PERPS_WS_BASE_URL
from data.kalshi_perps import get_perps, get_perps_limits, perps_messages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", nargs="?", help="Exact ticker returned by --list-markets")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-markets", action="store_true")
    mode.add_argument("--account", action="store_true")
    parser.add_argument("--capture-path", default=".runtime/perps_data.jsonl")
    args = parser.parse_args()
    if args.ticker and (args.list_markets or args.account):
        parser.error("Use ticker, --list-markets, or --account separately")
    if not (args.ticker or args.list_markets or args.account):
        parser.error("Provide a ticker, --list-markets, or --account")
    logging.basicConfig(level=logging.INFO)
    if args.list_markets:
        print(json.dumps(get_perps("/markets"), indent=2))
        return
    if args.account:
        account = {"enabled": get_perps("/enabled"), "limits": get_perps_limits()}
        if account["enabled"]["enabled"]:
            account.update(
                balance=get_perps("/balance", compute_available_balance=True),
                positions=get_perps("/positions"),
                risk=get_perps("/risk"),
            )
        print(json.dumps(account, indent=2))
        return
    get_ws_auth_headers(urlparse(PERPS_WS_BASE_URL).path)

    async def record():
        async for message in perps_messages(args.ticker, args.capture_path):
            if message["type"] == "ticker":
                print(json.dumps(message), flush=True)

    try:
        asyncio.run(record())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
