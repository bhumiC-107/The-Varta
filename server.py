"""Morning Signal: local RSS briefing server with optional MySQL deployment support."""
from __future__ import annotations

import datetime as dt
import errno
import hashlib
import html
import ipaddress
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

ROOT = Path(__file__).parent
DB = Path(os.environ.get("MORNING_SIGNAL_SQLITE_PATH", ROOT / "morning_signal.sqlite3"))
CONFIG = ROOT / "sources.json"
EDITORIAL = ROOT / "editorial.json"
PORT = 8765
MAX_FEED_BYTES = 1_000_000
MAX_POST_BYTES = 32_768
_refresh_lock = threading.Lock()
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/styles.css": "styles.css", "/app.js": "app.js", "/README.md": "README.md"}


class LocalHTTPServer(ThreadingHTTPServer):
    # Reuse-address permits ambiguous duplicate listeners on Windows. Keep one
    # app process on the local port so an older server cannot serve stale code.
    allow_reuse_address = False
FOCUS = {
    0: "Politics & world affairs", 1: "Technology & computer science",
    2: "Business & personal finance", 3: "Science, climate & health",
    4: "India, society & culture", 5: "Explainers & history", 6: "Week in review",
}
CATEGORIES = ["World", "India", "Technology", "Business", "Science", "Climate", "Health", "Society"]
READING_MODES = {"Quick scan", "Standard", "More context"}
STOP = set("about after again also amid an and are as at be been before being between by can could did do does for from had has have he her here hers him his how i if in into is it its may more most new no not of on or other our out over said she should since so than that the their them then there these they this those to up us was we were what when where which while who will with would you your update news says".split())


class CompatibleRow(dict):
    """Remote SQL row with sqlite3.Row-compatible name and integer indexing."""
    def __getitem__(self, key):
        if isinstance(key, int):
            key = tuple(self.keys())[key]
        return super().__getitem__(key)


def mysql_sql(statement):
    """Translate the app's small SQLite SQL subset to MySQL-compatible SQL."""
    statement = statement.strip().rstrip(";")
    match = re.match(r"INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO\s+(\w+)(?=\s|\()\s*", statement, re.I)
    if match:
        mode, table = match.group(1).upper(), match.group(2)
        prefix = "INSERT IGNORE INTO" if mode == "IGNORE" else "REPLACE INTO"
        statement = prefix + " " + table + " " + statement[match.end():]
    statement = re.sub(r"\b(WHERE|AND)\s+key(?=\s*(?:=|<>|<|>|\bLIKE\b|\bIN\b|\bIS\b))", r"\1 `key`", statement, flags=re.I)
    statement = re.sub(r"(?<=\.)key\b", "`key`", statement, flags=re.I)
    statement = re.sub(r"\bSELECT\s+key(?=\s*,)", "SELECT `key`", statement, flags=re.I)
    statement = re.sub(r"\(\s*key\s*,", "(`key`,", statement, flags=re.I)
    statement = re.sub(r"(?<![\w`])read(?![\w`])", "`read`", statement, flags=re.I)
    return statement.replace("?", "%s")


class MySQLCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    @staticmethod
    def _row(row):
        return CompatibleRow(row) if isinstance(row, dict) else row

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]


class MySQLConnection:
    def __init__(self, connection):
        self.connection = connection
        self.dialect = "mysql"

    def execute(self, statement, parameters=()):
        import pymysql.cursors
        cursor = self.connection.cursor(pymysql.cursors.DictCursor)
        cursor.execute(mysql_sql(statement), parameters)
        return MySQLCursor(cursor)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def columns(self, table):
        return [row[0] for row in self.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? ORDER BY ORDINAL_POSITION",
            (table,)).fetchall()]


