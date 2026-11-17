from pathlib import Path
import hashlib
import io
import json
import sqlite3

import pytest

pytest.importorskip("sqlite_utils", reason="rebuild-check needs the 'sqlite' extra")
from sqlite_utils import Database

from datawrench.rebuild_check import check_migration
from datawrench.rebuild_check.cli import main

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "rebuild_check"


def codes(report):
    return {item["code"] for item in report["findings"]}


def check(ledger, migration, **kwargs):
    return check_migration(ledger, [migration], temp_dir=ledger.parent, **kwargs)


def test_realistic_rebuild_detects_lost_dependencies_and_preserves_source(ledger):
    original = hashlib.sha256(ledger.read_bytes()).hexdigest()
    report = check(ledger, EXAMPLES / "broken-rebuild.sql")
    assert report["status"] == "failed"
    assert {(f["code"], f["object"]) for f in report["findings"]} == {
        ("object_missing", "trigger:account_audit"), ("object_missing", "index:balance_idx")}
    assert report["validation"]["after"] == {"integrity": [], "foreign_keys": [], "references": []}
    assert report["before"]["tables"]["accounts"]["row_count"] == report["after"]["tables"]["accounts"]["row_count"] == 2
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == original
    assert not list(ledger.parent.glob("rebuild-check-*"))


def test_good_rebuild_passes(ledger):
    report = check(ledger, EXAMPLES / "good-rebuild.sql")
    assert report["exit_code"] == 0, report["findings"]
    assert len(report["changes"]) == 1


def test_actual_sqlite_utils_generated_sql_loses_trigger(ledger, sql_file):
    db = Database(ledger)
    statements = db["accounts"].transform_sql(types={"balance": float}, tmp_suffix="test")
    db.close()
    sql = "PRAGMA foreign_keys=OFF; BEGIN;\n" + ";\n".join(s.rstrip(";") for s in statements) + ";\nCOMMIT;"
    report = check(ledger, sql_file(sql))
    assert report["exit_code"] == 1
    assert {(f["code"], f["object"]) for f in report["findings"]} == {("object_missing", "trigger:account_audit")}


def test_actual_alembic_offline_batch(ledger, sql_file):
    sa = pytest.importorskip("sqlalchemy")
    pytest.importorskip("alembic")
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = sa.create_engine(f"sqlite:///{ledger}")
    metadata = sa.MetaData()
    table = sa.Table("accounts", metadata, autoload_with=engine)
    output = io.StringIO()
    ctx = MigrationContext.configure(dialect_name="sqlite", opts={"as_sql": True, "output_buffer": output})
    with Operations(ctx).batch_alter_table("accounts", copy_from=table, recreate="always") as batch:
        batch.alter_column("balance", type_=sa.REAL(), existing_type=sa.Integer())
    engine.dispose()
    # Alembic's exact output fails on the surviving view in modern SQLite.
    report = check(ledger, sql_file("PRAGMA foreign_keys=OFF;\n" + output.getvalue()))
    assert report["exit_code"] == 2
    assert "no such table" in report["findings"][-1]["details"]["error"]
    # Supplying the runner's legacy-rename setting allows the rebuild but loses the trigger.
    report = check(ledger, sql_file("PRAGMA foreign_keys=OFF; PRAGMA legacy_alter_table=ON;\n" + output.getvalue()))
    assert ("object_missing", "trigger:account_audit") in {(f["code"], f["object"]) for f in report["findings"]}


def test_cross_table_orphan_and_baseline(ledger, sql_file):
    report = check(ledger, sql_file("PRAGMA foreign_keys=OFF; DELETE FROM accounts WHERE id=1;"))
    assert "foreign_key_violation" in codes(report)
    violation = next(f for f in report["findings"] if f["code"] == "foreign_key_violation")
    assert violation["details"]["result"]["table"] == "entries"
    assert not violation["details"]["preexisting"]


def test_empty_child_invalid_parent_unique_key(ledger, sql_file):
    sql = """PRAGMA foreign_keys=OFF; DELETE FROM entries;
    CREATE TABLE parents(code TEXT); CREATE TABLE children(code TEXT REFERENCES parents(code));"""
    report = check(ledger, sql_file(sql))
    assert "reference_invalid" in codes(report)
    assert any("foreign key mismatch" in f["details"]["result"]["error"] for f in report["findings"] if f["code"] == "reference_invalid")


def test_missing_parent_with_empty_child(ledger, sql_file):
    report = check(ledger, sql_file("CREATE TABLE empty_child (x REFERENCES absent(id));"))
    assert "reference_invalid" in codes(report)


def test_lost_foreign_key_with_no_orphans(ledger, sql_file):
    sql = """CREATE TABLE entries_new(id INTEGER PRIMARY KEY, account_id INTEGER, amount INTEGER);
    INSERT INTO entries_new SELECT * FROM entries; DROP TABLE entries;
    ALTER TABLE entries_new RENAME TO entries;
    CREATE INDEX entries_account_idx ON entries(account_id);"""
    report = check(ledger, sql_file(sql))
    assert "foreign_key_removed" in codes(report)
    assert report["validation"]["after"]["foreign_keys"] == []


def test_bad_surviving_trigger_never_runs(ledger, sql_file):
    sql = "CREATE TRIGGER broken AFTER INSERT ON entries BEGIN INSERT INTO nonexistent VALUES(new.nope); END;"
    report = check(ledger, sql_file(sql))
    assert "reference_invalid" in codes(report)
    assert report["after"]["tables"]["entries"]["row_count"] == 1
    assert report["after"]["tables"]["audit"]["row_count"] == 0


