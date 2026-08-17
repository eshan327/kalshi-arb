from __future__ import annotations

import asyncio
import logging
import threading

from core.auth import get_ws_auth_headers
from core.config import BRTI_RECALC_INTERVAL_SEC
from engine.streamer import run_market_streamer
from engine.trading.runtime import run_trading_loop
from feeds.brti_aggregator import run_brti_aggregator

logger = logging.getLogger(__name__)


async def _run_services() -> None:
    await asyncio.gather(
        run_market_streamer(),
        run_brti_aggregator(recalc_interval=BRTI_RECALC_INTERVAL_SEC),
        run_trading_loop(),
    )


def start_background_services() -> None:
    threading.Thread(
        target=lambda: asyncio.run(_run_services()),
        name="kalshi-runtime",
        daemon=True,
    ).start()


def validate_auth_or_exit() -> None:
    try:
        get_ws_auth_headers()
    except Exception as exc:
        logger.exception("Authentication failed: %s", exc)
        raise SystemExit(1)
