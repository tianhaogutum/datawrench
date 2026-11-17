"""One serial, bounded validator subprocess; no parallel validation workers."""

import json
import math
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

from ._worker import validate_options


class ValidationError(RuntimeError):
    pass


def read_options(path: Path | None) -> dict:
    if path is not None and path.stat().st_size > 1024 * 1024:
        raise ValueError("pandas read options exceed the 1 MiB limit")
    try:
        options = {} if path is None else json.loads(path.read_text(encoding="utf-8"))
    except RecursionError as exc:
        raise ValueError("pandas read options are nested too deeply") from exc
    return validate_options(options)


class Worker:
    def __init__(self, schema: Path, delimiter: str, options: dict, timeout: float = 10):
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("timeout must be finite and between 0 and 300 seconds")
        if schema.suffix.lower() not in {".yaml", ".yml", ".py"}:
            raise ValueError("schema must be Pandera YAML or a self-contained Python .py file")
        self.options = validate_options(options)
        if not schema.is_file():
            raise ValueError(f"schema file does not exist or is not a regular file: {schema}")
        self.schema = schema.resolve()
        self.delimiter = delimiter
        self.timeout = timeout
        self.process = None
        self.requests = 0
        self.starts = 0
        self.max_requests = 128
        self.max_rss_bytes = 192 * 1024 * 1024
        self.peak_rss_bytes = 0
        self.recycle_requested = False

    def _start(self):
        environment = {
            **os.environ,
            "POLARS_MAX_THREADS": "2",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "FAILUREPACK_PARENT_PID": str(os.getpid()),
        }
        self.process = subprocess.Popen(
            [sys.executable, "-m", "datawrench.failurepack._worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=environment,
            start_new_session=True,
        )
        self.requests = 0
        self.recycle_requested = False
        self.starts += 1

    def validate(self, data: Path) -> dict:
        if (
            self.process is None
            or self.process.poll() is not None
            or self.requests >= self.max_requests
            or self.recycle_requested
        ):
            self.close()
            self._start()
        request = {
            "data": str(data.resolve()),
            "schema": str(self.schema),
            "delimiter": self.delimiter,
            "pandas_options": self.options,
        }
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            ready, _, _ = select.select([self.process.stdout], [], [], self.timeout)
            if not ready:
                self.close()
                return {"status": "error", "kind": "Timeout", "message": "validator timed out"}
            line = self.process.stdout.readline(1024 * 1024 + 1)
            if not line or len(line) > 1024 * 1024:
                raise ValueError("validator exited or exceeded its response limit")
            result = json.loads(line)
            if result.get("status") not in {"ok", "error"}:
                raise ValueError("invalid validator response")
            self.requests += 1
            peak = result.get("worker_peak_rss_bytes", 0)
            self.peak_rss_bytes = max(self.peak_rss_bytes, peak)
            self.recycle_requested = peak >= self.max_rss_bytes
            return result
        except (OSError, ValueError) as exc:
            self.close()
            return {"status": "error", "kind": "WorkerError", "message": str(exc)}

    def close(self):
        if self.process is None:
            return
        process, self.process = self.process, None
        try:
            if process.poll() is None:
                process.stdin.write('{"quit":true}\n')
                process.stdin.flush()
                process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # The process may have exited between poll()/wait() and this
                # group signal. Do not let a stale or inaccessible group mask
                # the worker error; still stop the direct child if it remains.
                if process.poll() is None:
                    try:
                        process.kill()
                    except (ProcessLookupError, PermissionError):
                        pass
            process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except OSError:
                    # A broken worker can leave a buffered write error for
                    # close() after flush already reported the same failure.
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