@contextmanager
def connect():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        if not os.environ.get("VERCEL") and os.environ.get("MORNING_SIGNAL_ALLOW_REMOTE_DATABASE") != "1":
            raise RuntimeError("Remote databases are disabled locally; set MORNING_SIGNAL_ALLOW_REMOTE_DATABASE=1 only for a dedicated development database")
        from urllib.parse import unquote, urlsplit
        from database_config import is_mysql_database_url
        if not is_mysql_database_url(database_url):
            raise RuntimeError("DATABASE_URL must be a verified-TLS MySQL URL")
        try:
            import pymysql
        except ImportError as ex:
            raise RuntimeError("MySQL support requires installing requirements.txt") from ex
        parsed = urlsplit(database_url)
        con = pymysql.connect(
            host=parsed.hostname, port=parsed.port, user=unquote(parsed.username),
            password=unquote(parsed.password), database=unquote(parsed.path.lstrip("/")),
            connect_timeout=5, read_timeout=10, write_timeout=10, charset="utf8mb4",
            autocommit=False, ssl_verify_cert=True, ssl_verify_identity=True)
        try:
            yield MySQLConnection(con)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with connect() as db:
        mysql = getattr(db, "dialect", "sqlite") == "mysql"
        if mysql:
            lock = db.execute("SELECT GET_LOCK(?, 10)", ("morning-signal-schema-init",)).fetchone()
            if not lock or lock[0] != 1:
                raise RuntimeError("Could not acquire the schema initialization lock")
        if mysql:
            schema = """
                CREATE TABLE IF NOT EXISTS stories(id VARCHAR(64) PRIMARY KEY,title TEXT NOT NULL,url VARCHAR(768) NOT NULL,summary TEXT NOT NULL,published VARCHAR(40),category VARCHAR(64) NOT NULL,region VARCHAR(64) NOT NULL,created VARCHAR(40) NOT NULL,fetched_at VARCHAR(40),`read` TINYINT DEFAULT 0,saved TINYINT DEFAULT 0,hidden TINYINT DEFAULT 0,read_at VARCHAR(40),note VARCHAR(280),reading_minutes INT,feedback VARCHAR(16),UNIQUE KEY stories_url_unique(url));
                CREATE TABLE IF NOT EXISTS story_sources(story_id VARCHAR(64),source_id VARCHAR(128),source_name VARCHAR(191),url VARCHAR(768),last_seen VARCHAR(40),publisher_group VARCHAR(128),title TEXT,PRIMARY KEY(story_id,source_id));
                CREATE TABLE IF NOT EXISTS source_status(source_id VARCHAR(128) PRIMARY KEY,checked VARCHAR(40),error TEXT);
                CREATE TABLE IF NOT EXISTS reading_history(story_id VARCHAR(64) NOT NULL,completed_on CHAR(10) NOT NULL,theme VARCHAR(128) NOT NULL,completed_at VARCHAR(40) NOT NULL,PRIMARY KEY(story_id,completed_on));
                CREATE TABLE IF NOT EXISTS settings(`key` VARCHAR(191) PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS app_meta(`key` VARCHAR(191) PRIMARY KEY,value TEXT NOT NULL);
            """
        else:
            schema = """
        CREATE TABLE IF NOT EXISTS stories(
          id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL UNIQUE, summary TEXT NOT NULL,
          published TEXT, category TEXT NOT NULL, region TEXT NOT NULL, created TEXT NOT NULL,
          fetched_at TEXT, read INTEGER DEFAULT 0, saved INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0,
          read_at TEXT, note TEXT, reading_minutes INTEGER, feedback TEXT);
        CREATE TABLE IF NOT EXISTS story_sources(story_id TEXT, source_id TEXT, source_name TEXT, url TEXT, last_seen TEXT,
          publisher_group TEXT, title TEXT, PRIMARY KEY(story_id, source_id));
        CREATE TABLE IF NOT EXISTS source_status(source_id TEXT PRIMARY KEY, checked TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS reading_history(story_id TEXT NOT NULL, completed_on TEXT NOT NULL, theme TEXT NOT NULL, completed_at TEXT NOT NULL, PRIMARY KEY(story_id, completed_on));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        db.executescript(schema)
        source_columns = set(db.columns("story_sources")) if mysql else {row[1] for row in db.execute("PRAGMA table_info(story_sources)")}
        if "url" not in source_columns:
            db.execute("ALTER TABLE story_sources ADD COLUMN url TEXT")
        if "last_seen" not in source_columns:
            db.execute("ALTER TABLE story_sources ADD COLUMN last_seen TEXT")
        story_columns = set(db.columns("stories")) if mysql else {row[1] for row in db.execute("PRAGMA table_info(stories)")}
        for column, declaration in (("read_at", "TEXT"), ("note", "TEXT"), ("reading_minutes", "INTEGER"), ("fetched_at", "TEXT"), ("feedback", "TEXT")):
            if column not in story_columns:
                db.execute(f"ALTER TABLE stories ADD COLUMN {column} {declaration}")
        db.execute("UPDATE stories SET fetched_at=created WHERE fetched_at IS NULL")
        source_columns = set(db.columns("story_sources")) if mysql else {row[1] for row in db.execute("PRAGMA table_info(story_sources)")}
        for column in ("publisher_group", "title"):
            if column not in source_columns:
                db.execute(f"ALTER TABLE story_sources ADD COLUMN {column} TEXT")
        for source in get_sources():
            db.execute("UPDATE story_sources SET publisher_group=? WHERE source_id=?",
                (source.get("publisher_group", source["id"]), source["id"]))
        db.execute("UPDATE story_sources SET title=(SELECT title FROM stories WHERE stories.id=story_sources.story_id) WHERE title IS NULL")
        if not db.execute("SELECT 1 FROM app_meta WHERE key='source_urls_migration_v2'").fetchone():
            # Older versions had only one article URL per cluster. Preserve it only
            # under the first contributing source whose configured host matches it.
            publisher_hosts = {}
            for source in get_sources():
                host = urlparse(source.get("url", "")).hostname
                if host:
                    publisher_hosts[source["id"]] = host.lower().removeprefix("www.")
            db.execute("UPDATE story_sources SET url=NULL")
            stories = db.execute("SELECT id,url FROM stories").fetchall()
            for story in stories:
                story_host = urlparse(story["url"]).hostname.lower().removeprefix("www.") if story["url"] else None
                associations = db.execute("SELECT source_id FROM story_sources WHERE story_id=? ORDER BY source_id", (story["id"],)).fetchall()
                matching = next((a for a in associations if publisher_hosts.get(a["source_id"]) == story_host), None)
                if matching:
                    db.execute("UPDATE story_sources SET url=? WHERE story_id=? AND source_id=?", (story["url"], story["id"], matching["source_id"]))
            db.execute("INSERT OR IGNORE INTO app_meta VALUES ('source_urls_migration_v2','done')")
        defaults = {
          "regions": ["India", "Asia", "Europe", "Africa", "Americas", "Middle East", "Global"],
          "topics": ["Technology", "Science", "Business", "World", "India"],
          "sources": [s["id"] for s in get_sources() if s.get("enabled")], "excluded": ["Celebrity", "Influencer", "Entertainment gossip", "K-pop"],
          "length": 10, "difficulty": "Standard", "daily_goal": 1,
          "freshness_hours": 48,
        }
        for k, v in defaults.items():
            db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, json.dumps(v)))
        legacy_difficulty = db.execute("SELECT value FROM settings WHERE key='difficulty'").fetchone()
        if legacy_difficulty:
            old_value = json.loads(legacy_difficulty[0])
            migrated = {"Detailed":"More context", "Plain language":"Standard"}.get(old_value, old_value)
            if migrated not in READING_MODES:
                migrated = "Standard"
            if migrated != old_value:
                db.execute("UPDATE settings SET value=? WHERE key='difficulty'", (json.dumps(migrated),))
        if mysql:
            db.execute("SELECT RELEASE_LOCK(?)", ("morning-signal-schema-init",))


def get_sources():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def get_editorial():
    return json.loads(EDITORIAL.read_text(encoding="utf-8"))


def public_sources(sources):
    return [{key: value for key, value in source.items() if key != "url"} for source in sources]


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_publication_time(value, now=None):
    """Parse only timestamps with an explicit timezone; naive feed dates are unverified."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        parsed = parsed.astimezone(dt.timezone.utc)
        now = now or dt.datetime.now(dt.timezone.utc)
        if parsed > now + dt.timedelta(minutes=5):
            return None
        return parsed.isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def article_date(item):
    value = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date")
    return parse_publication_time(value)


def article_reading_minutes(item):
    """Read an explicit publisher/feed reading-time field; never estimate from article pages."""
    accepted = {"readingtime", "reading_time", "estimatedreadingtime", "readingminutes"}
    for element in item.iter():
        local_name = element.tag.rsplit("}", 1)[-1].lower()
        if local_name in accepted:
            match = re.search(r"\d+", clean_text(element.text or ""))
            if match:
                return max(1, min(180, int(match.group(0))))
    return None


def classify(title, summary, source):
    text = (title + " " + summary).lower()
    for category, words in get_editorial()["topic_rules"].items():
        def matches(word):
            suffix = "(?![a-z0-9])" if word.lower() in {"ai", "tech"} else r"[a-z0-9]*(?![a-z0-9])"
            return re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + suffix, text)
        if any(matches(w) for w in words):
            return category
    return "World"


def classify_region(title, summary, source_region=None):
    text = (title + " " + summary).lower()
    rules = get_editorial().get("region_rules", {})
    # India gets a dedicated shelf even when a story also mentions another region.
    order = ["India", "Asia", "Europe", "Africa", "Middle East", "Americas"]
    for region in order:
        words = rules.get(region, [])
        if any(re.search(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])", text) for w in words):
            return region
    # Use feed-section geography only when the source explicitly marks it as an item fallback.
    if source_region in order:
        return source_region
    return "Global"


