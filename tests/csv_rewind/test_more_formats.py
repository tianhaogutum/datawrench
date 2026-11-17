import csv
from pathlib import Path

import duckdb
import pytest

from datawrench.csv_rewind import AuditError, AuditOptions, audit
from datawrench.csv_rewind.audit import read_expression, sql_value


@pytest.mark.parametrize("delimiter", ["||", "::", "<->"])
@pytest.mark.parametrize("compression", ["none", "zstd"])
def test_native_multi_character_and_zstd_exports(tmp_path, delimiter, compression):
    slug = {"||": "pipe", "::": "colon", "<->": "arrow"}[delimiter]
    root = Path(__file__).resolve().parent / "fixtures"
    source = root / (f"duckdb-multi-{slug}.csv" + (".zst" if compression == "zstd" else ""))
    note = "text" + delimiter + 'with "quote" and\nline'
    report = audit(source, AuditOptions(header=True, delimiter=delimiter, types={"id": "BIGINT"}))
    assert report["rows_audited"] == 1 and report["lossy_cells"] == 1
    with duckdb.connect(config={"threads": 1}) as connection:
        assert connection.execute(report["frozen"]["sql"]).fetchall() == [("0001", note)]


def test_csvkit_bom_semicolon_producer_output():
    root = Path(__file__).resolve().parent / "fixtures"
    source = root / "csvkit-bom-semicolon.csv"
    assert source.read_bytes().startswith(b"\xef\xbb\xbf")
    with source.open(encoding="utf-8-sig", newline="") as stream:
        produced = list(csv.reader(stream, delimiter=";"))
    with (root / "survey.csv").open(newline="") as stream:
        expected = list(csv.reader(stream))
    assert produced == expected
    report = audit(source, AuditOptions(header=True))
    with duckdb.connect(config={"threads": 1}) as connection:
        connection.execute("PRAGMA disable_optimizer")
        actual = connection.execute(
            "SELECT CAST(COLUMNS(*) AS VARCHAR) FROM "
            + read_expression(source, report["frozen"]["parameters"])
        ).fetchall()
    assert actual == [tuple(row) for row in expected[1:]]


def test_utf16_big_endian_is_refused_with_a_working_recipe(tmp_path):
    source = tmp_path / "big-endian.csv"
    text = "id,note\n1,café\n"
    data = b"\xfe\xff" + text.encode("utf-16-be")
    source.write_bytes(data)
    assert source.read_text(encoding="utf-16") == text
    with pytest.raises(AuditError, match="UTF-16BE.*transcoding"):
        audit(source, AuditOptions(header=True, encoding="utf-16"))
    assert source.read_bytes() == data


def test_long_malformed_record_diagnostic_is_bounded(tmp_path):
    source = tmp_path / "long-bad-row.csv"
    source.write_text("id,note\n" + "1,ok\n" * 3000 + '2,"' + "x" * 200000 + '"junk\n')
    with pytest.raises(AuditError) as raised:
        audit(
            source,
            AuditOptions(
                header=True, delimiter=",", quote='"', escape='"', new_line=r"\n", sample_size=1
            ),
        )
    assert len(str(raised.value)) < 6_000
    assert raised.value.__cause__ is not None


def test_large_field_evidence_is_bounded(tmp_path):
    source = tmp_path / "large-field.csv"
    value = "x" * 300_000
    with duckdb.connect(config={"threads": 1, "memory_limit": "128MB"}) as connection:
        connection.execute(
            f"COPY (SELECT {sql_value(value)} AS value FROM range(3)) "
            f"TO {sql_value(str(source))} (HEADER true)"
        )
    report = audit(source, AuditOptions(header=True, types={"value": "INTEGER"}, max_examples=1))
    assert report["rows_audited"] == 3 and report["lossy_cells"] == 3
    assert report["examples"][0]["raw"]["length"] == 300_000
    assert len(report["examples"][0]["raw"]["text"]) == 512
    assert report["frozen"]["verified_rows"] == 3


@pytest.mark.parametrize("skip", [0, 1])
def test_invalid_utf16_during_width_fallback_is_a_clean_cli_error(tmp_path, skip, capsys):
    from datawrench.csv_rewind.cli import main

    source = tmp_path / "broken-utf16.csv"
    source.write_bytes(b"\xff\xfe" + ('"a"\t"b"\r'.encode("utf-16-le")[:-1]))
    report = tmp_path / "must-not-exist.json"
    code = main(
        [
            str(source),
            "--header",
            "no",
            "--delimiter",
            "\t",
            "--quote",
            '"',
            "--escape",
            '"',
            "--new-line",
            "cr",
            "--encoding",
            "utf-16",
            "--skip",
            str(skip),
            "--report",
            str(report),
        ]
    )
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err
    assert not report.exists()
