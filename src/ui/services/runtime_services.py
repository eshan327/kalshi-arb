from __future__ import annotations

import asyncio
import logging
import threading

from kalshi_python_sync.exceptions import UnauthorizedException

from core.auth import get_authenticated_client
from core.config import (
    BRTI_RECALC_INTERVAL_SEC,
    EXECUTION_ALLOW_LIVE_IN_DEMO_ENV,
    EXECUTION_ENABLED,
    EXECUTION_MODE,
    KALSHI_ENV,
)
from engine.execution.runtime import get_effective_execution_profile_snapshot, run_live_execution_loop
from engine.streamer import run_market_streamer
from feeds.brti_aggregator import run_brti_aggregator

logger = logging.getLogger(__name__)

_services_started = False
_services_lock = threading.Lock()


def _log_execution_mode() -> None:
    profile = get_effective_execution_profile_snapshot()
    paper_profile = profile.get("paper_profile") if isinstance(profile, dict) else {}

    if not EXECUTION_ENABLED:
        logger.info(
            "Execution loop disabled (KALSHI_EXECUTION_ENABLED=false). Paper profile snapshot: %s",
            paper_profile,
        )
        return

    if EXECUTION_MODE == "observe":
        logger.info("Execution mode observe: signals are computed and logged only.")
        return

    if EXECUTION_MODE == "paper":
        logger.info(
            "Execution mode paper: simulated orders/fills enabled; live REST trading blocked. Effective paper profile: %s",
            paper_profile,
        )
        return

    if EXECUTION_MODE == "live":
        if KALSHI_ENV == "prod":
            logger.warning("Execution mode live on prod environment: live order placement is enabled.")
            return
        if EXECUTION_ALLOW_LIVE_IN_DEMO_ENV:
            logger.warning(
                "Execution mode live on demo environment with explicit override enabled."
            )
            return

        logger.warning(
            "Execution mode live requested, but demo override is disabled; runtime will block order placement."
        )


async def _run_services() -> None:
    await asyncio.gather(
        asyncio.create_task(run_market_streamer()),
        asyncio.create_task(run_brti_aggregator(recalc_interval=BRTI_RECALC_INTERVAL_SEC)),
        asyncio.create_task(run_live_execution_loop()),
    )


def start_background_services_once() -> None:
    global _services_started

    with _services_lock:
        if _services_started:
            return

        _log_execution_mode()

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
