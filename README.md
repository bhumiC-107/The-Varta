# Morning Signal

Morning Signal is a concise RSS-based morning briefing. It groups related reports, prioritizes fresh and relevant stories, includes an India perspective, and links to publishers. It does not fetch article pages or use AI services.

Try the prototype here : https://the-varta-ekjozvpb3-bhumic-107s-projects.vercel.app/
### Local privacy and existing data

Vercel does not read, upload, migrate, modify, or delete `morning_signal.sqlite3` or its backups. Keep local files backed up as usual. Local SQLite behavior and data remain available by running `python server.py`; the Vercel version is intentionally read-only/stateless. Neither version sends notes or reading activity to external services. Refreshing contacts configured publishers, and opening a publisher article sends the browser to that publisher.

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

The Vercel version uses fixed default ranking preferences, because personalized settings are not sent to a server or stored remotely. Local mode retains its configurable preferences and reading features in SQLite. The stateless Vercel page does not save stories or settings across a reload.

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

- Public Vercel mode has no sign-in and is viewable by anyone with the URL. It does not retain feed results, preferences, or user actions. This suits a personal read-only session but is not a shared private publication system.
- RSS feeds are incomplete, can fail, and vary in how much context or reliable publication dating they provide. Regional labels, impact cues, and event clustering are heuristics; they can misclassify or miss stories.
- A few publishers have personal-use or non-collective use limits. Review their current terms before exposing feed excerpts on a public domain; disable feeds if public display is not permitted.
- The Vercel route is implemented but has not been deployed or verified against a live Vercel account. No claim is made that the Vercel runtime can reach every feed.
- Browser layout, keyboard use, and contrast should be manually checked on target browsers and narrow screens; this project has no automated browser or screen-reader runner.
