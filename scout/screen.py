"""Stage 3: score papers against config criteria. Deterministic criteria are computed in Python; judgement
criteria (topic fit, novelty, commercial applicability) come from one Claude call per paper with a JSON schema."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from datetime import datetime
from typing import Any

from . import db

log = logging.getLogger(__name__)

LLM_CRITERIA = ("topic_fit", "novelty", "commercial_applicability")

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        c: {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": 10},
                "reason": {"type": "string", "description": "one sentence, at most 25 words"},
            },
            "required": ["score", "reason"],
            "additionalProperties": False,
        }
        for c in LLM_CRITERIA
    },
    "required": list(LLM_CRITERIA),
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You screen newly published arXiv papers for an investor and research scout. For each paper you
receive the title, abstract, categories and a few extracted signals. Score three criteria from 0 to 10 and give a
one-sentence reason for each. Be calibrated: 8-10 is rare and means a clear standout; 5 is average for arXiv.

Criteria
- topic_fit: how well the paper fits this thesis:
{topic}
- novelty: how new the idea or result is relative to what is already common in the field. Surveys, position
  papers and small variations on known methods score low.
- commercial_applicability: evidence that this could become a product or company: a working end-to-end system,
  released code or models, strong results on realistic benchmarks, real-world or production deployment,
  clear users or buyers. Theory, pure analysis and toy settings score low.

Judge only from what is in front of you. Do not infer anything about the authors."""


def _count_hits(text: str, patterns: list[str]) -> list[str]:
    low = text.lower()
    return [p for p in patterns if p.lower() in low]


def deterministic_scores(conn: sqlite3.Connection, cfg: dict[str, Any], paper: sqlite3.Row) -> dict[str, tuple[float, str]]:
    scfg = cfg["screen"]
    out: dict[str, tuple[float, str]] = {}
    text = f"{paper['title']} {paper['abstract']} {paper['comment'] or ''}"
    if paper["code_url"]:
        out["code_released"] = (10.0, f"code link found: {paper['code_url']}")
    else:
        hits = _count_hits(text, scfg["commercial_signals"]["code_patterns"])
        out["code_released"] = ((4.0, f"mentions code release ({', '.join(hits[:2])}) but no link") if hits else (0.0, "no code link or release mentioned"))
    tr = scfg["track_record"]
    rows = conn.execute(
        "SELECT a.display_name, a.h_index, a.citation_count FROM paper_authors pa JOIN authors a ON a.author_id=pa.author_id "
        "WHERE pa.arxiv_id=? AND a.enriched_at IS NOT NULL", (paper["arxiv_id"],)).fetchall()
    if not rows:
        out["author_track_record"] = (0.0, "no citation data yet (authors not enriched or paper not indexed)")
    else:
        best, best_name = 0.0, ""
        for r in rows:
            h = r["h_index"] or 0
            c = r["citation_count"] or 0
            # log-scaled so that early-career authors with a few hundred citations still register
            s_h = math.log1p(h) / math.log1p(tr["h_index_full_score"])
            s_c = math.log1p(c) / math.log1p(tr["citations_full_score"])
            s = 10 * min(1.0, 0.5 * s_h + 0.5 * s_c)
            if s > best:
                best, best_name = s, f"{r['display_name']} (h={h}, {c} citations)"
        out["author_track_record"] = (round(best, 1), f"strongest author: {best_name}")
    return out


def keyword_hits(cfg: dict[str, Any], paper: sqlite3.Row) -> int:
    kws = cfg["screen"]["topic"].get("keywords", [])
    text = f"{paper['title']} {paper['abstract']}".lower()
    return sum(1 for k in kws if k.lower() in text)


def build_user_message(cfg: dict[str, Any], paper: sqlite3.Row) -> str:
    sig = cfg["screen"]["commercial_signals"]
    text = f"{paper['title']} {paper['abstract']} {paper['comment'] or ''}"
    signals = {
        "code_url": paper["code_url"],
        "system_signals": _count_hits(text, sig["system_patterns"]),
        "benchmark_signals": _count_hits(text, sig["benchmark_patterns"]),
    }
    return (f"Title: {paper['title']}\nCategories: {', '.join(json.loads(paper['categories']))}\n"
            f"Comment: {paper['comment'] or '-'}\nExtracted signals: {json.dumps(signals)}\n\nAbstract:\n{paper['abstract']}")


def make_client(cfg: dict[str, Any]):
    import anthropic
    key = cfg["general"]["secrets"]["anthropic"]
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env; run with --no-llm for deterministic criteria only")
    return anthropic.Anthropic(api_key=key)


