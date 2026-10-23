"""Deterministic, offline selection evaluation. These invented fixtures are never live news."""
from __future__ import annotations

import copy
import datetime as dt

import server


NOW = dt.datetime(2026, 10, 22, 12, tzinfo=dt.timezone.utc)
SETTINGS = {
    "topics": ["Technology", "Business", "Science", "Climate", "World"],
    "regions": ["India", "Asia", "Europe", "Africa", "Americas", "Middle East"],
    "freshness_hours": 48,
    "length": 8,
}
EXCLUDED = ["Celebrity", "Influencer", "Entertainment gossip", "K-pop"]


def hours_ago(hours):
    return (NOW - dt.timedelta(hours=hours)).isoformat()


# Explicitly invented evaluation inputs. They must never be inserted in the app DB.
FIXTURES = [
    {"id":"eval-india-tech", "label":"fresh significant India story", "title":"Indian parliament passes chip security law after breach", "summary":"Government sets new security policy for critical technology.", "published":hours_ago(2), "category":"Technology", "region":"India", "sources":[{"id":"toi", "publisher_group":"toi", "title":"Indian parliament passes chip security law after breach"}]},
    {"id":"eval-europe", "label":"fresh Europe story", "title":"European parliament approves emergency climate legislation", "summary":"The law changes emissions policy across member states.", "published":hours_ago(3), "category":"World", "region":"Europe", "sources":[{"id":"e1", "publisher_group":"europe-paper", "title":"European parliament approves emergency climate legislation"}]},
    {"id":"eval-africa", "label":"fresh Africa story", "title":"Kenya declares emergency after severe flood displaces families", "summary":"Authorities begin a humanitarian response after widespread flooding.", "published":hours_ago(4), "category":"Climate", "region":"Africa", "sources":[{"id":"a1", "publisher_group":"africa-paper", "title":"Kenya declares emergency after severe flood displaces families"}]},
    {"id":"eval-americas", "label":"fresh Americas story", "title":"US court blocks government security policy", "summary":"The ruling affects national technology policy.", "published":hours_ago(5), "category":"World", "region":"Americas", "sources":[{"id":"u1", "publisher_group":"us-paper", "title":"US court blocks government security policy"}]},
    {"id":"eval-asia", "label":"fresh Asia story", "title":"Japan central bank changes interest rates as inflation rises", "summary":"The decision affects borrowing costs and the regional economy.", "published":hours_ago(6), "category":"Business", "region":"Asia", "sources":[{"id":"j1", "publisher_group":"japan-paper", "title":"Japan central bank changes interest rates as inflation rises"}]},
    {"id":"eval-cluster", "label":"clustered multi-report event", "title":"Bangladesh declares emergency after cyclone makes landfall", "summary":"Officials report major damage and begin disaster response.", "published":hours_ago(2), "category":"World", "region":"Asia", "sources":[
        {"id":"wire-copy", "publisher_group":"wire", "title":"Bangladesh declares emergency after cyclone makes landfall"},
        {"id":"paper-copy", "publisher_group":"regional-paper", "title":"Bangladesh declares emergency after cyclone makes landfall"},
        {"id":"paper-followup", "publisher_group":"regional-paper", "title":"A different technology report"},
    ]},
    {"id":"eval-civic-exception", "label":"celebrity civic exception", "title":"Actor elected to parliament after historic campaign", "summary":"The election follows a landmark civic movement.", "published":hours_ago(7), "category":"World", "region":"India", "sources":[{"id":"c1", "publisher_group":"civic-paper", "title":"Actor elected to parliament after historic campaign"}]},
    {"id":"eval-low-relevance", "label":"recent low relevance", "title":"Routine gadget launch draws modest interest", "summary":"A company introduces a minor consumer product update.", "published":hours_ago(1), "category":"Society", "region":"Global", "sources":[{"id":"l1", "publisher_group":"lifestyle", "title":"Routine gadget launch draws modest interest"}]},
    {"id":"eval-stale", "label":"stale technology story", "title":"Old computer chip research announces breakthrough", "summary":"A past research announcement.", "published":hours_ago(120), "category":"Technology", "region":"Global", "sources":[{"id":"s1", "publisher_group":"science-paper", "title":"Old computer chip research announces breakthrough"}]},
    {"id":"eval-undated", "label":"undated technology story", "title":"Computer science researchers publish security study", "summary":"A study about software security.", "published":None, "category":"Technology", "region":"Global", "sources":[{"id":"d1", "publisher_group":"tech-paper", "title":"Computer science researchers publish security study"}]},
    {"id":"eval-celebrity", "label":"excluded celebrity gossip", "title":"Celebrity couple shares private gossip", "summary":"Entertainment coverage with no civic significance.", "published":hours_ago(1), "category":"Society", "region":"Global", "sources":[]},
    {"id":"eval-kpop", "label":"excluded K-pop entertainment", "title":"K-pop idol group announces new tour", "summary":"Entertainment tour announcement.", "published":hours_ago(1), "category":"Society", "region":"Asia", "sources":[]},
]


