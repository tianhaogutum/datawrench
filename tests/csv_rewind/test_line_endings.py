import csv
import json
from pathlib import Path

import duckdb
import pytest

from datawrench.csv_rewind import AuditError, AuditOptions, audit
from datawrench.csv_rewind.audit import read_expression


@pytest.mark.parametrize(
    ("record_newline", "option"),
    [
        ("\n", r"\n"),
        ("\r\n", r"\r\n"),
        ("\r", r"\r"),
    ],
)
def test_multiline_content_before_first_record_terminator(tmp_path, record_newline, option):
    source = tmp_path / "multiline-first.csv"
    rows = [["crlf\r\ninside", "0"], ["two\nlines", "0001"], ["bare\rcarriage", "NA"]]
    with source.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(
            stream, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator=record_newline
        )
        writer.writerows(rows)
    report = audit(
        source,
        AuditOptions(
            header=False, delimiter=";", quote='"', escape='"', new_line=option, null_values=()
        ),
    )
    assert report["rows_audited"] == 3
    with duckdb.connect(config={"threads": 1}) as connection:
        assert connection.execute(report["frozen"]["sql"]).fetchall() == [tuple(r) for r in rows]


def test_utf16_supplementary_decoder_failure_is_explained(tmp_path):
    source = tmp_path / "valid-utf16.csv"
    original = "id,note\n1,🦆\n"
    source.write_bytes(original.encode("utf-16"))
    assert source.read_text(encoding="utf-16") == original
    try:
        report = audit(source, AuditOptions(header=True, encoding="utf-16"))
    except AuditError as error:
        assert "Transcode to UTF-8" in str(error)
    else:  # A future upstream fix should not make this adapter test fail.
        assert report["frozen"]["verified_rows"] == 1


@pytest.mark.parametrize("bad_row", ['2,"unclosed\n', '2,"closed"junk\n', "2,extra,field\n", "2\n"])
def test_default_strict_reader_rejects_damage_after_sample(tmp_path, bad_row):
    source = tmp_path / "late-damage.csv"
    source.write_text("id,v\n" + "1,ok\n" * 3000 + bad_row)
    with pytest.raises(AuditError):
        audit(
            source,
            AuditOptions(
                header=True, delimiter=",", quote='"', escape='"', new_line=r"\n", sample_size=1
            ),
        )


@pytest.mark.parametrize("index", [176, 207])
def test_reproduced_utf16_cr_sniffer_variants(index):
    root = Path(__file__).resolve().parent / "fixtures"
    source = root / f"python-csv-utf16-cr-{index}.csv"
    meta = json.loads(source.with_suffix(".json").read_text())
    report = audit(
        source,
        AuditOptions(
            header=False,
            delimiter=meta["delimiter"],
            quote=meta["quote"],
            escape=meta["quote"],
            encoding="utf-16",
            new_line=r"\r",
            null_values=(),
        ),
    )
    with duckdb.connect(config={"threads": 1}) as connection:
        actual = connection.execute(
            "SELECT CAST(COLUMNS(*) AS VARCHAR) FROM "
            + read_expression(source, report["frozen"]["parameters"])
        ).fetchall()
    assert actual == [tuple(row) for row in meta["rows"]]
    assert report["frozen"]["verified_rows"] == len(meta["rows"])
    if report["type_inference"] == "unavailable_defaulted_to_text":
        assert all(c["inferred_type"] is None for c in report["columns"])
