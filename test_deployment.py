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

    def test_refresh_rejects_unexpected_input_and_stateful_actions(self):
        bad_body = self.make_handler("POST", "/api/refresh", b'{"notes":"no"}')
        bad_body.do_POST()
        self.assertEqual(bad_body.response[0], 400)
        action = self.make_handler("POST", "/api/story/123")
        action.do_POST()
        self.assertEqual(action.response[0], 404)


if __name__ == "__main__":
    unittest.main()
