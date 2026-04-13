from __future__ import annotations

import logging

from flask import Flask

from core.config import WEB_HOST, WEB_PORT
from ui.routes.log_routes import register_log_routes
from ui.routes.selection_routes import register_selection_routes
from ui.routes.state_routes import register_state_routes
from ui.services.runtime_services import start_background_services_once, validate_auth_or_exit

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    register_state_routes(app)
    register_selection_routes(app)
    register_log_routes(app)
    return app


app = create_app()


def run_web_app() -> None:
    validate_auth_or_exit()
    start_background_services_once()

    logger.info("Web dashboard running at http://%s:%s", WEB_HOST, WEB_PORT)
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
