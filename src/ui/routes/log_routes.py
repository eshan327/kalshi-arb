from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Flask, jsonify, request

from core.config import WS_LOG_DEFAULT_LIMIT
from engine.streamer import get_reconciliation_log, get_top10_impact_log, get_ws_message_log
from feeds.brti_aggregator import get_brti_ticks, get_brti_ws_log
from ui.services.dashboard_state_service import clamped_limit


LimitedLogFetcher = Callable[..., Any]


def _parse_limit_arg() -> int:
    requested_limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    return clamped_limit(requested_limit, WS_LOG_DEFAULT_LIMIT, WS_LOG_DEFAULT_LIMIT)


def _register_limited_log_route(
    app: Flask,
    *,
    route: str,
    endpoint: str,
    fetcher: LimitedLogFetcher,
) -> None:
    @app.get(route, endpoint=endpoint)
    def _handler(fetcher: LimitedLogFetcher = fetcher):
        return jsonify(fetcher(limit=_parse_limit_arg()))


def register_log_routes(app: Flask) -> None:
    route_specs: tuple[tuple[str, str, LimitedLogFetcher], ...] = (
        ("/api/ws-log", "api_ws_log", get_ws_message_log),
        ("/api/top10-impact", "api_top10_impact", get_top10_impact_log),
        ("/api/brti-ticks", "api_brti_ticks", get_brti_ticks),
        ("/api/brti-ws-log", "api_brti_ws_log", get_brti_ws_log),
        ("/api/reconciliation-log", "api_reconciliation_log", get_reconciliation_log),
    )

    for route, endpoint, fetcher in route_specs:
        _register_limited_log_route(
            app,
            route=route,
            endpoint=endpoint,
            fetcher=fetcher,
        )
