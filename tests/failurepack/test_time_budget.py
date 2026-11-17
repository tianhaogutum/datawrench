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

from datawrench.failurepack.bundle import list_failures, reduce_to_bundle
from datawrench.failurepack.table import InputError

ROOT = Path(__file__).resolve().parents[2]


def test_time_budget_publishes_the_last_verified_candidate(tmp_path):
    data, schema = ROOT / "examples/failurepack/orders.csv", ROOT / "examples/failurepack/orders.yaml"
    failures = list_failures(data, schema, ",", {}, 10, 32 * 1024 * 1024)
    selected = next(f["id"] for f in failures if f["check"] == "field_uniqueness")
    output = tmp_path / "time-limited"
    manifest = reduce_to_bundle(data, schema, output, selected, max_seconds=1e-9)
    assert manifest["reduction"]["stop_reason"] == "time_budget"
    assert manifest["reduction"]["one_minimal"] is False
    assert manifest["result"]["rows"] == 256
    events = [json.loads(line) for line in (output / "reduction.jsonl").read_text().splitlines()]
    assert events[-2]["stage"] == "stop"
    assert events[-1]["stage"] == "final"
    assert events[-1]["target_present"] is True
    result = subprocess.run(
        [sys.executable, str(output / "replay.py")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_invalid_time_budget_is_rejected_before_work_starts(tmp_path, seconds):
    with pytest.raises(InputError, match="max-seconds"):
        reduce_to_bundle(
            ROOT / "examples/failurepack/orders.csv",
            ROOT / "examples/failurepack/orders.yaml",
            tmp_path / "invalid",
            "unused",
            max_seconds=seconds,
        )
    assert not list(tmp_path.iterdir())
