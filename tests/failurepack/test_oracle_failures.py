import json
from pathlib import Path

import pytest

# Optional `pandera` extra (pandas, Pandera and dfshrink, which needs Python 3.12+);
# skip this module cleanly when it is not installed.
pytest.importorskip("pandas")
pytest.importorskip("pandera")
pytest.importorskip("dfshrink")

import pandas as pd

from datawrench.failurepack.bundle import list_failures, reduce_to_bundle
from datawrench.failurepack.validator import ValidationError, Worker


def target(data, schema):
    failures = list_failures(data, schema, ",", {}, 10, 32 * 1024 * 1024)
    return next(f["id"] for f in failures if f["reason"] == "dataframe_check")


def test_observed_nondeterminism_never_publishes_a_bundle(tmp_path):
    data, schema = tmp_path / "data.csv", tmp_path / "schema.py"
    data.write_text("amount\n1\n2\n")
    schema.write_text(
        "from pathlib import Path\n"
        "import pandera.pandas as pa\n"
        "state=Path(__file__).with_name('state.txt')\n"
        "count=int(state.read_text())+1 if state.exists() else 1\n"
        "state.write_text(str(count))\n"
        "schema=pa.DataFrameSchema({'amount':pa.Column(int)}, checks=[\n"
        " pa.Check(lambda df: count % 2 == 0,name='alternating')])\n"
    )
    selected = target(data, schema)
    with pytest.raises(ValidationError, match="nondeterministic"):
        reduce_to_bundle(data, schema, tmp_path / "output", selected)
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".output-*"))


def test_candidate_check_errors_recover_without_claiming_minimality(tmp_path):
    data, schema = tmp_path / "data.csv", tmp_path / "schema.py"
    pd.DataFrame({"amount": [60, 1, 60, 2]}).to_csv(data, index=False)
    schema.write_text(
        "import pandera.pandas as pa\n"
        "def total(df):\n"
        " if len(df)<=1: raise ValueError('not enough rows for this check')\n"
        " return df['amount'].sum()<=100\n"
        "schema=pa.DataFrameSchema({'amount':pa.Column(int)}, checks=[\n"
        " pa.Check(total,name='total_limit')])\n"
    )
    selected = target(data, schema)
    output = tmp_path / "output"
    manifest = reduce_to_bundle(data, schema, output, selected)
    assert manifest["result"]["rows"] == 2
    assert manifest["reduction"]["unresolved_candidates"] > 0
    assert manifest["reduction"]["one_minimal"] is False
    events = [json.loads(line) for line in (output / "reduction.jsonl").read_text().splitlines()]
    assert any(event.get("error", {}).get("kind") == "RuntimeError" for event in events)
    assert events[-1]["target_present"] is True


def test_worker_protocol_handles_schema_output_unicode_paths_and_process_exit(tmp_path):
    directory = tmp_path / "café 資料"
    directory.mkdir()
    data, schema = directory / "data.csv", directory / "schema.py"
    data.write_text("amount\n-1\n2\n")
    schema.write_text(
        "import pandera.pandas as pa\n"
        "import os\nos.write(1,b'native output without a newline')\n"
        "print('schema output must not corrupt the JSON protocol')\n"
        "schema=pa.DataFrameSchema({'amount':pa.Column(int,pa.Check.ge(0))})\n"
    )
    with Worker(schema, ",", {}, 10) as worker:
        assert worker.validate(data)["status"] == "ok"
        schema.write_text("import os\nos._exit(7)\n")
        assert worker.validate(data)["status"] == "error"
        assert worker.process is None
        schema.write_text(
            "import pandera.pandas as pa\n"
            "schema=pa.DataFrameSchema({'amount':pa.Column(int,pa.Check.ge(0))})\n"
        )
        assert worker.validate(data)["status"] == "ok"


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1, 0, 301])
def test_invalid_timeout_is_rejected_before_starting_a_worker(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        Worker(tmp_path / "schema.py", ",", {}, timeout)


def test_public_api_rejects_row_skipping_options(tmp_path):
    with pytest.raises(ValueError, match="unsupported"):
        Worker(Path("schema.py"), ",", {"skiprows": 1})
