from __future__ import annotations

from flask import Flask, jsonify, request

from engine.shadow import (
    get_shadow_events,
    get_shadow_runtime_snapshot,
    get_shadow_settings_snapshot,
    reset_shadow_ledger,
    reset_shadow_settings,
    update_shadow_settings,
)


def register_settings_routes(app: Flask) -> None:
    @app.get("/api/settings")
    def api_settings_get():
        return jsonify({"ok": True, "current_settings": get_shadow_settings_snapshot()})

    @app.post("/api/settings")
    def api_settings_post():
        payload = request.get_json(silent=True) or {}

        operation = str(payload.get("operation") or "update").strip().lower()
        if operation == "reset":
            settings = reset_shadow_settings()
            return jsonify({"ok": True, "status": "reset", "current_settings": settings})

        updated_payload = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        settings, errors = update_shadow_settings(updated_payload)
        if errors:
            return jsonify({"ok": False, "errors": errors, "current_settings": settings}), 400

        return jsonify({"ok": True, "status": "applied", "current_settings": settings})

    @app.get("/api/shadow/runtime")
    def api_shadow_runtime_get():
        return jsonify({"ok": True, "runtime": get_shadow_runtime_snapshot()})

    @app.get("/api/shadow/events")
    def api_shadow_events_get():
        requested_limit = request.args.get("limit", default=200, type=int)
        limit = max(1, min(2000, int(requested_limit or 200)))
        return jsonify({"ok": True, "events": get_shadow_events(limit=limit)})

    @app.post("/api/shadow/ledger/reset")
    def api_shadow_ledger_reset_post():
        return jsonify({"ok": True, "paper_ledger": reset_shadow_ledger()})
