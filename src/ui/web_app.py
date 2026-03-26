import asyncio
import logging
import threading
from flask import Flask, jsonify, render_template, request
from core.auth import get_authenticated_client
from core.config import (
    BRTI_RECALC_INTERVAL_SEC,
    ORDERBOOK_VIEW_DEPTH,
    WEB_HOST,
    WEB_PORT,
    WS_LOG_DEFAULT_LIMIT,
)
from engine.streamer import (
    get_live_market_info,
    get_live_orderbook_snapshot,
    get_reconciliation_log,
    get_top10_impact_log,
    get_ws_message_log,
    get_ws_message_log_size,
    get_ws_processing_stats,
    run_market_streamer,
)
from feeds.brti_aggregator import (
    get_brti_settlement_proxy,
    get_brti_state,
    get_brti_ticks,
    get_brti_ws_log,
    get_brti_ws_stats,
    run_brti_aggregator,
)
from ui.market_metadata import extract_suggested_strike

logger = logging.getLogger(__name__)

app = Flask(__name__)

_services_started = False
_services_lock = threading.Lock()


def _clamped_limit(raw_limit: int | None, default: int, max_limit: int) -> int:
    """Bounds API query limits to safe dashboard defaults."""
    if raw_limit is None:
        return default
    return max(1, min(raw_limit, max_limit))


def _start_background_services_once() -> None:
    global _services_started

    with _services_lock:
        if _services_started:
            return

        def _runner() -> None:
            asyncio.run(_run_services())

        thread = threading.Thread(target=_runner, name="kalshi-runtime", daemon=True)
        thread.start()
        _services_started = True


async def _run_services() -> None:
    await asyncio.gather(
        asyncio.create_task(run_market_streamer()),
        asyncio.create_task(run_brti_aggregator(recalc_interval=BRTI_RECALC_INTERVAL_SEC)),
    )


@app.get("/")
def index():
    return render_template("dashboard.html")


@app.get("/api/state")
def api_state():
    requested_depth = request.args.get("depth", default=ORDERBOOK_VIEW_DEPTH, type=int)
    depth = _clamped_limit(requested_depth, ORDERBOOK_VIEW_DEPTH, ORDERBOOK_VIEW_DEPTH)

    snapshot = get_live_orderbook_snapshot(depth=depth)
    brti = get_brti_state()
    log_size = get_ws_message_log_size()
    kalshi_stats = get_ws_processing_stats()
    brti_stats = get_brti_ws_stats()
    settlement_proxy = get_brti_settlement_proxy(window_seconds=60)
    market_info = get_live_market_info()
    suggested_strike = extract_suggested_strike(market_info)

    return jsonify(
        {
            "orderbook": snapshot,
            "brti": brti,
            "synthetic_settlement_proxy": settlement_proxy,
            "ws_log_size": log_size,
            "kalshi_ws_stats": kalshi_stats,
            "brti_ws_stats": brti_stats,
            "market_info": market_info,
            "suggested_strike": suggested_strike,
        }
    )


@app.get("/api/ws-log")
def api_ws_log():
    requested_limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    limit = _clamped_limit(requested_limit, WS_LOG_DEFAULT_LIMIT, WS_LOG_DEFAULT_LIMIT)
    return jsonify(get_ws_message_log(limit=limit))


@app.get("/api/top10-impact")
def api_top10_impact():
    requested_limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    limit = _clamped_limit(requested_limit, WS_LOG_DEFAULT_LIMIT, WS_LOG_DEFAULT_LIMIT)
    return jsonify(get_top10_impact_log(limit=limit))


@app.get("/api/brti-ticks")
def api_brti_ticks():
    requested_limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    limit = _clamped_limit(requested_limit, WS_LOG_DEFAULT_LIMIT, WS_LOG_DEFAULT_LIMIT)
    return jsonify(get_brti_ticks(limit=limit))


@app.get("/api/brti-ws-log")
def api_brti_ws_log():
    requested_limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    limit = _clamped_limit(requested_limit, WS_LOG_DEFAULT_LIMIT, WS_LOG_DEFAULT_LIMIT)
    return jsonify(get_brti_ws_log(limit=limit))


@app.get("/api/reconciliation-log")
def api_reconciliation_log():
    requested_limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    limit = _clamped_limit(requested_limit, WS_LOG_DEFAULT_LIMIT, WS_LOG_DEFAULT_LIMIT)
    return jsonify(get_reconciliation_log(limit=limit))


def run_web_app() -> None:
    try:
        client = get_authenticated_client()
        balance_res = client.get_balance()
        logger.info("Balance: $%s", f"{balance_res.balance / 100:,.2f}")
    except Exception as exc:
        logger.exception("Authentication failed: %s", exc)
        raise SystemExit(1)

    _start_background_services_once()
    logger.info("Web dashboard running at http://%s:%s", WEB_HOST, WEB_PORT)
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
