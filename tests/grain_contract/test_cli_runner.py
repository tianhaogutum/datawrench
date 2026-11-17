import json
import threading

import duckdb
import pytest

from datawrench.grain_contract import compile_contract
from datawrench.grain_contract.cli import main
from datawrench.grain_contract.runner import run_bundle

from .helpers import SIMPLE_SQL, simple_contract, simple_data


@pytest.fixture
def files(tmp_path):
    sql, contract, database = (
        tmp_path / n for n in ("model.sql", "contract.json", "warehouse.duckdb")
    )
    sql.write_text(SIMPLE_SQL)
    contract.write_text(json.dumps(simple_contract()))
    with duckdb.connect(
        str(database), config={"threads": "2", "memory_limit": "128MB"}
    ) as db:
        simple_data(db)
    return sql, contract, database


def test_cli_export_is_complete_and_rejects_existing_dest(files, tmp_path, capsys):
    sql, contract, _ = files
    out = tmp_path / "tests"
    assert (
        main(
            [
                "compile",
                str(sql),
                str(contract),
                "--out",
                str(out),
                "--dbt-model",
                "orders",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert len(list(out.glob("*.sql"))) == 9
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["dbt_model"] == "orders"
    assert (
        "-- depends_on: {{ ref('orders') }}"
        in (out / "orders__gc_001_primary_key.sql").read_text()
    )
    with duckdb.connect(str(files[2])) as db:
        for test in out.glob("*.sql"):
            # The actual dbt integration found a trailing-semicolon regression.
            query = test.read_text()
            assert (
                db.execute(f"SELECT COUNT(*) FROM ({query}) AS dbt_test").fetchone()[0]
                == 0
            )
    original = {p.name: p.read_bytes() for p in out.iterdir()}
    assert main(["compile", str(sql), str(contract), "--out", str(out)]) == 2
    assert original == {p.name: p.read_bytes() for p in out.iterdir()}
    assert (
        main(["compile", str(sql), str(contract), "--out", str(out), "--replace"]) == 0
    )
    assert (out / "gc_001_primary_key.sql").exists()
    assert not (out / "orders__gc_001_primary_key.sql").exists()
    (out / "handwritten.sql").write_text("SELECT 1")
    assert (
        main(["compile", str(sql), str(contract), "--out", str(out), "--replace"]) == 2
    )
    assert (out / "handwritten.sql").read_text() == "SELECT 1"


def test_cli_clean_dirty_and_runtime_error_have_distinct_exit_codes(files, capsys):
    sql, contract, database = files
    args = ["check", str(sql), str(contract), "--database", str(database), "--json"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    with duckdb.connect(str(database)) as db:
        db.execute("INSERT INTO customers VALUES(10,'Duplicate')")
    assert main(args) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "fail" and data["executed_checks"] == 9
    with duckdb.connect(str(database)) as db:
        db.execute("DROP TABLE customers")
    assert main(args) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error" and data["results"][-1]["error"]


def test_zero_sample_limit_still_detects_failure(files):
    _, _, database = files
    with duckdb.connect(str(database)) as db:
        db.execute("INSERT INTO customers VALUES(10,'Duplicate')")
    results = run_bundle(
        compile_contract(SIMPLE_SQL, simple_contract()), database, sample_limit=0
    )
    assert any(
        r.status == "fail" and r.sample_truncated and not r.sample for r in results
    )


def test_fail_fast_stops_before_expensive_fan_out_checks(files, capsys):
    sql, contract, database = files
    with duckdb.connect(str(database)) as db:
        db.execute("INSERT INTO customers VALUES(10,'Duplicate')")
    assert (
        main(
            [
                "check",
                str(sql),
                str(contract),
                "--database",
                str(database),
                "--fail-fast",
                "--json",
            ]
        )
        == 1
    )
    data = json.loads(capsys.readouterr().out)
    assert data["executed_checks"] == 2 and data["total_checks"] == 9
    assert data["results"][-1]["kind"] == "primary_key"
    # The early exit must release the read-only transaction and file handle.
    with duckdb.connect(str(database)) as db:
        db.execute("DELETE FROM customers WHERE name='Duplicate'")


def test_missing_database_never_creates_a_file(files, tmp_path, capsys):
    sql, contract, _ = files
    missing = tmp_path / "missing.duckdb"
    assert main(["check", str(sql), str(contract), "--database", str(missing)]) == 2
    assert not missing.exists()


def test_invalid_compile_leaves_no_partial_directory(files, tmp_path, capsys):
    sql, contract, _ = files
    c = simple_contract()
    c["scopes"]["$root"]["joins"]["c"]["cardinality"] = []
    contract.write_text(json.dumps(c))
    out = tmp_path / "tests"
    assert main(["compile", str(sql), str(contract), "--out", str(out)]) == 2
    assert not out.exists()
    assert "cardinality" in capsys.readouterr().err


def test_replace_restores_previous_output_if_install_rename_fails(
    files, tmp_path, monkeypatch, capsys
):
    from pathlib import Path

    sql, contract, _ = files
    out = tmp_path / "generated"
    args = ["compile", str(sql), str(contract), "--out", str(out)]
    assert main(args) == 0
    original = {p.name: p.read_bytes() for p in out.iterdir()}
    rename = Path.rename

    def fail_install(path, target):
        if path.name.startswith(".grain-contract-") and not path.name.startswith(
            ".grain-contract-backup-"
        ):
            raise OSError("simulated interrupted export")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_install)
    assert main(args + ["--replace"]) == 2
    assert original == {p.name: p.read_bytes() for p in out.iterdir()}
    assert not list(tmp_path.glob(".grain-contract-*"))


def test_replace_keeps_backup_if_restore_itself_is_blocked(
    files, tmp_path, monkeypatch, capsys
):
    from pathlib import Path

    sql, contract, _ = files
    out = tmp_path / "generated"
    args = ["compile", str(sql), str(contract), "--out", str(out)]
    assert main(args) == 0
    original = {p.name: p.read_bytes() for p in out.iterdir()}
    rename = Path.rename

    def fail_install_and_restore(path, target):
        if path.name.startswith(".grain-contract-"):
            raise OSError("simulated filesystem interruption")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_install_and_restore)
    assert main(args + ["--replace"]) == 2
    backups = list(tmp_path.glob(".grain-contract-backup-*"))
    assert len(backups) == 1
    assert original == {p.name: p.read_bytes() for p in backups[0].iterdir()}
    assert "previous output remains safe" in capsys.readouterr().err


def test_duplicate_json_keys_rejected(files, capsys):
    sql, contract, _ = files
    contract.write_text('{"version":1,"version":2}')
    assert main(["compile", str(sql), str(contract)]) == 2
    assert "duplicate JSON key" in capsys.readouterr().err


def test_timeout_cleans_up_timer_threads(files):
    _, _, database = files
    c = {
        "version": 1,
        "primary_keys": {"unused": ["id"]},
        "scopes": {},
        "output_grain": ["id"],
    }
    bundle = compile_contract("SELECT SUM(i) AS id FROM range(10000000000) t(i)", c)
    prior_threads = set(threading.enumerate())
    results = run_bundle(bundle, database, timeout=0.01)
    assert results[0].status == "error"
    assert "interrupt" in results[0].error.lower()
    assert set(threading.enumerate()) == prior_threads
    # Connection was closed; a following run is unaffected by the interruption.
    assert all(
        r.status == "pass"
        for r in run_bundle(compile_contract(SIMPLE_SQL, simple_contract()), database)
    )
