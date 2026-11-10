import sqlite3
from pathlib import Path

import pytest

from scout import db
from scout.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg():
    c = load_config()
    c["general"]["secrets"] = {"anthropic": "", "openalex": "", "semantic_scholar": "", "sec_user_agent": ""}
    return c


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    return db.connect(tmp_path / "test.sqlite")


@pytest.fixture
def feed_xml() -> str:
    return (FIXTURES / "arxiv_feed.xml").read_text(encoding="utf-8")


@pytest.fixture
def feed_xml_v2() -> str:
    return (FIXTURES / "arxiv_feed_v2.xml").read_text(encoding="utf-8")
