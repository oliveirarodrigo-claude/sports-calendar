#!/usr/bin/env python3
"""
Local dashboard server — localhost:8765
Serves the ICS file + all HTML dashboards in the project directory.
"""
import http.server
import mimetypes
from pathlib import Path

PORT     = 8765
BASE_DIR = Path(__file__).parent

KNOWN_PAGES = [
    ("index.html",             "🏠 Home"),
    ("calendar_changelog.html","📋 Calendar Activity"),
    ("fonseca.html",           "🎾 Fonseca Season"),
    ("changelog.html",         "🛠 Build Changelog"),
]

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.lstrip("/") or "index.html"

        # ICS — always fresh read
        if path == "cruzeiro.ics":
            f = BASE_DIR / "cruzeiro.ics"
            self._send_file(f, "text/calendar; charset=utf-8")
            return

        # HTML files
        if path.endswith(".html"):
            f = BASE_DIR / path
            self._send_file(f, "text/html; charset=utf-8")
            return

        self.send_error(404)

    def _send_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # silence request logs

if __name__ == "__main__":
    import os
    os.chdir(BASE_DIR)
    httpd = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dashboard running at http://localhost:{PORT}")
    httpd.serve_forever()
