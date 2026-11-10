from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.toml and .env. Relative paths in the config are resolved against the project root."""
    load_dotenv(PROJECT_ROOT / ".env")
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.toml"
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    g = cfg.setdefault("general", {})
    for key in ("db_path", "data_dir", "reports_dir"):
        g[key] = str((PROJECT_ROOT / g.get(key, key)).resolve())
    g["secrets"] = {
        "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
        "openalex": os.environ.get("OPENALEX_API_KEY", ""),
        "semantic_scholar": os.environ.get("SEMANTIC_SCHOLAR_API_KEY", ""),
        "sec_user_agent": os.environ.get("SEC_USER_AGENT", ""),
    }
    return cfg
