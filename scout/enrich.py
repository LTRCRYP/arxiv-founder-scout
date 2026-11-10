"""Stage 2: resolve paper authors against Semantic Scholar (primary) and OpenAlex (institutions, ORCID).

Both are queried per *paper* (by arXiv id / DOI), which disambiguates authors far better than a name
search. Authors of papers not yet indexed are left un-enriched and retried on the next run.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from . import db
from .http import Client, QuotaExhausted
from .names import compare_names, normalize_name

log = logging.getLogger(__name__)

S2_PAPER_URL = "https://api.semanticscholar.org/graph/v1/paper/arXiv:{id}"
S2_FIELDS = "authors.name,authors.affiliations,authors.homepage,authors.hIndex,authors.citationCount,authors.paperCount,authors.externalIds"
OA_WORK_URL = "https://api.openalex.org/works/https://doi.org/10.48550/arXiv.{id}"
OA_SELECT = "id,authorships"
OA_AUTHORS_URL = "https://api.openalex.org/authors"


def backfill_openalex_metrics(client: Client, conn: sqlite3.Connection, api_key: str, batch: int = 50) -> int:
    """Fill citation_count / h_index / works_count from OpenAlex for authors Semantic Scholar did not cover.
    One request per 50 authors (filter=ids.openalex:A1|A2|...)."""
    rows = conn.execute("SELECT author_id, openalex_id FROM authors WHERE openalex_id IS NOT NULL AND citation_count IS NULL").fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        ids = "|".join(r["openalex_id"].rsplit("/", 1)[-1] for r in chunk)
        params = {"filter": f"ids.openalex:{ids}", "select": "id,cited_by_count,works_count,summary_stats", "per-page": batch}
        if api_key:
            params["api_key"] = api_key
        data = client.get_json(OA_AUTHORS_URL, params=params) or {}
        by_id = {a["id"].rsplit("/", 1)[-1]: a for a in data.get("results", [])}
        for r in chunk:
            a = by_id.get(r["openalex_id"].rsplit("/", 1)[-1])
            if not a:
                continue
            conn.execute("UPDATE authors SET citation_count=?, works_count=?, h_index=? WHERE author_id=?",
                         (a.get("cited_by_count"), a.get("works_count"), (a.get("summary_stats") or {}).get("h_index"), r["author_id"]))
            done += 1
        conn.commit()
    return done


def _match_author(paper_authors: list[sqlite3.Row], name: str) -> sqlite3.Row | None:
    norm = normalize_name(name)
    best, best_kind = None, "none"
    for row in paper_authors:
        kind, _ = compare_names(norm, row["normalized_name"])
        if kind == "exact":
            return row
        if kind in ("fuzzy", "initials") and best is None:
            best, best_kind = row, kind
    return best


def enrich_paper_s2(client: Client, conn: sqlite3.Connection, arxiv_id: str, paper_authors: list[sqlite3.Row]) -> int:
    data = client.get_json(S2_PAPER_URL.format(id=arxiv_id), params={"fields": S2_FIELDS})
    if not data:
        return 0
    hits = 0
    for a in data.get("authors", []):
        row = _match_author(paper_authors, a.get("name", ""))
        if row is None:
            continue
        ext = a.get("externalIds") or {}
        conn.execute(
            """UPDATE authors SET s2_id=?, homepage=COALESCE(?, homepage), affiliations=?, citation_count=?, h_index=?,
               works_count=?, orcid=COALESCE(orcid, ?), enrich_method='paper', enriched_at=? WHERE author_id=?""",
            (a.get("authorId"), a.get("homepage"), json.dumps(a.get("affiliations") or []), a.get("citationCount"),
             a.get("hIndex"), a.get("paperCount"), ext.get("ORCID"), db.utcnow(), row["author_id"]))
        hits += 1
    return hits


def enrich_paper_openalex(client: Client, conn: sqlite3.Connection, arxiv_id: str, paper_authors: list[sqlite3.Row],
                          api_key: str) -> int:
    params = {"select": OA_SELECT}
    if api_key:
        params["api_key"] = api_key
    data = client.get_json(OA_WORK_URL.format(id=arxiv_id), params=params)
    if not data:
        return 0
    hits = 0
    for ship in data.get("authorships", []):
        author = ship.get("author") or {}
        row = _match_author(paper_authors, author.get("display_name", ""))
        if row is None:
            continue
        insts = [i.get("display_name") for i in ship.get("institutions", []) if i.get("display_name")]
        insts += [s for s in ship.get("raw_affiliation_strings", []) if s]
        existing = json.loads(row["affiliations"] or "[]")
        merged = list(dict.fromkeys(existing + insts))
        orcid = (author.get("orcid") or "").rsplit("/", 1)[-1] or None
        conn.execute(
            """UPDATE authors SET openalex_id=?, orcid=COALESCE(orcid, ?), affiliations=?,
               enrich_method=COALESCE(enrich_method, 'paper'), enriched_at=COALESCE(enriched_at, ?) WHERE author_id=?""",
            (author.get("id"), orcid, json.dumps(merged), db.utcnow(), row["author_id"]))
        hits += 1
    return hits


def run(cfg: dict[str, Any], conn: sqlite3.Connection, limit: int | None = None, since: datetime | None = None) -> dict[str, int]:
    ecfg = cfg["enrich"]
    ua = cfg["general"]["user_agent"]
    secrets = cfg["general"]["secrets"]
    s2_headers = {"x-api-key": secrets["semantic_scholar"]} if secrets["semantic_scholar"] else None
    # Without a key Semantic Scholar answers 429 intermittently from its shared pool; be patient.
    s2 = Client(ua, min_interval=ecfg.get("semantic_scholar_min_interval_seconds", 1.0), headers=s2_headers, max_retries=6)
    oa = Client(ua, min_interval=ecfg.get("openalex_min_interval_seconds", 0.2))
    use_oa = ecfg.get("use_openalex", True)
    use_s2 = ecfg.get("use_semantic_scholar", True)
    if use_oa and not secrets["openalex"]:
        log.warning("OPENALEX_API_KEY not set: OpenAlex shares a $0.10/day budget per IP without a key; enrichment may be throttled")

    sql = """SELECT p.arxiv_id FROM papers p
             WHERE EXISTS (SELECT 1 FROM paper_authors pa JOIN authors a ON a.author_id=pa.author_id
                           WHERE pa.arxiv_id=p.arxiv_id AND a.enriched_at IS NULL)
               AND NOT EXISTS (SELECT 1 FROM source_state s WHERE s.source='enrich' AND s.key=p.arxiv_id AND s.value='done')"""
    params: list[Any] = []
    if since:
        sql += " AND p.submitted_at >= ?"
        params.append(since.isoformat())
    # Oldest first: indexing services lag arXiv by a few days, so older papers are the ones that resolve.
    sql += " ORDER BY p.submitted_at ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    papers = [r["arxiv_id"] for r in conn.execute(sql, params)]
    run_id = db.start_run(conn, "enrich", {"limit": limit, "papers": len(papers)})
    stats = {"papers": len(papers), "s2_indexed": 0, "s2_authors": 0, "oa_indexed": 0, "oa_authors": 0, "errors": 0}
    if not use_s2 and not use_oa:
        return stats
    for i, arxiv_id in enumerate(papers, 1):
        pa = conn.execute("SELECT a.* FROM paper_authors pa JOIN authors a ON a.author_id=pa.author_id WHERE pa.arxiv_id=? ORDER BY pa.position", (arxiv_id,)).fetchall()
        indexed = False
        if not use_s2 and not use_oa:
            log.error("both enrichment services are unavailable for today; stopping")
            break
        try:
            if use_s2:
                try:
                    n = enrich_paper_s2(s2, conn, arxiv_id, pa)
                except QuotaExhausted as e:
                    log.warning("Semantic Scholar quota exhausted (%s); skipping it for the rest of this run", e)
                    use_s2, n = False, 0
                if n:
                    stats["s2_indexed"] += 1
                    stats["s2_authors"] += n
                    indexed = True
            if use_oa:
                pa = conn.execute("SELECT a.* FROM paper_authors pa JOIN authors a ON a.author_id=pa.author_id WHERE pa.arxiv_id=? ORDER BY pa.position", (arxiv_id,)).fetchall()
                try:
                    n = enrich_paper_openalex(oa, conn, arxiv_id, pa, secrets["openalex"])
                except QuotaExhausted as e:
                    log.warning("OpenAlex daily budget exhausted (%s); skipping it for the rest of this run. Set OPENALEX_API_KEY for a 10x budget.", e)
                    use_oa, n = False, 0
                if n:
                    stats["oa_indexed"] += 1
                    stats["oa_authors"] += n
                    indexed = True
        except Exception as e:  # keep going; one bad paper should not stop the batch
            stats["errors"] += 1
            log.warning("enrich %s failed: %s", arxiv_id, e)
            if stats["errors"] >= 10 and stats["errors"] > i // 2:
                log.error("too many errors; stopping enrich run")
                break
        if indexed:
            db.set_state(conn, "enrich", arxiv_id, "done")
        if i % 20 == 0:
            conn.commit()
            log.info("enriched %d/%d papers", i, len(papers))
    conn.commit()
    if use_oa:
        try:
            stats["oa_metrics_backfilled"] = backfill_openalex_metrics(oa, conn, secrets["openalex"])
        except (QuotaExhausted, Exception) as e:  # noqa: BLE001
            log.warning("OpenAlex metrics backfill stopped: %s", e)
    db.finish_run(conn, run_id, stats)
    return stats