def test_invalid_surviving_view(ledger, sql_file):
    report = check(ledger, sql_file("CREATE VIEW bad_view AS SELECT missing FROM accounts;"))
    assert report["exit_code"] == 1
    assert any(f["code"] == "reference_invalid" and f["object"] == "view:bad_view" for f in report["findings"])


def test_composite_foreign_key_reordering_is_not_a_change(ledger, sql_file):
    sql = """CREATE TABLE parents(a,b, PRIMARY KEY(a,b)) WITHOUT ROWID;
    CREATE TABLE children(x,y, FOREIGN KEY(x,y) REFERENCES parents(a,b) ON UPDATE CASCADE);
    """
    with sqlite3.connect(ledger) as conn:
        conn.executescript(sql)
    migration = """CREATE TABLE children_new(x,y, FOREIGN KEY(x,y) REFERENCES parents(a,b) ON UPDATE CASCADE);
    DROP TABLE children; ALTER TABLE children_new RENAME TO children;"""
    report = check(ledger, sql_file(migration))
    assert report["exit_code"] == 0, report["findings"]


def test_unique_check_and_table_flags(ledger, sql_file):
    with sqlite3.connect(ledger) as conn:
        conn.executescript("CREATE TABLE constrained(id TEXT PRIMARY KEY, n INTEGER UNIQUE CHECK(n>0)) STRICT, WITHOUT ROWID;")
    report = check(ledger, sql_file("DROP TABLE constrained; CREATE TABLE constrained(id TEXT PRIMARY KEY, n INTEGER);"))
    assert {"unique_removed", "check_removed", "table_option_removed"} <= codes(report)


def test_policy_is_exact_auditable_and_rejects_stale_entries(ledger, sql_file):
    migration = sql_file("DROP TRIGGER account_audit;")
    policy = {"allow": [{"code": "object_missing", "object": "trigger:account_audit", "reason": "reviewed retirement"}]}
    report = check(ledger, migration, policy=policy)
    assert report["exit_code"] == 0
    assert report["findings"][0]["accepted"]
    policy["allow"][0]["object"] = "trigger:typo"
    report = check(ledger, migration, policy=policy)
    assert "unused_policy" in codes(report) and report["exit_code"] == 1


@pytest.mark.parametrize("policy", [{}, {"allow": "all"}, {"allow": [{"code": "reference_invalid", "object": "table:x", "reason": "skip"}]}])
def test_invalid_policy(ledger, sql_file, policy):
    assert check(ledger, sql_file("SELECT 1;"), policy=policy)["exit_code"] == 2


@pytest.mark.parametrize("sql", ["BEGIN; CREATE TABLE x(a);", "THIS IS NOT SQL;", "SELECT 'unfinished;", "SELECT load_extension('foo');"])
def test_incomplete_or_invalid_migration_is_error(ledger, sql_file, sql):
    report = check(ledger, sql_file(sql))
    assert report["exit_code"] == 2
    assert report["findings"][-1]["details"]["file"].endswith("migration.sql")
    assert not list(ledger.parent.glob("rebuild-check-*"))


def test_filesystem_escape_is_denied(ledger, sql_file, tmp_path):
    escaped = tmp_path / "escaped.db"
    for sql in (f"ATTACH DATABASE '{escaped}' AS other;", f"VACUUM INTO '{escaped}';", "PRAGMA writable_schema=ON;"):
        report = check(ledger, sql_file(sql))
        assert report["exit_code"] == 2
        assert not escaped.exists()


def test_triggers_multiline_strings_comments_crlf_and_bom(ledger, sql_file):
    script = "\ufeff-- a semicolon ;\r\nBEGIN;\r\nCREATE TRIGGER another AFTER INSERT ON entries BEGIN\r\nINSERT INTO audit VALUES(new.account_id, 'one;two');\r\nSELECT 1; END;\r\nCOMMIT;\r\n-- tail"
    report = check(ledger, sql_file(script))
    assert report["exit_code"] == 0, report["findings"]


def test_multiple_files_share_transaction(ledger, sql_file):
    first = sql_file("BEGIN; CREATE TABLE x(a);", "first.sql")
    second = sql_file("INSERT INTO x VALUES(1); COMMIT;", "second.sql")
    report = check_migration(ledger, [first, second], temp_dir=ledger.parent)
    assert report["exit_code"] == 0, report["findings"]
    assert report["after"]["tables"]["x"]["row_count"] == 1


def test_wal_snapshot_includes_committed_rows(tmp_path, sql_file):
    path = tmp_path / "wal ? #%中.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE x(a)")
    conn.execute("INSERT INTO x VALUES(99)")
    conn.commit()
    assert Path(str(path) + "-wal").exists()
    report = check(path, sql_file("SELECT 1;"))
    assert report["exit_code"] == 0, report["findings"]
    assert report["before"]["tables"]["x"]["row_count"] == 1
    assert conn.execute("SELECT a FROM x").fetchone() == (99,)
    conn.close()


def test_timeout_and_json_error(ledger, sql_file, capsys):
    report = check(ledger, sql_file("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n;"), timeout=0.05)
    assert report["exit_code"] == 2 and "timeout" in codes(report)
    code = main([str(ledger.parent / "absent.db"), str(sql_file("SELECT 1;")), "--format", "json", "--temp-dir", str(ledger.parent)])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_bounded_fk_samples(tmp_path, sql_file):
    db = tmp_path / "many.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE p(id PRIMARY KEY); CREATE TABLE c(x REFERENCES p(id));")
        conn.executemany("INSERT INTO c VALUES(?)", ((n,) for n in range(200)))
    report = check(db, sql_file("SELECT 1;"), max_violations=3)
    rows = report["validation"]["after"]["foreign_keys"]
    assert len(rows) == 4 and rows[-1]["truncated"]
    assert all(f["details"]["preexisting"] for f in report["findings"] if f["code"] == "foreign_key_violation")
