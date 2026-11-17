"""Failure-oriented cases, not just mirrors of the implementation."""

import csv
import io
import json

import duckdb
import pytest

from datawrench.csv_rewind import AuditError, AuditOptions, audit
from datawrench.csv_rewind.cli import main


def test_no_null_coercion_can_be_replayed(tmp_path):
    source = tmp_path / "empty-fields.csv"
    source.write_text('id,v\n1,\n2,""\n')
    report = audit(source, AuditOptions(header=True, null_values=()))
    assert report["lossless"]
    with duckdb.connect(config={"threads": 1}) as connection:
        assert connection.execute(report["frozen"]["sql"]).fetchall() == [(1, ""), (2, "")]


@pytest.mark.parametrize(
    "body",
    [
        b"id,v\n1,ok\n2,bad,extra\n",
        b"id,v\n1,ok\n2\n",
        b'id,v\n1,ok\n2,"unclosed\n',
        b"id,v\n1,ok\n2,\xff\n",
    ],
)
def test_structural_or_encoding_damage_does_not_certify(tmp_path, body):
    source = tmp_path / "broken.csv"
    source.write_bytes(body)
    with pytest.raises(AuditError):
        audit(source, AuditOptions(header=True, delimiter=",", quote='"'))


def test_all_rows_including_anomaly_after_sniffing(tmp_path):
    source = tmp_path / "late.csv"
    with duckdb.connect(config={"threads": 1, "memory_limit": "128MB"}) as connection:
        connection.execute(
            "COPY (SELECT CASE WHEN range=10000 THEN 'bad' ELSE CAST(range AS VARCHAR) END "
            "AS id FROM range(10001)) TO ? (FORMAT CSV, HEADER true)",
            [str(source)],
        )
    events = io.StringIO()
    report = audit(
        source, AuditOptions(header=True, types={"id": "BIGINT"}, sample_size=1), events=events
    )
    assert report["rows_audited"] == 10001
    assert report["lossy_cells"] == 1
    assert json.loads(events.getvalue())["data_row"] == 10001
    assert json.loads(events.getvalue())["reason"] == "parse_error"


def test_unusual_headers_and_literal_path_are_data(tmp_path):
    source = tmp_path / "supplier'[1]*.csv"
    decoy = tmp_path / "supplier'1-extra.csv"
    decoy.write_text("id\nDECOY\n")
    expected = ["0001", "供应方", "hello'\\world", "two\nlines"]
    with source.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["a'", 'a"', "供应方代码", "x); SELECT 42; --"])
        writer.writerow(expected)
    report = audit(source, AuditOptions(header=True, null_values=()))
    assert report["rows_audited"] == 1
    with duckdb.connect(config={"threads": 1}) as connection:
        assert connection.execute(report["frozen"]["sql"]).fetchone() == tuple(expected)


def test_source_mutation_invalidates_result(tmp_path):
    source = tmp_path / "mutating.csv"
    source.write_text("id\n001\n002\n")

    class MutatingWriter(io.StringIO):
        def write(self, value):
            source.write_text("id\nCHANGED\n")
            return super().write(value)

    with pytest.raises(AuditError, match="changed during"):
        audit(source, AuditOptions(header=True, types={"id": "BIGINT"}), events=MutatingWriter())


def test_examples_are_bounded_but_events_are_complete(tmp_path):
    source = tmp_path / "evidence.csv"
    source.write_text('id\n"' + "0" * 1000 + '1"\n02\n03\n')
    events = io.StringIO()
    report = audit(
        source,
        AuditOptions(header=True, types={"id": "BIGINT"}, max_examples=1, max_cell_chars=8),
        events=events,
    )
    assert report["lossy_cells"] == 3
    assert report["examples_omitted"] == 2
    example = report["examples"][0]["raw"]
    assert example["truncated"] and example["length"] == 1001
    assert len(example["text"]) == 8 and len(example["sha256"]) == 64
    assert len(json.loads(events.getvalue().splitlines()[0])["raw"]) == 1001


def test_output_path_collision_cannot_replace_input(tmp_path, capsys):
    source = tmp_path / "input.csv"
    body = "id\n001\n"
    source.write_text(body)
    assert main([str(source), "--events", str(source)]) == 2
    assert source.read_text() == body
    assert "overwrite" in capsys.readouterr().err


@pytest.mark.parametrize("encoding", ["utf-16", "latin-1"])
def test_supported_upstream_encodings(tmp_path, encoding):
    source = tmp_path / "encoded.csv"
    source.write_text("id,note\n001,café\n002,naïve\n", encoding=encoding)
    report = audit(source, AuditOptions(header=True, encoding=encoding, null_values=()))
    assert report["lossless"]
    with duckdb.connect(config={"threads": 1}) as connection:
        assert connection.execute(report["frozen"]["sql"]).fetchall()[0] == ("001", "café")


def test_header_only_input_is_not_certified(tmp_path):
    source = tmp_path / "empty.csv"
    source.write_text("id,name\n")
    with pytest.raises(AuditError, match="No data records"):
        audit(source, AuditOptions(header=True))


def test_interruption_discards_staged_outputs(tmp_path, monkeypatch, capsys):
    import datawrench.csv_rewind.cli as cli

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    source = tmp_path / "input.csv"
    source.write_text("id\n001\n")
    target = tmp_path / "events.jsonl"
    target.write_text("previous output\n")
    monkeypatch.setattr(cli, "audit", interrupt)
    assert cli.main([str(source), "--events", str(target)]) == 130
    assert target.read_text() == "previous output\n"
    assert "interrupted" in capsys.readouterr().err
    assert not list(tmp_path.glob(".csv-rewind-*.tmp"))


def test_disk_full_during_evidence_keeps_previous_outputs(tmp_path, monkeypatch, capsys):
    import errno

    import datawrench.csv_rewind.cli as cli

    original_audit = cli.audit

    class DiskFullWriter:
        def __init__(self, stream):
            self.stream = stream
            self.calls = 0

        def write(self, value):
            self.calls += 1
            if self.calls > 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return self.stream.write(value)

    def failing_audit(source, options, *, events):
        return original_audit(source, options, events=DiskFullWriter(events))

    source = tmp_path / "input.csv"
    source.write_text("id\n001\n002\n")
    outputs = [tmp_path / name for name in ("report.json", "events.jsonl", "read.sql")]
    for output in outputs:
        output.write_text("previous\n")
    monkeypatch.setattr(cli, "audit", failing_audit)
    code = cli.main(
        [
            str(source),
            "--header",
            "yes",
            "--type",
            "id=BIGINT",
            "--report",
            str(outputs[0]),
            "--events",
            str(outputs[1]),
            "--freeze",
            str(outputs[2]),
        ]
    )
    assert code == 2
    assert "No space left" in capsys.readouterr().err
    assert all(output.read_text() == "previous\n" for output in outputs)
    assert not list(tmp_path.glob(".csv-rewind-*.tmp"))
