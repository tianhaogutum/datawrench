import os
import signal
import subprocess
import sys
import time

import pytest

# Optional `pandera` extra (pandas, Pandera and dfshrink, which needs Python 3.12+);
# skip this module cleanly when it is not installed.
pytest.importorskip("pandas")
pytest.importorskip("pandera")
pytest.importorskip("dfshrink")

from datawrench.failurepack.bundle import list_failures
from datawrench.failurepack.validator import Worker


def alive(pid):
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=3,
    )
    # An already dead, briefly unreaped orphan is not a running process.
    return result.returncode == 0 and result.stdout.strip()[:1] != "Z"


def test_memory_pressure_recycles_serially_without_losing_the_failure(tmp_path):
    data, schema = tmp_path / "data.csv", tmp_path / "schema.py"
    data.write_text("amount\n-1\n")
    schema.write_text(
        "import pandera.pandas as pa\n"
        "schema=pa.DataFrameSchema({'amount':pa.Column(int,pa.Check.ge(0))})\n"
    )
    with Worker(schema, ",", {}, 10) as worker:
        worker.max_rss_bytes = 1
        first = worker.validate(data)
        first_pid = worker.process.pid
        assert worker.recycle_requested
        second = worker.validate(data)
        assert first["failures"] == second["failures"]
        assert worker.process.pid != first_pid
        assert worker.starts == 2
        assert worker.peak_rss_bytes > 0
        assert not alive(first_pid)


def test_oracle_stops_if_parent_is_forcibly_killed(tmp_path):
    data, schema, pidfile = tmp_path / "data.csv", tmp_path / "schema.py", tmp_path / "pid"
    data.write_text("amount\n-1\n")
    schema.write_text(
        "import os,time\nfrom pathlib import Path\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(20)\n"
    )
    code = (
        "from pathlib import Path\nfrom datawrench.failurepack.validator import Worker\n"
        f"with Worker(Path({str(schema)!r}), ',', {{}}, 30) as worker:\n"
        f" worker.validate(Path({str(data)!r}))\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", code])
    worker_pid = None
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "schema did not start within the test deadline"
        worker_pid = int(pidfile.read_text())
        parent.kill()
        parent.wait(timeout=3)
        deadline = time.monotonic() + 5
        while alive(worker_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(worker_pid), "orphaned oracle remained running"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        if worker_pid is not None and alive(worker_pid):
            os.killpg(worker_pid, signal.SIGKILL)


def test_sigterm_cleans_partial_work_and_stops_the_oracle(tmp_path):
    data, schema, pidfile = tmp_path / "data.csv", tmp_path / "schema.py", tmp_path / "pid"
    data.write_text("amount\n-1\n2\n")
    schema.write_text(
        "import os,time\nfrom pathlib import Path\nimport pandera.pandas as pa\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "if os.environ.get('SLOW_SCHEMA'): time.sleep(20)\n"
        "schema=pa.DataFrameSchema({'amount':pa.Column(int,pa.Check.ge(0))})\n"
    )
    failures = list_failures(data, schema, ",", {}, 10, 32 * 1024 * 1024)
    target = failures[0]["id"]
    pidfile.unlink()
    parent = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "datawrench",
            "failurepack",
            "reduce",
            str(data),
            "--schema",
            str(schema),
            "--target",
            target,
            "--out",
            str(tmp_path / "output"),
            "--timeout",
            "30",
        ],
        env={**os.environ, "SLOW_SCHEMA": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker_pid = None
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pidfile.exists()
        worker_pid = int(pidfile.read_text())
        parent.terminate()
        _, stderr = parent.communicate(timeout=5)
        assert parent.returncode == 130, stderr
        assert not (tmp_path / "output").exists()
        assert not list(tmp_path.glob(".output-*"))
        assert not alive(worker_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        for stream in (parent.stdout, parent.stderr):
            stream.close()
        if worker_pid is not None and alive(worker_pid):
            os.killpg(worker_pid, signal.SIGKILL)
