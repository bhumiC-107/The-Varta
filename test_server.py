"""Focused regression checks; fixture headlines are test inputs, not displayed news."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import offline_evaluation


class FakeResponse:
    status = 200
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self, _limit): return self.payload


class FakeOpener:
    def __init__(self, response): self.response = response
    def open(self, *_args, **_kwargs): return self.response


class EditorialTests(unittest.TestCase):
    def setUp(self):
        self.source = {"id":"test", "name":"Test publisher", "region":"Global"}

    def test_topic_rules_use_word_boundaries(self):
        self.assertEqual(server.classify("Macron speaks to protestors", "The French government addresses unrest", self.source), "World")
        self.assertEqual(server.classify("Some diplomats leave Saudi Arabia amid attacks", "", self.source), "World")
        self.assertEqual(server.classify("AI chip maker reports quarterly revenue", "", self.source), "Technology")
        self.assertEqual(server.classify("Coral reefs face collapse", "The ecosystem study found severe climate impacts", self.source), "Climate")
        self.assertEqual(server.classify("Indian inflation eases", "", self.source), "Business")

    def test_regions_are_inferred_separately_from_topics(self):
        self.assertEqual(server.classify_region("Indian researchers study new climate pattern", ""), "India")
        self.assertEqual(server.classify_region("French government weighs a new budget", ""), "Europe")
        self.assertEqual(server.classify_region("Researchers publish a new study", ""), "Global")
        self.assertEqual(server.classify_region("A regional policy update", "", "Africa"), "Africa")
        self.assertEqual(server.classify_region("Indian researchers publish a policy update", "", "Africa"), "India")

    def test_new_regional_feeds_share_publisher_identity_and_are_documented(self):
        feeds = {item["id"]:item for item in server.get_sources()}
        self.assertEqual(feeds["cna-asia"]["publisher_group"], "channel-news-asia")
        self.assertEqual(server.independent_source_count([
            {"id":"cna-asia", "publisher_group":feeds["cna-asia"]["publisher_group"], "title":"Asia policy update"},
            {"id":"cna-world", "publisher_group":feeds["cna-asia"]["publisher_group"], "title":"World policy update"},
            {"id":"lemonde-europe", "publisher_group":feeds["lemonde-europe"]["publisher_group"], "title":"Europe policy update"},
        ]), 2)
        for feed_id in ("cna-asia", "lemonde-europe", "lemonde-africa", "lemonde-americas"):
            self.assertEqual(feeds[feed_id]["verification"], "verified")
            self.assertTrue(feeds[feed_id]["url"].startswith("https://"))
            self.assertTrue(feeds[feed_id]["terms"])
        self.assertFalse(feeds["africanews-latest"]["enabled"])
        self.assertEqual(feeds["africanews-latest"]["verification"], "unverified")

    def test_coverage_counts_unique_publishers_and_only_selected_story_regions(self):
        today = server.dt.datetime.now().astimezone().date()
        stamp = server.dt.datetime.now(server.dt.timezone.utc).isoformat()
        sources = [
            {"id":"cna-asia", "name":"CNA · Asia", "publisher":"CNA", "publisher_group":"cna"},
            {"id":"cna-business", "name":"CNA · Business", "publisher":"CNA", "publisher_group":"cna"},
        ]
        stories = [
            {"id":"one", "region":"Asia", "sources":[{"id":"cna-asia", "publisher_group":"cna", "last_seen":stamp}]},
            {"id":"two", "region":"India", "sources":[{"id":"cna-business", "publisher_group":"cna", "last_seen":stamp}]},
        ]
        statuses = {item["id"]:{"checked":stamp,"error":None} for item in sources}
        coverage = server.briefing_coverage(stories, sources, statuses, today)
        self.assertEqual(coverage["regions"], ["India", "Asia"])
        self.assertEqual(coverage["publishers"], [{"group":"cna", "name":"CNA"}])
        self.assertEqual(coverage["story_count"], 2)

    def test_relevance_prefers_recent_impact_over_volume_or_preference(self):
        now = server.dt.datetime(2026, 10, 22, 12, tzinfo=server.dt.timezone.utc)
        stories = [
            {"id":"impact", "title":"Developing: Parliament declares emergency after deadly flood", "summary":"Government response", "published":now.isoformat(), "category":"World", "region":"Asia", "sources":[{"name":"A"},{"name":"B"}]},
            {"id":"volume", "title":"A routine product update", "summary":"Company says product is new", "published":(now-server.dt.timedelta(days=5)).isoformat(), "category":"Technology", "region":"Americas", "sources":[{"name":"A"}]},
        ]
        ranked = server.rank_stories(stories, {"topics":["Technology"], "regions":["Americas"]}, now)
        self.assertEqual(ranked[0]["id"], "impact")
        self.assertIn("corroboration", {signal["key"] for signal in ranked[0]["rank_signals"]})
        self.assertEqual(ranked[0]["rank_reason"]," · ".join(ranked[0]["why_this_is_here"]))
        self.assertIn("computer science topic", ranked[1]["rank_reason"])

    def test_clustering_matches_related_titles_but_not_shared_generic_event_words(self):
        self.assertGreaterEqual(server.cluster_similarity(
            "Saudi diplomats ordered to leave amid Houthi attacks",
            "Some US diplomats told to leave Saudi Arabia amid Houthi attacks",
        ), .48)
        self.assertLess(server.cluster_similarity(
            "Saudi diplomats ordered to leave amid Houthi attacks",
            "Diplomats discuss climate policy during Saudi summit",
        ), .48)

    def test_related_story_match_requires_close_date_and_compatible_region(self):
        with tempfile.TemporaryDirectory(dir=server.ROOT) as directory:
            db_path = Path(directory) / "cluster.sqlite3"
            with patch.object(server, "DB", db_path):
                server.init_db()
                published = "2026-10-22T10:00:00+00:00"
                with server.connect() as db:
                    db.execute("INSERT INTO stories(id,title,url,summary,published,category,region,created) VALUES (?,?,?,?,?,?,?,?)", (
                        "existing", "Saudi diplomats ordered to leave amid Houthi attacks", "https://one.test/a", "", published, "World", "Middle East", published))
                    related = {"title":"Some US diplomats told to leave Saudi Arabia amid Houthi attacks", "published":"2026-10-22T11:00:00+00:00"}
                    old = {"title":related["title"], "published":"2026-10-27T10:00:00+00:00"}
                    wrong_region = {"title":related["title"], "published":"2026-10-22T11:00:00+00:00"}
                    self.assertEqual(server.find_related_story(db, related, "World", "Middle East"), "existing")
                    self.assertIsNone(server.find_related_story(db, old, "World", "Middle East"))
                    self.assertIsNone(server.find_related_story(db, wrong_region, "World", "Europe"))

    def test_exclusions_have_narrow_public_interest_exception(self):
        defaults = ["Celebrity", "Influencer", "Entertainment gossip", "K-pop"]
        self.assertTrue(server.should_exclude("Actor shares celebrity gossip", "", defaults))
        self.assertTrue(server.should_exclude("K-pop idol group announces tour", "", defaults))
        self.assertFalse(server.should_exclude("Actor leads humanitarian relief effort", "", defaults))
        self.assertFalse(server.should_exclude("Celebrity elected to parliament", "", defaults))

    def test_weekday_focus_rules(self):
        examples = [
            (0, {"category":"World", "region":"Europe", "title":"Election talks begin", "summary":""}),
            (1, {"category":"Technology", "region":"Global", "title":"New computer chip", "summary":""}),
            (2, {"category":"Business", "region":"Global", "title":"Markets open higher", "summary":""}),
            (3, {"category":"Science", "region":"Global", "title":"Study finds new species", "summary":"The research team shares evidence about this discovery and explains what the new species could mean for the local ecosystem and future study."}),
            (4, {"category":"World", "region":"India", "title":"Monsoon update", "summary":""}),
            (5, {"category":"World", "region":"Global", "title":"How the old trade route shaped cities", "summary":"This explainer follows the historical trade routes and examines how they changed cities over many generations, drawing on evidence from historians and archival records."}),
            (6, {"category":"World", "region":"Global", "title":"The week in review", "summary":""}),
        ]
        for weekday, story in examples:
            with self.subTest(weekday=weekday):
                self.assertTrue(server.focus_match(story, weekday))
        self.assertFalse(server.focus_match({"category":"World", "region":"Global", "title":"A routine announcement", "summary":""}, 0))
        self.assertFalse(server.focus_match({"category":"World", "region":"Global", "title":"A routine announcement", "summary":"This long report explains how markets could change over the coming week, with extensive background and history in the article body."}, 5))

    def test_balancing_retains_india_and_cycles_regions(self):
        stories = []
        for region, count in (("India", 8), ("Europe", 8), ("Asia", 8), ("Americas", 8), ("Africa", 8)):
            stories.extend({"id":f"{region}-{i}", "region":region, "title":f"{region} report {i}", "rank_score":10-i/10} for i in range(count))
        selected = server.balanced_briefing(stories, 10, ["India", "Asia", "Europe", "Africa", "Americas"])
        regions = [s["region"] for s in selected]
        self.assertEqual(len(regions), 10)
        self.assertGreaterEqual(regions.count("India"), 3)
        self.assertLessEqual(max(regions.count(r) for r in set(regions)), 3)

    def test_geography_does_not_force_low_scoring_region_story(self):
        stories = [{"id":f"eu-{i}","region":"Europe","rank_score":10-i*.1} for i in range(8)]
        stories += [{"id":"low-india","region":"India","rank_score":1}]
        result = server.balanced_briefing(stories, 5, ["India", "Europe"])
        self.assertNotIn("low-india", [s["id"] for s in result])


class ReadingProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=server.ROOT)
        self.db_path = Path(self.temp.name) / "reading.sqlite3"
        self.db_patch = patch.object(server, "DB", self.db_path)
        self.db_patch.start()
        server.init_db()
        with server.connect() as db:
            db.execute("INSERT INTO stories(id,title,url,summary,category,region,created) VALUES (?,?,?,?,?,?,?)",
                ("a" * 40, "Fixture deep read", "https://publisher.test/story", "fixture excerpt", "Technology", "Global", "2026-10-22T10:00:00+00:00"))

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_daily_goal_defaults_to_one_and_change_is_persisted(self):
        with server.connect() as db:
            settings=server.read_settings(db)
            self.assertEqual(settings["daily_goal"],1)
            self.assertEqual(settings["freshness_hours"],48)
            self.assertEqual(server.save_daily_goal(db,3),3)
        with server.connect() as db:
            self.assertEqual(server.read_settings(db)["daily_goal"],3)

    def test_reading_context_mode_preference_persists_locally(self):
        with server.connect() as db:
            for mode in ("Quick scan", "Standard", "More context"):
                db.execute("INSERT OR REPLACE INTO settings VALUES ('difficulty',?)", (server.json.dumps(mode),))
                self.assertEqual(server.read_settings(db)["difficulty"],mode)
        with server.connect() as db:
            self.assertEqual(server.read_settings(db)["difficulty"],"More context")

    def test_legacy_reading_preferences_migrate_to_supported_context_modes(self):
        with server.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('difficulty',?)", (server.json.dumps("Detailed"),))
        server.init_db()
        with server.connect() as db:
            self.assertEqual(server.read_settings(db)["difficulty"],"More context")
            db.execute("INSERT OR REPLACE INTO settings VALUES ('difficulty',?)", (server.json.dumps("Plain language"),))
        server.init_db()
        with server.connect() as db:
            self.assertEqual(server.read_settings(db)["difficulty"],"Standard")
            with self.assertRaises(ValueError): server.save_daily_goal(db,0)

    def test_saved_read_state_and_personal_note_persist(self):
        now=server.dt.datetime(2026,10,22,8,tzinfo=server.dt.timezone.utc)
        with server.connect() as db:
            server.update_story_record(db,"a"*40,{"saved":True,"read":True,"note":"  Remember this point.  "},now)
        with server.connect() as db:
            row=db.execute("SELECT saved,read,read_at,note FROM stories WHERE id=?",("a"*40,)).fetchone()
            self.assertEqual((row["saved"],row["read"],row["note"]),(1,1,"Remember this point."))
            self.assertEqual(row["read_at"],"2026-10-22T08:00:00+00:00")
            server.update_story_record(db,"a"*40,{"saved":False})
            server.update_story_record(db,"a"*40,{"read":False})
            with self.assertRaises(ValueError): server.update_story_record(db,"a"*40,{"note":"too soon"})
            with self.assertRaises(ValueError): server.update_story_record(db,"a"*40,{"note":"x"*281})
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT saved,note FROM stories WHERE id=?",("a"*40,)).fetchone()["saved"],0)
            self.assertEqual(db.execute("SELECT note FROM stories WHERE id=?",("a"*40,)).fetchone()["note"],"Remember this point.")

    def test_hidden_state_is_separate_from_read_and_persists(self):
        with server.connect() as db:
            server.update_story_record(db,"a"*40,{"hidden":True})
            row=db.execute("SELECT hidden,read FROM stories WHERE id=?",("a"*40,)).fetchone()
            self.assertEqual((row["hidden"],row["read"]),(1,0))
            server.update_story_record(db,"a"*40,{"read":True})
        with server.connect() as db:
            row=db.execute("SELECT hidden,read FROM stories WHERE id=?",("a"*40,)).fetchone()
            self.assertEqual((row["hidden"],row["read"]),(1,1))
            self.assertEqual([story["id"] for story in server.hidden_story_payloads(db)],["a"*40])

    def test_hidden_listing_includes_publisher_date_and_original_link(self):
        published="2026-10-22T10:00:00+00:00"
        with server.connect() as db:
            db.execute("UPDATE stories SET title=?,url=?,published=?,hidden=1 WHERE id=?",("Fixture India policy story","https://publisher.test/india-story",published,"a"*40))
            db.execute("INSERT INTO story_sources(story_id,source_id,source_name,url,publisher_group,title) VALUES (?,?,?,?,?,?)",
                ("a"*40,"fixture-source","Fixture Publisher","https://publisher.test/india-story","fixture-publisher","Fixture India policy story"))
        with server.connect() as db:
            item=server.hidden_story_payloads(db)[0]
            self.assertEqual((item["title"],item["published"],item["url"]),("Fixture India policy story",published,"https://publisher.test/india-story"))
            self.assertEqual(item["sources"][0]["name"],"Fixture Publisher")

    def test_restore_clears_hidden_flag_and_selector_rechecks_story(self):
        now=server.dt.datetime.now(server.dt.timezone.utc)
        published=now.isoformat()
        story_id="f"*40
        with server.connect() as db:
            db.execute("""INSERT INTO stories(id,title,url,summary,published,category,region,created,fetched_at,hidden)
                VALUES (?,?,?,?,?,?,?,?,?,1)""",(story_id,"Indian parliament passes major security law","https://publisher.test/india-law",
                "The parliament passed a major security law after debate, with officials describing its scope and the vote's next steps.",published,"India","India",published,published))
            db.execute("INSERT INTO story_sources(story_id,source_id,source_name,url,publisher_group,title,last_seen) VALUES (?,?,?,?,?,?,?)",
                (story_id,"toi-top","The Times of India","https://publisher.test/india-law","times-of-india","Indian parliament passes major security law",published))
        before=server.api_data()
        self.assertIn(story_id,[item["id"] for item in before["hiddenStories"]])
        self.assertNotIn(story_id,[item["id"] for item in before["stories"]])
        with server.connect() as db:
            server.update_story_record(db,story_id,{"hidden":False})
        after=server.api_data()
        self.assertNotIn(story_id,[item["id"] for item in after["hiddenStories"]])
        self.assertIn(story_id,[item["id"] for item in after["stories"]])
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT hidden FROM stories WHERE id=?",(story_id,)).fetchone()["hidden"],0)

    def test_history_is_written_only_for_an_explicit_deep_read_completion(self):
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reading_history").fetchone()[0],0)
            completed=server.dt.datetime(2026,10,22,8,tzinfo=server.dt.timezone.utc)
            server.log_deep_read(db,"a"*40,"Technology & computer science",completed)
            server.log_deep_read(db,"a"*40,"Technology & computer science",completed)
        with server.connect() as db:
            rows=db.execute("SELECT story_id,completed_on,theme FROM reading_history").fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(tuple(rows[0]),("a"*40,"2026-10-22","Technology & computer science"))
            self.assertEqual(db.execute("SELECT read FROM stories WHERE id=?",("a"*40,)).fetchone()[0],1)
            old_date=server.dt.datetime(2026,10,10,8,tzinfo=server.dt.timezone.utc)
            server.log_deep_read(db,"a"*40,"Old fixture theme",old_date)
        with server.connect() as db:
            week=server.weekly_reading_history(db,server.dt.date(2026,10,22))
            self.assertEqual(len(week),1)
            self.assertEqual(week[0]["theme"],"Technology & computer science")

    def test_explicit_feed_reading_time_is_optional(self):
        xml=server.ET.fromstring(b'<item><readingTime>7 min</readingTime></item>')
        absent=server.ET.fromstring(b'<item><description>Excerpt</description></item>')
        self.assertEqual(server.article_reading_minutes(xml),7)
        self.assertIsNone(server.article_reading_minutes(absent))


class FeedReliabilityTests(unittest.TestCase):
    def test_schema_upgrade_adds_source_last_seen_without_replacing_preferences_or_stories(self):
        sources = [{"id":"one", "name":"Publisher One", "publisher_group":"group-one", "region":"Global", "topic":"World", "url":"https://one.test/feed", "enabled":True, "verification":"verified"}]
        with tempfile.TemporaryDirectory(dir=server.ROOT) as directory:
            db_path = Path(directory) / "older.sqlite3"
            con = sqlite3.connect(db_path)
            con.executescript("""
                CREATE TABLE stories(id TEXT PRIMARY KEY,title TEXT NOT NULL,url TEXT NOT NULL UNIQUE,summary TEXT NOT NULL,published TEXT,category TEXT NOT NULL,region TEXT NOT NULL,created TEXT NOT NULL,read INTEGER DEFAULT 0,saved INTEGER DEFAULT 0,hidden INTEGER DEFAULT 0,read_at TEXT,note TEXT,reading_minutes INTEGER);
                CREATE TABLE story_sources(story_id TEXT,source_id TEXT,source_name TEXT,url TEXT,PRIMARY KEY(story_id,source_id));
                CREATE TABLE source_status(source_id TEXT PRIMARY KEY,checked TEXT,error TEXT);
                CREATE TABLE reading_history(story_id TEXT NOT NULL,completed_on TEXT NOT NULL,theme TEXT NOT NULL,completed_at TEXT NOT NULL,PRIMARY KEY(story_id,completed_on));
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO stories VALUES ('legacy','Existing saved story','https://one.test/article','Excerpt','2026-10-22T10:00:00+00:00','World','Global','2026-10-22T10:00:00+00:00',1,1,0,'2026-10-22T10:30:00+00:00','Keep this private note',6);
                INSERT INTO story_sources VALUES ('legacy','one','Publisher One','https://one.test/article');
                INSERT INTO settings VALUES ('sources','["one"]');
                INSERT INTO reading_history VALUES ('legacy','2026-10-22','Technology & computer science','2026-10-22T10:30:00+00:00');
            """)
            con.commit(); con.close()
            with patch.object(server, "DB", db_path), patch.object(server, "get_sources", return_value=sources):
                server.init_db()
                with server.connect() as db:
                    story = db.execute("SELECT title,read,saved,read_at,note,reading_minutes FROM stories WHERE id='legacy'").fetchone()
                    association = db.execute("SELECT url,last_seen,publisher_group,title FROM story_sources WHERE story_id='legacy'").fetchone()
                    preferences = server.read_settings(db)
                    history = db.execute("SELECT story_id,completed_on,theme,completed_at FROM reading_history").fetchone()
            self.assertEqual(tuple(story), ("Existing saved story", 1, 1, "2026-10-22T10:30:00+00:00", "Keep this private note", 6))
            self.assertEqual(tuple(association), ("https://one.test/article", None, "group-one", "Existing saved story"))
            self.assertEqual(tuple(history), ("legacy", "2026-10-22", "Technology & computer science", "2026-10-22T10:30:00+00:00"))
            self.assertEqual(preferences["sources"], ["one"])

    def test_local_server_refuses_a_second_listener_on_the_same_port(self):
        first = server.LocalHTTPServer(("127.0.0.1", 0), server.Handler)
        try:
            with self.assertRaises(OSError):
                server.LocalHTTPServer(("127.0.0.1", first.server_port), server.Handler)
        finally:
            first.server_close()

    def test_source_preferences_skip_disabled_and_unverified_feeds(self):
        sources = [
            {"id":"chosen", "name":"Chosen", "region":"Africa", "region_fallback":True, "topic":"World", "publisher_group":"chosen", "url":"https://one.test/feed", "enabled":True, "verification":"verified"},
            {"id":"disabled", "name":"Disabled", "region":"Asia", "topic":"World", "url":"https://two.test/feed", "enabled":False, "verification":"verified"},
            {"id":"unverified", "name":"Unverified", "region":"Europe", "topic":"World", "url":"https://three.test/feed", "enabled":True, "verification":"unverified"},
        ]
        payload = b'<rss><channel><item><title>A regional policy update</title><description>Fixture excerpt.</description><link>https://one.test/story</link><pubDate>Thu, 22 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>'
        with tempfile.TemporaryDirectory(dir=server.ROOT) as directory:
            db_path = Path(directory) / "source-preferences.sqlite3"
            with patch.object(server, "DB", db_path), patch.object(server, "get_sources", return_value=sources), patch.object(server, "build_opener", return_value=FakeOpener(FakeResponse(payload))) as opener:
                server.init_db()
                with server.connect() as db:
                    db.execute("UPDATE settings SET value=? WHERE key='sources'", ('["chosen","disabled","unverified"]',))
                result = server.refresh()
                with server.connect() as db:
                    statuses = dict(db.execute("SELECT source_id,error FROM source_status"))
                    story = db.execute("SELECT region FROM stories").fetchone()
                    association = db.execute("SELECT last_seen FROM story_sources WHERE source_id='chosen'").fetchone()
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(set(statuses), {"chosen"})
        self.assertEqual(story["region"], "Africa")
        self.assertTrue(association["last_seen"])
        opener.assert_called_once()

    def test_failed_feed_is_not_current_coverage_and_does_not_block_successful_feed(self):
        sources = [
            {"id":"broken", "name":"Broken", "publisher":"Broken", "publisher_group":"broken", "region":"Europe", "url":"https://one.test/feed", "enabled":True, "verification":"verified"},
            {"id":"good", "name":"Good", "publisher":"Good", "publisher_group":"good", "region":"India", "url":"https://two.test/feed", "enabled":True, "verification":"verified"},
        ]
        valid = b'<rss><channel><item><title>Indian government announces policy reform</title><description>Fixture excerpt.</description><link>https://two.test/story</link><pubDate>Thu, 22 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>'
        with tempfile.TemporaryDirectory(dir=server.ROOT) as directory:
            db_path = Path(directory) / "partial-refresh.sqlite3"
            responses = [FakeResponse(b"<rss><channel>"), FakeResponse(valid)]
            with patch.object(server, "DB", db_path), patch.object(server, "get_sources", return_value=sources), patch.object(server, "build_opener", side_effect=lambda *_: FakeOpener(responses.pop(0))):
                server.init_db()
                result = server.refresh()
                with server.connect() as db:
                    rows = {r[0]:{"checked":r[1],"error":r[2]} for r in db.execute("SELECT source_id,checked,error FROM source_status")}
                    stories = [server.story_payload(row, db) for row in db.execute("SELECT * FROM stories")]
            coverage = server.briefing_coverage(stories, sources, rows, today=server.dt.datetime.now().astimezone().date())
        self.assertEqual((result["checked"], result["failed"]), (2, 1))
        self.assertEqual(rows["broken"]["error"], "Feed XML is malformed")
        self.assertIsNone(rows["good"]["error"])
        self.assertEqual(coverage["publishers"], [{"group":"good", "name":"Good"}])
        self.assertEqual(coverage["regions"], ["India"])
        self.assertEqual(coverage["failed_today"], 1)

    def test_legacy_source_url_backfill_matches_publisher_host(self):
        sources = [
            {"id":"one", "name":"Publisher One", "region":"Global", "topic":"World", "url":"https://one.test/feed", "enabled":True, "verification":"verified"},
            {"id":"two", "name":"Publisher Two", "region":"Global", "topic":"World", "url":"https://two.test/feed", "enabled":True, "verification":"verified"},
        ]
        with tempfile.TemporaryDirectory(dir=server.ROOT) as directory:
            db_path = Path(directory) / "legacy.sqlite3"
            with patch.object(server, "DB", db_path), patch.object(server, "get_sources", return_value=sources):
                server.init_db()
                with server.connect() as db:
                    db.execute("DELETE FROM app_meta WHERE key='source_urls_migration_v2'")
                    db.execute("INSERT INTO stories(id,title,url,summary,category,region,created) VALUES (?,?,?,?,?,?,?)", ("legacy", "A fixture headline", "https://one.test/article", "", "World", "Global", "2026-10-22T10:00:00+00:00"))
                    db.execute("INSERT INTO story_sources(story_id,source_id,source_name) VALUES (?,?,?)", ("legacy", "one", "Publisher One"))
                    db.execute("INSERT INTO story_sources(story_id,source_id,source_name) VALUES (?,?,?)", ("legacy", "two", "Publisher Two"))
                server.init_db()
                with server.connect() as db:
                    urls = dict(db.execute("SELECT source_id,url FROM story_sources"))
        self.assertEqual(urls["one"], "https://one.test/article")
        self.assertIsNone(urls["two"])

    def test_refresh_clusters_related_feed_reports_and_keeps_both_links(self):
        sources = [
            {"id":"one", "name":"Publisher One", "region":"Global", "topic":"World", "url":"https://one.test/feed", "enabled":True, "verification":"verified"},
            {"id":"two", "name":"Publisher Two", "region":"Global", "topic":"World", "url":"https://two.test/feed", "enabled":True, "verification":"verified"},
        ]
        def feed(title, link):
            return (f'<rss version="2.0"><channel><item><title>{title}</title><description>Publisher excerpt.</description><link>{link}</link><pubDate>Thu, 22 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>').encode()
        with tempfile.TemporaryDirectory(dir=server.ROOT) as directory:
            db_path = Path(directory) / "cluster-refresh.sqlite3"
            responses = [FakeResponse(feed("Saudi diplomats ordered to leave amid Houthi attacks", "https://one.test/report")),
                         FakeResponse(feed("Some US diplomats told to leave Saudi Arabia amid Houthi attacks", "https://two.test/report"))]
            with patch.object(server, "DB", db_path), patch.object(server, "get_sources", return_value=sources), patch.object(server, "build_opener", side_effect=lambda *_: FakeOpener(responses.pop(0))):
                server.init_db()
                result = server.refresh()
                with server.connect() as db:
                    story_count = db.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
                    links = db.execute("SELECT source_name,url FROM story_sources ORDER BY source_id").fetchall()
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["grouped"], 1)
        self.assertEqual(story_count, 1)
        self.assertEqual([(x["source_name"], x["url"]) for x in links], [
            ("Publisher One", "https://one.test/report"), ("Publisher Two", "https://two.test/report")])

    def test_malformed_feed_records_recoverable_source_error(self):
        source = {"id":"broken", "name":"Broken test feed", "region":"Global", "url":"https://example.invalid/feed", "enabled":True, "verification":"verified"}
        with tempfile.TemporaryDirectory(dir=server.ROOT) as directory:
            db_path = Path(directory) / "briefing.sqlite3"
            with patch.object(server, "DB", db_path), patch.object(server, "get_sources", return_value=[source]), patch.object(server, "build_opener", return_value=FakeOpener(FakeResponse(b"<rss><channel>"))):
                server.init_db()
                result = server.refresh()
                db = sqlite3.connect(db_path)
                try:
                    row = db.execute("SELECT checked,error FROM source_status WHERE source_id='broken'").fetchone()
                    story_count = db.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
                finally:
                    db.close()
            self.assertEqual(result["failed"], 1)
            self.assertTrue(row[0])
            self.assertEqual(row[1], "Feed XML is malformed")
            self.assertEqual(story_count, 0)

    def test_valid_rss_feed_is_parsed_without_fetching_article_body(self):
        payload = b'''<rss version="2.0"><channel><item><title>AI chip research</title><description>Publisher RSS excerpt.</description><link>https://example.org/report</link><pubDate>Thu, 22 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>'''
        with patch.object(server, "build_opener", return_value=FakeOpener(FakeResponse(payload))):
            stories = server.fetch_source({"url":"https://example.org/feed"}, [])
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["summary"], "Publisher RSS excerpt.")
        self.assertEqual(stories[0]["url"], "https://example.org/report")

    def test_rejects_unsafe_publisher_urls_and_non_public_article_links(self):
        for url in ("javascript:alert(1)", "https://user:pass@example.org/feed", "https://localhost/feed", "https://127.0.0.1/feed", "https://example.org:8443/feed", "http://example.org/feed"):
            with self.subTest(url=url):
                self.assertFalse(server.safe_https_url(url))
        self.assertTrue(server.safe_https_url("https://www.example.org/story"))

    def test_feed_size_and_xml_entity_limits_are_enforced(self):
        oversized = b" " * (server.MAX_FEED_BYTES + 1)
        with patch.object(server, "build_opener", return_value=FakeOpener(FakeResponse(oversized))):
            with self.assertRaisesRegex(ValueError, "exceeded"):
                server.fetch_source({"url":"https://example.org/feed"}, [])
        with patch.object(server, "build_opener", return_value=FakeOpener(FakeResponse(b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss/>'))):
            with self.assertRaisesRegex(ValueError, "declarations"):
                server.fetch_source({"url":"https://example.org/feed"}, [])

    def test_redirect_policy_rejects_cross_host_and_http_downgrade(self):
        handler = server.SameHostHTTPSRedirectHandler("https://www.example.org/feed")
        request = server.urllib.request.Request("https://www.example.org/feed")
        headers = {}
        with self.assertRaisesRegex(ValueError, "redirect rejected"):
            handler.redirect_request(request, None, 302, "Found", headers, "https://127.0.0.1/private")
        with self.assertRaisesRegex(ValueError, "redirect rejected"):
            handler.redirect_request(request, None, 302, "Found", headers, "http://example.org/feed")


class HTTPBoundaryTests(unittest.TestCase):
    def test_direct_file_open_explains_that_local_server_is_required(self):
        html = (server.ROOT / "index.html").read_text(encoding="utf-8")
        script = (server.ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('href="./styles.css"', html)
        self.assertIn('src="./app.js"', html)
        self.assertIn('id="file-open-notice"', html)
        self.assertIn("window.location.protocol === 'file:'", script)
        self.assertIn("http://127.0.0.1:8765", html)

    def test_static_allowlist_excludes_database_and_config(self):
        self.assertEqual(set(server.STATIC_FILES.values()), {"index.html", "styles.css", "app.js", "README.md"})
        self.assertNotIn("/morning_signal.sqlite3", server.STATIC_FILES)
        self.assertNotIn("/sources.json", server.STATIC_FILES)

    def test_public_source_metadata_omits_feed_urls(self):
        result = server.public_sources([{"id":"one", "name":"Publisher", "url":"https://example.org/feed", "terms":"personal"}])
        self.assertEqual(result, [{"id":"one", "name":"Publisher", "terms":"personal"}])


class FreshnessAndFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.now = server.dt.datetime(2026, 10, 22, 12, tzinfo=server.dt.timezone.utc)

    def test_publication_timestamps_require_timezone_and_normalize_to_utc(self):
        iso = server.parse_publication_time("2026-10-22T17:30:00+05:30", self.now)
        rfc = server.parse_publication_time("Thu, 22 Oct 2026 17:30:00 +0530", self.now)
        self.assertEqual(iso, "2026-10-22T12:00:00+00:00")
        self.assertEqual(rfc, iso)
        self.assertIsNone(server.parse_publication_time("2026-10-22T12:00:00", self.now))
        self.assertIsNone(server.parse_publication_time("not a date", self.now))
        self.assertIsNone(server.parse_publication_time("2026-10-23T12:00:00Z", self.now))

    def test_recent_stale_and_undated_items_are_separated_and_deep_read_stays_recent(self):
        stories = [
            {"id":"fresh", "title":"New chip security research", "summary":"A report", "published":(self.now-server.dt.timedelta(hours=3)).isoformat(), "category":"Technology", "region":"India", "sources":[{"id":"toi","publisher_group":"toi","title":"New chip security research"}]},
            {"id":"old-theme", "title":"Old AI chip breakthrough", "summary":"Dated old", "published":(self.now-server.dt.timedelta(days=5)).isoformat(), "category":"Technology", "region":"Global", "sources":[{"id":"g","publisher_group":"guardian","title":"Old AI chip breakthrough"}]},
            {"id":"unknown", "title":"Recent computer science paper", "summary":"No reliable date", "published":None, "category":"Technology", "region":"Global", "sources":[{"id":"g","publisher_group":"guardian","title":"Recent computer science paper"}]},
            {"id":"weak", "title":"Routine notice", "summary":"No relevance cues", "published":(self.now-server.dt.timedelta(hours=2)).isoformat(), "category":"Society", "region":"Global", "sources":[{"id":"g","publisher_group":"guardian","title":"Routine notice"}]},
        ]
        result=server.select_daily_briefing(stories,{"topics":["Technology"],"regions":["India"],"freshness_hours":48,"length":8},1,self.now)
        self.assertEqual([s["id"] for s in result["stories"]],["fresh"])
        self.assertEqual(result["deep"]["id"],"fresh")
        self.assertEqual([s["id"] for s in result["unverified"]],["unknown"])
        self.assertEqual(result["stale_count"],1)
        self.assertEqual(result["fresh_count"],1)
        self.assertEqual(result["requested"],8)

    def test_reasons_name_the_signal_and_independent_sources_only(self):
        story={"id":"a","title":"Government declares emergency after parliament vote","summary":"Major change","published":(self.now-server.dt.timedelta(hours=3)).isoformat(),"category":"World","region":"Europe","sources":[
            {"id":"g1","publisher_group":"publisher-a","title":"Government declares emergency after parliament vote"},
            {"id":"g2","publisher_group":"publisher-b","title":"Authorities respond to parliament emergency decision"},
        ]}
        ranked=server.rank_stories([story],{"topics":[],"regions":[]},self.now)
        self.assertEqual(ranked[0]["independent_sources"],2)
        self.assertIn("corroboration",{signal["key"] for signal in ranked[0]["rank_signals"]})
        self.assertEqual(ranked[0]["rank_reason"]," · ".join(ranked[0]["why_this_is_here"]))
        copies=[{"id":"a","publisher_group":"publisher-a","title":"Parliament declares emergency after vote"},
                {"id":"b","publisher_group":"publisher-b","title":"Parliament declares emergency after vote"},
                {"id":"b-tech","publisher_group":"publisher-b","title":"A completely different technology headline"}]
        self.assertEqual(server.independent_source_count(copies),1)
        recent={"id":"r","title":"Community council schedules meeting","summary":"","published":(self.now-server.dt.timedelta(hours=3)).isoformat(),"category":"Society","region":"Global","sources":[]}
        self.assertEqual(server.rank_stories([recent],{"topics":[],"regions":[]},self.now)[0]["rank_reason"],"Published 3 hours ago")

    def test_more_like_this_feedback_is_local_and_changes_matching_scores(self):
        with tempfile.TemporaryDirectory(dir=server.ROOT) as directory:
            db_path=Path(directory)/"feedback.sqlite3"
            with patch.object(server,"DB",db_path):
                server.init_db()
                with server.connect() as db:
                    db.execute("INSERT INTO stories(id,title,url,summary,category,region,created) VALUES (?,?,?,?,?,?,?)",("a"*40,"Fixture technology story","https://publisher.test/story","","Technology","India",self.now.isoformat()))
                    server.update_story_record(db,"a"*40,{"feedback":"more"})
                    profile=server.relevance_feedback(db)
                    self.assertEqual(profile,{"topics":{"Technology":1},"regions":{"India":1}})
                    target={"id":"target","title":"A new software paper","summary":"","published":self.now.isoformat(),"category":"Technology","region":"India","sources":[]}
                    baseline=server.rank_stories([dict(target)],{"topics":[],"regions":[]},self.now)[0]["rank_score"]
                    adjusted=server.rank_stories([dict(target)],{"topics":[],"regions":[]},self.now,profile)[0]
                    self.assertGreater(adjusted["rank_score"],baseline)
                    self.assertIn("local feedback raised",adjusted["rank_reason"])
                    server.update_story_record(db,"a"*40,{"feedback":"less"})
                    less_profile=server.relevance_feedback(db)
                    downranked=server.rank_stories([dict(target)],{"topics":[],"regions":[]},self.now,less_profile)[0]
                    self.assertLess(downranked["rank_score"],baseline)
                    self.assertIn("local feedback lowered",downranked["rank_reason"])
                    server.update_story_record(db,"a"*40,{"feedback":"clear"})
                with server.connect() as db:
                    self.assertIsNone(db.execute("SELECT feedback FROM stories WHERE id=?",("a"*40,)).fetchone()[0])

    def test_stale_and_undated_items_do_not_fill_the_requested_quota(self):
        stories=[
            {"id":"fresh","title":"AI chip research","summary":"","published":self.now.isoformat(),"category":"Technology","region":"Global","sources":[]},
            {"id":"stale","title":"AI chip policy","summary":"government law","published":(self.now-server.dt.timedelta(days=4)).isoformat(),"category":"Technology","region":"India","sources":[]},
            {"id":"undated","title":"AI chip report","summary":"","published":None,"category":"Technology","region":"India","sources":[]},
        ]
        result=server.select_daily_briefing(stories,{"topics":["Technology"],"regions":[],"freshness_hours":48,"length":10},1,self.now)
        self.assertEqual([story["id"] for story in result["stories"]],["fresh"])
        self.assertEqual(len(result["unverified"]),1)
        self.assertEqual(result["deep"]["id"],"fresh")

    def test_explanations_and_breakdown_are_the_actual_score_components(self):
        story={"id":"score","title":"Government declares emergency after parliament vote","summary":"A technology policy decision","published":(self.now-server.dt.timedelta(hours=3)).isoformat(),"category":"Technology","region":"India","feedback":"more","sources":[
            {"id":"one","publisher_group":"one","title":"Government declares emergency after parliament vote"},
            {"id":"two","publisher_group":"two","title":"Emergency decision reported by parliament"},
        ]}
        profile={"topics":{"Technology":1},"regions":{"India":1}}
        result=server.rank_stories([story],{"topics":["Technology"],"regions":["India"],"freshness_hours":48},self.now,profile)[0]
        self.assertEqual(round(sum(item["score"] for item in result["rank_signals"]),2),result["rank_score"])
        self.assertEqual(result["why_this_is_here"],[item["label"] for item in result["rank_signals"][:2]])
        self.assertEqual(len(result["why_this_is_here"]),2)
        self.assertEqual({item["key"] for item in result["rank_signals"]},{"freshness","impact","topic","region","india","corroboration","feedback"})
        self.assertTrue(all(item["score"] != 0 for item in result["rank_signals"]))

    def test_topic_and_region_preferences_change_only_the_matching_score_signals(self):
        story={"id":"pref","title":"New software paper","summary":"","published":(self.now-server.dt.timedelta(hours=4)).isoformat(),"category":"Technology","region":"Asia","sources":[]}
        baseline=server.rank_stories([dict(story)],{"topics":[],"regions":[]},self.now)[0]
        preferred=server.rank_stories([dict(story)],{"topics":["Technology"],"regions":["Asia"]},self.now)[0]
        self.assertAlmostEqual(preferred["rank_score"]-baseline["rank_score"],2.6)
        self.assertEqual([s["key"] for s in preferred["rank_signals"] if s["key"] in {"topic","region"}], ["topic","region"])

    def test_preferred_region_alone_can_qualify_a_recent_story(self):
        story={"id":"region-only","title":"A local arts council announces its annual program","summary":"","published":(self.now-server.dt.timedelta(hours=2)).isoformat(),"category":"Society","region":"Africa","sources":[]}
        settings={"topics":[],"regions":["Africa"],"freshness_hours":48,"length":8}
        result=server.select_daily_briefing([story],settings,0,self.now)
        self.assertEqual([item["id"] for item in result["stories"]],["region-only"])
        self.assertIn("In your preferred Africa region",result["stories"][0]["why_this_is_here"])

    def test_clearing_feedback_undoes_local_topic_and_region_effects(self):
        with tempfile.TemporaryDirectory(dir=server.ROOT) as directory:
            with patch.object(server,"DB",Path(directory)/"undo.sqlite3"):
                server.init_db()
                story_id="b"*40
                with server.connect() as db:
                    db.execute("INSERT INTO stories(id,title,url,summary,category,region,created) VALUES (?,?,?,?,?,?,?)",(story_id,"Fixture technology story","https://publisher.test/story","","Technology","Asia",self.now.isoformat()))
                    target={"id":"target","title":"Recent software paper","summary":"","published":self.now.isoformat(),"category":"Technology","region":"Asia","sources":[]}
                    before=server.rank_stories([dict(target)],{"topics":[],"regions":[]},self.now)[0]["rank_score"]
                    server.update_story_record(db,story_id,{"feedback":"more"})
                    profile=server.relevance_feedback(db)
                    after=server.rank_stories([dict(target)],{"topics":[],"regions":[]},self.now,profile)[0]["rank_score"]
                    self.assertGreater(after,before)
                    server.update_story_record(db,story_id,{"feedback":"clear"})
                    cleared=server.relevance_feedback(db)
                    undone=server.rank_stories([dict(target)],{"topics":[],"regions":[]},self.now,cleared)[0]["rank_score"]
                    self.assertEqual(cleared,{"topics":{},"regions":{}})
                    self.assertEqual(undone,before)

    def test_empty_briefing_explains_why_and_offers_recovery_steps(self):
        stories=[
            {"id":"stale-only","title":"Old software law","summary":"government policy","published":(self.now-server.dt.timedelta(days=5)).isoformat(),"category":"Technology","region":"India","sources":[]},
            {"id":"undated-only","title":"Software report","summary":"","published":None,"category":"Technology","region":"Asia","sources":[]},
        ]
        result=server.select_daily_briefing(stories,{"topics":["Technology"],"regions":[],"freshness_hours":48,"length":8},1,self.now)
        self.assertEqual(result["stories"],[])
        self.assertEqual(result["empty_reason"],"No dated, recent stories matched the current relevance rules.")
        self.assertTrue(any("freshness window" in step for step in result["empty_next_steps"]))
        self.assertTrue(any("Date-unverified" in step for step in result["empty_next_steps"]))


class StoryContextTests(unittest.TestCase):
    def test_feed_based_paraphrase_uses_only_title_and_excerpt_and_is_labeled(self):
        story={"title":"City council approves a new flood plan","summary":"The council voted 8-2 on Tuesday. The plan funds barriers near the river.","category":"Society","url":"https://civic.example/story","sources":[{"name":"Civic News RSS","url":"https://civic.example/story","publisher_group":"civic-news","title":"City council approves a new flood plan"}]}
        context=server.story_context(story)
        self.assertIn(story["title"],context["what_happened"])
        self.assertIn(story["summary"],context["what_happened"])
        self.assertIn("generated framing",context["what_happened_label"])
        self.assertIn("publisher-provided",context["what_happened_label"])
        self.assertNotIn("will prevent flooding",context["what_happened"])

    def test_missing_or_brief_excerpt_is_called_out_as_insufficient_context(self):
        for excerpt in ("", "Officials announced a plan."):
            with self.subTest(excerpt=excerpt):
                context=server.story_context({"title":"Officials announce a plan","summary":excerpt,"category":"World","url":"https://wire.example/report","sources":[]})
                marker="excerpt is absent" if not excerpt else "excerpt is brief"
                self.assertTrue(any(marker in item for item in context["not_clear"]))
                if not excerpt:
                    self.assertIn("does not provide enough information",context["what_happened"])
                else:
                    self.assertIn(excerpt,context["what_happened"])

    def test_conflicting_clustered_headlines_are_attributed_without_merging_same_publisher(self):
        story={"title":"Central bank raises interest rates","summary":"A sufficiently detailed feed excerpt describes the decision and timing.","category":"Business","url":"https://one.example/rates","sources":[
            {"name":"Publisher One","publisher_group":"one","title":"Central bank raises interest rates","url":"https://one.example/rates"},
            {"name":"Publisher One Business","publisher_group":"one","title":"Central bank cuts interest rates","url":"https://one.example/other"},
            {"name":"Publisher Two","publisher_group":"two","title":"Central bank cuts interest rates","url":"https://two.example/rates"},
        ]}
        context=server.story_context(story)
        conflict=next(item for item in context["not_clear"] if "headlines differ" in item)
        self.assertIn("Publisher One",conflict)
        self.assertIn("Publisher Two",conflict)
        self.assertEqual([item["name"] for item in context["other_publishers"]],["Publisher Two"])

    def test_attribution_links_original_and_distinct_independent_publishers(self):
        story={"title":"A new research report","summary":"Researchers published a detailed report with methods and findings described in this feed excerpt.","category":"Science","url":"https://one.example/original","sources":[
            {"name":"Publisher One · Science","publisher_group":"one","title":"A new research report","url":"https://one.example/original"},
            {"name":"Publisher One · World","publisher_group":"one","title":"Research report published","url":"https://one.example/duplicate"},
            {"name":"Syndicated Publisher","publisher_group":"syndicated","title":"A new research report","url":"https://syndicated.example/repost"},
            {"name":"Publisher Two","publisher_group":"two","title":"Researchers publish new report","url":"https://two.example/report"},
        ]}
        context=server.story_context(story)
        self.assertEqual(context["original_publisher"],{"name":"Publisher One · Science","url":"https://one.example/original"})
        self.assertEqual(context["other_publishers"],[{"name":"Publisher Two","url":"https://two.example/report","publisher_group":"two"}])

    def test_three_display_modes_show_only_their_defined_feed_context(self):
        excerpt="Researchers tested a new battery design over twelve months. The team reported measurements from three laboratory trials and noted that independent replication has not yet been published. More details from the permitted feed excerpt explain the experimental setup and stated limits."
        story={"title":"Researchers report battery test results","summary":excerpt,"category":"Science","url":"https://one.example/battery","sources":[
            {"name":"Publisher One","publisher_group":"one","title":"Researchers report battery test results","url":"https://one.example/battery"},
            {"name":"Publisher Two","publisher_group":"two","title":"Battery researchers publish test results","url":"https://two.example/battery"},
        ]}
        context=server.story_context(story)
        quick=server.story_context_for_mode(context,"Quick scan")
        standard=server.story_context_for_mode(context,"Standard")
        more=server.story_context_for_mode(context,"More context")
        self.assertLessEqual(len(quick["summary"]),160)
        self.assertEqual(quick["publisher_excerpt"],"")
        self.assertEqual(quick["other_publishers"],[])
        self.assertIsNone(quick["why_matter"])
        self.assertTrue(standard["why_matter"])
        self.assertEqual(standard["not_clear"],[])
        self.assertEqual(standard["publisher_excerpt"],"")
        self.assertEqual(more["publisher_excerpt"],excerpt)
        self.assertTrue(more["why_matter"])
        self.assertTrue(more["other_publishers"])
        for mode_view in (quick, standard, more):
            self.assertEqual(mode_view["original_publisher"]["url"],story["url"])

    def test_insufficient_excerpt_is_not_filled_in_any_display_mode(self):
        story={"title":"Officials announce a new policy","summary":"A new policy was announced.","category":"World","url":"https://news.example/policy","sources":[{"name":"News Desk","publisher_group":"news","title":"Officials announce a new policy","url":"https://news.example/policy"}]}
        context=server.story_context(story)
        quick=server.story_context_for_mode(context,"Quick scan")
        standard=server.story_context_for_mode(context,"Standard")
        more=server.story_context_for_mode(context,"More context")
        self.assertIn("too brief",quick["summary"])
        self.assertIn(story["summary"],standard["summary"])
        self.assertIn("excerpt is brief",standard["summary"])
        self.assertEqual(more["publisher_excerpt"],story["summary"])
        self.assertIn("feed excerpt is brief", " ".join(more["not_clear"]))
        for display in (quick, standard, more):
            self.assertEqual(display["original_publisher"]["url"],story["url"])


class OfflineEvaluationTests(unittest.TestCase):
    def test_offline_fixture_suite_passes_selection_expectations(self):
        result, checks, excluded = offline_evaluation.evaluate()
        self.assertTrue(all(checks.values()), "Failed evaluation checks: " + ", ".join(k for k, v in checks.items() if not v))
        self.assertEqual(result["requested"], 8)
        self.assertLess(len(result["stories"]), result["requested"])
        self.assertEqual(set(excluded), {"eval-celebrity", "eval-kpop"})
        self.assertTrue(all(story["id"].startswith("eval-") for story in result["stories"]))

    def test_offline_fixtures_are_labeled_and_use_fixed_timestamps(self):
        self.assertTrue(all("label" in item and item["id"].startswith("eval-") for item in offline_evaluation.FIXTURES))
        self.assertEqual(offline_evaluation.NOW.isoformat(), "2026-10-22T12:00:00+00:00")
        self.assertNotIn("eval-", str(server.DB))


if __name__ == "__main__":
    unittest.main()
