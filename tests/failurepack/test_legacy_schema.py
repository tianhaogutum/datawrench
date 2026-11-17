from pathlib import Path

import pytest

# Optional `pandera` extra (pandas, Pandera and dfshrink, which needs Python 3.12+);
# skip this module cleanly when it is not installed.
pytest.importorskip("pandas")
pytest.importorskip("pandera")
pytest.importorskip("dfshrink")

from datawrench.failurepack.bundle import list_failures, reduce_to_bundle

ROOT = Path(__file__).resolve().parents[2]


def test_real_pandera_026_yaml_replays_without_conversion(tmp_path):
    data = ROOT / "examples/failurepack/orders.csv"
    old_schema = ROOT / "examples/failurepack/orders-pandera-0.26.1.yaml"
    current_schema = ROOT / "examples/failurepack/orders.yaml"
    old_failures = list_failures(data, old_schema, ",", {}, 10, 32 * 1024 * 1024)
    current_failures = list_failures(data, current_schema, ",", {}, 10, 32 * 1024 * 1024)
    assert old_failures == current_failures
    target = next(f["id"] for f in old_failures if f["check"] == "field_uniqueness")
    manifest = reduce_to_bundle(data, old_schema, tmp_path / "old-yaml", target)
    assert manifest["result"]["rows"] == 2
    assert (tmp_path / "old-yaml/schema.yaml").read_bytes() == old_schema.read_bytes()
