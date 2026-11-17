from pathlib import Path
import sqlite3

import pytest

pytest.importorskip("sqlite_utils", reason="rebuild-check needs the 'sqlite' extra")
from sqlite_utils import Database

from datawrench.rebuild_check import check_migration

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def chinook(tmp_path):
    path = tmp_path / "chinook.db"
    with sqlite3.connect(path) as conn:
        conn.executescript((FIXTURES / "chinook.sql").read_text(encoding="utf-8-sig"))
    return path


def test_chinook_upstream_full_database_baseline(chinook, sql_file):
    report = check_migration(chinook, [sql_file("SELECT 1;")], temp_dir=chinook.parent)
    assert report["exit_code"] == 0, report["findings"]
    assert len(report["before"]["tables"]) == 11
    assert report["before"]["tables"]["Track"]["row_count"] == 3503
    assert report["before"]["tables"]["PlaylistTrack"]["row_count"] == 8715


def test_sqlite_utils_rebuild_on_chinook(chinook, sql_file):
    db = Database(chinook)
    sql = db["Track"].transform_sql(types={"Milliseconds": float}, tmp_suffix="fixture")
    db.close()
    migration = sql_file("PRAGMA foreign_keys=OFF; BEGIN;\n" + ";\n".join(s.rstrip(";") for s in sql) + ";\nCOMMIT;")
    report = check_migration(chinook, [migration], temp_dir=chinook.parent)
    assert report["exit_code"] == 0, report["findings"]
    assert report["after"]["tables"]["Track"]["row_count"] == 3503
    assert report["validation"]["after"]["foreign_keys"] == []


def test_chinook_child_orphans_with_untouched_row_counts(chinook, sql_file):
    migration = sql_file("PRAGMA foreign_keys=OFF; UPDATE Track SET AlbumId=999999 WHERE TrackId=1;")
    report = check_migration(chinook, [migration], temp_dir=chinook.parent)
    assert report["exit_code"] == 1
    assert any(f["code"] == "foreign_key_violation" and f["object"] == "table:Track" for f in report["findings"])
    assert all(f["code"] != "row_count_changed" for f in report["findings"])


def test_django_real_generated_rebuild_loses_audit_trigger(tmp_path, sql_file):
    db = tmp_path / "django.db"
    # This is the schema emitted by the matching Django model before alter_field.
    with sqlite3.connect(db) as conn:
        conn.executescript('''CREATE TABLE "accounts" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "email" text NOT NULL UNIQUE, "balance" integer NOT NULL);
        CREATE TABLE audit(account_id INTEGER,event TEXT);
        CREATE INDEX "balance_idx" ON "accounts" ("balance") WHERE "balance" > 0;
        CREATE TRIGGER account_audit AFTER UPDATE OF balance ON accounts BEGIN INSERT INTO audit VALUES(new.id,'changed'); END;
        INSERT INTO accounts(email,balance) VALUES('a@example.test',100);''')
    report = check_migration(db, [sql_file("PRAGMA foreign_keys=OFF;\n" + (FIXTURES / "django-5.2-rebuild.sql").read_text())], temp_dir=tmp_path)
    assert report["exit_code"] == 1
    assert any(f["code"] == "object_missing" and f["object"] == "trigger:account_audit" for f in report["findings"])
    assert report["after"]["tables"]["accounts"]["row_count"] == 1


def test_sqlite_utils_generated_fts_and_count_triggers(tmp_path, sql_file):
    path = tmp_path / "fts.db"
    db = Database(path)
    db["documents"].insert_all([{"id": 1, "title": "SQLite rebuild"}, {"id": 2, "title": "Another document"}], pk="id")
    db["documents"].enable_fts(["title"], create_triggers=True, fts_version="FTS5")
    db["documents"].enable_counts()
    db.close()
    report = check_migration(path, [sql_file("SELECT 1;")], temp_dir=tmp_path)
    assert report["exit_code"] == 0, report["findings"]
    assert any(t["kind"] == "virtual" for t in report["before"]["tables"].values())


def test_real_sqlite_utils_transform_loses_deferred_timing(tmp_path, sql_file):
    path = tmp_path / "deferred.db"
    db = Database(path)
    db.conn.executescript("CREATE TABLE p(id INTEGER PRIMARY KEY); INSERT INTO p VALUES(1); CREATE TABLE c(x INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED,n INTEGER); INSERT INTO c VALUES(1,10);")
    sql = db["c"].transform_sql(types={"n": float}, tmp_suffix="timing")
    db.close()
    migration = sql_file("PRAGMA foreign_keys=OFF; BEGIN;\n" + ";\n".join(s.rstrip(";") for s in sql) + ";\nCOMMIT;")
    report = check_migration(path, [migration], temp_dir=tmp_path)
    assert report["exit_code"] == 1
    assert [f["code"] for f in report["findings"]] == ["foreign_key_timing_changed"]


def test_real_sqlite_utils_transform_preserves_empty_table_sequence(tmp_path, sql_file):
    path = tmp_path / "sequence.db"
    db = Database(path)
    db.conn.executescript("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT,n INTEGER); INSERT INTO t VALUES(100,10); DELETE FROM t;")
    sql = db["t"].transform_sql(types={"n": float}, tmp_suffix="sequence")
    db.close()
    migration = sql_file("PRAGMA foreign_keys=OFF; BEGIN;\n" + ";\n".join(s.rstrip(";") for s in sql) + ";\nCOMMIT;")
    report = check_migration(path, [migration], temp_dir=tmp_path)
    assert report["exit_code"] == 0, report["findings"]
    assert report["after"]["tables"]["t"]["sequence"] == 100
