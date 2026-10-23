"""Security and deployment tests use temporary databases and fixture credentials."""
import json
import os
import sqlite3
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import api.index as vercel_api
import import_sqlite_to_neon as importer
import private_auth
import server


class PrivateAuthTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "MORNING_SIGNAL_USERNAME": "owner",
            "MORNING_SIGNAL_PASSWORD": "a-test-password-that-is-long-enough",
            "MORNING_SIGNAL_SESSION_SECRET": "x" * 48,
            "APP_ORIGIN": "https://news.example.test",
        }

    def test_credentials_and_cookie_session(self):
        self.assertTrue(private_auth.authenticate("owner", self.env["MORNING_SIGNAL_PASSWORD"], self.env))
        self.assertFalse(private_auth.authenticate("owner", "wrong-password", self.env))
        token, csrf, expires = private_auth.create_session(b"s" * 48, now=1000)
        cookie = private_auth.cookie_header(token)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertEqual(private_auth.session_from_cookie(f"{private_auth.COOKIE_NAME}={token}", b"s" * 48, now=1001),
                         {"csrf": csrf, "expires": expires})
        self.assertIsNone(private_auth.session_from_cookie(f"{private_auth.COOKIE_NAME}={token}", b"z" * 48, now=1001))
        self.assertIsNone(private_auth.session_from_cookie(f"{private_auth.COOKIE_NAME}={token}", b"s" * 48, now=expires))

    def test_origin_and_required_origin_checks(self):
        headers = {"Host": "news.example.test", "Origin": "https://news.example.test"}
        self.assertTrue(private_auth.origin_matches(headers, self.env["APP_ORIGIN"], require_origin=True))
        self.assertFalse(private_auth.origin_matches({"Host": "news.example.test"}, self.env["APP_ORIGIN"], require_origin=True))
        self.assertFalse(private_auth.origin_matches({**headers, "Origin": "https://evil.example"}, self.env["APP_ORIGIN"]))
        self.assertFalse(private_auth.origin_matches({**headers, "Origin": "https://[invalid"}, self.env["APP_ORIGIN"]))
        self.assertFalse(private_auth.origin_matches({**headers, "Sec-Fetch-Site": "cross-site"}, self.env["APP_ORIGIN"]))

    def test_configuration_requires_long_secret_and_https_origin(self):
        with self.assertRaises(private_auth.AuthConfigurationError):
            private_auth.configuration({**self.env, "MORNING_SIGNAL_SESSION_SECRET": "short"})
        with self.assertRaises(private_auth.AuthConfigurationError):
            private_auth.configuration({**self.env, "APP_ORIGIN": "http://news.example.test"})


