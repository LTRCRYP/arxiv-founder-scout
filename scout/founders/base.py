from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .. import db
from ..names import last_name_key, normalize_name


@dataclass
class PersonRecord:
    full_name: str
    role: str | None = None
    location: str | None = None


@dataclass
class CompanyRecord:
    source: str
    source_ref: str
    name: str
    description: str | None = None
    website: str | None = None
    location: str | None = None
    founded_date: str | None = None      # 'YYYY-MM-DD' or 'YYYY'
    industry: str | None = None
    raised_usd: float | None = None
    first_filed_at: str | None = None
    people: list[PersonRecord] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def normalize_company_name(name: str) -> str:
    n = normalize_name(name)
    for suffix in (" incorporated", " inc", " corporation", " corp", " llc", " ltd", " limited", " co", " pbc", " plc", " gmbh", " sas"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n.strip()


def new_startup_cutoff(months: int, today: date | None = None) -> date:
    today = today or datetime.now(timezone.utc).date()
    y, m = today.year, today.month - months
    while m <= 0:
        y -= 1
        m += 12
    return date(y, m, min(today.day, 28))


def founded_within(founded: str | None, cutoff: date) -> bool:
    """True if the founded date (YYYY or YYYY-MM-DD) is on/after the cutoff. Year-only values pass if the
    year is >= cutoff year (we cannot know the month, so we err towards inclusion)."""
    if not founded:
        return False
    try:
        if len(founded) == 4:
            return int(founded) >= cutoff.year
        return date.fromisoformat(founded[:10]) >= cutoff
    except ValueError:
        return False


def upsert_company(conn: sqlite3.Connection, rec: CompanyRecord) -> int:
    conn.execute(
        """INSERT INTO companies(source, source_ref, name, normalized_name, description, website, location, founded_date,
           industry, raised_usd, first_filed_at, raw, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source, source_ref) DO UPDATE SET name=excluded.name, normalized_name=excluded.normalized_name,
           description=COALESCE(excluded.description, companies.description), website=COALESCE(excluded.website, companies.website),
           location=COALESCE(excluded.location, companies.location), founded_date=COALESCE(excluded.founded_date, companies.founded_date),
           industry=COALESCE(excluded.industry, companies.industry), raised_usd=COALESCE(excluded.raised_usd, companies.raised_usd),
           first_filed_at=COALESCE(MIN(excluded.first_filed_at, companies.first_filed_at), excluded.first_filed_at, companies.first_filed_at),
           raw=excluded.raw, updated_at=excluded.updated_at""",
        (rec.source, rec.source_ref, rec.name, normalize_company_name(rec.name), rec.description, rec.website, rec.location,
         rec.founded_date, rec.industry, rec.raised_usd, rec.first_filed_at, json.dumps(rec.raw, default=str), db.utcnow()))
    company_id = conn.execute("SELECT company_id FROM companies WHERE source=? AND source_ref=?", (rec.source, rec.source_ref)).fetchone()[0]
    n = 0
    for p in rec.people:
        norm = normalize_name(p.full_name)
        if not norm or " " not in norm:
            continue  # need at least a given name and a last name to match on
        conn.execute(
            """INSERT INTO people(company_id, full_name, normalized_name, last_name_key, role, location, source) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(company_id, normalized_name) DO UPDATE SET role=COALESCE(excluded.role, people.role),
               location=COALESCE(excluded.location, people.location)""",
            (company_id, p.full_name, norm, last_name_key(norm), p.role, p.location, rec.source))
        n += 1
    return n
