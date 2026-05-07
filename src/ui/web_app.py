from __future__ import annotations

import atexit
import logging
import signal
import threading

from flask import Flask

from core.config import (
    SESSION_EXPORT_AUTO_ON_SHUTDOWN,
    SESSION_EXPORT_ENABLED,
    WEB_HOST,
    WEB_PORT,
)
from ui.routes.export_routes import register_export_routes
from ui.routes.log_routes import register_log_routes
from ui.routes.selection_routes import register_selection_routes
from ui.routes.state_routes import register_state_routes
from ui.services.runtime_services import start_background_services_once, validate_auth_or_exit
from ui.services.session_export_service import export_current_session, mark_session_started

logger = logging.getLogger(__name__)

_shutdown_lock = threading.Lock()
_shutdown_export_done = False


def _auto_export_once(reason: str) -> None:
    global _shutdown_export_done

    if not SESSION_EXPORT_ENABLED or not SESSION_EXPORT_AUTO_ON_SHUTDOWN:
        return

    with _shutdown_lock:
        if _shutdown_export_done:
            return
        _shutdown_export_done = True

    try:
        result = export_current_session(reason=reason)
        if bool(result.get("ok")):
            logger.info("Session export completed at shutdown: %s", result.get("path"))
        else:
            logger.warning("Session export skipped at shutdown: %s", result.get("error"))
    except Exception as exc:  # pragma: no cover - shutdown safety net
        logger.warning("Session export failed during shutdown: %s", exc)


def _register_shutdown_export_handlers() -> None:
    def _signal_handler(signum, _frame):
        _auto_export_once(reason=f"signal_{signum}")
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):
            # Signal hooks can fail in non-main threads; atexit still provides coverage.
            continue

    atexit.register(lambda: _auto_export_once(reason="atexit"))


def create_app() -> Flask:
    app = Flask(__name__)
    register_state_routes(app)
    register_selection_routes(app)
    register_log_routes(app)
    register_export_routes(app)
    return app


app = create_app()


def run_web_app() -> None:
    validate_auth_or_exit()
    mark_session_started()
    start_background_services_once()
    _register_shutdown_export_handlers()

    logger.info("Web dashboard running at http://%s:%s", WEB_HOST, WEB_PORT)
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
