import json

import pytest

from datawrench.csv_rewind import AuditError, AuditOptions, audit, load_config
from datawrench.csv_rewind.cli import main
from datawrench.csv_rewind.config import MAX_JSON_BYTES, read_json_object


def recipe(tmp_path):
    source = tmp_path / "day-one.csv"
    source.write_text("id,amount,note\n12,1.25,\n13,2.50,hello\n")
    report = audit(source, AuditOptions(header=True))
    path = tmp_path / "accepted.json"
    path.write_text(json.dumps(report["frozen"]["config"]))
    return source, path, report


def test_reused_recipe_detects_new_leading_zero_and_retains_null_text(tmp_path):
    source, config, original = recipe(tmp_path)
    accepted = audit(source, load_config(config))
    assert accepted["lossless"]  # Empty VARCHAR fields are preserved by the frozen reader.
    assert accepted["baseline"]["source_sha256"] == original["input"]["sha256"]
    delivery = tmp_path / "day-two.csv"
    delivery.write_text("id,amount,note\n0012,1.25,\n14,2.50,hello\n")
    report = audit(delivery, load_config(config))
    assert report["lossy_cells"] == 1
    assert report["examples"][0]["reason"] == "leading_zero_loss"
    assert report["columns"][0]["recommended_type"] == "VARCHAR"
    assert report["input"]["sha256"] != report["baseline"]["source_sha256"]


@pytest.mark.parametrize(
    "header", ["amount,id,note", "id,amount,changed", "id,amount", "id,amount,note,new"]
)
def test_recipe_rejects_reorder_rename_missing_and_extra_columns(tmp_path, header):
    _, config, _ = recipe(tmp_path)
    source = tmp_path / "changed.csv"
    source.write_text(header + "\n" + ",".join("1" for _ in header.split(",")) + "\n")
    with pytest.raises(AuditError, match="Column layout changed"):
        audit(source, load_config(config))


def test_cli_exports_and_reloads_config_atomically(tmp_path, capsys):
    source, _, _ = recipe(tmp_path)
    config = tmp_path / "recipe.json"
    assert main([str(source), "--header", "yes", "--write-config", str(config)]) == 1
    assert main([str(source), "--read-config", str(config), "--json"]) == 0
    output = capsys.readouterr().out
    assert '"baseline"' in output
    previous = config.read_text()
    assert main([str(source), "--read-config", str(config), "--write-config", str(config)]) == 2
    assert config.read_text() == previous
    assert not list(tmp_path.glob(".csv-rewind-*.tmp"))


def test_explicit_type_override_is_still_checked_against_baseline_layout(tmp_path):
    source, config, _ = recipe(tmp_path)
    report = tmp_path / "report.json"
    assert (
        main(
            [
                str(source),
                "--read-config",
                str(config),
                "--type",
                "id=VARCHAR",
                "--report",
                str(report),
            ]
        )
        == 0
    )
    assert json.loads(report.read_text())["columns"][0]["candidate_type"] == "VARCHAR"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ignore_errors", True),
        ("strict_mode", False),
        ("null_padding", True),
        ("hive_partitioning", True),
        ("auto_detect", True),
        ("comment", "#"),
        ("force_not_null", ["id"]),
        ("header", "true"),
        ("skip", True),
        ("skip", -1),
        ("nullstr", []),
        ("nullstr", [1]),
        ("encoding", 5),
        ("dateformat", 1),
        ("unknown_option", "anything"),
    ],
)
def test_unsafe_or_mistyped_reader_parameters_are_rejected(tmp_path, key, value):
    _, config, _ = recipe(tmp_path)
    body = json.loads(config.read_text())
    body["parameters"][key] = value
    config.write_text(json.dumps(body))
    with pytest.raises(AuditError):
        load_config(config)


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'{"id":"BIGINT","id":"VARCHAR"}',
        b'{"id": NaN}',
        b"\xff",
        b"{broken}",
        b"[" * 2000 + b"]" * 2000,
        b'{"version":' + b"1" * 5000 + b"}",
    ],
)
def test_invalid_json_is_a_clean_cli_error(tmp_path, body, capsys):
    source = tmp_path / "input.csv"
    source.write_text("id\n1\n")
    schema = tmp_path / "schema.json"
    schema.write_bytes(body)
    assert main([str(source), "--types", str(schema)]) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_bom_json_and_schema_size_bound(tmp_path):
    path = tmp_path / "schema.json"
    path.write_bytes(b'\xef\xbb\xbf{"id":"VARCHAR"}')
    assert read_json_object(path) == {"id": "VARCHAR"}
    path.write_bytes(b" " * (MAX_JSON_BYTES + 1))
    with pytest.raises(AuditError, match="4 MiB"):
        read_json_object(path)


def test_engine_version_change_is_reported_but_verified(tmp_path):
    source, config, _ = recipe(tmp_path)
    body = json.loads(config.read_text())
    body["engine_version"] = "0.0.0"
    config.write_text(json.dumps(body))
    report = audit(source, load_config(config))
    assert report["lossless"] and report["frozen"]["verified"]
    assert any("0.0.0" in warning for warning in report["warnings"])


def test_recipe_does_not_reinfer_date_or_timestamp_formats(tmp_path):
    first = tmp_path / "iso.csv"
    first.write_text("day,at\n2024-01-31,2024-01-31 01:02:03\n")
    original = audit(first, AuditOptions(header=True))
    config = tmp_path / "iso.json"
    config.write_text(json.dumps(original["frozen"]["config"]))
    later = tmp_path / "new-format.csv"
    later.write_text("day,at\n31/01/2024,31/01/2024 01:02:03\n")
    report = audit(later, load_config(config))
    assert [example["reason"] for example in report["examples"]] == ["parse_error", "parse_error"]
    assert "dateformat" not in report["frozen"]["parameters"]
    assert "timestampformat" not in report["frozen"]["parameters"]
    fresh = audit(later, AuditOptions(header=True))
    assert all(example["reason"] == "temporal_format_change" for example in fresh["examples"])