def _request_params(cfg: dict[str, Any], paper: sqlite3.Row) -> dict[str, Any]:
    scfg = cfg["screen"]
    system = SYSTEM_PROMPT.format(topic=scfg["topic"]["description"].strip())
    return {
        "model": scfg.get("model", "claude-opus-5-5"),
        "max_tokens": int(scfg.get("max_tokens", 1024)),
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": build_user_message(cfg, paper)}],
        "output_config": {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    }


def parse_llm_response(message: Any) -> dict[str, tuple[float, str]] | None:
    if getattr(message, "stop_reason", None) == "refusal":
        log.warning("LLM declined to score a paper: %s", getattr(message, "stop_details", None))
        return None
    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
    out = {}
    for c in LLM_CRITERIA:
        item = data.get(c) or {}
        out[c] = (float(max(0, min(10, int(item.get("score", 0))))), str(item.get("reason", ""))[:300])
    return out


def llm_scores(client: Any, cfg: dict[str, Any], paper: sqlite3.Row) -> dict[str, tuple[float, str]] | None:
    params = _request_params(cfg, paper)
    # Server-side refusal fallback: if the model declines for policy reasons the API re-runs on a fallback model.
    message = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
    return parse_llm_response(message)


def llm_scores_batch(client: Any, cfg: dict[str, Any], papers: list[sqlite3.Row], poll_seconds: int = 30) -> dict[str, dict[str, tuple[float, str]] | None]:
    """Score many papers through the Message Batches API (half price, asynchronous)."""
    requests = [{"custom_id": p["arxiv_id"].replace(".", "_").replace("/", "_"), "params": _request_params(cfg, p)} for p in papers]
    id_map = {r["custom_id"]: p["arxiv_id"] for r, p in zip(requests, papers)}
    batch = client.messages.batches.create(requests=requests)
    log.info("batch %s submitted with %d requests", batch.id, len(requests))
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        time.sleep(poll_seconds)
    out: dict[str, dict[str, tuple[float, str]] | None] = {}
    for res in client.messages.batches.results(batch.id):
        arxiv_id = id_map.get(res.custom_id)
        if res.result.type == "succeeded":
            out[arxiv_id] = parse_llm_response(res.result.message)
        else:
            log.warning("batch item %s: %s", arxiv_id, res.result.type)
            out[arxiv_id] = None
    return out


def write_scores(conn: sqlite3.Connection, cfg: dict[str, Any], arxiv_id: str, scores: dict[str, tuple[float, str]], model: str | None) -> float | None:
    now = db.utcnow()
    for crit, (score, reason) in scores.items():
        conn.execute(
            "INSERT INTO paper_scores(arxiv_id, criterion, score, reason, model, scored_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(arxiv_id, criterion) DO UPDATE SET score=excluded.score, reason=excluded.reason, model=excluded.model, scored_at=excluded.scored_at",
            (arxiv_id, crit, score, reason, model if crit in LLM_CRITERIA else None, now))
    return update_composite(conn, cfg, arxiv_id)


def update_composite(conn: sqlite3.Connection, cfg: dict[str, Any], arxiv_id: str) -> float | None:
    """Weighted mean of available criteria, scaled to 0-100. Only written once the LLM criteria exist, so
    deterministic-only papers do not outrank fully screened ones."""
    weights = cfg["screen"]["weights"]
    rows = {r["criterion"]: r["score"] for r in conn.execute("SELECT criterion, score FROM paper_scores WHERE arxiv_id=? AND criterion!='composite'", (arxiv_id,))}
    if not all(c in rows for c in LLM_CRITERIA):
        return None
    num = sum(weights[c] * rows[c] for c in rows if c in weights)
    den = sum(weights[c] for c in rows if c in weights)
    if den == 0:
        return None
    composite = round(10 * num / den, 1)
    conn.execute(
        "INSERT INTO paper_scores(arxiv_id, criterion, score, reason, model, scored_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(arxiv_id, criterion) DO UPDATE SET score=excluded.score, scored_at=excluded.scored_at",
        (arxiv_id, "composite", composite, "weighted mean of criteria", None, db.utcnow()))
    return composite


def run(cfg: dict[str, Any], conn: sqlite3.Connection, limit: int | None = None, since: datetime | None = None,
        use_llm: bool = True, rescore: bool = False, client: Any = None) -> dict[str, int]:
    scfg = cfg["screen"]
    sql = "SELECT p.* FROM papers p"
    params: list[Any] = []
    where = []
    if not rescore:
        where.append("NOT EXISTS (SELECT 1 FROM paper_scores s WHERE s.arxiv_id=p.arxiv_id AND s.criterion='composite')")
    if since:
        where.append("p.submitted_at >= ?")
        params.append(since.isoformat())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.submitted_at DESC"
    papers = conn.execute(sql, params).fetchall()
    run_id = db.start_run(conn, "screen", {"limit": limit, "use_llm": use_llm, "papers": len(papers)})
    stats = {"papers": len(papers), "deterministic": 0, "llm_scored": 0, "llm_skipped_prefilter": 0, "llm_failed": 0}
    model = scfg.get("model", "claude-opus-5-5")
    min_hits = int(scfg.get("prefilter_min_keyword_hits", 0))

    for p in papers:
        write_scores(conn, cfg, p["arxiv_id"], deterministic_scores(conn, cfg, p), None)
        stats["deterministic"] += 1
    conn.commit()

    if use_llm:
        todo = [p for p in papers if keyword_hits(cfg, p) >= min_hits]
        stats["llm_skipped_prefilter"] = len(papers) - len(todo)
        if limit:
            todo = todo[:limit]
        if todo:
            client = client or make_client(cfg)
            if scfg.get("use_batch_api"):
                results = llm_scores_batch(client, cfg, todo)
                for p in todo:
                    sc = results.get(p["arxiv_id"])
                    if sc:
                        write_scores(conn, cfg, p["arxiv_id"], sc, model)
                        stats["llm_scored"] += 1
                    else:
                        stats["llm_failed"] += 1
            else:
                for i, p in enumerate(todo, 1):
                    try:
                        sc = llm_scores(client, cfg, p)
                    except Exception as e:
                        log.warning("LLM scoring failed for %s: %s", p["arxiv_id"], e)
                        sc = None
                    if sc:
                        write_scores(conn, cfg, p["arxiv_id"], sc, model)
                        stats["llm_scored"] += 1
                    else:
                        stats["llm_failed"] += 1
                    if i % 10 == 0:
                        conn.commit()
                        log.info("LLM scored %d/%d", i, len(todo))
    conn.commit()
    db.finish_run(conn, run_id, stats)
    return stats
