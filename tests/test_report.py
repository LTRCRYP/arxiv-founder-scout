from datetime import datetime, timezone

from scout import report
from scout.ingest import parse_feed, upsert_paper


def test_report_renders_markdown_and_html(cfg, conn, feed_xml, tmp_path):
    for e in parse_feed(feed_xml)[1]:
        upsert_paper(conn, e, run_id=1)
    conn.execute("INSERT INTO paper_scores VALUES ('2611.00001','composite',88,'x',NULL,'x')")
    conn.execute("INSERT INTO paper_scores VALUES ('2611.00001','topic_fit',9,'fits the thesis',NULL,'x')")
    conn.commit()
    paths = report.run(cfg, conn, since=datetime(2026, 11, 1, tzinfo=timezone.utc), until=datetime(2026, 11, 8, tzinfo=timezone.utc),
                       fmt="both", out=str(tmp_path / "r.md"))
    md = paths[0].read_text()
    assert "## 1. Top papers" in md and "Robotic Manipulation" in md and "fits the thesis" in md
    assert "## 3. Strong papers whose authors have no known startup" in md and "### 1. [Robotic" in md.split("## 3.")[1]
    html = paths[1].read_text()
    assert html.startswith("<!doctype html>") and "<h2>1. Top papers</h2>" in html and "<a href=" in html
