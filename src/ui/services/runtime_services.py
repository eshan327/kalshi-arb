from __future__ import annotations

import asyncio
import logging
import threading

from core.auth import get_ws_auth_headers
from core.config import BRTI_RECALC_INTERVAL_SEC, EXECUTION_MODE
from data.kalshi_trading import get_balance_summary
from engine.streamer import run_market_streamer
from engine.trading import run_trading_loop
from feeds.brti_aggregator import run_brti_aggregator

logger = logging.getLogger(__name__)

_services_started = False
_services_lock = threading.Lock()


async def _run_services() -> None:
    await asyncio.gather(
        asyncio.create_task(run_market_streamer()),
        asyncio.create_task(
            run_brti_aggregator(recalc_interval=BRTI_RECALC_INTERVAL_SEC)
        ),
        asyncio.create_task(run_trading_loop()),
    )


def start_background_services_once() -> None:
    global _services_started

    with _services_lock:
        if _services_started:
            return

        def _runner() -> None:
            asyncio.run(_run_services())

        thread = threading.Thread(target=_runner, name="kalshi-runtime", daemon=True)
        thread.start()
        _services_started = True


def validate_auth_or_exit() -> None:
    if EXECUTION_MODE == "paper":
        try:
            get_ws_auth_headers()
            logger.info(
                "Paper account enabled; live account balance will not be queried."
            )
            return
        except Exception as exc:
            logger.exception("Authentication failed: %s", exc)
            raise SystemExit(1)
    try:
        balance = get_balance_summary()
        logger.info("Balance: $%s", f"{balance['balance'] / 100:,.2f}")
    except Exception as exc:
        logger.exception("Authentication failed: %s", exc)
        raise SystemExit(1)
