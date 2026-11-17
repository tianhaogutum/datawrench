"""Produce actual DuckDB COPY CSV variants, without downloading data.

Regenerates the `duckdb-*` fixtures in tests/csv_rewind/fixtures/ (run from a clone:
`uv run --project ../.. --extra duckdb python make_fixtures.py`). Regeneration is an
explicit step; upgrading DuckDB never silently replaces the committed fixture bytes.
"""

import hashlib
import json
from pathlib import Path

import duckdb

from datawrench.csv_rewind.audit import sql_value

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "csv_rewind" / "fixtures"
OUT.mkdir(exist_ok=True)
rows = [
    ("000123", "9007199254740993", "31/01/2024", "1.2300", "TRUE", 'comma, and "quotes"'),
    ("004200", "9223372036854775808", "29/02/2024", "+2.50", "FALSE", "two\nlines"),
    ("123456", "18446744073709551615", "01/03/2024", "1e3", "TRUE", "café | 供应方"),
    ("000000", "100000000000000000001", "04/03/2024", "-0.00", "FALSE", ""),
]
names = ["supplier_id", "long_id", "received", "amount", "active", "note"]
variants = {
    "duckdb-comma.csv": "HEADER true",
    "duckdb-semicolon-crlf.csv": "HEADER true, DELIMITER ';', NEW_LINE '\r\n', FORCE_QUOTE *",
    "duckdb-no-header.tsv": "HEADER false, DELIMITER '\t'",
    "duckdb-compressed.csv.gz": "HEADER true, COMPRESSION gzip",
    "duckdb-backslash.csv": "HEADER true, DELIMITER '|', ESCAPE '\\', FORCE_QUOTE *",
}
manifest = {"engine": f"DuckDB {duckdb.__version__}", "names": names, "rows": rows, "files": {}}
with duckdb.connect(config={"threads": 1, "memory_limit": "128MB"}) as connection:
    connection.execute("CREATE TABLE supplier(" + ", ".join(n + " VARCHAR" for n in names) + ")")
    connection.executemany("INSERT INTO supplier VALUES (?, ?, ?, ?, ?, ?)", rows)
    for name, options in variants.items():
        target = OUT / name
        connection.execute(f"COPY supplier TO {sql_value(str(target))} (FORMAT CSV, {options})")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest["files"][name] = {"copy_options": options, "sha256": digest}
    canonical = OUT / "duckdb-canonical.csv"
    connection.execute(
        f"COPY (SELECT 42::BIGINT AS id, DATE '2024-02-29' AS day, "
        f"1.25::DECIMAL(10,2) AS amount, true AS active) TO {sql_value(str(canonical))} "
        "(FORMAT CSV, HEADER true)"
    )
    manifest["files"][canonical.name] = {
        "sha256": hashlib.sha256(canonical.read_bytes()).hexdigest()
    }
(OUT / "duckdb-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
print(f"Generated {len(variants) + 1} fixtures with DuckDB {duckdb.__version__}")
