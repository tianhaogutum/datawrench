"""Pinned 2.0 producer: older COPY writers truncate multi-character delimiters.

Regenerates the `duckdb-multi-*` fixtures in tests/csv_rewind/fixtures/. It needs a
DuckDB 2.0+ writer (install one in a separate environment); every supported reader,
including the 1.x series datawrench installs, is tested against the pinned bytes.
"""

import hashlib
import json
from pathlib import Path

import duckdb

from datawrench.csv_rewind.audit import sql_value

if int(duckdb.__version__.split(".")[0]) < 2:
    raise SystemExit(
        "Generate these fixtures with DuckDB 2.0+; older COPY writers truncate delimiters"
    )
root = Path(__file__).resolve().parents[2] / "tests" / "csv_rewind" / "fixtures"
manifest = {"producer": f"DuckDB {duckdb.__version__} COPY", "files": {}}
with duckdb.connect(config={"threads": 1, "memory_limit": "128MB"}) as connection:
    for delimiter, slug in [("||", "pipe"), ("::", "colon"), ("<->", "arrow")]:
        note = "text" + delimiter + 'with "quote" and\nline'
        for compression in ["none", "zstd"]:
            name = f"duckdb-multi-{slug}.csv" + (".zst" if compression == "zstd" else "")
            target = root / name
            connection.execute(
                f"COPY (SELECT '0001' AS id, {sql_value(note)} AS note) TO "
                f"{sql_value(str(target))} (HEADER true, DELIMITER {sql_value(delimiter)}, "
                f"COMPRESSION {sql_value(compression)})"
            )
            manifest["files"][name] = {
                "delimiter": delimiter,
                "note": note,
                "compression": compression,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
(root / "advanced-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print("Generated six pinned native CSV/Zstandard fixtures")
