from __future__ import annotations

from flask import Flask, jsonify, request

from core.config import WS_LOG_DEFAULT_LIMIT
from engine.stream_metrics import (
    get_reconciliation_log,
    get_top10_impact_log,
    get_ws_message_log,
)
from feeds.state.diagnostics_store import get_brti_ws_log
from feeds.state.tick_store import get_brti_ticks
from ui.services.dashboard_state_service import clamped_limit


def _parse_limit_arg() -> int:
    requested_limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    return clamped_limit(requested_limit, WS_LOG_DEFAULT_LIMIT, WS_LOG_DEFAULT_LIMIT)


def register_log_routes(app: Flask) -> None:
    @app.get("/api/ws-log")
    def api_ws_log():
        return jsonify(get_ws_message_log(limit=_parse_limit_arg()))

    @app.get("/api/top10-impact")
    def api_top10_impact():
        return jsonify(get_top10_impact_log(limit=_parse_limit_arg()))

    @app.get("/api/brti-ticks")
    def api_brti_ticks():
        return jsonify(get_brti_ticks(limit=_parse_limit_arg()))

    @app.get("/api/brti-ws-log")
    def api_brti_ws_log():
        return jsonify(get_brti_ws_log(limit=_parse_limit_arg()))

    @app.get("/api/reconciliation-log")
    def api_reconciliation_log():
        return jsonify(get_reconciliation_log(limit=_parse_limit_arg()))
