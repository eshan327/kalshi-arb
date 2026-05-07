from __future__ import annotations

from flask import Flask, jsonify, request

from ui.services.session_export_service import export_current_session, list_exported_sessions


def register_export_routes(app: Flask) -> None:
    @app.post("/api/export-session")
    def api_export_session_post():
        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason") or "manual")
        result = export_current_session(reason=reason)
        status = 200 if bool(result.get("ok")) else 400
        return jsonify(result), status

    @app.get("/api/export-sessions")
    def api_export_sessions_get():
        requested_limit = request.args.get("limit", default=50, type=int)
        limit = max(1, min(500, int(requested_limit or 50)))
        return jsonify({"sessions": list_exported_sessions(limit=limit)})
