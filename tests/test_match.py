import json
from datetime import date
from pathlib import Path

import pytest

from scout import match
from scout.founders import upsert_company
from scout.founders.csv_import import parse_file
from scout.ingest import parse_feed, upsert_paper

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def populated(cfg, conn, feed_xml):
    for e in parse_feed(feed_xml)[1]:
        upsert_paper(conn, e, run_id=1)
    for r in parse_file(FIXTURES / "companies.csv", cfg, date(2024, 11, 9)):
        upsert_company(conn, r)
    # enrichment results as Semantic Scholar / OpenAlex would have produced them
    conn.execute("UPDATE authors SET affiliations=?, homepage=?, enriched_at='x' WHERE normalized_name='jose garcia lopez'",
                 (json.dumps(["Stanford University", "Acme Robotics"]), "https://acme-robotics.ai/jose"))
    conn.execute("UPDATE authors SET affiliations=?, enriched_at='x' WHERE normalized_name='priya natarajan'", (json.dumps(["Stanford University"]),))
    conn.execute("UPDATE authors SET affiliations=?, enriched_at='x' WHERE normalized_name='john smith'", (json.dumps(["University of Nowhere"]),))
    # pretend Semantic Scholar knows 300 distinct "John Smith"s and one of each other name
    conn.execute("INSERT INTO name_frequency VALUES ('john smith', 300, 'x')")
    for n in ("jose garcia lopez", "priya natarajan", "amara okafor", "wei zhang"):
        conn.execute("INSERT INTO name_frequency VALUES (?, 1, 'x')", (n,))
    conn.commit()
    return conn


def _matches(conn):
    rows = conn.execute("SELECT a.display_name, pe.full_name, c.name, m.confidence, m.tier, m.evidence FROM matches m "
                        "JOIN authors a ON a.author_id=m.author_id JOIN people pe ON pe.person_id=m.person_id "
                        "JOIN companies c ON c.company_id=pe.company_id").fetchall()
    return {(r[0], r[2]): r for r in rows}


def test_matching_tiers_and_evidence(cfg, populated):
    stats = match.run(cfg, populated, use_network=False)
    m = _matches(populated)

    # Exact name + homepage on company domain + affiliation + co-author + area overlap -> confirmed
    jose = m[("José García-López", "Acme Robotics")]
    assert jose["tier"] == "confirmed"
    signals = {e["signal"] for e in json.loads(jose["evidence"])}
    assert {"name_exact", "homepage_domain", "affiliation_company_name", "coauthor_same_company"} <= signals

    # Exact name + co-author at the same company + topical overlap, but no direct link -> probable, not confirmed
    priya = m[("Priya Natarajan", "Acme Robotics")]
    assert priya["tier"] == "probable"

    # Very common name, no corroboration -> weak (never presented as fact)
    smith = m[("John Smith", "Smith Analytics")]
    assert smith["tier"] == "weak"
    assert "common_name_penalty" in {e["signal"] for e in json.loads(smith["evidence"])}

    # Initials-only founder name ("A. Okafor") with strong topical overlap -> at most probable
    okafor = m[("Amara Okafor", "Edge Kernels Inc")]
    assert okafor["tier"] in ("weak", "probable")
    assert "ambiguous_name_penalty" in {e["signal"] for e in json.loads(okafor["evidence"])}

    # Old Corp was filtered at import, so Wei Zhang has no match at all
    assert not any(k[0] == "Wei Zhang" for k in m)
    assert stats["confirmed"] == 1


def test_rerun_does_not_duplicate_matches(cfg, populated):
    match.run(cfg, populated, use_network=False)
    n = populated.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    match.run(cfg, populated, use_network=False)
    match.run(cfg, populated, use_network=False, rematch=True)
    assert populated.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == n


def test_review_overrides(cfg, populated):
    match.run(cfg, populated, use_network=False)
    mid = populated.execute("SELECT match_id FROM matches WHERE tier='weak' LIMIT 1").fetchone()[0]
    match.review(populated, mid, "rejected")
    assert populated.execute("SELECT reviewed FROM matches WHERE match_id=?", (mid,)).fetchone()[0] == "rejected"
    with pytest.raises(ValueError):
        match.review(populated, mid, "maybe")


def test_helpers():
    assert match.domain("https://www.Acme-Robotics.ai/team") == "acme-robotics.ai"
    assert match.domain(None) is None
    assert match.location_terms("Palo Alto, CA") == {"palo alto", "california"}
