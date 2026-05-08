from __future__ import annotations

from flask import Flask, jsonify, request, send_file

from engine.simulation import (
    get_latest_simulation_payload,
    resolve_output_artifact,
    run_monte_carlo_simulation,
)


def register_simulation_routes(app: Flask) -> None:
    @app.post("/api/simulation/generate")
    def api_simulation_generate_post():
        payload = request.get_json(silent=True) or {}
        result = run_monte_carlo_simulation(payload)
        status = 200 if bool(result.get("ok")) else 400
        return jsonify(result), status

    @app.get("/api/simulation/latest")
    def api_simulation_latest_get():
        result = get_latest_simulation_payload()
        status = 200 if bool(result.get("ok")) else 404
        return jsonify(result), status

    @app.get("/output/<path:artifact_path>")
    def api_simulation_artifact_get(artifact_path: str):
        resolved = resolve_output_artifact(artifact_path)
        if resolved is None:
            return jsonify({"ok": False, "error": "Artifact not found."}), 404
        return send_file(resolved)
