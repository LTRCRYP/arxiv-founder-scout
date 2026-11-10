"""Stage 5: cross-reference paper authors with founders/executives using several weak signals combined into a
confidence score with recorded evidence. Names alone never produce a 'confirmed' match."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from . import db
from .http import Client
from .names import compare_names, fuzzy_ratio, normalize_name

log = logging.getLogger(__name__)

US_STATES = {"al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming", "dc": "washington dc"}
STOP = set("the and for with from that this into over under using based towards toward learning model models method methods approach approaches paper propose proposed results show data new via our we are can which their these those than more most also both between while where when what about through within without".split())


def domain(url: str | None) -> str | None:
    if not url:
        return None
    u = url if "://" in url else "http://" + url
    host = (urlparse(u).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or None


def tokens(text: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z][a-z0-9\-]{3,}", (text or "").lower()) if t not in STOP}


def location_terms(loc: str | None) -> set[str]:
    """'Palo Alto, CA' -> {'palo alto', 'california'}"""
    out: set[str] = set()
    if not loc:
        return out
    for part in loc.split(","):
        p = part.strip().lower()
        if not p:
            continue
        if p in US_STATES:
            out.add(US_STATES[p])
        elif len(p) > 3:
            out.add(p)
    return out


class Matcher:
    def __init__(self, cfg: dict[str, Any], conn: sqlite3.Connection, use_network: bool = True):
        self.cfg = cfg
        self.mcfg = cfg["match"]
        self.w = cfg["match"]["signals"]
        self.conn = conn
        self.use_network = use_network
        ua = cfg["general"]["user_agent"]
        secrets = cfg["general"]["secrets"]
        # Fail fast here: a missing frequency check is recorded as evidence rather than retried for minutes.
        self.s2 = Client(ua, min_interval=1.0, max_retries=1, headers={"x-api-key": secrets["semantic_scholar"]} if secrets["semantic_scholar"] else None)
        self.orcid = Client(ua, min_interval=0.1, headers={"Accept": "application/json"})
        # name -> set of company_ids for people, used by the co-author signal
        self.people_by_name: dict[str, set[int]] = {}
        for r in conn.execute("SELECT normalized_name, company_id FROM people"):
            self.people_by_name.setdefault(r["normalized_name"], set()).add(r["company_id"])

    # ---- external lookups (cached in the db) -------------------------------------------------
    def s2_author_count(self, display_name: str) -> int | None:
        norm = normalize_name(display_name)
        row = self.conn.execute("SELECT s2_author_count FROM name_frequency WHERE normalized_name=?", (norm,)).fetchone()
        if row:
            return row["s2_author_count"]
        if not self.use_network:
            return None
        try:
            data = self.s2.get_json("https://api.semanticscholar.org/graph/v1/author/search", params={"query": display_name, "fields": "name", "limit": 100})
            count = sum(1 for a in (data or {}).get("data", []) if normalize_name(a.get("name", "")) == norm)
        except Exception as e:
            log.debug("s2 author search failed for %s: %s", display_name, e)
            return None
        self.conn.execute("INSERT OR REPLACE INTO name_frequency(normalized_name, s2_author_count, checked_at) VALUES (?,?,?)", (norm, count, db.utcnow()))
        return count

    def orcid_employers(self, orcid: str) -> list[str]:
        cached = db.get_state(self.conn, "orcid", orcid)
        if cached is not None:
            return json.loads(cached)
        if not self.use_network:
            return []
        orgs: list[str] = []
        try:
            data = self.orcid.get_json(f"https://pub.orcid.org/v3.0/{orcid}/employments")
            for group in (data or {}).get("affiliation-group", []):
                for s in group.get("summaries", []):
                    org = ((s.get("employment-summary") or {}).get("organization") or {}).get("name")
                    if org:
                        orgs.append(org)
        except Exception as e:
            log.debug("orcid lookup failed for %s: %s", orcid, e)
        db.set_state(self.conn, "orcid", orcid, json.dumps(orgs))
        return orgs

    # ---- scoring ---------------------------------------------------------------------------------
    def score_pair(self, author: sqlite3.Row, person: sqlite3.Row, company: sqlite3.Row,
                   coauthor_names: set[str], paper_text: str) -> tuple[float, list[dict[str, Any]]]:
        w = self.w
        ev: list[dict[str, Any]] = []
        kind, ratio = compare_names(author["normalized_name"], person["normalized_name"])
        if kind == "none":
            return 0.0, []
        name_w = {"exact": w["name_exact"], "fuzzy": w["name_fuzzy"], "initials": w["name_initials"]}[kind]
        ev.append({"signal": f"name_{kind}", "value": name_w, "detail": f"{author['display_name']} ~ {person['full_name']} (ratio {ratio:.2f})"})

        count = self.s2_author_count(author["display_name"])
        if count is None and self.use_network:
            ev.append({"signal": "name_frequency_unknown", "value": 0.0, "detail": "Semantic Scholar name-frequency check unavailable; treat name signal with extra caution"})
        if count is not None and count > int(self.mcfg.get("common_name_author_count", 20)):
            ev.append({"signal": "common_name_penalty", "value": w["common_name_penalty"], "detail": f"{count} Semantic Scholar authors share this exact name"})
        elif kind == "initials":
            ev.append({"signal": "ambiguous_name_penalty", "value": w["ambiguous_name_penalty"], "detail": "match relies on initials only"})

        cdom = domain(company["website"])
        adom = domain(author["homepage"])
        if cdom and adom and (cdom == adom or adom.endswith("." + cdom)):
            ev.append({"signal": "homepage_domain", "value": w["homepage_domain"], "detail": f"author homepage {adom} is on company domain {cdom}"})

        affs = json.loads(author["affiliations"] or "[]")
        cname = company["normalized_name"]
        for a in affs:
            an = normalize_name(a)
            if cname and (an == cname or (len(cname) > 4 and cname in an) or fuzzy_ratio(an, cname) >= 0.88):
                ev.append({"signal": "affiliation_company_name", "value": w["affiliation_company_name"], "detail": f"author affiliation '{a}' matches company '{company['name']}'"})
                break

        if author["orcid"]:
            for org in self.orcid_employers(author["orcid"]):
                on = normalize_name(org)
                if cname and (on == cname or cname in on or fuzzy_ratio(on, cname) >= 0.88):
                    ev.append({"signal": "orcid_employment", "value": w["orcid_employment"], "detail": f"ORCID employment '{org}' matches company"})
                    break

        for co in coauthor_names:
            if company["company_id"] in self.people_by_name.get(co, set()) and co != author["normalized_name"]:
                ev.append({"signal": "coauthor_same_company", "value": w["coauthor_same_company"], "detail": f"co-author '{co}' is also listed at {company['name']}"})
                break

        loc_terms = location_terms(person["location"]) | location_terms(company["location"])
        aff_text = " ".join(affs).lower()
        hit = next((t for t in loc_terms if t in aff_text), None)
        if hit:
            ev.append({"signal": "location_overlap", "value": w["location_overlap"], "detail": f"author affiliation mentions '{hit}'"})

        if company["description"]:
            ct, pt = tokens(company["description"]), tokens(paper_text)
            if ct and pt:
                overlap = len(ct & pt) / max(1, min(len(ct), len(pt)))
                val = round(w["area_overlap_max"] * min(1.0, overlap / 0.15), 3)
                if val > 0.01:
                    ev.append({"signal": "area_overlap", "value": val, "detail": f"shared terms: {', '.join(sorted(ct & pt)[:6])}"})
        elif (company["industry"] or "") in ("Other Technology", "Computers", "Telecommunications"):
            ev.append({"signal": "industry_tech_bonus", "value": w["industry_tech_bonus"], "detail": f"company industry group '{company['industry']}'"})

        conf = max(0.0, min(1.0, sum(e["value"] for e in ev)))
        return round(conf, 3), ev

    def tier(self, conf: float) -> str:
        if conf >= self.mcfg["confirmed_threshold"]:
            return "confirmed"
        if conf >= self.mcfg["probable_threshold"]:
            return "probable"
        return "weak"


def run(cfg: dict[str, Any], conn: sqlite3.Connection, since: datetime | None = None, rematch: bool = False,
        use_network: bool = True) -> dict[str, int]:
    m = Matcher(cfg, conn, use_network=use_network)
    run_id = db.start_run(conn, "match", {"since": since, "rematch": rematch})
    if rematch:
        # Drop stale automatic matches so pairs that no longer qualify disappear; keep manual decisions.
        conn.execute("DELETE FROM matches WHERE reviewed IS NULL")
    stats = {"people": 0, "candidate_pairs": 0, "stored": 0, "confirmed": 0, "probable": 0, "weak": 0}
    store_threshold = float(cfg["match"].get("store_threshold", 0.3))

    author_filter = ""
    params: list[Any] = []
    if since:
        author_filter = " AND a.author_id IN (SELECT pa.author_id FROM paper_authors pa JOIN papers p ON p.arxiv_id=pa.arxiv_id WHERE p.submitted_at >= ?)"
        params.append(since.isoformat())

    people = conn.execute("SELECT * FROM people").fetchall()
    companies: dict[int, sqlite3.Row] = {}
    for person in people:
        stats["people"] += 1
        company = companies.get(person["company_id"])
        if company is None:
            company = conn.execute("SELECT * FROM companies WHERE company_id=?", (person["company_id"],)).fetchone()
            companies[person["company_id"]] = company
        cands = conn.execute(
            "SELECT a.* FROM authors a WHERE a.normalized_name LIKE ? " + author_filter,
            [f"%{person['last_name_key']}"] + params).fetchall()
        for author in cands:
            kind, _ = compare_names(author["normalized_name"], person["normalized_name"])
            if kind == "none":
                continue
            stats["candidate_pairs"] += 1
            if not rematch and conn.execute("SELECT 1 FROM matches WHERE author_id=? AND person_id=?", (author["author_id"], person["person_id"])).fetchone():
                continue
            papers = conn.execute(
                "SELECT p.arxiv_id, p.title, p.abstract FROM paper_authors pa JOIN papers p ON p.arxiv_id=pa.arxiv_id WHERE pa.author_id=?",
                (author["author_id"],)).fetchall()
            coauthors = {r["normalized_name"] for r in conn.execute(
                "SELECT DISTINCT a2.normalized_name FROM paper_authors pa1 JOIN paper_authors pa2 ON pa1.arxiv_id=pa2.arxiv_id "
                "JOIN authors a2 ON a2.author_id=pa2.author_id WHERE pa1.author_id=? AND pa2.author_id!=?",
                (author["author_id"], author["author_id"]))}
            paper_text = " ".join(f"{p['title']} {p['abstract']}" for p in papers)
            conf, ev = m.score_pair(author, person, company, coauthors, paper_text)
            if conf < store_threshold:
                continue
            tier = m.tier(conf)
            conn.execute(
                "INSERT INTO matches(author_id, person_id, confidence, tier, evidence, created_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(author_id, person_id) DO UPDATE SET confidence=excluded.confidence, tier=excluded.tier, evidence=excluded.evidence",
                (author["author_id"], person["person_id"], conf, tier, json.dumps(ev), db.utcnow()))
            stats["stored"] += 1
            stats[tier] += 1
        if stats["people"] % 500 == 0:
            conn.commit()
    conn.commit()
    db.finish_run(conn, run_id, stats)
    return stats


def review(conn: sqlite3.Connection, match_id: int, decision: str) -> None:
    if decision not in ("accepted", "rejected"):
        raise ValueError("decision must be 'accepted' or 'rejected'")
    conn.execute("UPDATE matches SET reviewed=? WHERE match_id=?", (decision, match_id))
    conn.commit()
