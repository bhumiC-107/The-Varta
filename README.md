# Morning Signal

Morning Signal is a concise RSS-based morning briefing. It groups related reports, prioritizes fresh and relevant stories, includes an India perspective, and links to publishers. It does not fetch article pages or use AI services.

Prototype: https://the-varta-ekjozvpb3-bhumic-107s-projects.vercel.app/

## Run locally

Requires Python 3.10+. The app uses only the Python standard library.

```powershell
python server.py
```

Open <http://127.0.0.1:8765> and choose **Refresh feeds**. Stop with Ctrl+C. Local mode keeps story metadata, permitted feed excerpts, preferences, and reading activity in `morning_signal.sqlite3` using SQLite. To use a separate development database, set `MORNING_SIGNAL_SQLITE_PATH` to another filename first. The existing database and backups are excluded from Git and Vercel deployment.

## Deploy to Vercel without a database

The Vercel version is a public reader with no server-side database or sign-in. It needs no Neon/MySQL/Postgres account, database URL, password, session secret, or environment variable. On first open it automatically checks the enabled verified RSS feeds; **Refresh feeds** can check them again. The serverless request does not open SQLite or retain feed results. Read/save/hide actions, notes, the daily goal, reading history, and display mode are stored in this browser's `localStorage`, on this device only. They are not sent with feed requests. Clearing browser site data or using another browser/device clears or will not show that local reading state. Local mode remains available with SQLite.

1. Import the repository into Vercel with the repository root as the project root.
2. Keep the included `vercel.json`; no database or environment configuration is needed.
3. Deploy. Open the assigned HTTPS URL and use **Refresh feeds**.

The actual account limits, Python runtime packaging, and live feed reachability from Vercel are not verified by the fixture suite. A refresh is bounded by the Vercel function duration and may return partial results if feeds fail or the function budget is reached. There is no scheduled/background refresh or server cache. The public URL has no access control: anyone with it can view and refresh the configured feeds. Do not add personal or secret feeds to `sources.json`.

### Local privacy and existing data

Vercel does not read, upload, migrate, modify, or delete `morning_signal.sqlite3` or its backups. Keep local files backed up as usual. Local SQLite behavior and data remain available by running `python server.py`. In Vercel, reading state is persisted only by the browser's local storage; no notes, saved/read actions, or history are sent to the application server or publishers. Refreshing contacts configured feed publishers, and opening an article sends the browser to that publisher.

## Sources and feed limits

Edit `sources.json` to manage feeds. Only enabled entries with `verification: "verified"` are fetched by the stateless deployment. Unverified entries remain disabled. The selected sources are documented publisher RSS feeds:

| Publisher | Coverage | Feed terms/limits recorded in source configuration |
| --- | --- | --- |
| The Times of India | India | Personal, non-commercial use; RSS excerpts only and direct article links. |
| The Guardian | World, technology, business, science | Personal, non-commercial use under Guardian terms. Four feeds count as one publisher. |
| CNA | Asia | Personal, non-commercial use; do not modify or redistribute feed; link directly to articles. |
| Le Monde | Europe, Africa, Americas | Strictly personal, non-professional, non-collective use; other use requires authorization and a fee. |
| Africanews | Latest | Disabled and unverified: response format and local excerpt terms need confirmation. |
| NASA | News releases | Disabled and unverified: the configured endpoint and reuse terms have not been confirmed. |

The project is a personal reader, not a public news redistribution service. **The stateless Vercel URL is publicly accessible**, so publishing excerpts through it may not fit publishers’ personal/non-collective terms even though no database is used. Review the current publisher terms before public deployment. Disable any source whose terms do not allow the intended exposure. Source availability and terms can change; this project does not independently confirm live availability during deployment.

Feed requests have a timeout, 1 MB response cap, 35 item limit, XML entity declaration rejection, and redirects restricted to the same publisher host over HTTPS. Failures are isolated per source and shown with a sanitized reason and check time. The feed collector does not fetch article pages, bypass paywalls, log in, or use unofficial feed generators. Feed requests may be blocked by publisher infrastructure or Vercel network policy; the UI reports such failures rather than treating them as coverage.

## Selection and editorial behavior

- Stories are clustered conservatively using informative headline token overlap, category/region compatibility, and publication-time proximity. A cluster retains source links; same-publisher feeds count as one publisher for corroboration.
- The ranking uses publication recency, topic and region matches, modest India coverage, text-based impact cues, and independent publisher coverage. These are transparent heuristics, not proof of importance or accuracy.
- The briefing favors recent, relevant stories and can show fewer than the requested maximum. Stale items are excluded; missing or unreliable publication dates are labeled unverified and do not outrank dated recent stories. Fetch time is shown separately from publication time.
- Geographic variety is applied only among similarly ranked candidates; it does not force weak stories into regional quotas. Coverage indicators list the regions and publishers that actually contributed from successful feeds and do not claim global balance.
- Exactly one deep-read candidate is selected only when a recent relevant story matches the weekday theme. Important stories outside that theme can still appear in the briefing. If no candidate qualifies, the feature explains that instead of substituting an unrelated or stale story.
- Default exclusions cover celebrity, influencer, entertainment gossip, and K-pop terms, with a narrow configured civic/humanitarian exception. Keyword rules can miss context and are not human editorial review.
- Story context uses only feed titles, excerpts, and source metadata. It is a labeled feed-based paraphrase; short or missing excerpts produce an insufficiency note. “Why it may matter” is a cautious category template and cannot establish real-world consequences. Publisher links remain the authority for the full report.

Vercel ranking uses fixed defaults; local personalization settings are not sent to the server. The Vercel page automatically fetches stories on first open and keeps an optional reading list, read status, hidden list, personal notes, daily goal, streak, and weekly deep-read history in browser local storage. These browser-only settings persist across reloads in the same browser profile. They are not synced or backed up by the app.

## Project files

- `server.py`: local SQLite application, bounded RSS/Atom collector, clustering, ranking, reading controls, and stateless feed collection helper.
- `api/index.py`: public Vercel handler; serves the app and runs one-request RSS refreshes without a database.
- `vercel.json`: Vercel Python routing and function duration.
- `sources.json`, `editorial.json`: editable source and editorial rules.
- `index.html`, `styles.css`, `app.js`: responsive interface.
- `test_server.py`, `test_deployment.py`: local and stateless deployment regression tests with fixtures.
- `offline_evaluation.py`: deterministic offline editorial evaluation; fixtures are never mixed into live briefings.

## Checks

Run the tests and syntax checks with Python 3.10+ and Node.js:

```powershell
python -m unittest -v
python -m py_compile server.py api/index.py test_server.py test_deployment.py offline_evaluation.py
node --check app.js
python offline_evaluation.py
```

Tests use mocked feeds, fixtures, and temporary SQLite databases. They do not establish live feed availability or a successful Vercel deployment.

## Known limitations

- Public Vercel mode has no sign-in and is viewable by anyone with the URL. Preferences and reading state stay in browser local storage, so they are not private from other users of the same browser profile, synced across devices, or recoverable if site data is cleared.
- RSS feeds are incomplete, can fail, and vary in how much context or reliable publication dating they provide. Regional labels, impact cues, and event clustering are heuristics; they can misclassify or miss stories.
- A few publishers have personal-use or non-collective use limits. Review their current terms before exposing feed excerpts on a public domain; disable feeds if public display is not permitted.
- The Vercel route is implemented but has not been deployed or verified against a live Vercel account. No claim is made that the Vercel runtime can reach every feed.
- Browser layout, keyboard use, and contrast should be manually checked on target browsers and narrow screens; this project has no automated browser or screen-reader runner.
