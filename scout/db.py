from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  arxiv_id TEXT PRIMARY KEY,
  version INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL,
  abstract TEXT NOT NULL,
  primary_category TEXT,
  categories TEXT NOT NULL,          -- JSON list
  submitted_at TEXT NOT NULL,        -- ISO8601 UTC
  updated_at TEXT,
  abs_url TEXT,
  pdf_url TEXT,
  code_url TEXT,
  comment TEXT,
  journal_ref TEXT,
  doi TEXT,
  first_seen_run INTEGER,
  last_seen_run INTEGER
);
CREATE INDEX IF NOT EXISTS idx_papers_submitted ON papers(submitted_at);

CREATE TABLE IF NOT EXISTS authors (
  author_id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  s2_id TEXT,
  openalex_id TEXT,
  orcid TEXT,
  homepage TEXT,
  affiliations TEXT,                 -- JSON list of strings
  citation_count INTEGER,
  h_index INTEGER,
  works_count INTEGER,
  enrich_method TEXT,                -- 'paper' (disambiguated via the paper) or 'name'
  enriched_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_authors_norm ON authors(normalized_name);
CREATE INDEX IF NOT EXISTS idx_authors_s2 ON authors(s2_id);

CREATE TABLE IF NOT EXISTS paper_authors (
  arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
  author_id INTEGER NOT NULL REFERENCES authors(author_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  affiliation TEXT,                  -- as given by arXiv for this paper, usually empty
  PRIMARY KEY (arxiv_id, author_id)
);
CREATE INDEX IF NOT EXISTS idx_pa_author ON paper_authors(author_id);

CREATE TABLE IF NOT EXISTS paper_scores (
  arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
  criterion TEXT NOT NULL,           -- topic_fit, novelty, commercial_applicability, code_released, author_track_record, composite
  score REAL NOT NULL,               -- 0-10 per criterion, 0-100 for composite
  reason TEXT,
  model TEXT,
  scored_at TEXT NOT NULL,
  PRIMARY KEY (arxiv_id, criterion)
);

CREATE TABLE IF NOT EXISTS companies (
  company_id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_ref TEXT NOT NULL,          -- CIK, CSV row key, YC slug ...
  name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  description TEXT,
  website TEXT,
  location TEXT,
  founded_date TEXT,                 -- ISO date or YYYY
  industry TEXT,
  raised_usd REAL,
  first_filed_at TEXT,
  raw TEXT,                          -- JSON blob of source fields
  updated_at TEXT NOT NULL,
  UNIQUE (source, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_companies_norm ON companies(normalized_name);

CREATE TABLE IF NOT EXISTS people (
  person_id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
  full_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  last_name_key TEXT NOT NULL,
  role TEXT,
  location TEXT,
  source TEXT NOT NULL,
  UNIQUE (company_id, normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_people_last ON people(last_name_key);

CREATE TABLE IF NOT EXISTS matches (
  match_id INTEGER PRIMARY KEY,
  author_id INTEGER NOT NULL REFERENCES authors(author_id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
  confidence REAL NOT NULL,
  tier TEXT NOT NULL,                -- confirmed, probable, weak
  evidence TEXT NOT NULL,            -- JSON list of {signal, value, detail}
  reviewed TEXT,                     -- NULL, 'accepted', 'rejected'
  created_at TEXT NOT NULL,
  UNIQUE (author_id, person_id)
);

CREATE TABLE IF NOT EXISTS name_frequency (
  normalized_name TEXT PRIMARY KEY,
  s2_author_count INTEGER,
  checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY,
  stage TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  params TEXT,
  stats TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
  source TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (source, key)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def start_run(conn: sqlite3.Connection, stage: str, params: dict[str, Any] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO runs(stage, started_at, params) VALUES (?, ?, ?)",
        (stage, utcnow(), json.dumps(params or {}, default=str)),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, stats: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, stats=? WHERE run_id=?",
        (utcnow(), json.dumps(stats, default=str), run_id),
    )
    conn.commit()


def get_state(conn: sqlite3.Connection, source: str, key: str) -> str | None:
    row = conn.execute("SELECT value FROM source_state WHERE source=? AND key=?", (source, key)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, source: str, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO source_state(source, key, value, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(source, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (source, key, value, utcnow()),
    )
