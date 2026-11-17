"""A complete local first-delivery / next-delivery review workflow.

Run from a clone: `uv run --project ../.. --extra duckdb python repeated_delivery.py`.
Writes reports, full evidence, recipes and verified SQL under ./output/.
"""

import csv
import json
from pathlib import Path

import duckdb

from datawrench.csv_rewind import AuditOptions, audit, load_config

output = Path(__file__).resolve().parent / "output"
output.mkdir(exist_ok=True)
names = ["id", "long_id", "day", "amount", "active"]
first_rows = [
    ["12", "9007199254740992.0", "2024-01-31", "1.00", "true"],
    ["13", "9007199254740994.0", "2024-02-01", "2.50", "false"],
]
later_rows = [
    ["0012", "9007199254740993", "01/02/2024", "1.000", "TRUE"],
    ["14", "9007199254740994.0", "31/12/2024", "2.50", "false"],
]
for filename, rows in [("first.csv", first_rows), ("later.csv", later_rows)]:
    with (output / filename).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(names)
        writer.writerows(rows)
first = audit(
    output / "first.csv",
    AuditOptions(
        header=True,
        types={"long_id": "DOUBLE", "amount": "DECIMAL(10,2)"},
    ),
)
assert first["lossless"]
config = output / "accepted.json"
config.write_text(json.dumps(first["frozen"]["config"], indent=2) + "\n")
with (output / "losses.jsonl").open("w", encoding="utf-8") as events:
    later = audit(output / "later.csv", load_config(config), events=events)
assert later["lossy_cells"] == 6
(output / "later-report.json").write_text(json.dumps(later, indent=2) + "\n")
(output / "later-read.sql").write_text(later["frozen"]["sql"])
with duckdb.connect(config={"threads": 1, "memory_limit": "128MB"}) as connection:
    assert connection.execute(later["frozen"]["sql"]).fetchall() == [tuple(r) for r in later_rows]
print("First delivery: exact; accepted recipe saved.")
print("Later delivery: 6 changed cells; every original string survives the verified frozen import.")
print("Reports and evidence:", output)
