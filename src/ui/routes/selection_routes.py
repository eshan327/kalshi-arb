from __future__ import annotations

from flask import Flask, jsonify, request

from ui.services.dashboard_state_service import build_market_selection_payload, request_market_selection


def register_selection_routes(app: Flask) -> None:
    @app.get("/api/market-selection")
    def api_market_selection_get():
        return jsonify(build_market_selection_payload())

    @app.post("/api/market-selection")
    def api_market_selection_post():
        payload = request.get_json(silent=True) or {}
        asset = payload.get("asset")
        body, status_code = request_market_selection(str(asset) if asset is not None else "")
        return jsonify(body), status_code
