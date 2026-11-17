"""Create the example ledger database (a real SQLite file) from ledger.sql next to this script."""
from pathlib import Path
import sqlite3
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: python create_ledger.py TARGET.db")
target = Path(sys.argv[1])
if target.exists():
    raise SystemExit(f"Refusing to overwrite {target}")
with sqlite3.connect(target) as conn:
    conn.executescript((Path(__file__).parent / "ledger.sql").read_text())
print(target)