def token_set(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOP}


IMPACT_TERMS = {"election", "government", "parliament", "court", "law", "policy", "war", "conflict", "attack", "ceasefire", "sanction", "crisis", "emergency", "disaster", "death", "killed", "injured", "rights", "protest", "vaccine", "outbreak", "climate", "collapse", "resign", "ban", "reform", "budget", "inflation", "rates", "jobs", "layoffs", "chip", "security", "breach", "minister", "president", "diplomat", "treaty", "border", "summit", "constitution", "vote", "legislation", "military", "invasion", "refugee", "humanitarian"}
WORLD_AFFAIRS_TERMS = {"election", "government", "parliament", "court", "law", "policy", "war", "conflict", "attack", "ceasefire", "sanction", "crisis", "emergency", "rights", "protest", "resign", "reform", "minister", "president", "diplomat", "treaty", "border", "summit", "constitution", "vote", "legislation", "military", "invasion", "refugee", "humanitarian"}


def matches_any(text, terms):
    return any(re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text.lower()) for term in terms)


def should_exclude(title, summary, excluded):
    """Apply configurable exclusions, with a narrow public-interest override."""
    config = get_editorial()
    text = f"{title} {summary}".lower()
    terms = config.get("excluded_terms", {})
    matched = any(matches_any(text, terms.get(category, [])) for category in excluded)
    return matched and not matches_any(text, config.get("public_interest_exceptions", []))


def cluster_similarity(title_a, title_b):
    """Compare informative title tokens rather than RSS summary boilerplate."""
    a, b = token_set(title_a), token_set(title_b)
    if len(a) < 2 or len(b) < 2:
        return 0.0
    shared = a & b
    return len(shared) / len(a | b) if len(shared) >= 2 else 0.0


def find_related_story(db, item, category, region):
    item_published = parse_publication_time(item.get("published"))
    item_time = dt.datetime.fromisoformat(item_published) if item_published else None
    if item_time is None:
        return None
    candidates = db.execute("SELECT id,title,published,category,region FROM stories ORDER BY COALESCE(published,created) DESC LIMIT 250").fetchall()
    best = None
    for old in candidates:
        if category != old["category"] and category not in {"World", "India"} and old["category"] not in {"World", "India"}:
            continue
        if region != old["region"] and "Global" not in {region, old["region"]}:
            continue
        if not old["published"]:
            continue
        if item_time:
            old_published = parse_publication_time(old["published"])
            try:
                old_time = dt.datetime.fromisoformat(old_published) if old_published else None
                if old_time is None:
                    continue
                if abs((item_time - old_time).total_seconds()) > 72 * 3600:
                    continue
            except (ValueError, TypeError):
                continue
        similarity = cluster_similarity(item["title"], old["title"])
        if similarity >= .48 and (best is None or similarity > best[0]):
            best = (similarity, old["id"])
    return best[1] if best else None


def independent_source_count(sources):
    """Count distinct publishers, collapsing repeated/syndicated-looking headlines."""
    representatives = []
    groups = set()
    seen_groups = set()
    for source in sources:
        group = source.get("publisher_group") or source.get("id") or source.get("name")
        title = source.get("title", "")
        if not group or group in seen_groups:
            continue
        seen_groups.add(group)
        if title and any(cluster_similarity(title, old_title) >= .78 for _, old_title in representatives if old_title):
            continue
        groups.add(group)
        representatives.append((group, title))
    return len(groups)


def story_age_hours(story, now):
    published = parse_publication_time(story.get("published"), now)
    if not published:
        return None
    timestamp = dt.datetime.fromisoformat(published)
    return max(0, (now - timestamp).total_seconds() / 3600)


def topic_interest_label(category):
    return {"Technology":"computer science", "Business":"business and finance", "India":"India", "Science":"science", "World":"world affairs"}.get(category, category.lower())


CONFLICT_MARKERS = (
    ("raise", "cut"), ("raises", "cuts"), ("raised", "cut"),
    ("increase", "decrease"), ("increases", "decreases"), ("increased", "decreased"),
    ("rise", "fall"), ("rises", "falls"), ("rose", "fell"),
    ("win", "lose"), ("approve", "reject"), ("accept", "reject"),
    ("confirm", "deny"), ("expand", "shrink"), ("open", "close"),
    ("include", "exclude"), ("support", "oppose"), ("gain", "lose"),
)


