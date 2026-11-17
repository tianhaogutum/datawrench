"""A malformed manifest must never silently weaken replay verification."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Optional `pandera` extra (pandas, Pandera and dfshrink, which needs Python 3.12+);
# skip this module cleanly when it is not installed.
pytest.importorskip("pandas")
pytest.importorskip("pandera")
pytest.importorskip("dfshrink")

import yaml

from datawrench.failurepack.bundle import list_failures, reduce_to_bundle

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def replay_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("replay-metadata")
    data, schema = ROOT / "examples/failurepack/orders.csv", ROOT / "examples/failurepack/orders.yaml"
    failures = list_failures(data, schema, ",", {}, 10, 32 * 1024 * 1024)
    target = next(f["id"] for f in failures if f["check"] == "field_uniqueness")
    reduce_to_bundle(data, schema, root / "valid", target)
    return root / "valid"


@pytest.mark.parametrize(
    "change",
    [
        "schema_traversal",
        "no_hashes",
        "missing_worker_hash",
        "no_versions",
        "zero_repeats",
        "nan_timeout",
        "wrong_row_count",
        "wrong_target_description",
        "huge_timeout",
        "missing_target_fields",
        "consistent_but_false_row_count",
    ],
)
def test_malformed_manifest_is_rejected(replay_bundle, tmp_path, change):
    bundle = tmp_path / "bundle"
    shutil.copytree(replay_bundle, bundle)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if change == "schema_traversal":
        manifest["schema_file"] = "../outside.py"
    elif change == "no_hashes":
        manifest["files"] = {}
    elif change == "missing_worker_hash":
        del manifest["files"]["_validator.py"]
    elif change == "no_versions":
        manifest["validator_versions"] = {}
    elif change == "zero_repeats":
        manifest["reduction"]["stability_runs"] = 0
    elif change == "nan_timeout":
        manifest["reduction"]["timeout_seconds"] = float("nan")
    elif change == "wrong_row_count":
        manifest["result"]["rows"] = 123
    elif change == "wrong_target_description":
        manifest["target"]["column"] = "different_column"
    elif change == "huge_timeout":
        manifest["reduction"]["timeout_seconds"] = 10**1000
    elif change == "missing_target_fields":
        manifest["target"] = {"id": "pa:" + hashlib.sha256(b"{}").hexdigest()}
    else:
        manifest["result"]["rows"] = 3
        manifest["result"]["source_record_ranges"] = [[1, 3]]
    manifest_path.write_text(json.dumps(manifest))
    result = subprocess.run(
        [sys.executable, str(bundle / "replay.py")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "manifest" in result.stderr.lower() or "row count" in result.stderr.lower()
    assert "Traceback" not in result.stderr


def test_symlink_member_is_rejected(replay_bundle, tmp_path):
    bundle = tmp_path / "bundle"
    shutil.copytree(replay_bundle, bundle)
    member = bundle / "data.csv"
    member.unlink()
    member.symlink_to(replay_bundle / "data.csv")
    result = subprocess.run(
        [sys.executable, str(bundle / "replay.py")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "member" in result.stderr


def test_replay_rejects_row_skipping_parser_settings(replay_bundle, tmp_path):
    bundle = tmp_path / "bundle"
    shutil.copytree(replay_bundle, bundle)
    settings_path = bundle / "read-options.json"
    settings = json.loads(settings_path.read_text())
    settings["pandas_options"] = {"skiprows": 1}
    settings_path.write_text(json.dumps(settings))
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["read-options.json"] = hashlib.sha256(settings_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    result = subprocess.run(
        [sys.executable, str(bundle / "replay.py")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "unsupported" in result.stderr


def test_deeply_nested_manifest_is_a_clean_input_error(replay_bundle, tmp_path):
    bundle = tmp_path / "bundle"
    shutil.copytree(replay_bundle, bundle)
    (bundle / "manifest.json").write_text("[" * 1500 + "0" + "]" * 1500)
    result = subprocess.run(
        [sys.executable, str(bundle / "replay.py")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_replay_distinguishes_absent_target_from_invalid_package(replay_bundle, tmp_path):
    bundle = tmp_path / "bundle"
    shutil.copytree(replay_bundle, bundle)
    # Intentional rule update: remove uniqueness, then repackage its checksum.
    # The recorded old target stays intact so this exercises the regression path.
    schema_path = bundle / "schema.yaml"
    schema = yaml.safe_load(schema_path.read_text())
    schema["columns"]["order_id"]["unique"] = False
    schema_path.write_text(yaml.safe_dump(schema))
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["schema.yaml"] = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    result = subprocess.run(
        [sys.executable, str(bundle / "replay.py")],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["reproduced"] is False


def test_version_drift_is_explicitly_opt_in(replay_bundle, tmp_path):
    bundle = tmp_path / "bundle"
    shutil.copytree(replay_bundle, bundle)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["validator_versions"]["pandas"] = "0.0.0"
    manifest_path.write_text(json.dumps(manifest))
    strict = subprocess.run(
        [sys.executable, str(bundle / "replay.py")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert strict.returncode == 2
    assert "version" in strict.stderr
    explicit = subprocess.run(
        [sys.executable, str(bundle / "replay.py"), "--allow-version-drift"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert explicit.returncode == 0, explicit.stderr
