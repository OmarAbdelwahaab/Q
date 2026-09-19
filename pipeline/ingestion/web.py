"""Lightweight HTTP server for health checks and static media hosting."""

from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path
from typing import Any

from pipeline.logging import get_logger


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_background_web_server(port: int, storage_root: Path) -> ThreadingHTTPServer:
    """Run an HTTP server in a background thread to respond to Railway health checks and serve rendered media."""
    logger = get_logger(__name__, service="web_server")
    render_dir = (storage_root / "render").resolve()
    render_dir.mkdir(parents=True, exist_ok=True)

    class PipelineRequestHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(render_dir), **kwargs)

        def do_GET(self) -> None:
            if self.path in ("/", "/health", "/healthz", "/status"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok", "service": "quran-pipeline"}\n')
                return

            if self.path.startswith("/render/"):
                self.path = self.path[len("/render"):]

            super().do_GET()

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("HTTP request", extra={"request": format % args})

    server = ThreadingHTTPServer(("0.0.0.0", port), PipelineRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Background web server started", extra={"port": port, "render_dir": str(render_dir)})
    return server
