from __future__ import annotations

import asyncio
import logging
import threading

from kalshi_python_sync.exceptions import UnauthorizedException

from core.auth import get_authenticated_client
from core.config import BRTI_RECALC_INTERVAL_SEC
from engine.streamer import run_market_streamer
from feeds.brti_aggregator import run_brti_aggregator

logger = logging.getLogger(__name__)

_services_started = False
_services_lock = threading.Lock()


async def _run_services() -> None:
    await asyncio.gather(
        asyncio.create_task(run_market_streamer()),
        asyncio.create_task(run_brti_aggregator(recalc_interval=BRTI_RECALC_INTERVAL_SEC)),
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
    try:
        client = get_authenticated_client()
        balance_res = client.get_balance()
        logger.info("Balance: $%s", f"{balance_res.balance / 100:,.2f}")
    except UnauthorizedException:
        logger.error(
            "Kalshi returned 401 Unauthorized. Most often: "
            "KALSHI_ENV does not match where the API key was created "
            "(demo keys from demo.kalshi.co require KALSHI_ENV=demo; "
            "production keys from kalshi.com require KALSHI_ENV=prod), "
            "or KALSHI_*_KEY_ID does not belong to that private key file."
        )
        raise SystemExit(1)
    except Exception as exc:
        logger.exception("Authentication failed: %s", exc)
        raise SystemExit(1)
