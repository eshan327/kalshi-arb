from __future__ import annotations

import asyncio
import logging

from core.auth import get_dashboard_token, get_ws_auth_headers
from data.account_state import run_account_sync
from engine.streamer import run_market_streamer
from engine.trading.runtime import run_trading_loop

logger = logging.getLogger(__name__)


async def run_background_services() -> None:
    await asyncio.gather(
        run_market_streamer(),
        run_account_sync(),
        run_trading_loop(),
    )


def validate_auth_or_exit() -> None:
    try:
        get_dashboard_token()
        get_ws_auth_headers()
    except Exception as exc:
        logger.exception("Authentication failed: %s", exc)
        raise SystemExit(1)
