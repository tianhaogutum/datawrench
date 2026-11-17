"""Standalone Pandera oracle; also copied verbatim into reproduction bundles.

One JSON request per line on stdin, one JSON response per line on stdout.
Validator output is kept off the protocol. This executes trusted schema code.
"""

import ast
import contextlib
import hashlib
import json
import os
import resource
import runpy
import signal
import sys
import threading
import time
from pathlib import Path

ALLOWED_PANDAS_OPTIONS = {
    "dtype",
    "keep_default_na",
    "na_values",
    "na_filter",
    "parse_dates",
    "date_format",
    "dayfirst",
    "true_values",
    "false_values",
    "decimal",
    "thousands",
    "index_col",
    "float_precision",
}


def validate_options(options: dict) -> dict:
    if not isinstance(options, dict):
        raise ValueError("read-options must be a JSON object")
    if not all(isinstance(name, str) for name in options):
        raise ValueError("pandas read-option names must be strings")
    unknown = set(options) - ALLOWED_PANDAS_OPTIONS
    if unknown:
        raise ValueError("unsupported pandas read options: " + ", ".join(sorted(unknown)))
    try:
        serialized = json.dumps(options, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("pandas read options must be finite JSON values") from exc
    if len(serialized.encode("utf-8")) > 1024 * 1024:
        raise ValueError("pandas read options exceed the 1 MiB limit")
    return json.loads(serialized)


def failure_identity(error) -> dict:
    check = str(error.check) if error.check is not None else None
    identity = {
        "schema_context": type(error.schema).__name__,
        "column": str(error.column_name) if error.column_name is not None else None,
        "check": check,
        "check_index": error.check_index,
        "reason": getattr(error.reason_code, "value", str(error.reason_code)),
    }
    # Pandera uses generic labels for separate compound-uniqueness rules and
    # wraps individual index-level checks as MultiIndex errors without a
    # column_name. Recover rule context, never observed values or row indices.
    cases = error.failure_cases
    case_columns = []
    if hasattr(cases, "columns") and "column" in cases.columns:
        case_columns = sorted({str(value) for value in cases["column"].dropna()})
    if check == "multiple_fields_uniqueness":
        # reshape_failure_cases drops all-null cells. The error description
        # retains Pandera's complete tuple of columns before that reshaping.
        message = str(error)
        prefix, suffix = "columns '", "' not unique:\n"
        start = message.find(prefix)
        stop = message.find(suffix, start + len(prefix))
        if start < 0 or stop < 0:
            raise ValueError("cannot identify Pandera's full compound uniqueness group")
        columns = ast.literal_eval(message[start + len(prefix) : stop])
        if not isinstance(columns, tuple) or not columns:
            raise ValueError("invalid compound uniqueness group in Pandera error")
        identity["unique_columns"] = sorted(str(column) for column in columns)
    elif identity["schema_context"] == "MultiIndex" and len(case_columns) == 1:
        identity["column"] = case_columns[0]
    elif identity["schema_context"] == "Index" and identity["column"] is None:
        identity["column"] = str(error.schema.name) if error.schema.name is not None else None
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return {"id": "pa:" + hashlib.sha256(encoded).hexdigest(), **identity}


def validate(request: dict) -> dict:
    options = validate_options(request.get("pandas_options", {}))
    import pandas as pd
    import pandera.pandas as pa

    path = Path(request["schema"])
    if path.suffix.lower() in {".yaml", ".yml"}:
        schema = pa.DataFrameSchema.from_yaml(path)
    else:
        namespace = runpy.run_path(str(path))
        schema = namespace.get("schema")
        if isinstance(schema, type) and issubclass(schema, pa.DataFrameModel):
            schema = schema.to_schema()
        if not isinstance(schema, pa.DataFrameSchema):
            raise ValueError("Python schema must define a Pandera schema/model as 'schema'")
    frame = pd.read_csv(
        request["data"],
        sep=request["delimiter"],
        encoding="utf-8-sig",
        **options,
    )
    parsed_rows = len(frame)
    try:
        schema.validate(frame, lazy=True)
    except (pa.errors.SchemaErrors, pa.errors.SchemaError) as exc:
        errors = getattr(exc, "schema_errors", [exc])
        failures = {}
        for error in errors:
            identity = failure_identity(error)
            if identity["reason"] == "check_error":
                raise RuntimeError(
                    "a Pandera check raised an exception instead of returning a result"
                ) from exc
            failures[identity["id"]] = identity
        return {
            "status": "ok",
            "parsed_rows": parsed_rows,
            "failures": sorted(failures.values(), key=lambda f: f["id"]),
        }
    return {"status": "ok", "parsed_rows": parsed_rows, "failures": []}


def serve():
    sys.dont_write_bytecode = True
    expected_parent = os.environ.get("FAILUREPACK_PARENT_PID")
    if expected_parent:
        expected_parent = int(expected_parent)

        def stop_if_parent_exits():
            # A killed CLI cannot enforce its timeout. Do not leave an oracle
            # or schema-created descendants running after its parent vanishes.
            while True:
                if os.getppid() != expected_parent:
                    if os.getpgrp() == os.getpid():
                        os.killpg(os.getpgrp(), signal.SIGKILL)
                    os._exit(70)
                time.sleep(0.2)

        threading.Thread(target=stop_if_parent_exits, daemon=True).start()
    # Preserve a dedicated protocol descriptor, then route fd 1 itself to the
    # diagnostic sink. Python redirect_stdout alone cannot catch native writes.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("quit"):
                return
            with contextlib.redirect_stdout(sys.stderr):
                result = validate(request)
        except Exception as exc:
            result = {"status": "error", "kind": type(exc).__name__, "message": str(exc)[:1000]}
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result["worker_peak_rss_bytes"] = peak if sys.platform == "darwin" else peak * 1024
        protocol.write(json.dumps(result, sort_keys=True) + "\n")
        protocol.flush()


if __name__ == "__main__":
    serve()
