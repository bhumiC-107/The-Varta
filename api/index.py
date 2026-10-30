"""Stateless Vercel entry point: no login, database, or server-side user storage."""
from http.server import BaseHTTPRequestHandler
import json
import mimetypes
from urllib.parse import parse_qs, urlsplit

import server as app


class handler(BaseHTTPRequestHandler):
    """Serve static assets and a one-request feed collector on Vercel."""

    server_version = "MorningSignal"
    sys_version = ""

    def log_message(self, *_args):
        # Do not write request details to function logs.
        pass

    def _normalise_route(self):
        parsed = urlsplit(self.path)
        route = parse_qs(parsed.query, keep_blank_values=True).get("__ms_route", [None])[0]
        # Vercel's project success preview can request the function URL directly,
        # without the rewrite query. Treat that entry URL as the site root.
        if route is None and parsed.path == "/api/index":
            route = "/"
        if route is not None:
            if not route.startswith("/") or route.startswith("//") or "\\" in route:
                route = "/__invalid_route__"
            self.path = route

    def _headers(self, content_type, length):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, value, status=200):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._normalise_route()
        path = urlsplit(self.path).path
        if path == "/api/session":
            return self.send_json({"private": False, "stateless": True})
        if path == "/api/config":
            sources = app.public_sources(app.get_sources())
            return self.send_json({"sources": sources, "categories": app.CATEGORIES,
                                   "glossary": app.get_glossary(),
                                   "focus": app.FOCUS[app.dt.date.today().weekday()], "stateless": True})
        if path == "/api/briefing":
            return self.send_json({"stories": [], "hiddenStories": [], "unverifiedStories": [],
                "selection": {"freshness_hours": 48, "morning_budget": 10, "budget_used_minutes": 0,
                              "budget_limited": False, "fresh_count": 0, "stale_count": 0,
                              "undated_count": 0, "requested": 10,
                              "empty_reason": "Refresh feeds to collect today's stories.",
                              "empty_next_steps": ["Select Refresh feeds to check the configured publishers"]},
                "deepRead": None, "focus": app.FOCUS[app.dt.date.today().weekday()],
                "settings": {"regions": [], "topics": [], "sources": [], "excluded": [], "length": 10,
                             "difficulty": "Standard", "daily_goal": 1, "freshness_hours": 48,
                             "morning_budget": 10},
                "reading": {"goal": 1, "read_today": 0, "deep_read_completed": False, "history": [], "saved": []},
                "coverage": {"checked_today": 0, "failed_today": 0, "story_count": 0, "regions": [], "publishers": []},
                "sources": [{**s, "status": None} for s in app.public_sources(app.get_sources())]})
        name = app.STATIC_FILES.get(path)
        if not name:
            return self.send_json({"error": "Not found"}, 404)
        file_path = app.ROOT / name
        try:
            body = file_path.read_bytes()
        except OSError:
            return self.send_json({"error": "The requested page is unavailable"}, 404)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".html"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self._normalise_route()
        if urlsplit(self.path).path != "/api/refresh":
            return self.send_json({"error": "This public reader does not store preferences or reading activity."}, 404)
        if self.headers.get_content_type() != "application/json":
            return self.send_json({"error": "Content-Type must be application/json"}, 415)
        length_text = self.headers.get("Content-Length", "")
        if not length_text.isdigit() or len(length_text) > 5 or int(length_text) > 1024:
            return self.send_json({"error": "Refresh request is invalid"}, 400)
        try:
            data = json.loads(self.rfile.read(int(length_text)))
        except (ValueError, UnicodeError):
            return self.send_json({"error": "Refresh request must be valid JSON"}, 400)
        if data != {}:
            return self.send_json({"error": "Unexpected refresh input"}, 400)
        try:
            return self.send_json(app.stateless_briefing())
        except Exception:
            return self.send_json({"error": "Feed refresh could not complete. Please try again."}, 503)