def story_context(story):
    """Build a cautious, attributed account using only stored feed metadata."""
    title = clean_text(story.get("title", ""))
    excerpt = clean_text(story.get("summary", ""))
    sources = story.get("sources", [])
    original = next((source for source in sources if source.get("url") == story.get("url")), sources[0] if sources else {})
    publisher = original.get("name") or "Publisher"
    headline = title.rstrip(" .!?;")
    excerpt_sentences = re.split(r"(?<=[.!?])\s+", excerpt.strip()) if excerpt else []
    short_excerpt = excerpt_sentences[0].strip() if excerpt_sentences else ""
    if len(short_excerpt) > 160:
        short_excerpt = short_excerpt[:157].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    quick_summary = short_excerpt if len(excerpt) >= 80 and short_excerpt else "The feed excerpt is too brief for a reliable short summary. Open the original publisher report."
    standard_excerpt = excerpt.strip()
    if len(standard_excerpt) > 260:
        standard_excerpt = standard_excerpt[:257].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    if excerpt:
        what_happened = f"The {publisher} feed headline says “{headline}.” The excerpt adds: “{standard_excerpt}”"
        if len(excerpt) < 80:
            what_happened += " The excerpt is brief and does not provide enough context for a reliable summary; open the publisher report."
    elif headline:
        what_happened = f"The {publisher} feed headline says “{headline}.” The feed does not provide enough information here for a reliable summary; open the publisher report for context."
    else:
        what_happened = "The feed does not provide enough information for a reliable summary. Open the publisher report for context."

    why = {
        "India": "This may be relevant to public affairs in India; this feed item alone does not establish its wider impact.",
        "Technology": "This may be relevant to computing or technology; broader effects are not established by this feed item.",
        "Business": "This may be relevant to business or finance; specific market or personal-finance effects are not established here.",
        "Science": "This may be relevant to scientific research; methods and significance are not established by the feed excerpt alone.",
        "Climate": "This may be relevant to climate or the environment; the feed item alone does not establish the scale or attribution of effects.",
        "Health": "This may be relevant to health; the feed item alone does not establish medical guidance or wider effects.",
        "Society": "This may be relevant to communities or public life; the feed item alone does not establish the wider impact.",
        "World": "This may be relevant to world affairs; the feed item alone does not establish the broader effects.",
    }.get(story.get("category"), "This may be relevant to the selected briefing; the feed item alone does not establish its wider significance.")

    unclear = []
    if re.search(r"^\s*(?:\[?breaking\]?|developing|live(?: updates)?|updated|update:|latest:)", title, re.I):
        unclear.append("The headline marks this as developing or updated; this feed item does not confirm whether later reports changed the details or outcome.")
    headline_sources = [source for source in sources if source.get("title") and source.get("publisher_group")]
    detected_conflict = None
    for left_index, left in enumerate(headline_sources):
        left_words = set(re.findall(r"[a-z]+", left["title"].lower()))
        for right in headline_sources[left_index + 1:]:
            if left.get("publisher_group") == right.get("publisher_group"):
                continue
            right_words = set(re.findall(r"[a-z]+", right["title"].lower()))
            for first, second in CONFLICT_MARKERS:
                if first in left_words and second in right_words or second in left_words and first in right_words:
                    detected_conflict = (left, right)
                    break
            if detected_conflict:
                break
        if detected_conflict:
            break
    if detected_conflict:
        left, right = detected_conflict
        unclear.append(f"Clustered feed headlines differ: {left.get('name') or 'One publisher'} says “{clean_text(left['title'])}”; {right.get('name') or 'another publisher'} says “{clean_text(right['title'])}”. The reports differ on the wording of this point; check the original reports for confirmation.")
    if len(excerpt) < 80:
        detail = "absent" if not excerpt else "brief"
        unclear.append(f"The feed excerpt is {detail}; background, supporting evidence, and fuller context are not available in this feed item.")

    primary_group = original.get("publisher_group")
    independent = []
    seen_groups = {primary_group} if primary_group else set()
    representative_titles = [original.get("title") or title]
    for source in sources:
        group = source.get("publisher_group") or source.get("id")
        if not group or group in seen_groups:
            continue
        seen_groups.add(group)
        source_title = source.get("title") or ""
        if source_title and any(cluster_similarity(source_title, representative) >= .78 for representative in representative_titles if representative):
            continue
        if source_title:
            representative_titles.append(source_title)
        independent.append({"name":source.get("name") or "Publisher", "url":source.get("url") or "", "publisher_group":group})
    return {
        "what_happened": what_happened,
        "what_happened_label": f"Morning Signal feed-based summary; generated framing from {publisher}'s RSS. Quoted headline and excerpt wording is publisher-provided, not the full article.",
        "quick_summary": quick_summary,
        "quick_summary_label": f"Very short summary from {publisher}'s RSS excerpt; not the full article.",
        "publisher_excerpt": excerpt,
        "publisher_excerpt_attribution": f"Publisher-provided RSS excerpt from {publisher}; this is not the full article.",
        "why_matter": why,
        "not_clear": unclear,
        "original_publisher": {"name":publisher, "url":story.get("url", "")},
        "other_publishers": independent,
    }


def story_context_for_mode(context, mode="Standard"):
    """Return only the feed context intended for the chosen reader display mode."""
    if mode not in READING_MODES:
        mode = "Standard"
    if mode == "Quick scan":
        return {"mode":mode, "summary":context["quick_summary"], "summary_label":context["quick_summary_label"],
            "why_matter":None, "not_clear":[], "publisher_excerpt":"", "publisher_excerpt_attribution":"",
            "original_publisher":context["original_publisher"], "other_publishers":[]}
    if mode == "More context":
        return {"mode":mode, "summary":context["what_happened"], "summary_label":context["what_happened_label"],
            "why_matter":context["why_matter"], "not_clear":context["not_clear"],
            "publisher_excerpt":context["publisher_excerpt"], "publisher_excerpt_attribution":context["publisher_excerpt_attribution"],
            "original_publisher":context["original_publisher"], "other_publishers":context["other_publishers"]}
    return {"mode":"Standard", "summary":context["what_happened"], "summary_label":context["what_happened_label"],
        "why_matter":context["why_matter"], "not_clear":[], "publisher_excerpt":"", "publisher_excerpt_attribution":"",
        "original_publisher":context["original_publisher"], "other_publishers":[]}


