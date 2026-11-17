"""Run the full selected-failure integration and replay it.

With the `pandera` extra installed, from the repository root: python examples/failurepack/demo.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from datawrench.failurepack.bundle import list_failures, reduce_to_bundle

root = Path(__file__).resolve().parent
# A fresh directory in the system temporary folder; nothing is written next to the example.
output = Path(tempfile.mkdtemp(prefix="failurepack-demo-")) / "bundle"
failures = list_failures(root / "orders.csv", root / "orders.yaml", ",", {}, 10, 32 * 1024 * 1024)
target = next(f["id"] for f in failures if f["check"] == "field_uniqueness")
manifest = reduce_to_bundle(root / "orders.csv", root / "orders.yaml", output, target)
print(
    f"Selected duplicate-ID failure: {manifest['source']['rows']} "
    f"-> {manifest['result']['rows']} rows"
)
print(f"Bundle: {output}")
raise SystemExit(subprocess.run([sys.executable, str(output / "replay.py")], timeout=30).returncode)
