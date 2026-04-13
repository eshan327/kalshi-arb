from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from core.config import ORDERBOOK_VIEW_DEPTH
from ui.services.dashboard_state_service import build_dashboard_state_payload, clamped_limit


def register_state_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        return render_template("dashboard.html")

    @app.get("/api/state")
    def api_state():
        requested_depth = request.args.get("depth", default=ORDERBOOK_VIEW_DEPTH, type=int)
        depth = clamped_limit(requested_depth, ORDERBOOK_VIEW_DEPTH, ORDERBOOK_VIEW_DEPTH)
        return jsonify(build_dashboard_state_payload(depth=depth))
