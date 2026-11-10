"""Y Combinator companies via the third-party yc-oss mirror. DISABLED by default (see config.toml).

ycombinator.com's terms of use prohibit scraping and data mining, so this tool never touches ycombinator.com.
The mirror carries company-level fields only (no founder names), so it contributes companies for the
"research area vs company" and co-author signals but cannot produce name matches on its own.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator

from ..http import Client
from .base import CompanyRecord, founded_within, new_startup_cutoff

log = logging.getLogger(__name__)


def fetch(cfg: dict[str, Any], conn: sqlite3.Connection) -> Iterator[CompanyRecord]:
    url = cfg["founders"]["yc_oss"]["url"]
    cutoff = new_startup_cutoff(int(cfg["general"].get("new_startup_months", 24)))
    client = Client(cfg["general"]["user_agent"], min_interval=1.0)
    for c in client.get_json(url) or []:
        launched = c.get("launched_at")
        founded = datetime.fromtimestamp(launched, tz=timezone.utc).date().isoformat() if launched else None
        if not founded_within(founded, cutoff):
            continue
        yield CompanyRecord(
            source="yc_oss", source_ref=str(c.get("slug") or c.get("id")), name=c.get("name", ""),
            description=c.get("long_description") or c.get("one_liner"), website=c.get("website"),
            location=c.get("all_locations"), founded_date=founded, industry=c.get("industry"),
            raw={"batch": c.get("batch"), "tags": c.get("tags"), "status": c.get("status")},
        )
