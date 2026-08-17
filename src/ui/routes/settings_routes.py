from __future__ import annotations

from flask import Flask, jsonify, request

from engine.trading import (
    control_trading,
    get_trading_events,
    get_trading_runtime_snapshot,
    get_trading_settings_snapshot,
    reset_trading_settings,
    submit_manual_order,
    update_trading_settings,
)


def register_settings_routes(app: Flask) -> None:
    @app.get("/api/settings")
    def api_settings_get():
        return jsonify(
            {"ok": True, "current_settings": get_trading_settings_snapshot()}
        )

    @app.post("/api/settings")
    def api_settings_post():
        payload = request.get_json(silent=True) or {}

        operation = str(payload.get("operation") or "update").strip().lower()
        if operation == "reset":
            settings = reset_trading_settings()
            return jsonify(
                {"ok": True, "status": "reset", "current_settings": settings}
            )

        updated_payload = (
            payload.get("settings")
            if isinstance(payload.get("settings"), dict)
            else payload
        )
        settings, errors = update_trading_settings(updated_payload)
        if errors:
            return jsonify(
                {"ok": False, "errors": errors, "current_settings": settings}
            ), 400

        return jsonify({"ok": True, "status": "applied", "current_settings": settings})

    @app.get("/api/trading/runtime")
    def api_trading_runtime_get():
        return jsonify({"ok": True, "runtime": get_trading_runtime_snapshot()})

    @app.get("/api/trading/events")
    def api_trading_events_get():
        requested_limit = request.args.get("limit", default=200, type=int)
        limit = max(1, min(2000, int(requested_limit or 200)))
        return jsonify({"ok": True, "events": get_trading_events(limit=limit)})

    @app.post("/api/trading/control")
    def api_trading_control_post():
        payload = request.get_json(silent=True) or {}
        try:
            result = control_trading(
                str(payload.get("operation") or ""),
                str(payload.get("execution_mode") or ""),
            )
            return jsonify(result), 200 if result.get("ok", True) else 502
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.post("/api/trading/manual")
    def api_trading_manual_post():
        payload = request.get_json(silent=True) or {}
        try:
            result = submit_manual_order(
                side=str(payload.get("side") or ""),
                action=str(payload.get("action") or ""),
                count=payload.get("count"),
            )
            return jsonify(result), 200 if result.get("ok", True) else 502
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
