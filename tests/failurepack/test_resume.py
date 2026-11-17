import json
from pathlib import Path

import pytest

# Optional `pandera` extra (pandas, Pandera and dfshrink, which needs Python 3.12+);
# skip this module cleanly when it is not installed.
pytest.importorskip("pandas")
pytest.importorskip("pandera")
pytest.importorskip("dfshrink")

import pandas as pd

from datawrench.failurepack.bundle import list_failures, reduce_to_bundle, resume_bundle, sha256
from datawrench.failurepack.cli import main
from datawrench.failurepack.table import InputError
from datawrench.failurepack.validator import ValidationError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def budget_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("resume")
    data, schema = ROOT / "examples/failurepack/orders.csv", ROOT / "examples/failurepack/orders.yaml"
    failures = list_failures(data, schema, ",", {}, 10, 32 * 1024 * 1024)
    target = next(f["id"] for f in failures if f["check"] == "field_uniqueness")
    reduce_to_bundle(data, schema, root / "seed", target, max_evals=1)
    return root / "seed"


@pytest.mark.parametrize("interface", ["api", "cli"])
def test_continue_a_budgeted_bundle_without_the_original_input(
    budget_bundle,
    tmp_path,
    interface,
    capsys,
):
    before = sha256(budget_bundle / "manifest.json")
    parent = json.loads((budget_bundle / "manifest.json").read_text())
    output = tmp_path / "continued"
    if interface == "api":
        manifest = resume_bundle(budget_bundle, output, max_evals=1000)
    else:
        assert main(["resume", str(budget_bundle), "--out", str(output)]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["stop_reason"] == "completed"
        assert summary["one_minimal"] is True
        manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["result"]["rows"] == 2
    assert manifest["target"] == parent["target"]
    assert manifest["parent_bundle"]["manifest_sha256"] == before
    assert manifest["parent_bundle"]["source"] == parent["source"]
    assert manifest["parent_bundle"]["result"] == parent["result"]
    assert sha256(budget_bundle / "manifest.json") == before
    assert pd.read_csv(output / "data.csv").order_id.tolist() == [48, 48]


def test_continuation_rejects_changed_parent_data(budget_bundle, tmp_path):
    import shutil

    parent = tmp_path / "parent"
    shutil.copytree(budget_bundle, parent)
    (parent / "data.csv").write_text("changed")
    with pytest.raises(ValidationError, match="parent bundle"):
        resume_bundle(parent, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_continuation_never_overwrites_an_output(budget_bundle, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(InputError, match="exists"):
        resume_bundle(budget_bundle, output)
