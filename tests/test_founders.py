from datetime import date
from pathlib import Path

from scout.founders import upsert_company
from scout.founders.base import founded_within, new_startup_cutoff, normalize_company_name
from scout.founders.csv_import import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_cutoff_and_founded_within():
    assert new_startup_cutoff(24, today=date(2026, 11, 9)) == date(2024, 11, 9)
    cutoff = date(2024, 11, 9)
    assert founded_within("2025-03-01", cutoff) and founded_within("2024", cutoff)
    assert not founded_within("2024-06-01", cutoff) and not founded_within("2015-01-01", cutoff)
    assert not founded_within(None, cutoff)


def test_normalize_company_name():
    assert normalize_company_name("Acme Robotics, Inc.") == "acme robotics"
    assert normalize_company_name("Edge Kernels LLC") == "edge kernels"


def test_csv_import_filters_old_companies_and_splits_founders(cfg, conn):
    recs = list(parse_file(FIXTURES / "companies.csv", cfg, date(2024, 11, 9)))
    names = [r.name for r in recs]
    assert "Old Corp" not in names and len(recs) == 3
    acme = next(r for r in recs if r.name == "Acme Robotics")
    assert [p.full_name for p in acme.people] == ["Jose Garcia Lopez", "Priya Natarajan"]
    assert acme.raised_usd == 4_000_000 and acme.founded_date == "2025-03-01"
    for r in recs:
        upsert_company(conn, r)
    for r in recs:  # second import must not duplicate
        upsert_company(conn, r)
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 4
