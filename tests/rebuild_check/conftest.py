from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "rebuild_check"


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as conn:
        conn.executescript((EXAMPLES / "ledger.sql").read_text())
    return path


@pytest.fixture
def sql_file(tmp_path):
    def write(sql, name="migration.sql"):
        path = tmp_path / name
        path.write_text(sql, encoding="utf-8")
        return path
    return write
