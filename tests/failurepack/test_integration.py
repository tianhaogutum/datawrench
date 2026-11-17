import json
import subprocess
import sys
from pathlib import Path

import pytest

# Optional `pandera` extra (pandas, Pandera and dfshrink, which needs Python 3.12+);
# skip this module cleanly when it is not installed.
pytest.importorskip("pandas")
pytest.importorskip("pandera")
pytest.importorskip("dfshrink")

import pandas as pd
import pandera.pandas as pa

from datawrench.failurepack.bundle import list_failures, reduce_to_bundle
from datawrench.failurepack.cli import main
from datawrench.failurepack.table import InputError
from datawrench.failurepack.validator import ValidationError, Worker, read_options

ROOT = Path(__file__).resolve().parents[2]


def failures(data, schema, options=None):
    return list_failures(data, schema, ",", options or {}, 10, 32 * 1024 * 1024)


def pick(data, schema, reason, options=None):
    return next(f["id"] for f in failures(data, schema, options) if f["reason"] == reason)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp("orders")
    source = ROOT / "examples/failurepack/orders.csv"
    schema = ROOT / "examples/failurepack/orders.yaml"
    target = pick(source, schema, "series_contains_duplicates")
    output = directory / "bundle"
    manifest = reduce_to_bundle(source, schema, output, target)
    return output, manifest


def test_selected_failure_survives_and_unrelated_failure_is_removed(bundle):
    output, manifest = bundle
    result = pd.read_csv(output / "data.csv")
    assert len(result) == 2
    assert result.order_id.tolist() == [48, 48]
    assert (result.amount >= 0).all()
    assert "\n" in result.note.iloc[0]
    assert manifest["result"]["source_record_ranges"] == [[48, 48], [201, 201]]
    assert manifest["reduction"]["one_minimal"] is True
    assert (output / "schema.yaml").read_bytes() == (ROOT / "examples/failurepack/orders.yaml").read_bytes()
    events = [json.loads(line) for line in (output / "reduction.jsonl").read_text().splitlines()]
    assert events[0]["stage"] == "baseline"
    assert events[-1]["stage"] == "final"
    assert events[-1]["target_present"] is True
    for event in events:
        if event.get("cache_hit"):
            original_event = events[event["reused_event"] - 1]
            assert original_event["source_record_ranges"] == event["source_record_ranges"]
            assert original_event["target_present"] == event["target_present"]
        else:
            assert len(event["observed_failure_ids"]) == 2
    # Independently establish that each retained row is required for uniqueness.
    for index in range(2):
        row = output.parent / f"one-{index}.csv"
        result.iloc[[index]].to_csv(row, index=False)
        assert manifest["target"]["id"] not in {
            f["id"] for f in failures(row, output / "schema.yaml")
        }


