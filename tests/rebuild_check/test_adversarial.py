import io
import sqlite3

import pytest

pytest.importorskip("sqlite_utils", reason="rebuild-check needs the 'sqlite' extra")

from datawrench.rebuild_check import check_migration
from datawrench.rebuild_check.core import Deadline, statements, trigger_update_columns


def run(db, sql, **kwargs):
    path = db.parent / "edge.sql"
    path.write_text(sql)
    return check_migration(db, [path], temp_dir=db.parent, **kwargs)


def test_select_error_after_first_result_is_not_discarded(ledger):
    sql = "SELECT CASE x WHEN 2 THEN abs(-9223372036854775808) ELSE x END FROM (SELECT 1 AS x UNION ALL SELECT 2);"
    report = run(ledger, sql)
    assert report["exit_code"] == 2
    assert "integer overflow" in report["findings"][-1]["details"]["error"]


@pytest.mark.parametrize("header,column", [
    ('UPDATE OF balance, misspelled', 'misspelled'),
    ('UPDATE OF "weird""column"', 'weird"column'),
    ("UPDATE OF 'single''quoted'", "single'quoted"),
    ('UPDATE OF `back``tick`', 'back`tick'),
    ('UPDATE OF [bracket name]', 'bracket name'),
])
def test_unknown_trigger_columns_are_reported_even_if_some_are_valid(ledger, header, column):
    sql = f"CREATE TRIGGER header_test AFTER {header} ON accounts BEGIN SELECT 1; END;"
    report = run(ledger, sql)
    issue = next(f for f in report["findings"] if f["object"] == "trigger:header_test")
    assert issue["code"] == "reference_invalid"
    assert issue["details"]["result"]["columns"] == [column]


def test_quoted_trigger_name_does_not_confuse_event(ledger):
    report = run(ledger, 'CREATE TRIGGER "UPDATE" AFTER UPDATE OF "balance" ON accounts BEGIN SELECT 1; END;')
    assert report["exit_code"] == 0, report["findings"]


def test_broken_view_with_trigger_can_be_fixed(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.executescript("CREATE VIEW v AS SELECT absent FROM accounts; CREATE TRIGGER vt INSTEAD OF INSERT ON v BEGIN SELECT 1; END;")
    report = run(ledger, "DROP VIEW v; CREATE VIEW v AS SELECT balance FROM accounts;")
    assert report["migration"]["completed"]
    assert report["validation"]["before"]["references"]
    assert report["validation"]["after"]["references"] == []
    assert any(f["code"] == "object_missing" and f["object"] == "trigger:vt" for f in report["findings"])


def test_instead_of_trigger_is_compiled_but_never_executed(ledger):
    sql = "CREATE TRIGGER view_write INSTEAD OF INSERT ON balances BEGIN INSERT INTO absent VALUES(new.id); END;"
    report = run(ledger, sql)
    assert report["exit_code"] == 1
    assert any(f["code"] == "reference_invalid" and f["object"] == "view:balances" for f in report["findings"])
    assert report["after"]["tables"]["accounts"]["row_count"] == 2


@pytest.mark.parametrize("cut", [1, 2, 3, 7, 19, 65536])
def test_streaming_statement_boundaries_use_sqlite_scanner(cut):
    class SmallReads(io.StringIO):
        def read(self, size=-1):
            return super().read(min(size, cut))
    script = """-- comment;\nBEGIN; CREATE TABLE x(v); INSERT INTO x VALUES('semi;colon');
    CREATE TRIGGER tr AFTER INSERT ON x BEGIN SELECT ';'; SELECT 1; END;
    COMMIT; -- tail\n"""
    db = sqlite3.connect(":memory:", isolation_level=None)
    for _, sql in statements(SmallReads(script), Deadline(1)):
        list(db.execute(sql))
    assert not db.in_transaction
    assert db.execute("SELECT v FROM x").fetchall() == [("semi;colon",)]
    db.close()


def test_mutating_source_via_attached_uri_is_impossible(ledger):
    report = run(ledger, f"ATTACH '{ledger.as_uri()}?mode=rw' AS original; DROP TABLE original.accounts;")
    assert report["exit_code"] == 2
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 2


def test_cascade_loss_in_unmodified_child_is_detected(ledger):
    # With enforcement on, dropping a parent performs implicit ON DELETE CASCADE.
    # Avoid the surviving-view issue so this intentionally bad SQL completes.
    sql = """DROP VIEW balances; CREATE TABLE accounts_new(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, balance INTEGER DEFAULT 0);
    INSERT INTO accounts_new SELECT * FROM accounts; DROP TABLE accounts; ALTER TABLE accounts_new RENAME TO accounts;
    CREATE INDEX balance_idx ON accounts(balance) WHERE balance > 0;
    CREATE TRIGGER account_audit AFTER UPDATE OF balance ON accounts BEGIN INSERT INTO audit VALUES(new.id, 'balance changed'); END;
    CREATE VIEW balances AS SELECT id,balance FROM accounts;"""
    report = run(ledger, sql)
    assert report["migration"]["completed"]
    assert any(f["code"] == "row_count_changed" and f["object"] == "table:entries" for f in report["findings"])
    assert report["validation"]["after"]["foreign_keys"] == []


def test_unicode_names_use_sqlite_ascii_case_rules(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.executescript('CREATE TABLE "Ä"(id); CREATE TABLE "ä"(id);')
    report = run(ledger, 'DROP TABLE "Ä";')
    assert any(f["object"] == "table:Ä" and f["code"] == "object_missing" for f in report["findings"])
    assert "ä" in report["after"]["tables"]


def test_rename_old_first_rewrites_cross_table_references_to_dropped_backup(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.executescript("CREATE TRIGGER entries_audit AFTER INSERT ON entries BEGIN INSERT INTO audit SELECT new.account_id,email FROM accounts WHERE id=new.account_id; END;")
    sql = """PRAGMA foreign_keys=OFF;
    ALTER TABLE accounts RENAME TO accounts_old;
    CREATE TABLE accounts(id INTEGER PRIMARY KEY,email TEXT NOT NULL UNIQUE,balance REAL DEFAULT 0);
    INSERT INTO accounts SELECT * FROM accounts_old;
    DROP TABLE accounts_old;
    CREATE INDEX balance_idx ON accounts(balance) WHERE balance>0;
    CREATE TRIGGER account_audit AFTER UPDATE OF balance ON accounts BEGIN INSERT INTO audit VALUES(new.id, 'balance changed'); END;
    PRAGMA foreign_keys=ON;"""
    report = run(ledger, sql)
    assert report["migration"]["completed"], report["findings"]
    assert report["after"]["tables"]["accounts"]["row_count"] == 2
    assert any(f["code"] == "reference_invalid" and f["object"] == "view:balances" for f in report["findings"])
    assert any(f["code"] == "reference_invalid" and f["object"] == "table:entries" for f in report["findings"])
    assert any(f["code"] == "dependency_changed" and f["object"] == "trigger:entries_audit" for f in report["findings"])


@pytest.mark.parametrize("suffix", [chr(code) for code in range(0x110000) if chr(code).isspace()])
def test_all_python_whitespace_suffixes_match_sqlite_executescript(ledger, suffix):
    script = "SELECT 1;" + suffix
    oracle = sqlite3.connect(":memory:")
    try:
        try:
            oracle.executescript(script)
            expected = 0
        except sqlite3.Error:
            expected = 2
    finally:
        oracle.close()
    report = run(ledger, script)
    assert report["exit_code"] == expected, (repr(suffix), report["findings"])
