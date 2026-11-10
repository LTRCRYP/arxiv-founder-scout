"""Stage 1: pull new arXiv submissions via the official API into SQLite, idempotently."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from . import db
from .http import Client
from .names import normalize_name

log = logging.getLogger(__name__)

API_URL = "https://export.arxiv.org/api/query"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}
CODE_URL_RE = re.compile(r"https?://(?:www\.)?(?:github\.com|gitlab\.com|huggingface\.co|bitbucket\.org)/[\w\-./]+", re.I)
ID_RE = re.compile(r"arxiv\.org/abs/(.+?)(v(\d+))?$")


def parse_feed(xml_text: str) -> tuple[int, list[dict[str, Any]]]:
    """Parse an arXiv Atom feed into (total_results, entries)."""
    root = ET.fromstring(xml_text)
    total_el = root.find("os:totalResults", NS)
    total = int(total_el.text) if total_el is not None and total_el.text else 0
    entries = []
    for e in root.findall("atom:entry", NS):
        raw_id = (e.findtext("atom:id", "", NS) or "").strip()
        m = ID_RE.search(raw_id)
        if not m:
            continue
        arxiv_id, version = m.group(1), int(m.group(3) or 1)
        title = " ".join((e.findtext("atom:title", "", NS) or "").split())
        abstract = " ".join((e.findtext("atom:summary", "", NS) or "").split())
        authors = []
        for a in e.findall("atom:author", NS):
            name = " ".join((a.findtext("atom:name", "", NS) or "").split())
            aff = a.findtext("arxiv:affiliation", None, NS)
            if name:
                authors.append({"name": name, "affiliation": aff})
        cats = [c.get("term") for c in e.findall("atom:category", NS) if c.get("term")]
        primary_el = e.find("arxiv:primary_category", NS)
        primary = primary_el.get("term") if primary_el is not None else (cats[0] if cats else None)
        abs_url = pdf_url = None
        for link in e.findall("atom:link", NS):
            if link.get("title") == "pdf":
                pdf_url = link.get("href")
            elif link.get("rel") == "alternate":
                abs_url = link.get("href")
        comment = e.findtext("arxiv:comment", None, NS)
        journal_ref = e.findtext("arxiv:journal_ref", None, NS)
        doi = e.findtext("arxiv:doi", None, NS)
        code_url = None
        for text in (comment or "", abstract):
            cm = CODE_URL_RE.search(text)
            if cm:
                code_url = cm.group(0).rstrip(".,);")
                break
        entries.append({
            "arxiv_id": arxiv_id, "version": version, "title": title, "abstract": abstract,
            "authors": authors, "categories": cats, "primary_category": primary,
            "submitted_at": e.findtext("atom:published", "", NS), "updated_at": e.findtext("atom:updated", "", NS),
            "abs_url": abs_url or f"https://arxiv.org/abs/{arxiv_id}", "pdf_url": pdf_url,
            "code_url": code_url, "comment": comment, "journal_ref": journal_ref, "doi": doi,
        })
    return total, entries


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def fetch_window(client: Client, categories: list[str], since: datetime, until: datetime,
                 page_size: int = 200, max_results: int | None = None) -> Iterator[dict[str, Any]]:
    cat_q = " OR ".join(f"cat:{c}" for c in categories)
    query = f"({cat_q}) AND submittedDate:[{_fmt(since)} TO {_fmt(until)}]"
    start, yielded = 0, 0
    while True:
        params = {"search_query": query, "start": start, "max_results": page_size,
                  "sortBy": "submittedDate", "sortOrder": "ascending"}
        text = client.get_text(API_URL, params=params)
        total, entries = parse_feed(text)
        log.info("arXiv page start=%d got=%d total=%d", start, len(entries), total)
        if not entries:
            # arXiv occasionally returns an empty page transiently; one retry is built into Client. Stop here.
            return
        for ent in entries:
            yield ent
            yielded += 1
            if max_results and yielded >= max_results:
                return
        start += len(entries)
        if start >= total:
            return


def get_or_create_author(conn: sqlite3.Connection, name: str) -> int:
    norm = normalize_name(name)
    if not norm:
        norm = name.lower()
    row = conn.execute("SELECT author_id FROM authors WHERE normalized_name=?", (norm,)).fetchone()
    if row:
        return int(row["author_id"])
    cur = conn.execute("INSERT INTO authors(display_name, normalized_name) VALUES (?,?)", (name, norm))
    return int(cur.lastrowid)


def upsert_paper(conn: sqlite3.Connection, ent: dict[str, Any], run_id: int) -> str:
    """Insert or update a paper. Returns 'inserted', 'updated' (newer version) or 'unchanged'."""
    row = conn.execute("SELECT version FROM papers WHERE arxiv_id=?", (ent["arxiv_id"],)).fetchone()
    cats = json.dumps(ent["categories"])
    if row is None:
        conn.execute(
            """INSERT INTO papers(arxiv_id, version, title, abstract, primary_category, categories, submitted_at,
               updated_at, abs_url, pdf_url, code_url, comment, journal_ref, doi, first_seen_run, last_seen_run)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ent["arxiv_id"], ent["version"], ent["title"], ent["abstract"], ent["primary_category"], cats,
             ent["submitted_at"], ent["updated_at"], ent["abs_url"], ent["pdf_url"], ent["code_url"],
             ent["comment"], ent["journal_ref"], ent["doi"], run_id, run_id))
        status = "inserted"
    elif ent["version"] > int(row["version"]):
        conn.execute(
            """UPDATE papers SET version=?, title=?, abstract=?, primary_category=?, categories=?, updated_at=?,
               pdf_url=?, code_url=COALESCE(?, code_url), comment=?, journal_ref=?, doi=?, last_seen_run=?
               WHERE arxiv_id=?""",
            (ent["version"], ent["title"], ent["abstract"], ent["primary_category"], cats, ent["updated_at"],
             ent["pdf_url"], ent["code_url"], ent["comment"], ent["journal_ref"], ent["doi"], run_id, ent["arxiv_id"]))
        status = "updated"
    else:
        conn.execute("UPDATE papers SET last_seen_run=? WHERE arxiv_id=?", (run_id, ent["arxiv_id"]))
        status = "unchanged"
    for pos, a in enumerate(ent["authors"]):
        author_id = get_or_create_author(conn, a["name"])
        conn.execute(
            """INSERT INTO paper_authors(arxiv_id, author_id, position, affiliation) VALUES (?,?,?,?)
               ON CONFLICT(arxiv_id, author_id) DO UPDATE SET position=excluded.position,
               affiliation=COALESCE(excluded.affiliation, paper_authors.affiliation)""",
            (ent["arxiv_id"], author_id, pos, a.get("affiliation")))
    return status