def test_standalone_replay_and_tamper_detection(bundle, tmp_path):
    import shutil

    output, _ = bundle
    result = subprocess.run(
        [sys.executable, "-I", str(output / "replay.py")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["reproduced"] is True
    copied = tmp_path / "copy"
    shutil.copytree(output, copied)
    (copied / "data.csv").write_text("changed")
    result = subprocess.run(
        [sys.executable, str(copied / "replay.py")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "bundle member changed" in result.stderr


def test_budget_exhaustion_keeps_verified_input(tmp_path):
    data = ROOT / "examples/failurepack/orders.csv"
    schema = ROOT / "examples/failurepack/orders.yaml"
    target = pick(data, schema, "series_contains_duplicates")
    output = tmp_path / "budget"
    manifest = reduce_to_bundle(data, schema, output, target, max_evals=1)
    assert manifest["result"]["rows"] == 256
    assert manifest["reduction"]["one_minimal"] is False
    assert manifest["reduction"]["predicate_calls"] == 1


def test_python_aggregate_schema_needs_a_pair(tmp_path):
    data, schema = tmp_path / "aggregate.csv", tmp_path / "schema.py"
    pd.DataFrame({"amount": [1, 60, 2, 60, 3]}).to_csv(data, index=False)
    schema.write_text(
        "import pandera.pandas as pa\n"
        "schema = pa.DataFrameSchema({'amount': pa.Column(int)}, checks=[\n"
        " pa.Check(lambda df: df['amount'].sum() <= 100, name='total_limit')])\n"
    )
    target = pick(data, schema, "dataframe_check")
    output = tmp_path / "aggregate-bundle"
    manifest = reduce_to_bundle(data, schema, output, target)
    assert manifest["result"]["rows"] == 2
    assert pd.read_csv(output / "data.csv").amount.tolist() == [60, 60]


def test_structural_failure_can_reduce_to_header_only(tmp_path):
    data, schema = tmp_path / "input.csv", tmp_path / "schema.yaml"
    pd.DataFrame({"amount": [1, 2, 3]}).to_csv(data, index=False)
    pa.DataFrameSchema({"missing": pa.Column(int)}).to_yaml(schema)
    target = pick(data, schema, "column_not_in_dataframe")
    manifest = reduce_to_bundle(data, schema, tmp_path / "empty", target)
    assert manifest["result"]["rows"] == 0
    assert manifest["reduction"]["one_minimal"] is True
    assert (tmp_path / "empty/data.csv").read_bytes() == b"amount\n"


def test_parser_options_and_lexical_leading_zeros(tmp_path):
    data, schema = tmp_path / "input.csv", tmp_path / "schema.yaml"
    pd.DataFrame({"id": ["001", "002", "002", "003"]}).to_csv(data, index=False)
    pa.DataFrameSchema({"id": pa.Column("str", unique=True)}).to_yaml(schema)
    options = {"dtype": {"id": "str"}, "keep_default_na": False}
    target = pick(data, schema, "series_contains_duplicates", options)
    output = tmp_path / "lexical"
    reduce_to_bundle(data, schema, output, target, options=options)
    assert (output / "data.csv").read_text() == "id\n002\n002\n"
    assert json.loads((output / "read-options.json").read_text())["pandas_options"] == options


def test_absent_target_and_existing_output_leave_inputs_untouched(tmp_path):
    data, schema = ROOT / "examples/failurepack/orders.csv", ROOT / "examples/failurepack/orders.yaml"
    with pytest.raises(ValidationError, match="absent"):
        reduce_to_bundle(data, schema, tmp_path / "absent", "pa:unknown")
    assert not (tmp_path / "absent").exists()
    assert list(tmp_path.iterdir()) == []
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep.txt").write_text("keep me")
    with pytest.raises(InputError, match="exists"):
        reduce_to_bundle(data, schema, output, "pa:unknown")
    assert (output / "keep.txt").read_text() == "keep me"


def test_check_exception_is_not_a_quality_failure(tmp_path):
    data, schema = tmp_path / "input.csv", tmp_path / "schema.py"
    pd.DataFrame({"amount": [1]}).to_csv(data, index=False)
    schema.write_text(
        "import pandera.pandas as pa\n"
        "schema=pa.DataFrameSchema({'amount':pa.Column(int,pa.Check(lambda s: 1/0))})\n"
    )
    with pytest.raises(ValidationError, match="check raised"):
        failures(data, schema)


def test_timeout_kills_worker_and_recovery_works(tmp_path):
    data, schema = tmp_path / "input.csv", tmp_path / "schema.py"
    data.write_text("amount\n1\n")
    schema.write_text("import time\ntime.sleep(10)\n")
    with Worker(schema, ",", {}, timeout=1) as worker:
        response = worker.validate(data)
        assert response["kind"] == "Timeout"
        assert worker.process is None
        worker.schema = ROOT / "examples/failurepack/orders.yaml"
        worker.timeout = 10
        response = worker.validate(ROOT / "examples/failurepack/orders.csv")
        assert response["status"] == "ok"


def test_unsupported_parser_options_and_cli_errors(tmp_path, capsys):
    options = tmp_path / "options.json"
    options.write_text('{"skiprows": 1}')
    with pytest.raises(ValueError, match="unsupported"):
        read_options(options)
    assert main(["list", "missing.csv", "--schema", "missing.yaml"]) == 2
    assert "failurepack:" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
