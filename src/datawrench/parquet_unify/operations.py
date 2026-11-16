from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from .catalog import scan
from .data import Summary, compatible_schema, entry_batches, source_summary
from .recipes import duckdb_recipe, literal, polars_recipe
from .types import UnifyError, schema_spec


def _duckdb():
    import duckdb
    connection = duckdb.connect(":memory:", config={"threads": 2, "autoinstall_known_extensions": False,
                                                    "autoload_known_extensions": False})
    connection.execute("SET TimeZone='UTC'")
    return connection


def _arrow_reader(result, batch_size):
    # DuckDB 1.5 renamed fetch_record_batch to to_arrow_reader; 1.4.x connections only have the old name.
    method = getattr(result, "to_arrow_reader", None)
    return method(batch_size) if method is not None else result.fetch_record_batch(batch_size)


def _arrow_table(result):
    # DuckDB 1.5 renamed fetch_arrow_table to to_arrow_table; 1.4.x connections only have the old name.
    method = getattr(result, "to_arrow_table", None)
    return method() if method is not None else result.fetch_arrow_table()


def _polars():
    # Set before the first import, as Polars' pool is initialized once per process.
    os.environ["POLARS_MAX_THREADS"] = "2"
    import polars as pl
    if pl.thread_pool_size() > 2:
        raise UnifyError("Polars was already initialized with >2 threads; use a fresh process with POLARS_MAX_THREADS=2")
    return pl


def _summarize_reader(reader, target):
    if not compatible_schema(reader.schema, target):
        raise UnifyError(f"engine schema differs: actual={reader.schema}; expected={target}")
    summary = Summary(target)
    for batch in reader:
        summary.add(pa.Table.from_batches([batch]))
    return summary.report()


def _naive_result(table, target, expected):
    try:
        if not compatible_schema(table.schema, target):
            return {"status": "changed", "reason": "schema differs", "actual_schema": schema_spec(table.schema), "rows": table.num_rows}
        summary = Summary(target)
        summary.add(table)
        result = summary.report()
        return {"status": "equal" if result == expected else "changed", **result}
    except Exception as exc:
        return {"status": "changed", "reason": str(exc), "actual_schema": schema_spec(table.schema)}


def verify(catalog, target):
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)
    expected = source_summary(catalog, target)
    result = {"ok": False, "reference": expected, "engines": {}, "naive": {}}
    # Generation is deliberately inside each try: unsupported mappings are engine errors too.
    for engine in ("duckdb", "polars"):
        connection = None
        try:
            if engine == "duckdb":
                sql = duckdb_recipe(catalog, target)
                connection = _duckdb()
                reader = _arrow_reader(connection.execute(sql), 65536)
                actual_schema = reader.schema
                actual = _summarize_reader(reader, target)
            else:
                _polars()
                scope = {"__name__": "parquet_unify_generated"}
                # Execute only our generated, literal-escaped program; never user Python/SQL.
                exec(compile(polars_recipe(catalog, target), "<generated-polars-recipe>", "exec"), scope)
                table = scope["result"].collect(engine="streaming").to_arrow()
                actual_schema = table.schema
                actual = _summarize_reader(table.to_reader(), target)
            result["engines"][engine] = {"ok": actual == expected, **actual, "actual_schema": schema_spec(actual_schema)}
            if actual != expected:
                result["engines"][engine]["error"] = "row count or per-column checksum differs from Arrow reference"
        except BaseException as exc:
            # Rust panics in Polars cross PyO3 as PanicException (a BaseException).
            # Preserve process-control exceptions rather than swallowing interrupts.
            if not isinstance(exc, Exception) and type(exc).__name__ != "PanicException":
                raise
            result["engines"][engine] = {"ok": False, "error": str(exc)}
        finally:
            if connection is not None:
                connection.close()
    # Naive directory glob is intentional, and can include skipped unreadable files.
    # Explicit file input stays a single-file read. Do not accidentally glob a metacharacter path.
    try:
        if any(c in str(catalog.root) for c in '*?[]'):
            raise UnifyError("naive glob unavailable for paths containing glob metacharacters")
        naive_path = str(catalog.root / "**" / "*.parquet") if catalog.root.is_dir() else str(catalog.root)
        connection = None
        try:
            connection = _duckdb()
            table = _arrow_table(connection.execute(f"SELECT * FROM read_parquet({literal(naive_path)})"))
            result["naive"]["duckdb"] = _naive_result(table, target, expected)
        except Exception as exc:
            result["naive"]["duckdb"] = {"status": "error", "error": str(exc)}
        finally:
            if connection is not None:
                connection.close()
        try:
            pl = _polars()
            table = pl.scan_parquet(naive_path).collect(engine="streaming").to_arrow()
            result["naive"]["polars"] = _naive_result(table, target, expected)
        except BaseException as exc:
            if not isinstance(exc, Exception) and type(exc).__name__ != "PanicException":
                raise
            result["naive"]["polars"] = {"status": "error", "error": str(exc)}
    except Exception as exc:
        result["naive"] = {e: {"status": "error", "error": str(exc)} for e in ("duckdb", "polars")}
    result["ok"] = all(e["ok"] for e in result["engines"].values())
    return result


def rewrite(catalog, target, output):
    raw = Path(output).expanduser()
    if raw.is_symlink():
        raise UnifyError("output must not be a symlink")
    out = raw.resolve()
    if out.exists():
        raise UnifyError("output must not exist; in-place/overwrite rewrites are forbidden")
    if not out.parent.is_dir():
        raise UnifyError("output parent must already be a directory")
    if catalog.root.is_dir() and out.is_relative_to(catalog.root):
        raise UnifyError("output must be outside the input directory")
    # Only synthetic filenames enter the output; no input path/partition is used as a destination.
    staging = Path(tempfile.mkdtemp(prefix=".parquet-unify-", dir=out.parent))
    try:
        for i, entry in enumerate(catalog.entries):
            with pq.ParquetWriter(staging / f"part-{i:05d}.parquet", target, compression="zstd", version="2.6") as writer:
                for table in entry_batches(entry, target):
                    writer.write_table(table)
        expected = source_summary(catalog, target)
        rewritten = scan(staging)
        actual = source_summary(rewritten, target)
        if expected != actual or len(rewritten.groups()) != 1:
            raise UnifyError("rewrite validation failed: schema/checksum mismatch")
        # mkdir reserves the destination without clobbering even if it appeared while writing.
        # Files are moved only into this newly-created directory, never over existing output.
        out.mkdir()
        try:
            for file in sorted(staging.iterdir()):
                file.rename(out / file.name)
        except BaseException:
            shutil.rmtree(out)
            raise
        return {"out": str(out), "files": len(catalog.entries), **actual}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