def run(cfg: dict[str, Any], conn: sqlite3.Connection, since: datetime | None = None,
        until: datetime | None = None, max_results: int | None = None) -> dict[str, int]:
    acfg = cfg["arxiv"]
    until = until or datetime.now(timezone.utc)
    if since is None:
        last = db.get_state(conn, "arxiv", "last_until")
        since = datetime.fromisoformat(last) if last else until - timedelta(days=acfg.get("default_days", 1))
    client = Client(cfg["general"]["user_agent"], min_interval=acfg.get("min_interval_seconds", 3.0))
    run_id = db.start_run(conn, "ingest", {"since": since, "until": until, "categories": acfg["categories"]})
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    log.info("Ingesting %s from %s to %s", acfg["categories"], since.isoformat(), until.isoformat())
    for ent in fetch_window(client, acfg["categories"], since, until, acfg.get("page_size", 200), max_results):
        stats[upsert_paper(conn, ent, run_id)] += 1
        if sum(stats.values()) % 100 == 0:
            conn.commit()
    if max_results is None:
        # Overlap by a day on the next run so late-arriving items in the window are not missed.
        db.set_state(conn, "arxiv", "last_until", (until - timedelta(days=1)).isoformat())
    conn.commit()
    db.finish_run(conn, run_id, stats)
    return stats
