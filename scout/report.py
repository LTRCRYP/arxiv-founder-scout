"""Stage 6: weekly Markdown (and optional HTML) report with three lists."""
from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _scores(conn: sqlite3.Connection, arxiv_id: str) -> dict[str, sqlite3.Row]:
    return {r["criterion"]: r for r in conn.execute("SELECT * FROM paper_scores WHERE arxiv_id=?", (arxiv_id,))}


def _authors(conn: sqlite3.Connection, arxiv_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT a.* FROM paper_authors pa JOIN authors a ON a.author_id=pa.author_id WHERE pa.arxiv_id=? ORDER BY pa.position", (arxiv_id,)).fetchall()


def _paper_block(conn: sqlite3.Connection, p: sqlite3.Row, rank: int) -> list[str]:
    sc = _scores(conn, p["arxiv_id"])
    comp = sc.get("composite")
    authors = _authors(conn, p["arxiv_id"])
    names = ", ".join(a["display_name"] for a in authors[:6]) + (" et al." if len(authors) > 6 else "")
    lines = [f"### {rank}. [{p['title']}]({p['abs_url']})  \n**Score {comp['score']:.0f}/100** · {p['primary_category']} · submitted {p['submitted_at'][:10]}  \n_{names}_",
             ""]
    for crit in ("topic_fit", "novelty", "commercial_applicability", "code_released", "author_track_record"):
        if crit in sc:
            lines.append(f"- **{crit.replace('_', ' ')}** {sc[crit]['score']:.0f}/10: {sc[crit]['reason']}")
    lines.append("")
    return lines


def _match_block(conn: sqlite3.Connection, m: sqlite3.Row, window_papers: set[str]) -> list[str]:
    a = conn.execute("SELECT * FROM authors WHERE author_id=?", (m["author_id"],)).fetchone()
    pe = conn.execute("SELECT pe.*, c.name AS company_name, c.website, c.founded_date, c.industry, c.location AS company_location, c.raised_usd, c.source AS company_source FROM people pe JOIN companies c ON c.company_id=pe.company_id WHERE pe.person_id=?", (m["person_id"],)).fetchone()
    papers = conn.execute("SELECT p.arxiv_id, p.title, p.abs_url FROM paper_authors pa JOIN papers p ON p.arxiv_id=pa.arxiv_id WHERE pa.author_id=? ORDER BY p.submitted_at DESC", (a["author_id"],)).fetchall()
    tier_label = {"confirmed": "Confirmed", "probable": "Probable", "weak": "Weak, needs review"}[m["tier"]]
    hedge = "" if m["tier"] == "confirmed" else " (unverified)"
    raised = f", raised ${pe['raised_usd']:,.0f}" if pe["raised_usd"] else ""
    lines = [f"### {a['display_name']} ↔ {pe['full_name']} at **{pe['company_name']}**{hedge}  \n"
             f"**{tier_label}, confidence {m['confidence']:.2f}** · role: {pe['role'] or '-'} · founded {pe['founded_date'] or '?'} · {pe['industry'] or ''} {pe['company_location'] or ''}{raised} · source: {pe['company_source']}"
             + (f" · [{pe['website']}]({pe['website']})" if pe["website"] else ""), ""]
    for e in json.loads(m["evidence"]):
        sign = "+" if e["value"] >= 0 else "−"
        lines.append(f"- {sign}{abs(e['value']):.2f} {e['signal']}: {e['detail']}")
    lines.append("- papers: " + "; ".join(f"[{p['title'][:70]}]({p['abs_url']})" + (" (this week)" if p["arxiv_id"] in window_papers else "") for p in papers[:3]))
    if m["reviewed"]:
        lines.append(f"- manual review: **{m['reviewed']}**")
    lines.append("")
    return lines


def build_markdown(cfg: dict[str, Any], conn: sqlite3.Connection, since: datetime, until: datetime) -> str:
    rcfg = cfg["report"]
    win = (since.isoformat(), until.isoformat())
    window_papers = {r["arxiv_id"] for r in conn.execute("SELECT arxiv_id FROM papers WHERE submitted_at BETWEEN ? AND ?", win)}
    n_scored = conn.execute("SELECT COUNT(*) FROM paper_scores s JOIN papers p ON p.arxiv_id=s.arxiv_id WHERE s.criterion='composite' AND p.submitted_at BETWEEN ? AND ?", win).fetchone()[0]
    n_companies = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    n_people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]

    out = [f"# arXiv founder scout — week of {since.date()} to {until.date()}", "",
           f"Papers ingested this window: **{len(window_papers)}** ({', '.join(cfg['arxiv']['categories'])}); fully screened: **{n_scored}**. "
           f"Founder database: **{n_companies}** companies, **{n_people}** people. Match tiers: confirmed ≥ {cfg['match']['confirmed_threshold']}, "
           f"probable ≥ {cfg['match']['probable_threshold']}; anything below is listed only as *needs review* and must not be treated as a fact.", ""]

    # 1. Top papers
    top = conn.execute(
        "SELECT p.* FROM papers p JOIN paper_scores s ON s.arxiv_id=p.arxiv_id AND s.criterion='composite' "
        "WHERE p.submitted_at BETWEEN ? AND ? ORDER BY s.score DESC, p.submitted_at DESC LIMIT ?", (*win, rcfg["top_papers"])).fetchall()
    out += ["## 1. Top papers", ""]
    if not top:
        out += ["_No fully screened papers in this window. Run `scout screen` (needs ANTHROPIC_API_KEY)._", ""]
    for i, p in enumerate(top, 1):
        out += _paper_block(conn, p, i)

    # 2. Author-founder matches
    matches = conn.execute(
        "SELECT DISTINCT m.* FROM matches m JOIN paper_authors pa ON pa.author_id=m.author_id JOIN papers p ON p.arxiv_id=pa.arxiv_id "
        "WHERE p.submitted_at BETWEEN ? AND ? AND COALESCE(m.reviewed,'')!='rejected' ORDER BY m.confidence DESC", win).fetchall()
    strong = [m for m in matches if m["tier"] in ("confirmed", "probable") or m["reviewed"] == "accepted"]
    weak = [m for m in matches if m not in strong]
    out += ["## 2. Author ↔ founder matches", ""]
    if not strong:
        out += ["_No confirmed or probable matches for authors publishing this window._", ""]
    for m in strong:
        out += _match_block(conn, m, window_papers)
    if weak:
        out += [f"<details><summary>Needs review: {len(weak)} weak name-level matches (not evidence of anything on their own)</summary>", ""]
        for m in weak[:40]:
            a = conn.execute("SELECT display_name FROM authors WHERE author_id=?", (m["author_id"],)).fetchone()
            pe = conn.execute("SELECT pe.full_name, c.name FROM people pe JOIN companies c ON c.company_id=pe.company_id WHERE person_id=?", (m["person_id"],)).fetchone()
            out.append(f"- {m['confidence']:.2f} · {a['display_name']} ~ {pe['full_name']} ({pe['name']}) · match id {m['match_id']}")
        out += ["", "</details>", ""]

    # 3. High-scoring papers with no known startup
    founded_authors = {r["author_id"] for r in conn.execute("SELECT author_id FROM matches WHERE (tier IN ('confirmed','probable') OR reviewed='accepted') AND COALESCE(reviewed,'')!='rejected'")}
    cands = conn.execute(
        "SELECT p.*, s.score AS composite FROM papers p JOIN paper_scores s ON s.arxiv_id=p.arxiv_id AND s.criterion='composite' "
        "WHERE p.submitted_at BETWEEN ? AND ? AND s.score >= ? ORDER BY s.score DESC", (*win, rcfg["unfounded_min_score"])).fetchall()
    out += [f"## 3. Strong papers whose authors have no known startup (score ≥ {rcfg['unfounded_min_score']})", ""]
    shown = 0
    for p in cands:
        ids = {a["author_id"] for a in _authors(conn, p["arxiv_id"])}
        if ids & founded_authors:
            continue
        shown += 1
        out += _paper_block(conn, p, shown)
        if shown >= rcfg["unfounded_max"]:
            break
    if shown == 0:
        out += ["_Nothing above the threshold this window._", ""]
    out += ["---", f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. Founder data: SEC Form D (US, quarterly, lags up to 3 months) and CSV imports. "
            "Only public professional information is used._"]
    return "\n".join(out)


