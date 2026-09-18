from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_json(path: str) -> dict[str, Any]:
    return json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))


def load_yaml(path: str) -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> dict[str, str]:
    database_path = tmp_path / "transfer-test.sqlite3"
    return {
        "database_path": str(database_path),
        "database_url": f"sqlite+aiosqlite:///{database_path}",
    }