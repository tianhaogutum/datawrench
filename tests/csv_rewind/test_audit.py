import io
import json
from pathlib import Path

import duckdb
import pytest

from datawrench.csv_rewind import AuditError, AuditOptions, audit
from datawrench.csv_rewind.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_real_engine_precision_and_frozen_replay():
    events = io.StringIO()
    report = audit(
        FIXTURES / "survey.csv",
        AuditOptions(
            types={"code": "BIGINT", "big": "DOUBLE"},
            header=True,
        ),
        events=events,
    )
    rows = [json.loads(line) for line in events.getvalue().splitlines()]
    assert report["rows_audited"] == 2
    assert report["lossy_cells"] == 10
    assert rows[0]["raw"] == "00123"
    assert rows[0]["rendered"] == "123"
    assert rows[1]["reason"] == "numeric_value_change"
    assert rows[1]["rendered"] == "9007199254740992.0"
    with duckdb.connect(config={"threads": 1}) as connection:
        preserved = connection.execute(report["frozen"]["sql"]).fetchall()
    assert preserved[0] == ("00123", "9007199254740993", "01/02/2024", "1.00", "TRUE")


def test_nulls_whitespace_multiline_and_failed_cast(tmp_path):
    source = tmp_path / "input.csv"
    source.write_text('id,v\n1,\n2,""\n3, 42 \n4,"two\nlines"\n', encoding="utf-8")
    events = io.StringIO()
    report = audit(source, AuditOptions(types={"v": "INTEGER"}, header=True), events=events)
    changed = [json.loads(line) for line in events.getvalue().splitlines()]
    assert report["rows_audited"] == 4
    assert [event["reason"] for event in changed] == [
        "null_coercion",
        "null_coercion",
        "whitespace_change",
        "parse_error",
    ]
    assert changed[-1]["data_row"] == 4
    assert changed[-1]["raw"] == "two\nlines"
    with duckdb.connect(config={"threads": 1}) as connection:
        assert connection.execute(report["frozen"]["sql"]).fetchall()[-1] == (4, "two\nlines")


def test_lossless_schema_retains_types(tmp_path):
    source = tmp_path / "ok.csv"
    source.write_text("id,day,active\n23,2024-01-31,true\n24,2024-02-01,false\n")
    report = audit(source, AuditOptions(header=True))
    assert report["lossless"]
    assert [c["recommended_type"] for c in report["columns"]] == ["BIGINT", "DATE", "BOOLEAN"]


def test_cli_writes_outputs_and_cleans_up_on_error(tmp_path, capsys):
    source = tmp_path / "bad.csv"
    source.write_text("id\n001\n002\n")
    output = tmp_path / "report.json"
    events = tmp_path / "losses.jsonl"
    frozen = tmp_path / "read.sql"
    code = main(
        [
            str(source),
            "--type",
            "id=BIGINT",
            "--header",
            "yes",
            "--report",
            str(output),
            "--events",
            str(events),
            "--freeze",
            str(frozen),
        ]
    )
    assert code == 1
    assert json.loads(output.read_text())["lossy_cells"] == 2
    assert len(events.read_text().splitlines()) == 2
    previous = output.read_text()
    assert main([str(source), "--type", "missing=DATE", "--report", str(output)]) == 2
    assert "Unknown candidate" in capsys.readouterr().err
    assert output.read_text() == previous
    assert not list(tmp_path.glob(".csv-rewind-*.tmp"))


@pytest.mark.parametrize("value", ["INTEGER); DROP TABLE x; --", "DECIMAL(39,0)", "JSON"])
def test_rejects_unsupported_or_unsafe_types(value):
    with pytest.raises(AuditError, match="Unsupported type"):
        audit(FIXTURES / "survey.csv", AuditOptions(types={"code": value}))
