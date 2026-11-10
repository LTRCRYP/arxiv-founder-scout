from scout.ingest import parse_feed, upsert_paper


def _counts(conn):
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("papers", "authors", "paper_authors"))


def test_parse_feed_extracts_fields(feed_xml):
    total, entries = parse_feed(feed_xml)
    assert total == 3 and len(entries) == 3
    e = entries[0]
    assert e["arxiv_id"] == "2611.00001" and e["version"] == 1
    assert e["title"] == "Robotic Manipulation with Foundation Models"  # whitespace collapsed
    assert e["code_url"] == "https://github.com/acme-robotics/pick"
    assert e["primary_category"] == "cs.RO" and e["categories"] == ["cs.RO", "cs.AI"]
    assert e["authors"][0] == {"name": "José García-López", "affiliation": "Stanford University"}
    assert e["pdf_url"].endswith("2611.00001v1")


def test_reingest_is_idempotent(conn, feed_xml):
    _, entries = parse_feed(feed_xml)
    for e in entries:
        upsert_paper(conn, e, run_id=1)
    first = _counts(conn)
    # 6 distinct author rows: Wei Zhang appears twice and is merged; "J. Smith" is deliberately NOT merged with
    # "John Smith" at ingest (initials are resolved later, during matching, with corroborating evidence).
    assert first == (3, 6, 7)
    statuses = [upsert_paper(conn, e, run_id=2) for e in entries]
    assert statuses == ["unchanged"] * 3
    assert _counts(conn) == first


def test_newer_version_updates_in_place(conn, feed_xml, feed_xml_v2):
    for e in parse_feed(feed_xml)[1]:
        upsert_paper(conn, e, run_id=1)
    before = _counts(conn)
    statuses = [upsert_paper(conn, e, run_id=2) for e in parse_feed(feed_xml_v2)[1]]
    assert statuses == ["updated", "unchanged"]
    assert _counts(conn) == before
    row = conn.execute("SELECT version, title, code_url FROM papers WHERE arxiv_id='2611.00001'").fetchone()
    assert row["version"] == 2 and row["title"].endswith("(v2)")
    assert row["code_url"] == "https://github.com/acme-robotics/pick"  # kept
    # an older version arriving later never overwrites a newer one
    assert upsert_paper(conn, parse_feed(feed_xml)[1][0], run_id=3) == "unchanged"
    assert conn.execute("SELECT version FROM papers WHERE arxiv_id='2611.00001'").fetchone()[0] == 2


def test_author_identity_is_normalised(conn, feed_xml):
    for e in parse_feed(feed_xml)[1]:
        upsert_paper(conn, e, run_id=1)
    names = {r[0] for r in conn.execute("SELECT normalized_name FROM authors")}
    assert "jose garcia lopez" in names
    assert conn.execute("SELECT COUNT(*) FROM authors WHERE normalized_name LIKE '%zhang'").fetchone()[0] == 1
