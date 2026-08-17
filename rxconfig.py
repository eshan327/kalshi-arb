import sys
from pathlib import Path

import reflex as rx

sys.path.insert(0, str(Path(__file__).parent / "src"))

from core.config import WEB_HOST, WEB_PORT  # noqa: E402


config = rx.Config(
    app_name="kalshi_dashboard",
    frontend_port=WEB_PORT,
    backend_port=8000,
    backend_host=WEB_HOST,
    telemetry_enabled=False,
    plugins=[rx.plugins.RadixThemesPlugin()],
    disable_plugins=[rx.plugins.SitemapPlugin],
)
