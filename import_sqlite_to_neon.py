"""Explicit one-time, idempotent import of local Morning Signal data to Neon."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import server
from database_config import is_neon_database_url

TABLES = {
    "stories": ("id", ("id", "title", "url", "summary", "published", "category", "region", "created", "fetched_at", "read", "saved", "hidden", "read_at", "note", "reading_minutes", "feedback")),
    "story_sources": (("story_id", "source_id"), ("story_id", "source_id", "source_name", "url", "last_seen", "publisher_group", "title")),
    "source_status": ("source_id", ("source_id", "checked", "error")),
    "settings": ("key", ("key", "value")),
    "reading_history": (("story_id", "completed_on"), ("story_id", "completed_on", "theme", "completed_at")),
    "app_meta": ("key", ("key", "value")),
}


class ImportErrorSafe(ValueError):
    pass


def validate_target(url, expected_host, expected_database):
    if not is_neon_database_url(url):
        raise ImportErrorSafe("DATABASE_URL must be an SSL Neon Postgres URL")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    database = unquote(parsed.path.lstrip("/").split("/", 1)[0])
    if not expected_host or host != expected_host.lower().strip():
        raise ImportErrorSafe("Neon host does not match --expected-host")
    if not expected_database or database != expected_database:
        raise ImportErrorSafe("Database name does not match --expected-database")


@contextmanager
def open_source(path):
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.suffix.lower() not in {".sqlite3", ".db"}:
        raise ImportErrorSafe("Source must be an existing SQLite database file")
    source = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        source.close()
        raise ImportErrorSafe("SQLite source failed its integrity check")
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"stories", "settings"}.issubset(tables):
        source.close()
        raise ImportErrorSafe("SQLite source is missing required Morning Signal tables")
    try:
        yield source
    finally:
        source.close()


def read_source(source):
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    for table, (_, columns) in TABLES.items():
        if table not in tables:
            result[table] = []
            continue
        present = {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
        result[table] = [dict(row) for row in source.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
        for row in result[table]:
            for column in columns:
                if column not in present:
                    row[column] = None
    story_by_id = {row["id"]: row for row in result["stories"]}
    source_by_id = {item["id"]: item for item in server.get_sources()}
    for story in result["stories"]:
        if not story.get("fetched_at"):
            story["fetched_at"] = story.get("created")
        for column in ("read", "saved", "hidden"):
            if story[column] is None:
                story[column] = 0
    for association in result["story_sources"]:
        story = story_by_id.get(association["story_id"], {})
        source_info = source_by_id.get(association["source_id"], {})
        association.setdefault("publisher_group", source_info.get("publisher_group", association["source_id"]))
        if not association["publisher_group"]:
            association["publisher_group"] = source_info.get("publisher_group", association["source_id"])
        if not association["title"]:
            association["title"] = story.get("title")
        if not association["url"] and story.get("url"):
            story_host = (urlsplit(story["url"]).hostname or "").lower().removeprefix("www.")
            feed_host = (urlsplit(source_info.get("url", "")).hostname or "").lower().removeprefix("www.")
            if story_host and story_host == feed_host:
                association["url"] = story["url"]
    return result


def source_fingerprint(data):
    digest = hashlib.sha256()
    for table in TABLES:
        for row in data[table]:
            digest.update(table.encode("utf-8") + b"\0")
            digest.update(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _target_ready_for_import(db):
    marker = db.execute("SELECT value FROM app_meta WHERE key='sqlite_import_fingerprint_v1'").fetchone()
    counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("stories", "story_sources", "source_status", "reading_history")}
    if marker:
        return marker[0]
    if any(counts.values()):
        raise ImportErrorSafe("Neon target already contains user data; choose a fresh database or an already-imported source")
    expected_defaults = {
        "regions": ["India", "Asia", "Europe", "Africa", "Americas", "Middle East", "Global"],
        "topics": ["Technology", "Science", "Business", "World", "India"],
        "sources": [source["id"] for source in server.get_sources() if source.get("enabled")],
        "excluded": ["Celebrity", "Influencer", "Entertainment gossip", "K-pop"],
        "length": 10, "difficulty": "Standard", "daily_goal": 1, "freshness_hours": 48,
    }
    stored_settings = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key,value FROM settings")}
    if any(key not in expected_defaults or value != expected_defaults[key]
           for key, value in stored_settings.items()):
        raise ImportErrorSafe("Neon target has customized settings; choose a fresh database before importing")
    return None


def import_data(data, db):
    fingerprint = source_fingerprint(data)
    current = _target_ready_for_import(db)
    if current:
        if current == fingerprint:
            return {table: len(rows) for table, rows in data.items()}, False
        raise ImportErrorSafe("Neon target was imported from a different SQLite snapshot")
    for table, (keys, columns) in TABLES.items():
        if not data[table]:
            continue
        key_columns = (keys,) if isinstance(keys, str) else keys
        updates = [column for column in columns if column not in key_columns]
        placeholders = ",".join("?" for _ in columns)
        names = ",".join(columns)
        update_sql = ",".join(f"{column}=EXCLUDED.{column}" for column in updates)
        sql = f"INSERT INTO {table} ({names}) VALUES ({placeholders}) ON CONFLICT ({','.join(key_columns)}) DO UPDATE SET {update_sql}" if updates else f"INSERT INTO {table} ({names}) VALUES ({placeholders}) ON CONFLICT ({','.join(key_columns)}) DO NOTHING"
        for row in data[table]:
            db.execute(sql, tuple(row.get(column) for column in columns))
    db.execute("INSERT INTO app_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
               ("sqlite_import_fingerprint_v1", fingerprint))
    return {table: len(rows) for table, rows in data.items()}, True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Copy a local SQLite database to a fresh Neon Postgres database")
    parser.add_argument("--source", default=str(server.DB), help="Local SQLite file; read-only")
    parser.add_argument("--confirm", action="store_true", help="Confirm writing this source snapshot into Neon")
    parser.add_argument("--expected-host", help="Exact Neon endpoint hostname to protect against a wrong target")
    parser.add_argument("--expected-database", help="Exact database name to protect against a wrong target")
    args = parser.parse_args(argv)
    try:
        with open_source(args.source) as source:
            data = read_source(source)
        counts = {table: len(rows) for table, rows in data.items()}
        if not args.confirm:
            print("Read-only check complete. No data was written.")
            print("Rows available: " + ", ".join(f"{table}={count}" for table, count in counts.items()))
            return 0
        target_url = os.environ.get("DATABASE_URL", "")
        validate_target(target_url, args.expected_host, args.expected_database)
        os.environ["MORNING_SIGNAL_ALLOW_REMOTE_DATABASE"] = "1"
        server.init_db()
        with server.connect() as db:
            counts, written = import_data(data, db)
        print(("Import complete" if written else "This source snapshot was already imported") + ". No story text or credentials were printed.")
        print("Rows: " + ", ".join(f"{table}={count}" for table, count in counts.items()))
        return 0
    except ImportErrorSafe as ex:
        print(str(ex), file=sys.stderr)
        return 2
    except Exception:
        print("Import failed. No credentials or story content were printed; verify the source, target, and database connectivity.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
