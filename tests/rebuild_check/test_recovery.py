import json
from pathlib import Path
import sqlite3
import time

import pytest

pytest.importorskip("sqlite_utils", reason="rebuild-check needs the 'sqlite' extra")

from datawrench.rebuild_check import check_migration
from datawrench.rebuild_check.cli import main
import datawrench.rebuild_check.core as core


def test_pathological_literal_honors_cpu_deadline(ledger, sql_file):
    migration = sql_file("SELECT '" + ";" * 100000 + "';")
    start = time.monotonic()
    report = check_migration(ledger, [migration], timeout=0.05, temp_dir=ledger.parent)
    assert report["exit_code"] == 2
    assert any(f["code"] == "timeout" for f in report["findings"])
    assert time.monotonic() - start < 1


def test_exclusive_lock_backup_is_bounded_and_cleaned(ledger, sql_file):
    conn = sqlite3.connect(ledger)
    conn.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        report = check_migration(ledger, [sql_file("SELECT 1;")], timeout=0.15, temp_dir=ledger.parent)
        assert report["exit_code"] == 2
        assert report["findings"][-1]["code"] == "timeout"
        assert time.monotonic() - start < 2
        assert not list(ledger.parent.glob("rebuild-check-*"))
    finally:
        conn.rollback()
        conn.close()


def test_wal_uncommitted_writes_are_not_in_snapshot(tmp_path, sql_file):
    path = tmp_path / "concurrent.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE x(a)")
    conn.execute("INSERT INTO x VALUES(1)")
    conn.commit()
    conn.execute("INSERT INTO x VALUES(2)")
    try:
        report = check_migration(path, [sql_file("SELECT 1;")], temp_dir=tmp_path)
        assert report["exit_code"] == 0, report["findings"]
        assert report["before"]["tables"]["x"]["row_count"] == 1
        assert conn.execute("SELECT count(*) FROM x").fetchone()[0] == 2
    finally:
        conn.rollback()
        conn.close()


def test_keyboard_interrupt_cleans_disposable_database(ledger, sql_file, monkeypatch):
    def interrupted(stream, deadline):
        yield 1, "CREATE TABLE partial(a);"
        raise KeyboardInterrupt
    monkeypatch.setattr(core, "statements", interrupted)
    with pytest.raises(KeyboardInterrupt):
        check_migration(ledger, [sql_file("SELECT 1;")], temp_dir=ledger.parent)
    assert not list(ledger.parent.glob("rebuild-check-*"))
    with sqlite3.connect(ledger) as conn:
        assert not conn.execute("SELECT name FROM sqlite_schema WHERE name='partial'").fetchall()


@pytest.mark.parametrize("content", [b"not sqlite", bytes(range(256)), b"SQLite format 3\x00" + b"x"*512])
def test_non_sqlite_and_corrupt_files_return_structured_errors(tmp_path, sql_file, content):
    path = tmp_path / "bad.db"
    path.write_bytes(content)
    report = check_migration(path, [sql_file("SELECT 1;")], temp_dir=tmp_path)
    assert report["exit_code"] == 2
    assert report["findings"][-1]["code"] in {"backup_error", "baseline_error"}
    assert path.read_bytes() == content


def test_invalid_utf8_sql_and_oversized_policy(ledger, sql_file, tmp_path, capsys):
    migration = sql_file("SELECT 1;")
    migration.write_bytes(b"SELECT 1;\xff")
    report = check_migration(ledger, [migration], temp_dir=tmp_path)
    assert report["exit_code"] == 2
    policy = tmp_path / "policy.json"
    policy.write_text(" " * (1024*1024 + 1))
    code = main([str(ledger), str(migration), "--format", "json", "--policy", str(policy)])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["findings"][0]["code"] == "policy_error"


def test_disabled_check_constraints_cannot_hide_bad_data(ledger, sql_file):
    migration = sql_file("CREATE TABLE x(a CHECK(a>0)); PRAGMA ignore_check_constraints=ON; INSERT INTO x VALUES(-1);")
    report = check_migration(ledger, [migration], temp_dir=ledger.parent)
    assert report["exit_code"] == 1
    assert any(f["code"] == "integrity_failed" for f in report["findings"])
