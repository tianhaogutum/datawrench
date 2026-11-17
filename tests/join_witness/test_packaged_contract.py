import copy
import json
from importlib.resources import files

import jsonschema
import pytest

from datawrench.join_witness import analyze
from datawrench.join_witness.cli import main
from .test_engine import ROOT, example_spec


def schema():
    return json.loads(files("datawrench.join_witness").joinpath("data").joinpath("report-v1.schema.json").read_text())


@pytest.mark.parametrize("options", [{}, {"redact": True}, {"sample_bytes": 0}, {"rank_by": "left:amount"}, {"top": 0}])
def test_report_schema_validates_real_reports_and_is_itself_valid(options):
    contract = schema()
    jsonschema.Draft202012Validator.check_schema(contract)
    report = analyze(example_spec(), **options)
    jsonschema.Draft202012Validator(contract).validate(report)


def test_schema_rejects_floats_for_exact_sums_and_raw_rows_when_redacted():
    validator = jsonschema.Draft202012Validator(schema())
    report = analyze(example_spec(), left_measures=("amount",), redact=True)
    invalid = copy.deepcopy(report)
    invalid["measures"]["left"]["amount"]["predicted_join_sum"] = 55.0
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    invalid = copy.deepcopy(report)
    invalid["top_keys"][0]["left_records"][0]["values"] = {"secret": "raw"}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)


def test_schema_and_demo_run_without_repository_relative_input_paths(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["--print-schema"]) == 0
    jsonschema.Draft202012Validator.check_schema(json.loads(capsys.readouterr().out))
    assert main(["--demo", "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    jsonschema.Draft202012Validator(schema()).validate(report)
    assert report["summary"]["predicted_join_rows"] == 6
    assert report["measures"]["left"]["amount"]["predicted_join_sum"] == "55.00"


def test_packaged_demo_assets_match_the_documented_polars_exports():
    for name in ("orders.csv", "customers.csv"):
        assert files("datawrench.join_witness").joinpath("data").joinpath(name).read_bytes() == (ROOT / "examples" / "join_witness" / name).read_bytes()
