"""Pluggable founder/company sources. Each adapter exposes `fetch(cfg, conn) -> Iterable[CompanyRecord]`.

To add a source: create a module in this package with a `fetch` function and add its name to
`[founders] enabled` in config.toml.
"""
from __future__ import annotations

import importlib
import logging
import sqlite3
from typing import Any

from .. import db
from .base import CompanyRecord, upsert_company

log = logging.getLogger(__name__)


def run(cfg: dict[str, Any], conn: sqlite3.Connection, only: list[str] | None = None) -> dict[str, dict[str, int]]:
    names = only or cfg["founders"].get("enabled", [])
    results: dict[str, dict[str, int]] = {}
    for name in names:
        mod = importlib.import_module(f"{__name__}.{name}")
        run_id = db.start_run(conn, f"founders:{name}", {})
        stats = {"companies": 0, "people": 0}
        log.info("founders source %s: fetching", name)
        for rec in mod.fetch(cfg, conn):
            n_people = upsert_company(conn, rec)
            stats["companies"] += 1
            stats["people"] += n_people
            if stats["companies"] % 500 == 0:
                conn.commit()
        conn.commit()
        db.finish_run(conn, run_id, stats)
        results[name] = stats
    return results


__all__ = ["CompanyRecord", "run", "upsert_company"]