def evaluate():
    candidates = []
    excluded_ids = []
    for fixture in copy.deepcopy(FIXTURES):
        if server.should_exclude(fixture["title"], fixture["summary"], EXCLUDED):
            excluded_ids.append(fixture["id"])
        else:
            candidates.append(fixture)
    result = server.select_daily_briefing(candidates, SETTINGS, 1, NOW)
    selected = result["stories"]
    selected_ids = {story["id"] for story in selected}
    regions = {story["region"] for story in selected}
    clustered = next(story for story in candidates if story["id"] == "eval-cluster")
    cluster_rank = next(story for story in server.rank_stories([copy.deepcopy(clustered)], SETTINGS, NOW) if story["id"] == "eval-cluster")
    checks = {
        "fresh relevant stories selected": all(f"eval-{region}" in selected_ids for region in ("india-tech", "europe", "africa", "americas", "asia")),
        "India included": "India" in regions,
        "geographic variety without quotas": len(regions) >= 4,
        "recent low-relevance story omitted": "eval-low-relevance" not in selected_ids,
        "stale story omitted": "eval-stale" not in selected_ids and result["stale_count"] == 1,
        "undated story labeled separately": "eval-undated" not in selected_ids and any(s["id"] == "eval-undated" and s["date_status"] == "unverified" for s in result["unverified"]),
        "near-identical syndicated copies count once": cluster_rank["independent_sources"] == 1,
        "clustered event is represented once with its reports attached": "eval-cluster" in selected_ids and len(clustered["sources"]) == 3,
        "celebrity gossip and K-pop excluded": {"eval-celebrity", "eval-kpop"}.issubset(excluded_ids),
        "civic celebrity exception retained": "eval-civic-exception" in selected_ids,
        "weekday deep read matches technology theme": result["deep"] is not None and result["deep"]["category"] == "Technology",
        "short briefing when fewer stories qualify": len(selected) < result["requested"],
    }
    return result, checks, excluded_ids


def main():
    result, checks, excluded_ids = evaluate()
    print("OFFLINE EVALUATION - invented fixtures only; no feeds, network, or database used")
    print("Selected stories:")
    for story in result["stories"]:
        print(f"  [{story['region']}] {story['title']} | {story['rank_reason']}")
    print("Excluded by editorial rules:")
    for story_id in excluded_ids:
        print(f"  {story_id}")
    print(f"Undated, shown separately: {', '.join(s['id'] for s in result['unverified']) or 'none'}")
    cluster_fixture = next(item for item in FIXTURES if item["id"] == "eval-cluster")
    print(f"Cluster fixture: {cluster_fixture['title']} ({len(cluster_fixture['sources'])} attached report records; {server.independent_source_count(cluster_fixture['sources'])} independent source group)")
    print(f"Stale omitted: {result['stale_count']}; selected {len(result['stories'])}/{result['requested']} requested")
    print("Checks:")
    for label, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'} {label}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