def markdown_to_html(md: str, title: str) -> str:
    """Tiny renderer for the subset of Markdown this report emits."""
    def inline(s: str) -> str:
        s = html.escape(s, quote=False)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])_(.+?)_(?!\w)", r"<em>\1</em>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return s.replace("  \n", "<br>")
    body, in_list = [], False
    for block in md.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        if block.startswith("<details") or block.startswith("</details"):
            body.append(block)
            continue
        if block.startswith("#"):
            level = len(block) - len(block.lstrip("#"))
            body.append(f"<h{level}>{inline(block[level:].strip())}</h{level}>")
        elif block.startswith("- "):
            items = "".join(f"<li>{inline(l[2:])}</li>" for l in block.split("\n") if l.startswith("- "))
            body.append(f"<ul>{items}</ul>")
        elif block == "---":
            body.append("<hr>")
        else:
            body.append(f"<p>{inline(block)}</p>")
    css = ("body{font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#1a1a1a;background:#fff}"
           "h1,h2{border-bottom:1px solid #ddd;padding-bottom:.3rem}h3{margin-bottom:.2rem}ul{margin-top:.2rem}li{margin:.15rem 0}"
           "a{color:#0b5fff}summary{cursor:pointer;color:#666}@media(prefers-color-scheme:dark){body{background:#111;color:#eee}a{color:#7ab0ff}h1,h2{border-color:#333}}")
    return f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{css}</style></head><body>{''.join(body)}</body></html>"


def run(cfg: dict[str, Any], conn: sqlite3.Connection, since: datetime | None = None, until: datetime | None = None,
        fmt: str = "md", out: str | None = None) -> list[Path]:
    until = until or datetime.now(timezone.utc)
    since = since or until - timedelta(days=7)
    md = build_markdown(cfg, conn, since, until)
    reports_dir = Path(cfg["general"]["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    iso = until.isocalendar()
    stem = Path(out).with_suffix("") if out else reports_dir / f"{iso.year}-W{iso.week:02d}"
    written = []
    if fmt in ("md", "both"):
        p = stem.with_suffix(".md")
        p.write_text(md, encoding="utf-8")
        written.append(p)
    if fmt in ("html", "both"):
        p = stem.with_suffix(".html")
        p.write_text(markdown_to_html(md, f"arXiv founder scout {iso.year}-W{iso.week:02d}"), encoding="utf-8")
        written.append(p)
    return written
