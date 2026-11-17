import sqlite3

import pytest

pytest.importorskip("sqlite_utils", reason="rebuild-check needs the 'sqlite' extra")

from datawrench.rebuild_check import check_migration
from datawrench.rebuild_check.constraints import foreign_key_timing


def run(db, sql):
    migration = db.parent / "contracts.sql"
    migration.write_text(sql)
    return check_migration(db, [migration], temp_dir=db.parent)


@pytest.mark.parametrize("clause,deferred", [
    ("", False), ("DEFERRABLE", False), ("NOT DEFERRABLE", False),
    ("DEFERRABLE INITIALLY DEFERRED", True), ("DEFERRABLE INITIALLY IMMEDIATE", False),
    ("NOT DEFERRABLE INITIALLY DEFERRED", False),
    ("NOT DEFERRABLE INITIALLY IMMEDIATE", False),
])
def test_foreign_key_timing_matches_real_sqlite(clause, deferred):
    sql = f'CREATE TABLE c(x REFERENCES p(id) {clause});'
    assert foreign_key_timing(sql)[0]["deferred"] is deferred
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript("PRAGMA foreign_keys=ON; CREATE TABLE p(id PRIMARY KEY);" + sql)
    db.execute("BEGIN")
    if deferred:
        db.execute("INSERT INTO c VALUES(10)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("COMMIT")
    else:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO c VALUES(10)")
    db.close()


def test_deferral_loss_is_detected_with_unchanged_pragma_metadata(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.executescript("CREATE TABLE p(id PRIMARY KEY); CREATE TABLE c(x REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED);")
    report = run(ledger, "DROP TABLE c; CREATE TABLE c(x REFERENCES p(id));")
    assert report["exit_code"] == 1
    assert any(f["code"] == "foreign_key_timing_changed" for f in report["findings"])


def test_timing_does_not_depend_on_quoting_or_comments(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.executescript('CREATE TABLE p(a,b,PRIMARY KEY(a,b)); CREATE TABLE c(x,y,CONSTRAINT fk FOREIGN KEY(x,y) REFERENCES p(a,b) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED);')
    report = run(ledger, 'DROP TABLE c; CREATE TABLE c(x,y,FOREIGN KEY("x", "y") REFERENCES "p"("a","b") ON DELETE CASCADE /* ON DELETE? */ DEFERRABLE INITIALLY DEFERRED);')
    assert report["exit_code"] == 0, report["findings"]


def test_timing_is_independent_of_sqlite_fk_id_order(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.executescript('CREATE TABLE p(id PRIMARY KEY); CREATE TABLE q(id PRIMARY KEY); CREATE TABLE c(x REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED,y REFERENCES q(id));')
    report = run(ledger, 'DROP TABLE c; CREATE TABLE c(x,y,FOREIGN KEY(y) REFERENCES q(id),FOREIGN KEY(x) REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED);')
    assert report["exit_code"] == 0, report["findings"]


def test_multiple_inline_keys_implicit_parent_columns_and_quoted_keywords(ledger):
    sql = '''CREATE TABLE c("x" REFERENCES "references" DEFERRABLE INITIALLY DEFERRED REFERENCES p,
    "a" TEXT DEFAULT 'DEFERRABLE INITIALLY DEFERRED', CHECK(length('REFERENCES')>0));'''
    keys = foreign_key_timing(sql)
    assert [(k["parent"], k["deferred"]) for k in keys] == [("references", True), ("p", False)]


def test_sequence_rebuild_loss_and_autoincrement_removal(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.executescript("CREATE TABLE seq(id INTEGER PRIMARY KEY AUTOINCREMENT,x); INSERT INTO seq VALUES(100,1); DELETE FROM seq;")
    report = run(ledger, "DROP TABLE seq; CREATE TABLE seq(id INTEGER PRIMARY KEY AUTOINCREMENT,x);")
    assert any(f["code"] == "sequence_regressed" for f in report["findings"])
    report = run(ledger, "DROP TABLE seq; CREATE TABLE seq(id INTEGER PRIMARY KEY,x);")
    assert any(f["code"] == "autoincrement_removed" for f in report["findings"])


def test_preserved_sequence_and_invalid_type(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.executescript("CREATE TABLE seq(id INTEGER PRIMARY KEY AUTOINCREMENT); INSERT INTO seq VALUES(100); DELETE FROM seq;")
    report = run(ledger, "DROP TABLE seq; CREATE TABLE seq(id INTEGER PRIMARY KEY AUTOINCREMENT); INSERT INTO sqlite_sequence VALUES('seq',100);")
    assert report["exit_code"] == 0, report["findings"]
    report = run(ledger, "UPDATE sqlite_sequence SET seq=x'ffff' WHERE name='seq';")
    assert report["exit_code"] == 1
    assert any(f["code"] == "integrity_failed" for f in report["findings"])


def test_preexisting_orphans_survive_fk_id_reordering(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.executescript('CREATE TABLE p(id PRIMARY KEY); CREATE TABLE q(id PRIMARY KEY); CREATE TABLE c(x REFERENCES p(id), y REFERENCES q(id)); INSERT INTO c VALUES(1,2);')
    report = run(ledger, 'PRAGMA foreign_keys=OFF; CREATE TABLE c_new(x,y,FOREIGN KEY(y) REFERENCES q(id),FOREIGN KEY(x) REFERENCES p(id)); INSERT INTO c_new SELECT * FROM c; DROP TABLE c; ALTER TABLE c_new RENAME TO c;')
    violations = [f for f in report["findings"] if f["code"] == "foreign_key_violation"]
    assert len(violations) == 2
    assert all(f["details"]["preexisting"] for f in violations)


def test_without_rowid_violation_counts_do_not_mark_new_orphans_preexisting(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.executescript('CREATE TABLE p(id PRIMARY KEY); CREATE TABLE c(id INTEGER PRIMARY KEY, x REFERENCES p(id)) WITHOUT ROWID; INSERT INTO c VALUES(1,99);')
    report = run(ledger, 'PRAGMA foreign_keys=OFF; INSERT INTO c VALUES(2,99);')
    violations = [f for f in report["findings"] if f["code"] == "foreign_key_violation"]
    assert len(violations) == 2
    assert sorted(f["details"]["preexisting"] for f in violations) == [False, True]
