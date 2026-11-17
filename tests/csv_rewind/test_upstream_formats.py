import json
from pathlib import Path

import duckdb
import pytest

from datawrench.csv_rewind import AuditOptions, audit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "duckdb-manifest.json").read_text())


@pytest.mark.parametrize(
    "filename",
    [
        "duckdb-comma.csv",
        "duckdb-semicolon-crlf.csv",
        "duckdb-no-header.tsv",
        "duckdb-compressed.csv.gz",
        "duckdb-backslash.csv",
    ],
)
def test_real_duckdb_copy_variants(filename):
    no_header = filename.endswith(".tsv")
    names = [f"column{i}" for i in range(6)] if no_header else MANIFEST["names"]
    candidates = dict(
        zip(names, ["BIGINT", "DOUBLE", "DATE", "DOUBLE", "BOOLEAN", "VARCHAR"], strict=True)
    )
    report = audit(
        FIXTURES / filename,
        AuditOptions(
            types=candidates,
            header=not no_header,
            date_format="%d/%m/%Y",
        ),
    )
    assert report["rows_audited"] == 4
    assert report["lossy_cells"] > 10
    with duckdb.connect(config={"threads": 1}) as connection:
        assert connection.execute(report["frozen"]["sql"]).fetchall() == [
            tuple(row) for row in MANIFEST["rows"]
        ]


def test_actual_typed_copy_is_lossless():
    report = audit(FIXTURES / "duckdb-canonical.csv", AuditOptions(header=True))
    assert report["lossless"]


def test_downloaded_csvkit_realdata():
    report = audit(FIXTURES / "csvkit-FY09_EDU_Recipients_by_State.csv", AuditOptions(header=True))
    assert report["rows_audited"] == 53
    assert report["columns"][2]["name"] == "Code"
    with duckdb.connect(config={"threads": 1}) as connection:
        values = connection.execute(report["frozen"]["sql"]).fetchall()
    assert len(values) == 53
    assert values[0][1] == "AL"
