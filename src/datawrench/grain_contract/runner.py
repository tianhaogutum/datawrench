"""Optional DuckDB runner. All checks share one read-only transaction snapshot."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .compiler import Bundle, ContractError


@dataclass(frozen=True)
class Result:
    id: str
    kind: str
    scope: str
    join: str | None
    status: str
    description: str
    columns: tuple[str, ...]
    sample: tuple[tuple[Any, ...], ...]
    sample_truncated: bool
    elapsed_seconds: float
    error: str | None = None
    key_columns: tuple[str, ...] = ()

    def to_dict(self):
        return asdict(self)


def run_bundle(
    bundle: Bundle,
    database: str | Path,
    *,
    timeout: float = 30,
    sample_limit: int = 10,
    fail_fast: bool = False,
) -> tuple[Result, ...]:
    if bundle.dialect != "duckdb":
        raise ContractError(
            "local execution requires dialect=duckdb; run other dialects in their own engine"
        )
    if not 0 < timeout <= 3600:
        raise ContractError("timeout must be in (0, 3600] seconds")
    if type(sample_limit) is not int or not 0 <= sample_limit <= 1000:
        raise ContractError("sample_limit must be an integer between 0 and 1000")
    database = Path(database)
    if not database.is_file():
        raise ContractError(f"database does not exist: {database}")
    try:
        import duckdb
    except ImportError as exc:
        raise ContractError(
            "the check subcommand needs duckdb: pipx install 'datawrench[duckdb]' "
            "(or pip install 'datawrench[duckdb]')"
        ) from exc
    try:
        con = duckdb.connect(
            str(database),
            read_only=True,
            config={
                "threads": "2",
                "memory_limit": "256MB",
                "enable_external_access": "false",
            },
        )
    except duckdb.Error as exc:
        raise ContractError(f"cannot open database: {exc}") from exc
    results = []
    try:
        con.execute("BEGIN TRANSACTION")
        for check in bundle.checks:
            started = time.monotonic()
            timer = threading.Timer(timeout, con.interrupt)
            timer.daemon = True
            timer.start()
            try:
                cursor = con.execute(check.sql)
                rows = cursor.fetchmany(sample_limit + 1)
                results.append(
                    Result(
                        check.id,
                        check.kind,
                        check.scope,
                        check.join,
                        "fail" if rows else "pass",
                        check.description,
                        tuple(c[0] for c in cursor.description),
                        tuple(tuple(r) for r in rows[:sample_limit]),
                        len(rows) > sample_limit,
                        round(time.monotonic() - started, 6),
                        key_columns=check.key_columns,
                    )
                )
            except duckdb.Error as exc:
                results.append(
                    Result(
                        check.id,
                        check.kind,
                        check.scope,
                        check.join,
                        "error",
                        check.description,
                        (),
                        (),
                        False,
                        round(time.monotonic() - started, 6),
                        str(exc),
                        key_columns=check.key_columns,
                    )
                )
                break  # a SQL/runtime error may have aborted the transaction
            finally:
                timer.cancel()
                timer.join()
            if fail_fast and results[-1].status == "fail":
                break
    finally:
        try:
            con.execute("ROLLBACK")
        finally:
            con.close()
    return tuple(results)
