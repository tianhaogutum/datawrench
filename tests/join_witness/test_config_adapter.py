import json
import shutil

import pytest
from tableproof import JoinSpec, TableProofError

from datawrench.join_witness import analyze
from datawrench.join_witness import engine
from datawrench.join_witness.cli import main
from .test_engine import FIXTURES, ROOT


def make_config(tmp_path, fail_on="error", multiple=False):
    source = FIXTURES / "tableproof"
    for name in ("samples.tsv", "results.tsv", "merged.tsv"):
        shutil.copyfile(source / name, tmp_path / name)
    path = tmp_path / "tableproof.toml"
    text = f'''version = 1
[report]
show_raw_keys = false
fail_on = "{fail_on}"
[[joins]]
name = "samples-to-results"
left = "samples.tsv"
right = "results.tsv"
left_keys = ["sample_id"]
right_keys = ["sample_id"]
relationship = "one-to-many"
result = "merged.tsv"
join_type = "left"
'''
    if multiple:
        text += '''[[joins]]
name = "second"
left = "samples.tsv"
right = "results.tsv"
left_keys = ["sample_id"]
right_keys = ["sample_id"]
relationship = "one-to-many"
'''
    path.write_text(text)
    return path


def test_existing_tableproof_config_is_used_without_redeclaring_contract(tmp_path, capsys):
    path = make_config(tmp_path)
    assert main(["--config", str(path), "--right-measure", "value", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["upstream_audit"]["name"] == "samples-to-results"
    assert report["summary"]["predicted_join_rows"] == 4
    assert report["upstream_audit"]["result"]["row_delta"] == 0
    assert report["redacted"] is True


def test_config_warning_threshold_and_explicit_raw_override(tmp_path, capsys):
    path = make_config(tmp_path, fail_on="warning")
    assert main(["--config", str(path), "--show-rows", "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["exit_status"] == 1
    assert report["redacted"] is False
    assert report["top_keys"][0]["key"] == ["S001"]


def test_multiple_audits_require_a_unique_name(tmp_path, capsys):
    path = make_config(tmp_path, multiple=True)
    assert main(["--config", str(path)]) == 2
    assert "--audit" in capsys.readouterr().err
    assert main(["--config", str(path), "--audit", "second", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["upstream_audit"]["name"] == "second"


def test_config_and_existing_result_are_protected_from_output(tmp_path, capsys):
    path = make_config(tmp_path)
    before = path.read_bytes()
    assert main(["--config", str(path), "--output", str(path)]) == 2
    assert "overwrite" in capsys.readouterr().err
    assert path.read_bytes() == before
    assert main(["--config", str(path), "--output", str(tmp_path / "merged.tsv")]) == 2


def test_api_rejects_reinterpreting_a_materialized_result(tmp_path):
    path = make_config(tmp_path)
    spec = JoinSpec("result", tmp_path / "samples.tsv", tmp_path / "results.tsv",
                    ("sample_id",), ("sample_id",), "one-to-many", result=tmp_path / "merged.tsv", join_type="left")
    with pytest.raises(TableProofError, match="result.*join|join.*result"):
        analyze(spec, how="inner")


def test_materialized_result_changes_are_part_of_snapshot(tmp_path, monkeypatch):
    make_config(tmp_path)
    result = tmp_path / "merged.tsv"
    spec = JoinSpec("result", tmp_path / "samples.tsv", tmp_path / "results.tsv",
                    ("sample_id",), ("sample_id",), "one-to-many", result=result, join_type="left")
    original = engine.assertions

    def changed_result(request):
        result.write_text(result.read_text() + "S001\tB1\t999\n")
        return original(request)

    monkeypatch.setattr(engine, "assertions", changed_result)
    with pytest.raises(TableProofError, match="Input changed"):
        analyze(spec)


def test_bad_config_types_are_regular_usage_errors(tmp_path, capsys):
    path = make_config(tmp_path)
    path.write_text(path.read_text().replace('fail_on = "error"', 'fail_on = ["error"]'))
    assert main(["--config", str(path)]) == 2
    assert "configuration" in capsys.readouterr().err


def test_ambiguous_direct_config_arguments_are_rejected(tmp_path, capsys):
    path = make_config(tmp_path)
    assert main(["--config", str(path), "--key", "sample_id"]) == 2
    assert "not both" in capsys.readouterr().err
