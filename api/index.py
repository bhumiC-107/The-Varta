"""Private Vercel HTTP entry point; the local BaseHTTPRequestHandler stays intact."""
import hmac
import threading
import os
from urllib.parse import parse_qs, urlsplit

import private_auth
import server as app
from database_config import is_mysql_database_url

_schema_lock = threading.Lock()
_schema_ready = False

LOGIN_PAGE = b'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in - Morning Signal</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f6f2;color:#172b29;font:16px system-ui,sans-serif}.card{width:min(390px,calc(100% - 36px));padding:28px;background:white;border:1px solid #dce2dd;border-radius:10px}h1{font:500 30px Georgia,serif}label{display:block;margin:18px 0 6px;font-weight:650}input{box-sizing:border-box;width:100%;padding:11px;border:1px solid #9ca9a2;border-radius:5px;font:inherit}button{margin-top:20px;padding:11px 16px;border:0;border-radius:5px;background:#1f6b59;color:white;font:inherit;font-weight:700}input:focus-visible,button:focus-visible{outline:3px solid #075ea8;outline-offset:3px}.error{color:#8d241a}</style><main class="card"><p>PRIVATE MORNING BRIEFING</p><h1>Morning Signal</h1><p>Sign in to open your private briefing.</p><form method="post" action="/api/login"><label for="username">Username</label><input id="username" name="username" autocomplete="username" required maxlength="128"><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required><button type="submit">Sign in</button></form></main></html>'''


def _ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if not _schema_ready:
            if not is_mysql_database_url(os.environ.get("DATABASE_URL")):
                raise RuntimeError("MySQL database is not configured")
            app.init_db()
            _schema_ready = True


class handler(app.Handler):
    """Vercel's Python runtime accepts BaseHTTPRequestHandler subclasses."""

    def _normalise_route(self):
        parsed = urlsplit(self.path)
        route = parse_qs(parsed.query, keep_blank_values=True).get("__ms_route", [None])[0]
        if route is not None:
            if not route.startswith("/") or route.startswith("//") or "\\" in route:
                route = "/__invalid_route__"
            self.path = route

    def _configuration(self):
        return private_auth.configuration()

    def _session(self, secret):
        return private_auth.session_from_cookie(self.headers.get("Cookie", ""), secret)

    def _local_request(self):
        try:
            _, _, _, origin = self._configuration()
        except private_auth.AuthConfigurationError:
            self.send_json({"error": "Private access is not configured"}, 503)
            return False
        if not private_auth.origin_matches(self.headers, origin, require_origin=self.command not in {"GET", "HEAD"}):
            self.send_json({"error": "Request origin is not allowed"}, 403)
            return False
        return True

    def _respond_login(self, status=200):
        body = LOGIN_PAGE if status == 200 else LOGIN_PAGE.replace(
            b"<form method=", b"<p class=\"error\">Sign-in failed. Check your credentials and try again.</p><form method=")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._normalise_route()
        path = urlsplit(self.path).path
        try:
            _, _, secret, _ = self._configuration()
        except private_auth.AuthConfigurationError:
            return self.send_json({"error": "Private access is not configured"}, 503)
        if not self._local_request():
            return
        session = self._session(secret)
        if path == "/" and not session:
            return self._respond_login()
        if path == "/api/session":
            if not session:
                return self.send_json({"error": "Authentication required"}, 401)
            return self.send_json({"private": True, "csrf": session["csrf"]})
        if not session:
            return self.send_json({"error": "Authentication required"}, 401)
        try:
            _ensure_schema()
            return super().do_GET()
        except Exception:
            return self.send_json({"error": "Morning Signal is temporarily unavailable. Check the database configuration and try again."}, 503)

    def do_POST(self):
        self._normalise_route()
        path = urlsplit(self.path).path
        try:
            _, _, secret, origin = self._configuration()
        except private_auth.AuthConfigurationError:
            return self.send_json({"error": "Private access is not configured"}, 503)
        if not private_auth.origin_matches(self.headers, origin, require_origin=True):
            return self.send_json({"error": "Request origin is not allowed"}, 403)
        if path == "/api/login":
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                return self.send_json({"error": "Expected a sign-in form"}, 415)
            length_text = self.headers.get("Content-Length", "")
            if not length_text.isdecimal() or len(length_text) > 5 or int(length_text) > 8192:
                return self.send_json({"error": "Sign-in request is invalid"}, 400)
            try:
                values = parse_qs(self.rfile.read(int(length_text)).decode("utf-8"), strict_parsing=True)
                username = values.get("username", [""])[0]
                password = values.get("password", [""])[0]
            except (UnicodeError, ValueError):
                return self.send_json({"error": "Sign-in request is invalid"}, 400)
            if not private_auth.authenticate(username, password):
                return self._respond_login(401)
            token, _, _ = private_auth.create_session(secret)
            self._pending_cookie = private_auth.cookie_header(token)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        session = self._session(secret)
        if not session:
            return self.send_json({"error": "Authentication required"}, 401)
        csrf = self.headers.get("X-CSRF-Token", "")
        if not csrf or not hmac.compare_digest(csrf, session["csrf"]):
            return self.send_json({"error": "CSRF token is missing or invalid"}, 403)
        if path == "/api/logout":
            self._pending_cookie = private_auth.cookie_header("", 0)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            _ensure_schema()
            return super().do_POST()
        except Exception:
            return self.send_json({"error": "Morning Signal is temporarily unavailable. Check the database configuration and try again."}, 503)

    def end_headers(self):
        cookie = getattr(self, "_pending_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()
