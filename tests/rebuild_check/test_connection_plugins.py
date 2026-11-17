import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

sqlite_utils = pytest.importorskip("sqlite_utils", reason="rebuild-check needs the 'sqlite' extra")
from sqlite_utils.plugins import pm

from datawrench.rebuild_check import check_migration
from datawrench.rebuild_check.cli import main


def prepare(conn):
    conn.create_collation("casefold", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()))
    conn.create_function("positive", 1, lambda n: n > 0, deterministic=True)


@pytest.fixture
def custom_db(tmp_path):
    path = tmp_path / "custom.db"
    with sqlite3.connect(path) as conn:
        prepare(conn)
        conn.executescript("CREATE TABLE custom(id INTEGER PRIMARY KEY,code TEXT UNIQUE COLLATE casefold,n INTEGER CHECK(positive(n))); INSERT INTO custom VALUES(1,'A',1);")
    return path


def test_custom_collation_and_function_use_replica_only(custom_db, sql_file):
    before = hashlib.sha256(custom_db.read_bytes()).digest()
    migration = sql_file("SELECT 1;")
    unsupported = check_migration(custom_db, [migration], temp_dir=custom_db.parent)
    assert unsupported["exit_code"] == 2
    called = []
    def setup(conn):
        called.append(conn.execute("PRAGMA database_list").fetchone()[2])
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        prepare(conn)
    report = check_migration(custom_db, [migration], temp_dir=custom_db.parent, prepare_connection=setup)
    assert report["exit_code"] == 0, report["findings"]
    assert report["connection_prepared"] and len(called) == 1
    assert Path(called[0]) != custom_db and "rebuild-check-" in called[0]
    assert hashlib.sha256(custom_db.read_bytes()).digest() == before


def test_actual_host_plugin_hook_dispatch(custom_db, sql_file, capsys):
    class AppPlugin:
        @sqlite_utils.hookimpl
        def prepare_connection(self, conn):
            prepare(conn)
    plugin = AppPlugin()
    pm.register(plugin, name="rebuild-check-test-app")
    try:
        code = main([str(custom_db), str(sql_file("SELECT 1;")), "--connection-plugins", "--format", "json", "--temp-dir", str(custom_db.parent)])
        out = capsys.readouterr().out
        assert code == 0, out
        assert json.loads(out)["connection_prepared"]
    finally:
        pm.unregister(plugin)


@pytest.mark.parametrize("mode", ["write", "exception", "transaction"])
def test_failed_preparers_are_structured_and_cleaned(custom_db, sql_file, mode):
    def setup(conn):
        prepare(conn)
        if mode == "write":
            conn.execute("DELETE FROM custom")
        elif mode == "transaction":
            conn.execute("BEGIN")
        else:
            raise RuntimeError("plugin setup failed")
    report = check_migration(custom_db, [sql_file("SELECT 1;")], temp_dir=custom_db.parent, prepare_connection=setup)
    assert report["exit_code"] == 2
    assert report["findings"][-1]["code"] == "preparation_error"
    assert not list(custom_db.parent.glob("rebuild-check-*"))


def test_plugin_discovery_error_preserves_json_contract(custom_db, sql_file, monkeypatch, capsys):
    import sqlite_utils.plugins
    def broken():
        raise ImportError("installed plugin cannot be imported")
    monkeypatch.setattr(sqlite_utils.plugins, "ensure_plugins_loaded", broken)
    code = main([str(custom_db), str(sql_file("SELECT 1;")), "--connection-plugins", "--format", "json", "--temp-dir", str(custom_db.parent)])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["findings"][-1]["code"] == "preparation_error"


def test_application_row_factory_does_not_break_inspection(custom_db, sql_file):
    def setup(conn):
        prepare(conn)
        conn.row_factory = None
        conn.isolation_level = "DEFERRED"
    migration = sql_file("INSERT INTO custom VALUES(2,'B',2);")
    report = check_migration(custom_db, [migration], temp_dir=custom_db.parent, prepare_connection=setup)
    assert report["migration"]["completed"], report["findings"]
    assert report["after"]["tables"]["custom"]["row_count"] == 2
    assert [f["code"] for f in report["findings"]] == ["row_count_changed"]
