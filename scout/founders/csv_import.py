"""CSV import for Crunchbase / PitchBook exports or hand-made lists. Column names are mapped in config."""
from __future__ import annotations

import csv
import glob
import hashlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from ..config import PROJECT_ROOT
from .base import CompanyRecord, PersonRecord, founded_within, new_startup_cutoff

log = logging.getLogger(__name__)


def _pick(row: dict[str, str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in row and row[c] not in (None, ""):
            return row[c].strip()
    return None


def _parse_date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    if re.fullmatch(r"\d{4}", s):
        return s
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m", "%b %Y", "%B %Y"):
        try:
            from datetime import datetime
            d = datetime.strptime(s, fmt)
            return d.date().isoformat() if "%d" in fmt else f"{d.year:04d}-{d.month:02d}-01"
        except ValueError:
            pass
    m = re.search(r"(19|20)\d{2}", s)
    return m.group(0) if m else None


def _parse_money(s: str | None) -> float | None:
    if not s:
        return None
    digits = re.sub(r"[^\d.]", "", s)
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def parse_file(path: Path, cfg: dict[str, Any], cutoff) -> Iterator[CompanyRecord]:
    ccfg = cfg["founders"]["csv_import"]
    cols = ccfg["columns"]
    sep = ccfg.get("founders_separator", ";")
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = _pick(row, cols["name"])
            if not name:
                continue
            founded = _parse_date(_pick(row, cols.get("founded_date", [])))
            if founded and not founded_within(founded, cutoff):
                continue
            founders_raw = _pick(row, cols.get("founders", [])) or ""
            people = [PersonRecord(full_name=n.strip(), role="Founder") for n in re.split(rf"\s*{re.escape(sep)}\s*|\s*,\s*(?=[A-Z])", founders_raw) if n.strip()]
            website = _pick(row, cols.get("website", []))
            ref = website or hashlib.sha1(f"{path.name}:{name}".encode()).hexdigest()[:16]
            yield CompanyRecord(
                source="csv_import", source_ref=ref.lower(), name=name,
                description=_pick(row, cols.get("description", [])), website=website,
                location=_pick(row, cols.get("location", [])), founded_date=founded,
                industry=_pick(row, cols.get("industry", [])), raised_usd=_parse_money(_pick(row, cols.get("raised_usd", []))),
                people=people, raw={"file": path.name},
            )


def fetch(cfg: dict[str, Any], conn: sqlite3.Connection) -> Iterator[CompanyRecord]:
    cutoff = new_startup_cutoff(int(cfg["general"].get("new_startup_months", 24)))
    for pattern in cfg["founders"]["csv_import"].get("paths", []):
        pat = pattern if Path(pattern).is_absolute() else str(PROJECT_ROOT / pattern)
        files = sorted(glob.glob(pat))
        if not files:
            log.info("csv_import: no files match %s", pattern)
        for fp in files:
            log.info("csv_import: reading %s", fp)
            yield from parse_file(Path(fp), cfg, cutoff)
