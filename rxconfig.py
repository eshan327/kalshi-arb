import os
import sys
from pathlib import Path

os.environ.setdefault("REFLEX_WEB_WORKDIR", ".runtime/web")
os.environ.setdefault("REFLEX_STATES_WORKDIR", ".runtime/states")

import reflex as rx

sys.path.insert(0, str(Path(__file__).parent / "src"))

from core.config import WEB_HOST, WEB_PORT  # noqa: E402

config = rx.Config(
    app_name="kalshi_dashboard",
    app_module_import="ui.app",
    frontend_port=WEB_PORT,
    backend_port=8000,
    backend_host=WEB_HOST,
    show_built_with_reflex=False,
    telemetry_enabled=False,
    plugins=[rx.plugins.RadixThemesPlugin()],
    disable_plugins=[rx.plugins.SitemapPlugin],
)
