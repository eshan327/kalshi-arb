from __future__ import annotations

import logging

from flask import Flask

from core.config import WEB_HOST, WEB_PORT
from ui.routes.log_routes import register_log_routes
from ui.routes.settings_routes import register_settings_routes
from ui.routes.state_routes import register_state_routes
from ui.services.runtime_services import (
    start_background_services,
    validate_auth_or_exit,
)

logger = logging.getLogger(__name__)


app = Flask(__name__)
register_state_routes(app)
register_log_routes(app)
register_settings_routes(app)


def run_web_app() -> None:
    validate_auth_or_exit()
    start_background_services()

    logger.info("Web dashboard running at http://%s:%s", WEB_HOST, WEB_PORT)
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
