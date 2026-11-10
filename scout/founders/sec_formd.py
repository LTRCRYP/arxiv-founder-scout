"""SEC Form D quarterly data sets -> companies + related persons (executive officers, directors, promoters).

Source: https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets (quarterly ZIPs of TSVs).
Access rules: max 10 requests/second and a declared User-Agent of the form "Name email" (SEC_USER_AGENT in .env).
"""
from __future__ import annotations

import csv
import io
import logging
import re
import sqlite3
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .. import db
from ..http import Client
from .base import CompanyRecord, PersonRecord, founded_within, new_startup_cutoff

log = logging.getLogger(__name__)

INDEX_URL = "https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets"
ZIP_RE = re.compile(r'href="(/files/[^"]+/form-d-data-sets/(\d{4})q([1-4])_d(?:_\d)?\.zip)"', re.I)


def quarters_since(cutoff: date, today: date | None = None) -> list[tuple[int, int]]:
    today = today or datetime.now(timezone.utc).date()
    out = []
    y, q = cutoff.year, (cutoff.month - 1) // 3 + 1
    while (y, q) <= (today.year, (today.month - 1) // 3 + 1):
        out.append((y, q))
        q += 1
        if q == 5:
            y, q = y + 1, 1
    return out


def discover_zip_urls(client: Client) -> dict[tuple[int, int], str]:
    html = client.get_text(INDEX_URL)
    found: dict[tuple[int, int], str] = {}
    for m in ZIP_RE.finditer(html):
        key = (int(m.group(2)), int(m.group(3)))
        found.setdefault(key, "https://www.sec.gov" + m.group(1))
    return found


def _read_tsv(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    member = next((n for n in zf.namelist() if n.upper().endswith(f"/{name}.TSV") or n.upper() == f"{name}.TSV"), None)
    if member is None:
        return iter(())
    with zf.open(member) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        csv.field_size_limit(10**8)
        yield from csv.DictReader(text, delimiter="\t")


def parse_quarter(zip_path: Path, cfg: dict[str, Any], cutoff: date) -> Iterable[CompanyRecord]:
    fcfg = cfg["founders"]["sec_formd"]
    industries = set(fcfg.get("industry_groups", []))
    entity_types = set(fcfg.get("entity_types", []))
    roles = set(fcfg.get("roles", []))
    excl = [re.compile(p, re.I) for p in fcfg.get("exclude_name_patterns", [])]

    with zipfile.ZipFile(zip_path) as zf:
        offering = {r["ACCESSIONNUMBER"]: r for r in _read_tsv(zf, "OFFERING")}
        submission = {r["ACCESSIONNUMBER"]: r for r in _read_tsv(zf, "FORMDSUBMISSION")}
        issuers_by_acc: dict[str, dict[str, str]] = {}
        for r in _read_tsv(zf, "ISSUERS"):
            if r.get("IS_PRIMARYISSUER_FLAG", "").upper() == "YES":
                issuers_by_acc[r["ACCESSIONNUMBER"]] = r
        persons: dict[str, list[dict[str, str]]] = {}
        for r in _read_tsv(zf, "RELATEDPERSONS"):
            persons.setdefault(r["ACCESSIONNUMBER"], []).append(r)

    for acc, iss in issuers_by_acc.items():
        off = offering.get(acc, {})
        sub = submission.get(acc, {})
        if off.get("INDUSTRYGROUPTYPE") not in industries:
            continue
        if off.get("ISPOOLEDINVESTMENTFUNDTYPE", "").lower() == "true":
            continue
        if entity_types and iss.get("ENTITYTYPE") not in entity_types:
            continue
        name = (iss.get("ENTITYNAME") or "").strip()
        if not name or any(p.search(name) for p in excl):
            continue
        year = (iss.get("YEAROFINC_VALUE_ENTERED") or "").strip()
        founded = year if re.fullmatch(r"\d{4}", year) else None
        if not founded_within(founded, cutoff):
            continue
        filing_date = (sub.get("FILING_DATE") or "").strip()
        try:
            filed_iso = datetime.strptime(filing_date, "%d-%b-%Y").date().isoformat()
        except ValueError:
            filed_iso = None
        people = []
        for p in persons.get(acc, []):
            rel = {p.get("RELATIONSHIP_1"), p.get("RELATIONSHIP_2"), p.get("RELATIONSHIP_3")} & roles
            if not rel:
                continue
            full = " ".join(x for x in (p.get("FIRSTNAME"), p.get("MIDDLENAME"), p.get("LASTNAME")) if x and x.strip())
            role = ", ".join(sorted(rel))
            if p.get("RELATIONSHIPCLARIFICATION"):
                role += f" ({p['RELATIONSHIPCLARIFICATION'].strip()[:80]})"
            loc = ", ".join(x for x in (p.get("CITY"), p.get("STATEORCOUNTRY")) if x)
            people.append(PersonRecord(full_name=full, role=role, location=loc or None))
        amount = off.get("TOTALAMOUNTSOLD")
        try:
            raised = float(amount) if amount else None
        except ValueError:
            raised = None
        yield CompanyRecord(
            source="sec_formd",
            source_ref=iss.get("CIK") or acc,
            name=name,
            location=", ".join(x for x in (iss.get("CITY"), iss.get("STATEORCOUNTRY")) if x) or None,
            founded_date=founded,
            industry=off.get("INDUSTRYGROUPTYPE"),
            raised_usd=raised,
            first_filed_at=filed_iso,
            people=people,
            raw={"accession": acc, "entity_type": iss.get("ENTITYTYPE"), "jurisdiction": iss.get("JURISDICTIONOFINC"),
                 "exemptions": off.get("FEDERALEXEMPTIONS_ITEMS_LIST"), "is_amendment": off.get("ISAMENDMENT"),
                 "total_offering": off.get("TOTALOFFERINGAMOUNT"), "sic": sub.get("SIC_CODE")},
        )


def fetch(cfg: dict[str, Any], conn: sqlite3.Connection) -> Iterator[CompanyRecord]:
    ua = cfg["general"]["secrets"].get("sec_user_agent") or ""
    cutoff = new_startup_cutoff(int(cfg["general"].get("new_startup_months", 24)))
    data_dir = Path(cfg["general"]["data_dir"]) / "formd"
    data_dir.mkdir(parents=True, exist_ok=True)
    online = bool(re.search(r"\S+@\S+", ua))
    if online:
        client = Client(ua, min_interval=0.15, timeout=300)
        urls = discover_zip_urls(client)
    else:
        log.warning("SEC_USER_AGENT not set in .env ('Name email@example.com' is required by SEC EDGAR); "
                    "using cached Form D zips in %s only", data_dir)
        urls = {}
    for (y, q) in quarters_since(cutoff):
        url = urls.get((y, q))
        zip_path = data_dir / f"{y}q{q}_d.zip"
        if not url:
            if not zip_path.exists():
                log.info("Form D %dQ%d not available (not published yet, or offline)", y, q)
                continue
        elif not zip_path.exists() or db.get_state(conn, "sec_formd", f"{y}q{q}") != url:
            # Re-download when the SEC changes the file URL (they re-publish corrected quarters).
            log.info("downloading %s", url)
            resp = client.get(url, stream=True)
            resp.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    f.write(chunk)
            db.set_state(conn, "sec_formd", f"{y}q{q}", url)
        n = 0
        for rec in parse_quarter(zip_path, cfg, cutoff):
            n += 1
            yield rec
        log.info("Form D %dQ%d: %d qualifying companies", y, q, n)