class PostgresAdapterTests(unittest.TestCase):
    def test_sqlite_sql_subset_translation(self):
        self.assertEqual(server.postgres_sql("INSERT OR IGNORE INTO stories(id) VALUES (?)"),
                         "INSERT INTO stories (id) VALUES (%s) ON CONFLICT DO NOTHING")
        self.assertEqual(server.postgres_sql("INSERT OR REPLACE INTO settings VALUES (?,?)"),
                         "INSERT INTO settings VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value")
        self.assertEqual(server.postgres_sql("SELECT * FROM stories WHERE id=?"),
                         "SELECT * FROM stories WHERE id=%s")
        with self.assertRaises(ValueError):
            server.postgres_sql("INSERT OR REPLACE INTO stories VALUES (?)")

    def test_postgres_connection_uses_translated_statements_and_named_rows(self):
        class Column:
            name = "story_id"
        class Cursor:
            description = [Column()]
            def execute(self, statement, parameters):
                self.statement, self.parameters = statement, parameters
            def fetchone(self):
                return self.row_factory(self)(("fixture-story",))
        class Connection:
            def cursor(self, row_factory=None):
                self.cursor_value = Cursor()
                self.cursor_value.row_factory = row_factory
                return self.cursor_value
        connection = Connection()
        db = server.PostgresConnection(connection)
        row = db.execute("SELECT story_id FROM stories WHERE id=?", ("fixture",)).fetchone()
        self.assertEqual(connection.cursor_value.statement, "SELECT story_id FROM stories WHERE id=%s")
        self.assertEqual(row["story_id"], "fixture-story")
        self.assertEqual(row[0], "fixture-story")

    def test_remote_database_is_disabled_in_local_app_without_explicit_opt_in(self):
        fixture_url = "postgresql://" + "fixture" + ":" + "placeholder" + "@ep.example.neon.tech/db?sslmode=require"
        with patch.dict(os.environ, {"DATABASE_URL": fixture_url}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Remote databases are disabled locally"):
                with server.connect():
                    pass


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.original_db = server.DB
        self.source_path = self.root / "fixture.sqlite3"
        self.target_path = self.root / "target.sqlite3"
        with patch.object(server, "DB", self.source_path):
            server.init_db()
            with server.connect() as db:
                db.execute("INSERT INTO stories (id,title,url,summary,category,region,created,published,read,saved,hidden,read_at,note,reading_minutes,fetched_at,feedback) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("a" * 40, "Fixture story", "https://publisher.example/story", "Fixture excerpt", "World", "India", "2026-10-22T08:00:00+00:00", "2026-10-22T07:00:00+00:00", 1, 1, 1, "2026-10-22T08:30:00+00:00", "private fixture note", 4, "2026-10-22T08:00:00+00:00", "more"))
                db.execute("INSERT INTO story_sources (story_id,source_id,source_name,url,last_seen,publisher_group,title) VALUES (?,?,?,?,?,?,?)",
                           ("a" * 40, "fixture-source", "Fixture publisher", "https://publisher.example/story", "2026-10-22T08:00:00+00:00", "fixture-publisher", "Fixture story"))
                db.execute("INSERT INTO source_status VALUES (?,?,?)", ("fixture-source", "2026-10-22T08:01:00+00:00", None))
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("topics", json.dumps(["Technology"])))
                db.execute("INSERT INTO reading_history VALUES (?,?,?,?)", ("a" * 40, "2026-10-22", "Politics & world affairs", "2026-10-22T08:30:00+00:00"))
        with patch.object(server, "DB", self.target_path):
            server.init_db()

    def tearDown(self):
        server.DB = self.original_db
        self.temp.cleanup()

    def test_import_preserves_fixture_state_and_is_idempotent(self):
        with importer.open_source(self.source_path) as source:
            data = importer.read_source(source)
        with patch.object(server, "DB", self.target_path), server.connect() as db:
            counts, written = importer.import_data(data, db)
            self.assertTrue(written)
            self.assertEqual(counts["stories"], 1)
        with patch.object(server, "DB", self.target_path), server.connect() as db:
            self.assertEqual(importer.import_data(data, db)[1], False)
            story = db.execute("SELECT * FROM stories WHERE id=?", ("a" * 40,)).fetchone()
            self.assertEqual((story["read"], story["saved"], story["hidden"]), (1, 1, 1))
            self.assertEqual(story["note"], "private fixture note")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reading_history").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM story_sources").fetchone()[0], 1)

    def test_import_refuses_different_snapshot_and_nonfresh_target(self):
        with importer.open_source(self.source_path) as source:
            data = importer.read_source(source)
        with patch.object(server, "DB", self.target_path), server.connect() as db:
            importer.import_data(data, db)
        changed = {key: [dict(row) for row in rows] for key, rows in data.items()}
        changed["stories"][0]["note"] = "different snapshot"
        with patch.object(server, "DB", self.target_path), server.connect() as db:
            with self.assertRaises(importer.ImportErrorSafe):
                importer.import_data(changed, db)
        with patch.object(server, "DB", self.root / "custom.sqlite3"):
            server.init_db()
            with server.connect() as db:
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("length", "7"))
                with self.assertRaisesRegex(importer.ImportErrorSafe, "customized settings"):
                    importer.import_data(data, db)

    def test_target_validation_requires_explicit_matching_neon_endpoint(self):
        url = "postgresql://" + "fixture" + ":" + "placeholder" + "@ep-test-123.neon.tech/morning?sslmode=require"
        importer.validate_target(url, "ep-test-123.neon.tech", "morning")
        for args in ((url, "other.neon.tech", "morning"), (url, "ep-test-123.neon.tech", "other"),
                     ("postgresql://" + "fixture" + ":" + "placeholder" + "@localhost/morning?sslmode=require", "localhost", "morning")):
            with self.assertRaises(importer.ImportErrorSafe):
                importer.validate_target(*args)

    def test_cli_without_confirmation_is_read_only_and_reports_no_story_content(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = importer.main(["--source", str(self.source_path)])
        self.assertEqual(result, 0)
        self.assertIn("No data was written", output.getvalue())
        self.assertNotIn("Fixture story", output.getvalue())
        self.assertNotIn("private fixture note", output.getvalue())

    def test_deployment_ignores_local_databases_backups_and_env_files(self):
        gitignore = (Path(__file__).parent / ".gitignore").read_text(encoding="utf-8")
        vercelignore = (Path(__file__).parent / ".vercelignore").read_text(encoding="utf-8")
        config = json.loads((Path(__file__).parent / "vercel.json").read_text(encoding="utf-8"))
        for pattern in ("*.sqlite3", "*.db", "*.bak", ".env.*"):
            self.assertIn(pattern, gitignore)
        for pattern in ("*.sqlite3", "*.db", "*.bak", ".env.*"):
            self.assertIn(pattern, vercelignore)
        self.assertIn("**/*.sqlite3", config["functions"]["api/index.py"]["excludeFiles"])


class VercelPrivateRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin = "https://news.example.test"
        cls.env = patch.dict(os.environ, {
            "APP_ORIGIN": cls.origin,
            "MORNING_SIGNAL_USERNAME": "owner",
            "MORNING_SIGNAL_PASSWORD": "fixture-password-long-enough-123",
            "MORNING_SIGNAL_SESSION_SECRET": "k" * 48,
            "DATABASE_URL": "postgresql://" + "fixture" + ":" + "placeholder" + "@ep-fixture.neon.tech/morning?sslmode=require",
        }, clear=True)
        cls.env.start()
        cls.pg_patch = patch.object(vercel_api, "is_neon_database_url", return_value=True)
        cls.pg_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.pg_patch.stop()
        cls.env.stop()
        vercel_api._schema_ready = False

    def make_handler(self, method, path, headers=None):
        from email.message import Message
        instance = object.__new__(vercel_api.handler)
        instance.command = method
        instance.path = path
        instance.headers = Message()
        instance.headers["Host"] = "news.example.test"
        instance.headers["Origin"] = self.origin
        for key, value in (headers or {}).items():
            if instance.headers.get(key) is not None:
                instance.headers.replace_header(key, value)
            else:
                instance.headers[key] = value
        instance.response = None
        instance.response_headers = {}
        instance.rfile = __import__("io").BytesIO()
        instance.send_json = lambda value, status=200: setattr(instance, "response", (status, value))
        instance._respond_login = lambda status=200: setattr(instance, "response", (status, "login"))
        instance.send_response = lambda status: setattr(instance, "response_status", status)
        instance.send_header = lambda key, value: instance.response_headers.__setitem__(key, value)
        def end_headers():
            if getattr(instance, "_pending_cookie", None):
                instance.response_headers["Set-Cookie"] = instance._pending_cookie
            instance.response = (instance.response_status, instance.response_headers.copy())
        instance.end_headers = end_headers
        return instance

    def test_private_pages_and_apis_require_login(self):
        for path in ("/api/briefing", "/app.js", "/"):
            instance = self.make_handler("GET", path)
            instance.do_GET()
            self.assertEqual(instance.response[0], 200 if path == "/" else 401)
        instance = self.make_handler("GET", "/api/briefing", {"Origin": "https://evil.example"})
        instance.do_GET()
        self.assertEqual(instance.response[0], 403)

    def test_login_csrf_protection_private_api_and_logout(self):
        instance = self.make_handler("POST", "/api/settings")
        instance.do_POST()
        self.assertEqual(instance.response[0], 401)
        token, csrf, _ = private_auth.create_session(b"k" * 48)
        cookie = f"{private_auth.COOKIE_NAME}={token}"
        session_handler = self.make_handler("GET", "/api/session", {"Cookie": cookie})
        session_handler.do_GET()
        self.assertEqual(session_handler.response[0], 200)
        self.assertEqual(session_handler.response[1], {"private": True, "csrf": csrf})
        instance = self.make_handler("POST", "/api/settings", {"Cookie": cookie})
        instance.do_POST()
        self.assertEqual(instance.response[0], 403)
        instance = self.make_handler("POST", "/api/settings", {"Cookie": cookie, "X-CSRF-Token": csrf})
        instance.headers["Content-Type"] = "application/json"
        instance.headers["Content-Length"] = "2"
        instance.rfile = __import__("io").BytesIO(b"{}")
        self.assertTrue(private_auth.origin_matches(instance.headers, self.origin, require_origin=True))
        self.assertEqual(private_auth.session_from_cookie(cookie, b"k" * 48)["csrf"], csrf)
        login = self.make_handler("POST", "/api/login")
        login.headers["Content-Type"] = "application/x-www-form-urlencoded"
        login.headers["Content-Length"] = str(len(b"username=owner&password=fixture-password-long-enough-123"))
        login.rfile = __import__("io").BytesIO(b"username=owner&password=fixture-password-long-enough-123")
        login.do_POST()
        self.assertEqual(login.response[0], 303)
        self.assertIn("Secure; HttpOnly; SameSite=Strict", login.response[1]["Set-Cookie"])
        issued = SimpleCookie()
        issued.load(login.response[1]["Set-Cookie"])
        token = issued[private_auth.COOKIE_NAME].value
        session = private_auth.session_from_cookie(f"{private_auth.COOKIE_NAME}={token}", b"k" * 48)
        logout = self.make_handler("POST", "/api/logout", {
            "Cookie": f"{private_auth.COOKIE_NAME}={token}", "X-CSRF-Token": session["csrf"]})
        logout.do_POST()
        self.assertEqual(logout.response[0], 204)
        self.assertIn("Max-Age=0", logout.response[1]["Set-Cookie"])

    def test_wrong_content_type_and_oversized_login_are_rejected(self):
        instance = self.make_handler("POST", "/api/login", {"Content-Type": "text/plain", "Content-Length": "1"})
        instance.do_POST()
        self.assertEqual(instance.response[0], 415)
        instance = self.make_handler("POST", "/api/login", {
            "Content-Type": "application/x-www-form-urlencoded", "Content-Length": "8193"})
        instance.do_POST()
        self.assertEqual(instance.response[0], 400)
        instance = self.make_handler("POST", "/api/login", {
            "Content-Type": "application/x-www-form-urlencoded", "Content-Length": "9" * 5000})
        instance.do_POST()
        self.assertEqual(instance.response[0], 400)


if __name__ == "__main__":
    unittest.main()
