"""Stateless Vercel entry point: no login, database, or server-side user storage."""
import json
from urllib.parse import parse_qs, urlsplit

import server as app


class handler(app.Handler):
    """Serve static files and a one-request feed collection endpoint."""

    def _local_request(self):
        # Vercel terminates HTTPS before this request handler; app routes remain same-origin.
        return True

    def _normalise_route(self):
        parsed = urlsplit(self.path)
        route = parse_qs(parsed.query, keep_blank_values=True).get("__ms_route", [None])[0]
        if route is not None:
            if not route.startswith("/") or route.startswith("//") or "\\" in route:
                route = "/__invalid_route__"
            self.path = route

    def do_GET(self):
        self._normalise_route()
        path = urlsplit(self.path).path
        if path == "/api/session":
            return self.send_json({"private": False, "stateless": True})
        if path == "/api/config":
            sources = app.public_sources(app.get_sources())
            return self.send_json({"sources": sources, "categories": app.CATEGORIES,
                                   "focus": app.FOCUS[app.dt.date.today().weekday()], "stateless": True})
        if path == "/api/briefing":
            return self.send_json({"stories": [], "hiddenStories": [], "unverifiedStories": [],
                "selection": {"freshness_hours": 48, "fresh_count": 0, "stale_count": 0,
                              "undated_count": 0, "requested": 10,
                              "empty_reason": "Refresh feeds to collect today's stories.",
                              "empty_next_steps": ["Select Refresh feeds to check the configured publishers"]},
                "deepRead": None, "focus": app.FOCUS[app.dt.date.today().weekday()],
                "settings": {"regions": [], "topics": [], "sources": [], "excluded": [], "length": 10,
                             "difficulty": "Standard", "daily_goal": 1, "freshness_hours": 48},
                "reading": {"goal": 1, "read_today": 0, "deep_read_completed": False, "history": [], "saved": []},
                "coverage": {"checked_today": 0, "failed_today": 0, "story_count": 0, "regions": [], "publishers": []},
                "sources": [{**s, "status": None} for s in app.public_sources(app.get_sources())]})
        return super().do_GET()

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
            briefing = app.stateless_briefing()
            return self.send_json(briefing)
        except Exception:
            return self.send_json({"error": "Feed refresh could not complete. Please try again."}, 503)

    def end_headers(self):
        super().end_headers()
