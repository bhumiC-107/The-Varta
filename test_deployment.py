"""Stateless Vercel deployment tests use feed fixtures and never touch user data."""
import datetime as dt
import io
import json
import unittest
from email.message import Message
from unittest.mock import patch

import api.index as vercel_api
import server


class StatelessBriefingTests(unittest.TestCase):
    def test_feed_collection_and_failures_need_no_database(self):
        now = dt.datetime(2026, 10, 24, 6, tzinfo=dt.timezone.utc)
        sources = server.get_sources()
        def fetch(source, _excluded, _timeout):
            if source["id"] != sources[0]["id"]:
                raise ValueError("Feed XML is malformed")
            return [{"title": "India parliament passes major climate policy", "summary": "A recent government vote on climate policy.",
                     "url": "https://publisher.example/fixture", "published": now.isoformat(), "reading_minutes": 3}]
        with patch.object(server, "connect", side_effect=AssertionError("stateless collector must not open SQLite")):
            result = server.stateless_briefing(fetcher=fetch, now=now)
        self.assertTrue(result["stories"])
        self.assertEqual(result["stories"][0]["title"], "India parliament passes major climate policy")
        self.assertEqual(result["coverage"]["story_count"], 1)
        self.assertGreater(result["refresh"]["failed"], 0)
        self.assertTrue(any(source.get("status", {}).get("error") for source in result["sources"] if source.get("status")))

    def test_deployment_has_no_database_auth_or_stateful_endpoints(self):
        self.assertNotIn("DATABASE_URL", vercel_api.__doc__)
        self.assertFalse(hasattr(vercel_api, "_ensure_schema"))
        self.assertFalse(hasattr(vercel_api, "private_auth"))


class VercelHandlerTests(unittest.TestCase):
    def make_handler(self, method, path, body=b"{}", content_type="application/json"):
        handler = object.__new__(vercel_api.handler)
        handler.command, handler.path = method, path
        handler.headers = Message()
        handler.headers["Content-Type"] = content_type
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.response = None
        handler.send_json = lambda value, status=200: setattr(handler, "response", (status, value))
        handler.send_response = lambda status: setattr(handler, "status", status)
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        return handler

    def test_public_session_and_refresh_does_not_require_credentials(self):
        session = self.make_handler("GET", "/api/session")
        session.do_GET()
        self.assertEqual(session.response, (200, {"private": False, "stateless": True}))
        refresh = self.make_handler("POST", "/api/refresh")
        expected = {"stories": []}
        with patch.object(server, "stateless_briefing", return_value=expected):
            refresh.do_POST()
        self.assertEqual(refresh.response, (200, expected))

    def test_glossary_configuration_is_available_to_stateless_reader(self):
        config = self.make_handler("GET", "/api/config")
        with patch.object(server, "connect", side_effect=AssertionError("public config must not open SQLite")):
            config.do_GET()
        self.assertEqual(config.response[0], 200)
        glossary=config.response[1]["glossary"]
        self.assertTrue(glossary)
        self.assertTrue(all(server.safe_https_url(item["source_url"]) for item in glossary))

    def test_direct_function_preview_url_falls_back_to_site_root(self):
        handler = self.make_handler("GET", "/api/index")
        handler.do_GET()
        self.assertEqual(handler.status, 200)
        self.assertIn(b"Morning Signal", handler.wfile.getvalue())
        self.assertFalse(issubclass(vercel_api.handler, server.Handler))

    def test_refresh_rejects_unexpected_input_and_stateful_actions(self):
        bad_body = self.make_handler("POST", "/api/refresh", b'{"notes":"no"}')
        bad_body.do_POST()
        self.assertEqual(bad_body.response[0], 400)
        action = self.make_handler("POST", "/api/story/123")
        action.do_POST()
        self.assertEqual(action.response[0], 404)

    def test_discovered_local_handler_uses_stateless_vercel_branch(self):
        def instance(method, path, body=b"{}"):
            value = object.__new__(server.Handler)
            value.command, value.path = method, path
            value.headers = Message()
            value.headers["Host"] = "the-varta.vercel.app"
            value.headers["Content-Type"] = "application/json"
            value.headers["Content-Length"] = str(len(body))
            value.rfile = io.BytesIO(body)
            value.response = None
            value.send_json = lambda payload, status=200: setattr(value, "response", (status, payload))
            return value
        with patch.dict("os.environ", {"VERCEL": "1"}):
            page = instance("GET", "/api/index?__ms_route=/api/session")
            page.do_GET()
            self.assertEqual(page.response, (200, {"private": False, "stateless": True}))
            briefing = instance("GET", "/api/index?__ms_route=/api/briefing")
            with patch.object(server, "connect", side_effect=AssertionError("hosted GET must not open SQLite")):
                briefing.do_GET()
            self.assertEqual(briefing.response[0], 200)
            self.assertEqual(briefing.response[1]["stories"], [])
            refresh = instance("POST", "/api/index?__ms_route=/api/refresh")
            fixture = {"stories": [{"title": "fixture"}]}
            with patch.object(server, "stateless_briefing", return_value=fixture), patch.object(server, "connect", side_effect=AssertionError("hosted refresh must not open SQLite")):
                refresh.do_POST()
            self.assertEqual(refresh.response, (200, fixture))


if __name__ == "__main__":
    unittest.main()