def rank_stories(stories, settings, now=None, feedback=None):
    """Score freshness, stated interests and coverage without rewarding feed volume."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    topics, regions = set(settings.get("topics", [])), set(settings.get("regions", []))
    freshness_hours = max(1, min(720, int(settings.get("freshness_hours", 48))))
    feedback = feedback or {"topics": {}, "regions": {}}
    ranked = []
    for story in stories:
        text = f"{story['title']} {story.get('summary', '')}"
        age_hours = story_age_hours(story, now)
        story["age_hours"] = age_hours
        story["date_status"] = "unverified" if age_hours is None else "stale" if age_hours > freshness_hours else "recent"
        recency = 0 if age_hours is None else max(0, 5 * (1 - age_hours / freshness_hours))
        impact_count = sum(1 for term in IMPACT_TERMS if matches_any(text, [term]))
        impact = min(1.5, impact_count * .45)
        source_count = independent_source_count(story.get("sources", []))
        corroboration = min(1.2, max(0, source_count - 1) * .6)
        category, region = story.get("category"), story.get("region")
        topic_match = category in topics and (category != "World" or matches_any(text, WORLD_AFFAIRS_TERMS))
        story["topic_match"] = topic_match
        topic_bonus = 1.6 if topic_match else 0
        region_bonus = 1.0 if region in regions else 0
        india_bonus = .65 if region == "India" else 0
        topic_feedback = max(-3, min(3, int(feedback.get("topics", {}).get(category, 0)))) * .65
        region_feedback = max(-3, min(3, int(feedback.get("regions", {}).get(region, 0)))) * .45
        direct_feedback = 1.2 if story.get("feedback") == "more" else -1.2 if story.get("feedback") == "less" else 0
        story["independent_sources"] = source_count
        story["qualifies"] = bool(topic_match or region in regions or region == "India" or impact_count or source_count > 1 or topic_feedback > 0 or region_feedback > 0 or story.get("feedback") == "more")
        # These are the actual score components. Keep the UI explanation and
        # numeric score derived from this same list so they cannot drift apart.
        feedback_score = topic_feedback + region_feedback + direct_feedback
        signals = []
        if recency > 0:
            age_label = "less than 1 hour ago" if age_hours < 1 else f"{int(age_hours)} hours ago" if age_hours < 24 else f"{int(age_hours // 24)} days ago"
            signals.append({"key":"freshness", "label":f"Published {age_label}", "score":round(recency, 2)})
        if impact:
            signals.append({"key":"impact", "label":f"Impact-word cues in headline or excerpt ({impact_count}; heuristic)", "score":round(impact, 2)})
        if topic_bonus:
            signals.append({"key":"topic", "label":f"Matches your {topic_interest_label(category)} topic", "score":round(topic_bonus, 2)})
        if region_bonus:
            signals.append({"key":"region", "label":f"In your preferred {region} region", "score":round(region_bonus, 2)})
        if india_bonus:
            signals.append({"key":"india", "label":"India coverage", "score":round(india_bonus, 2)})
        if corroboration:
            signals.append({"key":"corroboration", "label":f"Covered by {source_count} independent publishers", "score":round(corroboration, 2)})
        if feedback_score:
            direction = "raised" if feedback_score > 0 else "lowered"
            signals.append({"key":"feedback", "label":f"Your local feedback {direction} this match", "score":round(feedback_score, 2)})
        signals.sort(key=lambda signal: (-abs(signal["score"]), signal["key"]))
        story["rank_signals"] = signals
        story["why_this_is_here"] = [signal["label"] for signal in signals[:2]]
        story["rank_score"] = round(sum(signal["score"] for signal in signals), 2)
        story["rank_reason"] = " · ".join(story["why_this_is_here"]) or "No non-zero ranking signal; date is unverified"
        story["context"] = story_context(story)
        story["display_context"] = story_context_for_mode(story["context"], settings.get("difficulty", "Standard"))
        ranked.append(story)
    return sorted(ranked, key=lambda s: (s["rank_score"], s.get("published") or ""), reverse=True)


def safe_https_url(value):
    """Accept public-looking HTTPS publisher URLs; never allow script/local URLs."""
    if not isinstance(value, str) or len(value) > 2048 or "\\" in value or any(ord(c) < 32 or c.isspace() for c in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not host or parsed.username is not None or parsed.password is not None or port not in (None, 443) or "%" in parsed.netloc:
        return False
    host = host.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(host)
        return address.is_global
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        return bool(re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", ascii_host))


class SameHostHTTPSRedirectHandler(HTTPRedirectHandler):
    def __init__(self, original_url):
        super().__init__()
        self.original_host = urlsplit(original_url).hostname.lower().removeprefix("www.")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urlsplit(newurl).hostname
        if not safe_https_url(newurl) or not host or host.lower().removeprefix("www.") != self.original_host:
            raise ValueError("Feed redirect rejected; only same-publisher HTTPS redirects are allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_error(ex):
    """Store a useful error class without echoing publisher URLs or local details."""
    if isinstance(ex, urllib.error.HTTPError):
        return f"Publisher returned HTTP {ex.code}"
    if isinstance(ex, TimeoutError) or isinstance(ex, urllib.error.URLError) and isinstance(ex.reason, TimeoutError):
        return "Feed request timed out"
    if isinstance(ex, ValueError) and str(ex).startswith("Feed "):
        return str(ex)[:180]
    if isinstance(ex, ET.ParseError):
        return "Feed XML is malformed"
    return "Feed request failed; check the URL and try again"


def fetch_source(source, excluded, timeout=12):
    if not safe_https_url(source.get("url")):
        raise ValueError("Feed URL must be a public HTTPS URL without credentials")
    request = urllib.request.Request(source["url"], headers={"User-Agent": "MorningSignal/1.0 (personal RSS reader)"})
    opener = build_opener(SameHostHTTPSRedirectHandler(source["url"]))
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"Feed returned HTTP {response.status}")
        payload = response.read(MAX_FEED_BYTES + 1)
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("Feed response exceeded the 1 MB size limit")
    if re.search(br"<!\s*(DOCTYPE|ENTITY)\b", payload, re.I):
        raise ValueError("Feed XML declarations are not supported")
    root = ET.fromstring(payload)
    items = root.findall(".//item")
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall(".//a:entry", ns):
            link = e.find("a:link", ns)
            items.append(e)
        atom = True
    else:
        atom = False
    result = []
    for item in items[:35]:
        if atom:
            title = item.findtext("{http://www.w3.org/2005/Atom}title") or ""
            summary = item.findtext("{http://www.w3.org/2005/Atom}summary") or item.findtext("{http://www.w3.org/2005/Atom}content") or ""
            link_el = item.find("{http://www.w3.org/2005/Atom}link")
            url = link_el.attrib.get("href", "") if link_el is not None else ""
            pub = item.findtext("{http://www.w3.org/2005/Atom}published") or item.findtext("{http://www.w3.org/2005/Atom}updated")
            published = parse_publication_time(pub)
        else:
            title = item.findtext("title") or ""
            summary = item.findtext("description") or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
            url = item.findtext("link") or ""
            published = article_date(item)
        reading_minutes = article_reading_minutes(item)
        title, summary = clean_text(title), clean_text(summary)[:420]
        if not title or not safe_https_url(url):
            continue
        low = (title + " " + summary).lower()
        if should_exclude(title, summary, excluded):
            continue
        result.append({"title": title[:300], "summary": summary, "url": url, "published": published, "reading_minutes": reading_minutes})
    if not result:
        raise ValueError("Feed returned no usable stories")
    return result


def _refresh():
    sources = get_sources()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    stats = {"new": 0, "grouped": 0, "failed": 0, "checked": 0}
    deadline = time.monotonic() + 48 if os.environ.get("VERCEL") else None
    with connect() as db:
        selected = json.loads(db.execute("SELECT value FROM settings WHERE key='sources'").fetchone()[0])
        for source in sources:
            if source["id"] not in selected or not source.get("enabled") or source.get("verification") != "verified":
                continue
            stats["checked"] += 1
            if deadline is not None and deadline - time.monotonic() < .25:
                stats["failed"] += 1
                db.execute("INSERT OR REPLACE INTO source_status VALUES (?,?,?)", (source["id"], now, "Refresh time budget reached; try refreshing again"))
                continue
            try:
                excluded = [json.loads(r[0]) for r in db.execute("SELECT value FROM settings WHERE key='excluded'")]
                timeout = min(4, max(.25, deadline - time.monotonic())) if deadline is not None else 12
                items = fetch_source(source, excluded[0] if excluded else [], timeout)
                db.execute("INSERT OR REPLACE INTO source_status VALUES (?,?,?)", (source["id"], now, None))
                for item in items:
                    key = hashlib.sha1((source["id"] + item["url"]).encode()).hexdigest()
                    existing = db.execute("SELECT id,title FROM stories WHERE url=?", (item["url"],)).fetchone()
                    category = classify(item["title"], item["summary"], source)
                    region = classify_region(item["title"], item["summary"], source.get("region") if source.get("region_fallback") else None)
                    if existing:
                        sid = existing["id"]
                        db.execute("UPDATE stories SET title=?,summary=?,published=COALESCE(?,published),category=?,region=?,fetched_at=?,reading_minutes=COALESCE(?,reading_minutes) WHERE id=?",
                          (item["title"], item["summary"], item["published"], category, region, now, item.get("reading_minutes"), sid))
                    else:
                        candidate = find_related_story(db, item, category, region)
                        sid = candidate or key
                        if candidate:
                            db.execute("UPDATE stories SET fetched_at=? WHERE id=?", (now, sid))
                        if not candidate:
                            db.execute("INSERT OR IGNORE INTO stories(id,title,url,summary,published,category,region,created,fetched_at,reading_minutes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (sid, item["title"], item["url"], item["summary"], item["published"], category, region, now, now, item.get("reading_minutes")))
                            stats["new"] += 1
                    source_link_exists = db.execute("SELECT 1 FROM story_sources WHERE story_id=? AND source_id=?", (sid, source["id"])).fetchone()
                    db.execute("INSERT OR REPLACE INTO story_sources(story_id,source_id,source_name,url,last_seen,publisher_group,title) VALUES (?,?,?,?,?,?,?)", (sid, source["id"], source["name"], item["url"], now, source.get("publisher_group", source["id"]), item["title"]))
                    if not existing and candidate and not source_link_exists:
                        stats["grouped"] += 1
            except Exception as ex:
                stats["failed"] += 1
                db.execute("INSERT OR REPLACE INTO source_status VALUES (?,?,?)", (source["id"], now, safe_error(ex)))
    return stats


def refresh():
    if not _refresh_lock.acquire(blocking=False):
        raise RuntimeError("A feed refresh is already running")
    try:
        return _refresh()
    finally:
        _refresh_lock.release()


def read_settings(db):
    return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM settings")}


def save_daily_goal(db, value):
    try:
        goal = int(value)
    except (TypeError, ValueError) as ex:
        raise ValueError("Daily goal must be between 1 and 10 articles") from ex
    if not 1 <= goal <= 10:
        raise ValueError("Daily goal must be between 1 and 10 articles")
    db.execute("INSERT OR REPLACE INTO settings VALUES ('daily_goal',?)", (json.dumps(goal),))
    return goal


def update_story_record(db, story_id, changes, now=None):
    current = db.execute("SELECT read FROM stories WHERE id=?", (story_id,)).fetchone()
    if not current:
        raise LookupError("Story not found")
    updates = {key: int(bool(value)) for key, value in changes.items() if key in ("read", "saved", "hidden")}
    if "note" in changes:
        note = changes["note"]
        if not isinstance(note, str) or len(note) > 280:
            raise ValueError("Notes must be 280 characters or fewer")
        if not updates.get("read", current["read"]):
            raise ValueError("Mark the article read before adding a note")
        updates["note"] = note.strip()
    if "feedback" in changes:
        feedback = changes["feedback"]
        if not isinstance(feedback, str) or feedback not in {"more", "less", "clear"}:
            raise ValueError("Feedback must be more, less, or clear")
        updates["feedback"] = None if feedback == "clear" else feedback
    if not updates:
        raise ValueError("No valid action")
    if "read" in updates:
        now = now or dt.datetime.now().astimezone()
        updates["read_at"] = now.astimezone(dt.timezone.utc).isoformat() if updates["read"] else None
    db.execute("UPDATE stories SET " + ",".join(f"{key}=?" for key in updates) + " WHERE id=?", (*updates.values(), story_id))


def log_deep_read(db, story_id, theme, completed_at=None):
    completed_at = completed_at or dt.datetime.now().astimezone()
    row = db.execute("SELECT 1 FROM stories WHERE id=?", (story_id,)).fetchone()
    if not row:
        raise LookupError("Story not found")
    stamp = completed_at.astimezone(dt.timezone.utc).isoformat()
    db.execute("UPDATE stories SET read=1,read_at=? WHERE id=?", (stamp, story_id))
    db.execute("INSERT OR IGNORE INTO reading_history(story_id,completed_on,theme,completed_at) VALUES (?,?,?,?)",
        (story_id, completed_at.date().isoformat(), theme, stamp))


def weekly_reading_history(db, today=None):
    today = today or dt.datetime.now().astimezone().date()
    start = (today - dt.timedelta(days=6)).isoformat()
    result = [dict(row) for row in db.execute("""SELECT h.completed_on date,h.theme,s.id story_id,s.title,s.url
        FROM reading_history h JOIN stories s ON s.id=h.story_id
        WHERE h.completed_on>=? AND h.completed_on<=? ORDER BY h.completed_on DESC,h.completed_at DESC""", (start, today.isoformat()))]
    return [item for item in result if safe_https_url(item["url"])]


def story_payload(row, db):
    item = dict(row)
    item["published"] = parse_publication_time(item.get("published"))
    if not safe_https_url(item.get("url")):
        item["url"] = ""
    item["sources"] = [dict(x) for x in db.execute("SELECT source_id id,source_name name,url,last_seen,publisher_group,title FROM story_sources WHERE story_id=?", (item["id"],))]
    for source in item["sources"]:
        if not safe_https_url(source.get("url")):
            source["url"] = ""
    return item


def hidden_story_payloads(db):
    """Return locally hidden stories for the separate restore view only."""
    rows = db.execute("SELECT * FROM stories WHERE hidden=1 ORDER BY COALESCE(published,created) DESC LIMIT 500").fetchall()
    return [story_payload(row, db) for row in rows if safe_https_url(row["url"])]


def focus_match(story, weekday):
    category = story["category"].lower()
    title = story["title"].lower()
    title_summary = (title + " " + story.get("summary", "")).lower()
    if weekday == 0:
        return category == "world" and any(re.search(r"\b" + term, title_summary) for term in ("politic", "election", "government", "diplomat", "geopolit", "conflict", "war", "treaty", "parliament", "protest"))
    if weekday == 1:
        return category == "technology"
    if weekday == 2:
        return category == "business"
    if weekday == 3:
        return category in {"science", "climate", "health"} and len(story.get("summary", "")) >= 100
    if weekday == 4:
        return story["region"] == "India" or category == "society"
    if weekday == 5:
        return len(story.get("summary", "")) >= 100 and (re.match(r"\s*(how|why|what|explained|a history of|the history of)\b", title) is not None)
    return any(term in title for term in ("week in review", "weekly review", "weekend read", "week's review", "weekend briefing", "what happened this week", "week in pictures"))


def balanced_briefing(stories, limit, preferred_regions):
    """Add modest regional variety only among stories near the editorial top tier."""
    limit = min(limit, len(stories))
    if not stories:
        return []
    top_score = stories[0].get("rank_score", 0)
    # A region slot is considered only when its story is still close to the top scores.
    quality_pool = [s for s in stories if s.get("rank_score", 0) >= top_score - 2.5]
    pool_ids = {s["id"] for s in quality_pool if "id" in s}
    regional = {}
    for story in quality_pool:
        regional.setdefault(story["region"], []).append(story)

    selected = []
    india_target = min(len(regional.get("India", [])), max(1, round(limit * .3)))
    if regional.get("India") and regional["India"][0].get("rank_score", 0) >= top_score - 1.75:
        selected.extend(regional["India"][:india_target])
    order = [r for r in preferred_regions if r in regional and r != "India"]
    order += [r for r in regional if r != "India" and r not in order]
    cursors = {r: 0 for r in regional}
    cursors["India"] = len(selected)
    while len(selected) < min(limit, len(quality_pool)):
        progressed = False
        for region in order:
            if cursors.get(region, 0) < len(regional[region]):
                selected.append(regional[region][cursors[region]])
                cursors[region] += 1
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break
    return selected[:limit]


def relevance_feedback(db):
    profile = {"topics": {}, "regions": {}}
    for row in db.execute("SELECT category,region,feedback FROM stories WHERE feedback IN ('more','less')"):
        delta = 1 if row["feedback"] == "more" else -1
        profile["topics"][row["category"]] = profile["topics"].get(row["category"], 0) + delta
        profile["regions"][row["region"]] = profile["regions"].get(row["region"], 0) + delta
    return profile


def select_daily_briefing(stories, settings, weekday, now=None, feedback=None):
    """Return only recent, relevant briefing items; retain undated relevant items separately."""
    ranked = rank_stories(stories, settings, now, feedback)
    window_hours = max(1, min(720, int(settings.get("freshness_hours", 48))))
    recent = [story for story in ranked if story["date_status"] == "recent" and story["qualifies"]]
    undated = [story for story in ranked if story["date_status"] == "unverified" and story["qualifies"]]
    requested = max(1, min(25, int(settings.get("length", 10))))
    selected = balanced_briefing(recent, requested, settings.get("regions", []))
    deep = next((story for story in recent if focus_match(story, weekday)), None)
    stale_count = sum(1 for story in ranked if story["date_status"] == "stale")
    empty_reason = None
    next_steps = []
    if not selected:
        if not ranked:
            empty_reason = "No stories are stored yet."
            next_steps = ["Refresh enabled sources", "Check source status for feed errors"]
        else:
            empty_reason = "No dated, recent stories matched the current relevance rules."
            if stale_count:
                next_steps.append(f"Review the freshness window ({window_hours} hours) or refresh sources")
            if undated:
                next_steps.append("Date-unverified items are listed separately and do not replace dated stories")
            next_steps.extend(["Check source status", "Adjust preferred topics or regions in settings"])
    return {"stories": selected, "unverified": undated[:3], "deep": deep,
        "fresh_count": len(recent), "undated_count": len(undated),
        "stale_count": stale_count, "available_count": len(ranked), "requested": requested,
        "empty_reason": empty_reason, "empty_next_steps": next_steps}


def briefing_coverage(stories, sources, statuses, today=None):
    """Describe selected stories linked to feeds that succeeded and saw them today."""
    today = today or dt.datetime.now().astimezone().date()
    statuses = statuses or {}
    checked_today, successful_today, failed_today = set(), set(), set()
    for source in sources:
        source_id = source["id"]
        status = statuses.get(source_id) or {}
        checked = status.get("checked")
        if not checked:
            continue
        try:
            stamp = dt.datetime.fromisoformat(checked)
            if stamp.tzinfo is None or stamp.astimezone().date() != today:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        checked_today.add(source_id)
        (failed_today if status.get("error") else successful_today).add(source_id)

    source_map = {source["id"]: source for source in sources}
    regions, publisher_names, contributing_story_ids = set(), {}, set()
    for story in stories:
        contributing = []
        for association in story.get("sources", []):
            source_id = association.get("id")
            source = source_map.get(source_id)
            if not source or source_id not in successful_today or not association.get("last_seen"):
                continue
            try:
                seen = dt.datetime.fromisoformat(association["last_seen"])
            except (TypeError, ValueError, OverflowError):
                continue
            if seen.tzinfo is None or seen.astimezone().date() != today:
                continue
            contributing.append((association, source))
        if not contributing:
            continue
        contributing_story_ids.add(story.get("id"))
        regions.add(story.get("region") or "Global")
        for association, source in contributing:
            group = association.get("publisher_group") or source.get("publisher_group") or association.get("id")
            publisher_names[group] = source.get("publisher") or source["name"]

    region_order = ["India", "Asia", "Europe", "Africa", "Middle East", "Americas", "Global"]
    return {
        "checked_today": len(checked_today),
        "failed_today": len(failed_today),
        "story_count": len(contributing_story_ids),
        "regions": [region for region in region_order if region in regions],
        "publishers": [{"group": group, "name": publisher_names[group]} for group in sorted(publisher_names)],
    }


def api_data():
    with connect() as db:
        settings = read_settings(db)
        sources = get_sources()
        enabled = settings.get("sources", [s["id"] for s in sources if s.get("enabled")])
        rows = db.execute("SELECT * FROM stories WHERE hidden=0 ORDER BY COALESCE(published,created) DESC LIMIT 500").fetchall()
        stories = []
        for row in rows:
            if not safe_https_url(row["url"]):
                continue
            item = story_payload(row, db)
            item["sources"] = [source for source in item["sources"] if source["id"] in enabled]
            if item["sources"]:
                stories.append(item)
        hidden_stories = hidden_story_payloads(db)
        weekday = dt.date.today().weekday()
        today_focus = FOCUS[weekday]
        selection = select_daily_briefing(stories, settings, weekday, feedback=relevance_feedback(db))
        stories = selection["stories"]
        deep = selection["deep"]
        local_today = dt.datetime.now().astimezone().date()
        day_start = dt.datetime.combine(local_today, dt.time.min).astimezone(dt.timezone.utc).isoformat()
        day_end = dt.datetime.combine(local_today + dt.timedelta(days=1), dt.time.min).astimezone(dt.timezone.utc).isoformat()
        read_today = db.execute("SELECT COUNT(*) FROM stories WHERE read=1 AND read_at>=? AND read_at<?", (day_start, day_end)).fetchone()[0]
        history = weekly_reading_history(db, local_today)
        completed_today = bool(deep and db.execute("SELECT 1 FROM reading_history WHERE story_id=? AND completed_on=?", (deep["id"], local_today.isoformat())).fetchone())
        saved_rows = db.execute("SELECT * FROM stories WHERE saved=1 OR (note IS NOT NULL AND note!='') ORDER BY COALESCE(published,created) DESC").fetchall()
        saved_stories = [story_payload(row, db) for row in saved_rows if safe_https_url(row["url"])]
        safe_sources = public_sources(sources)
        source_statuses, source_rows = {}, []
        for source in safe_sources:
            row = db.execute("SELECT checked,error FROM source_status WHERE source_id=?", (source["id"],)).fetchone()
            status = dict(row) if row else None
            source_statuses[source["id"]] = status
            source_rows.append({**source, "status": status})
        coverage = briefing_coverage(stories, sources, source_statuses, local_today)
        selection_summary = {key: selection[key] for key in ("fresh_count", "undated_count", "stale_count", "available_count", "requested", "empty_reason", "empty_next_steps")}
        selection_summary["freshness_hours"] = max(1, min(720, int(settings.get("freshness_hours", 48))))
        return {"stories": stories, "hiddenStories": hidden_stories, "unverifiedStories": selection["unverified"], "selection": selection_summary, "deepRead": deep, "focus": today_focus, "settings": settings,
          "reading": {"goal": max(1, min(10, int(settings.get("daily_goal", 1)))), "read_today": read_today,
            "deep_read_completed": completed_today, "history": history, "saved": saved_stories},
          "coverage": coverage, "sources": source_rows}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    server_version = "MorningSignal"
    sys_version = ""
    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()
    def _local_request(self):
        host = (self.headers.get("Host") or "").lower()
        if host not in {"127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:8765"}:
            self.send_json({"error":"This service accepts requests only through its local address"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.netloc.lower() != host:
                self.send_json({"error":"Cross-origin requests are not allowed"}, 403)
                return False
        if self.headers.get("Sec-Fetch-Site", "").lower() in {"cross-site", "same-site"}:
            self.send_json({"error":"Cross-site requests are not allowed"}, 403)
            return False
        return True
    def send_json(self, value, status=200):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if not self._local_request(): return
        path = urlparse(self.path).path
        if path == "/api/session":
            return self.send_json({"private": False})
        if path == "/api/briefing":
            return self.send_json(api_data())
        if path == "/api/config":
            sources = public_sources(get_sources())
            return self.send_json({"sources": sources, "categories": CATEGORIES, "focus": FOCUS[dt.date.today().weekday()]})
        name = STATIC_FILES.get(path)
        file = ROOT / name if name else None
        if not file or not file.is_file():
            self.send_error(404); return
        data = file.read_bytes(); self.send_response(200); self.send_header("Content-Type", {".html":"text/html; charset=utf-8", ".css":"text/css; charset=utf-8", ".js":"text/javascript; charset=utf-8"}[file.suffix]); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        if not self._local_request(): return
        if self.headers.get_content_type() != "application/json": return self.send_json({"error":"Content-Type must be application/json"}, 415)
        try:
            length_text = self.headers.get("Content-Length", "")
            if not length_text.isdigit(): raise ValueError()
            length = int(length_text)
            if length > MAX_POST_BYTES: return self.send_json({"error":"Request body is too large"}, 413)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict): raise ValueError()
        except Exception: return self.send_json({"error":"Expected a JSON object with valid input"}, 400)
        path = urlparse(self.path).path
        if path == "/api/refresh":
            if data: return self.send_json({"error":"Unexpected refresh input"}, 400)
            try: return self.send_json(refresh())
            except RuntimeError as ex: return self.send_json({"error":str(ex)}, 409)
        if path == "/api/settings":
            allowed = {"regions", "topics", "sources", "excluded", "length", "difficulty", "daily_goal", "freshness_hours"}
            if set(data) - allowed: return self.send_json({"error":"Unexpected settings field"}, 400)
            try:
                source_ids = {s["id"] for s in get_sources() if s.get("enabled") and s.get("verification") == "verified"}
                region_ids = {"India", "Asia", "Europe", "Africa", "Americas", "Middle East", "Global"}
                topic_ids = set(CATEGORIES)
                excluded_ids = set(get_editorial().get("excluded_terms", {}))
                for key, valid in (("regions", region_ids), ("topics", topic_ids), ("sources", source_ids), ("excluded", excluded_ids)):
                    if key in data and (not isinstance(data[key], list) or len(data[key]) > len(valid) or any(not isinstance(v, str) or v not in valid for v in data[key])):
                        raise ValueError(f"Invalid {key} preference")
                if "length" in data and (type(data["length"]) is not int or not 1 <= data["length"] <= 25): raise ValueError("Briefing length must be 1 to 25")
                if "freshness_hours" in data and (type(data["freshness_hours"]) is not int or data["freshness_hours"] not in {24, 48, 72, 168}): raise ValueError("Freshness window must be 24, 48, 72, or 168 hours")
                if "difficulty" in data and (not isinstance(data["difficulty"], str) or data["difficulty"] not in READING_MODES): raise ValueError("Invalid reading context mode")
            except ValueError as ex: return self.send_json({"error":str(ex)}, 400)
            goal = None
            if "daily_goal" in data:
                try:
                    if type(data["daily_goal"]) is not int: raise ValueError()
                    goal = int(data["daily_goal"])
                except (TypeError, ValueError): return self.send_json({"error":"Daily goal must be between 1 and 10 articles"}, 400)
                if not 1 <= goal <= 10: return self.send_json({"error":"Daily goal must be between 1 and 10 articles"}, 400)
            with connect() as db:
                for key, value in data.items():
                    if key in allowed:
                        if key == "daily_goal":
                            value = goal
                        db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
            return self.send_json({"ok": True})
        if path == "/api/reading/complete":
            if set(data) != {"story_id"}: return self.send_json({"error":"Expected only story_id"}, 400)
            story_id = data.get("story_id", "")
            briefing = api_data()
            deep = briefing.get("deepRead")
            if not re.fullmatch(r"[a-f0-9]{40}", str(story_id)) or not deep or deep["id"] != story_id:
                return self.send_json({"error":"Only today's featured deep read can be completed here"}, 400)
            with connect() as db:
                log_deep_read(db, story_id, briefing["focus"])
            return self.send_json({"ok":True})
        if path.startswith("/api/story/"):
            if path.count("/") != 3: return self.send_json({"error":"Invalid story route"}, 400)
            sid = path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[a-f0-9]{40}", sid): return self.send_json({"error":"Invalid story"}, 400)
            if not data or set(data) - {"read", "saved", "hidden", "note", "feedback"}: return self.send_json({"error":"Invalid story update fields"}, 400)
            if any(key in data and type(data[key]) is not bool for key in ("read", "saved", "hidden")): return self.send_json({"error":"Story actions must be boolean"}, 400)
            with connect() as db:
                try: update_story_record(db, sid, data)
                except LookupError as ex: return self.send_json({"error":str(ex)}, 404)
                except ValueError as ex: return self.send_json({"error":str(ex)}, 400)
            return self.send_json({"ok": True})
        return self.send_json({"error":"Not found"}, 404)


if __name__ == "__main__":
    init_db()
    try:
        app_server = LocalHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as ex:
        if ex.errno == errno.EADDRINUSE:
            raise SystemExit(f"Port {PORT} is already in use. Stop the existing Morning Signal server before starting another.") from None
        raise
    print(f"Morning Signal is running at http://127.0.0.1:{PORT}")
    app_server.serve_forever()
