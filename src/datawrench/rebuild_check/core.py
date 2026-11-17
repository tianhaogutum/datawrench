"""SQLite supplies backup, statement completion, metadata and compilation.

This module does not execute application DML probes or generate migrations.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Callable, Iterable

from sqlparse import lexer, tokens as T
from sqlite_utils.db import quote_identifier
from sqlite_utils.create_table_parser import ParseError, parse_autoincrement, parse_checks

from .constraints import foreign_key_timing


FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
ALLOWABLE = {
    "object_missing", "dependency_changed", "foreign_key_removed", "unique_removed",
    "primary_key_changed", "row_count_changed", "table_option_removed", "check_removed",
    "foreign_key_timing_changed", "autoincrement_removed", "sequence_regressed",
}
MAX_STATEMENT = 8 * 1024 * 1024


def fold(value: str) -> str:
    """SQLite's identifier comparison folds ASCII, not arbitrary Unicode."""
    return value.translate(FOLD)


def signature(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sql_tokens(sql: str) -> list[tuple[Any, str]]:
    return [(t, v) for t, v in lexer.tokenize(sql)
            if t not in T.Whitespace and t not in T.Comment]


def sql_fingerprint(sql: str) -> tuple[str, ...]:
    # Conservative: preserve string and quoted-identifier spelling. Only ignore
    # comments, whitespace and unquoted ASCII case. This is not semantic equality.
    return tuple(v if t in T.Literal.String else fold(v)
                 for t, v in sql_tokens(sql) if v != ";")


def identifier(value: str) -> str:
    """Decode a single identifier token; SQLite accepts all four quote forms."""
    if value.startswith("[") and value.endswith("]"):
        return value[1:-1]
    if len(value) >= 2 and value[0] in ('"', "'", "`") and value[-1] == value[0]:
        quote = value[0]
        return value[1:-1].replace(quote * 2, quote)
    return value


def trigger_update_columns(sql: str) -> list[str]:
    """Inspect just the UPDATE OF header with sqlparse's established lexer.

    SQLite's compiler intentionally ignores unknown header column names. The
    event is the first DML keyword in CREATE TRIGGER; quoted names stay literals.
    """
    tokens = sql_tokens(sql)
    for i, (kind, value) in enumerate(tokens):
        if kind in T.Keyword.DML and fold(value) in {"insert", "delete", "update"}:
            if fold(value) != "update" or i + 1 >= len(tokens) or fold(tokens[i + 1][1]) != "of":
                return []
            columns = []
            for kind, value in tokens[i + 2:]:
                if kind in T.Keyword and fold(value) == "on":
                    return columns
                if value != ",":
                    columns.append(identifier(value))
            return []
    return []


class DeadlineExceeded(Exception):
    pass


class Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def check(self, *_: Any) -> int:
        if time.monotonic() >= self.end:
            raise DeadlineExceeded("The total check deadline was exceeded")
        return 0

    def progress(self) -> int:
        return int(time.monotonic() >= self.end)


def finding(report: dict, code: str, obj: str, message: str,
            *, severity: str = "error", **details: Any) -> None:
    report["findings"].append({"code": code, "object": obj, "severity": severity,
                               "message": message, "details": details, "accepted": False})


def foreign_keys(conn: sqlite3.Connection, table: str) -> list[dict]:
    groups: dict[int, list[dict]] = defaultdict(list)
    for row in conn.execute(f"PRAGMA foreign_key_list({quote_identifier(table)})"):
        groups[row["id"]].append(dict(row))
    keys = []
    for key_id, rows in sorted(groups.items()):
        rows.sort(key=lambda r: r["seq"])
        keys.append({"id": key_id, "parent": rows[0]["table"],
                     "columns": [r["from"] for r in rows],
                     "parent_columns": [r["to"] for r in rows],
                     "on_update": rows[0]["on_update"], "on_delete": rows[0]["on_delete"],
                     "match": rows[0]["match"]})
    return keys


def fk_signature(key: dict) -> str:
    return signature({k: [fold(c) if c is not None else None for c in v]
                      if isinstance(v, list) else fold(v)
                      for k, v in key.items() if k != "id"})


def validation_identity(category: str, detail: Any) -> str:
    if category != "foreign_keys" or not isinstance(detail, dict):
        return signature(detail)
    # PRAGMA FK ids are local ordering details, not stable constraint ids.
    value = {k: v for k, v in detail.items() if k not in {"foreign_key_id", "contract"}}
    for key in ("table", "parent"):
        if key in value:
            value[key] = fold(value[key])
    if detail.get("contract"):
        value["contract"] = fk_signature(detail["contract"])
    return signature(value)


def snapshot(conn: sqlite3.Connection, *, count_rows: bool, deadline: Deadline) -> dict:
    objects = {}
    for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema "
                            "WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL "
                            "ORDER BY type, name"):
        key = f"{row['type']}:{row['name']}"
        objects[key] = {"type": row["type"], "name": row["name"],
                        "table": row["tbl_name"], "sql": row["sql"]}
    table_list = {r["name"]: dict(r) for r in conn.execute("PRAGMA table_list")
                  if r["schema"] == "main"}
    has_sequence = conn.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone()
    sequences = dict(conn.execute("SELECT name, seq FROM sqlite_sequence")) if has_sequence else {}
    tables = {}
    for obj in objects.values():
        deadline.check()
        if obj["type"] != "table":
            continue
        name = obj["name"]
        kind = table_list.get(name, {}).get("type", "table")
        columns = [dict(r) for r in conn.execute(f"PRAGMA table_xinfo({quote_identifier(name)})")]
        uniques = []
        for index in conn.execute(f"PRAGMA index_list({quote_identifier(name)})"):
            if index["unique"] and index["origin"] == "u":
                cols = [dict(r) for r in conn.execute(
                    f"PRAGMA index_xinfo({quote_identifier(index['name'])})") if r["key"]]
                uniques.append([{k: c[k] for k in ("name", "coll", "desc")} for c in cols])
        checks = []
        checks_error = None
        timing = []
        autoincrement = None
        if kind == "table":
            try:
                checks = [{"expression": c.check, "column": c.column, "name": c.name}
                          for c in parse_checks(obj["sql"])]
                autoincrement = parse_autoincrement(obj["sql"])
                timing = foreign_key_timing(obj["sql"])
            except ParseError as exc:
                # The host's CHECK parser has narrower coverage than SQLite.
                # Surface that coverage gap instead of claiming all checks agree.
                checks_error = str(exc)
        tables[name] = {
            "kind": kind, "columns": columns,
            "primary_key": [c["name"] for c in sorted(columns, key=lambda c: c["pk"]) if c["pk"]],
            "foreign_keys": foreign_keys(conn, name), "unique_keys": uniques, "checks": checks,
            "checks_error": checks_error,
            "foreign_key_timing": timing, "autoincrement": autoincrement,
            "sequence": sequences.get(name) if isinstance(sequences.get(name), int) else None,
            "sequence_type": type(sequences[name]).__name__ if name in sequences else None,
            "without_rowid": bool(table_list.get(name, {}).get("wr", False)),
            "strict": bool(table_list.get(name, {}).get("strict", False)),
            "row_count": conn.execute(f"SELECT count(*) FROM {quote_identifier(name)}").fetchone()[0]
            if count_rows and kind == "table" else None,
        }
    return {"objects": objects, "tables": tables}


def removed_values(before: Iterable, after: Iterable, key=signature) -> list:
    remaining = Counter(key(v) for v in after)
    missing = []
    for value in before:
        sig = key(value)
        if remaining[sig]:
            remaining[sig] -= 1
        else:
            missing.append(value)
    return missing


def compare(report: dict, before: dict, after: dict) -> None:
    new_objects = {fold(k): v for k, v in after["objects"].items()}
    old_keys = {fold(k) for k in before["objects"]}
    for key, obj in before["objects"].items():
        new = new_objects.get(fold(key))
        if new is None:
            finding(report, "object_missing", key, "An existing schema object was removed",
                    before_sql=obj["sql"], table=obj["table"])
        elif sql_fingerprint(obj["sql"]) != sql_fingerprint(new["sql"]):
            report["changes"].append({"object": key, "before_sql": obj["sql"], "after_sql": new["sql"]})
            if obj["type"] != "table":
                finding(report, "dependency_changed", key, "A surviving dependency definition changed",
                        before_sql=obj["sql"], after_sql=new["sql"])
    for key in after["objects"]:
        if fold(key) not in old_keys:
            report["changes"].append({"object": key, "before_sql": None,
                                      "after_sql": after["objects"][key]["sql"]})
    new_tables = {fold(k): v for k, v in after["tables"].items()}
    for name, table in before["tables"].items():
        new = new_tables.get(fold(name))
        if new is None:
            continue
        obj = f"table:{name}"
        for fk in removed_values(table["foreign_keys"], new["foreign_keys"], fk_signature):
            finding(report, "foreign_key_removed", obj, "A foreign-key contract was removed or changed", foreign_key=fk)
        timing_key = lambda key: signature({k: [fold(v) for v in value] if isinstance(value, list)
                                           else fold(value) if isinstance(value, str) else value
                                           for k, value in key.items()})
        if not table["checks_error"] and not new["checks_error"] and (
            Counter(map(timing_key, table["foreign_key_timing"])) != Counter(map(timing_key, new["foreign_key_timing"]))
        ):
            # Report timing only if both schemas retain the same FK endpoints;
            # endpoint changes are already explained by foreign_key_removed.
            endpoint = lambda key: timing_key({k: v for k, v in key.items() if k != "deferred"})
            if Counter(map(endpoint, table["foreign_key_timing"])) == Counter(map(endpoint, new["foreign_key_timing"])):
                finding(report, "foreign_key_timing_changed", obj, "Foreign-key enforcement timing changed",
                        before=table["foreign_key_timing"], after=new["foreign_key_timing"])
        if table["autoincrement"] and not new["autoincrement"]:
            finding(report, "autoincrement_removed", obj, "AUTOINCREMENT no-reuse behavior was removed")
        if table["autoincrement"] and new["autoincrement"] and table["sequence"] is not None and (
            new["sequence"] is None or new["sequence"] < table["sequence"]
        ):
            finding(report, "sequence_regressed", obj, "The AUTOINCREMENT high-water mark decreased",
                    before=table["sequence"], after=new["sequence"])
        unique_key = lambda u: signature([{k: fold(v) if isinstance(v, str) else v for k, v in c.items()} for c in u])
        for unique in removed_values(table["unique_keys"], new["unique_keys"], unique_key):
            finding(report, "unique_removed", obj, "A UNIQUE constraint was removed or changed", columns=unique)
        if [fold(c) for c in table["primary_key"]] != [fold(c) for c in new["primary_key"]]:
            finding(report, "primary_key_changed", obj, "The primary-key columns or order changed",
                    before=table["primary_key"], after=new["primary_key"])
        for option in ("strict", "without_rowid"):
            if table[option] and not new[option]:
                finding(report, "table_option_removed", obj, f"The {option} table option was removed", option=option)
        for side, data in (("before", table), ("after", new)):
            if data["checks_error"]:
                finding(report, "check_parse_incomplete", obj, "sqlite-utils could not inspect all table constraints",
                        severity="warning", stage=side, error=data["checks_error"])
        if not table["checks_error"] and not new["checks_error"]:
            for check in removed_values(table["checks"], new["checks"], lambda c: sql_fingerprint(c["expression"])):
                finding(report, "check_removed", obj, "A CHECK constraint was removed or changed", check=check)
        if table["row_count"] is not None and new["row_count"] is not None and table["row_count"] != new["row_count"]:
            finding(report, "row_count_changed", obj, "The number of rows changed",
                    before=table["row_count"], after=new["row_count"])


def validate(conn: sqlite3.Connection, snap: dict, deadline: Deadline, max_violations: int) -> dict:
    """Compile references and stream bounded FK/integrity results, never run DML."""
    result: dict[str, list] = {"integrity": [], "foreign_keys": [], "references": []}
    for row in conn.execute("PRAGMA integrity_check(100)"):
        deadline.check()
        if row[0] != "ok":
            result["integrity"].append(row[0])
    total = 0
    for name, table in snap["tables"].items():
        deadline.check()
        if table["sequence_type"] not in {None, "int"}:
            result["integrity"].append(f"Invalid sqlite_sequence type for {name}: {table['sequence_type']}")
        if table["kind"] != "table" or not table["foreign_keys"]:
            continue
        try:
            cursor = conn.execute(f"PRAGMA foreign_key_check({quote_identifier(name)})")
            for row in cursor:
                deadline.check()
                if total >= max_violations:
                    result["foreign_keys"].append({"table": name, "truncated": True})
                    break
                key = next((k for k in table["foreign_keys"] if k["id"] == row[3]), None)
                result["foreign_keys"].append({"table": row[0], "rowid": row[1], "parent": row[2],
                                               "foreign_key_id": row[3], "contract": key})
                total += 1
        except sqlite3.Error as exc:
            result["references"].append({"object": f"table:{name}", "operation": "foreign_key_check", "error": str(exc)})
    triggers = {fold(o["table"]) for o in snap["objects"].values() if o["type"] == "trigger"}
    columns_by_object = {fold(name): table["columns"] for name, table in snap["tables"].items()}
    for obj in snap["objects"].values():
        if obj["type"] == "view":
            try:
                columns_by_object[fold(obj["name"])] = [dict(r) for r in conn.execute(
                    f"PRAGMA table_xinfo({quote_identifier(obj['name'])})")]
            except sqlite3.Error:
                # The SELECT probe below records the actual view error. This
                # must not prevent a migration from fixing a broken baseline.
                columns_by_object[fold(obj["name"])] = []
    for obj in snap["objects"].values():
        if obj["type"] == "trigger":
            known = {fold(c["name"]) for c in columns_by_object.get(fold(obj["table"]), [])}
            missing = [c for c in trigger_update_columns(obj["sql"]) if fold(c) not in known]
            if missing:
                result["references"].append({"object": f"trigger:{obj['name']}", "operation": "update_of",
                                              "error": "Unknown UPDATE OF trigger columns", "columns": missing})
    for obj in snap["objects"].values():
        deadline.check()
        name, kind = obj["name"], obj["type"]
        q = quote_identifier(name)
        probes = []
        if kind == "view":
            probes.append(("select", f"SELECT * FROM {q} LIMIT 0"))
        if kind in {"view", "table"} and (fold(name) in triggers or (kind == "table" and snap["tables"][name]["foreign_keys"])):
            columns = columns_by_object[fold(name)]
            writable = [quote_identifier(c["name"]) for c in columns if c["hidden"] == 0]
            probes.extend((("insert", f"INSERT INTO {q} DEFAULT VALUES"), ("delete", f"DELETE FROM {q}")))
            if writable:
                probes.append(("update", f"UPDATE {q} SET " + ", ".join(f"{c}={c}" for c in writable)))
        for operation, sql in probes:
            try:
                # Exhaust the EXPLAIN result cursor to release the statement.
                for _ in conn.execute("EXPLAIN " + sql):
                    deadline.check()
            except sqlite3.Error as exc:
                if kind == "view" and str(exc) == f"cannot modify {name} because it is a view":
                    continue
                result["references"].append({"object": f"{kind}:{name}", "operation": operation, "error": str(exc)})
    return result


def migration_authorizer(action: int, arg1: str | None, arg2: str | None,
                         database: str | None, source: str | None) -> int:
    # A temporary replica is not a SQL filesystem sandbox. In particular ATTACH
    # and VACUUM INTO would otherwise be able to write outside the replica.
    if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and fold(arg2 or "") == "load_extension":
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and fold(arg1 or "") in {
        "writable_schema", "temp_store_directory", "data_store_directory",
    } and arg2 is not None:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def statements(stream: Any, deadline: Deadline) -> Iterable[tuple[int, str]]:
    """Stream statements using SQLite's own completion scanner (trigger aware)."""
    parts: list[str] = []
    size = 0
    start_line = 1
    line_no = 1
    while chunk := stream.read(65536):
        for segment in chunk.splitlines(keepends=True):
            for i, piece in enumerate(segment.split(";")):
                # Completion scans can be expensive for semicolons inside a
                # string. Check here as well as at chunk/statement boundaries.
                deadline.check()
                if i:
                    parts.append(";")
                    size += 1
                    candidate = "".join(parts)
                    if sqlite3.complete_statement(candidate):
                        yield start_line, candidate
                        parts, size = [], 0
                        start_line = line_no
                parts.append(piece)
                size += len(piece)
                if size > MAX_STATEMENT:
                    raise ValueError(f"SQL statement exceeds {MAX_STATEMENT} characters")
            line_no += segment.count("\n")
            if not parts or not any(p.strip(" \t\n\r\f") for p in parts):
                start_line = line_no
            deadline.check()
    tail = "".join(parts)
    # Python's Unicode strip() also removes characters SQLite rejects (NBSP,
    # em-space, vertical tab, etc.). Submit every nonempty tail to SQLite so
    # invalid trailing tokens cannot turn into a false pass.
    if tail:
        yield start_line, tail


def load_policy(policy: dict | None) -> list[dict]:
    if policy is None:
        return []
    if not isinstance(policy, dict) or set(policy) != {"allow"} or not isinstance(policy["allow"], list):
        raise ValueError('Policy must be an object with an "allow" list')
    seen = set()
    for entry in policy["allow"]:
        if not isinstance(entry, dict) or set(entry) != {"code", "object", "reason"}:
            raise ValueError("Each policy entry needs exactly code, object and reason")
        if any(not isinstance(v, str) or not v.strip() for v in entry.values()):
            raise ValueError("Policy fields must be nonempty strings")
        if entry["code"] not in ALLOWABLE:
            raise ValueError(f"This finding cannot be accepted by policy: {entry['code']}")
        key = (entry["code"], entry["object"])
        if key in seen:
            raise ValueError(f"Duplicate policy entry: {key}")
        seen.add(key)
    return policy["allow"]


def apply_policy(report: dict, entries: list[dict]) -> None:
    for entry in entries:
        matched = False
        for item in report["findings"]:
            if (item["code"], item["object"]) == (entry["code"], entry["object"]):
                item["accepted"] = True
                item["acceptance_reason"] = entry["reason"]
                matched = True
        if not matched:
            finding(report, "unused_policy", entry["object"], "A policy entry did not match any finding", entry=entry)


def check_migration(database: str | Path, migrations: Iterable[str | Path], *,
                    timeout: float = 30, foreign_keys_enabled: bool = True,
                    count_rows: bool = True, max_violations: int = 100,
                    policy: dict | None = None, temp_dir: str | Path | None = None,
                    prepare_connection: Callable[[sqlite3.Connection], None] | None = None) -> dict:
    """Return a JSON-compatible report; source is opened read-only, never migrated.

    `status` is passed/failed/error; `exit_code` is 0/1/2 respectively. Policies
    accept only exact preservation differences, never reference/integrity errors.
    SQLite >= 3.37 is required. An optional preparer registers application
    functions/collations on the replica only, before baseline validation. The
    timeout covers backup and SQLite VM work, not arbitrary Python hook code.
    """
    report: dict = {"schema_version": 1, "status": "error", "exit_code": 2,
                    "sqlite_version": sqlite3.sqlite_version, "findings": [], "changes": [],
                    "connection_prepared": False,
                    "before": None, "after": None, "validation": {"before": None, "after": None},
                    "migration": {"completed": False, "statements_executed": 0}}
    started = time.monotonic()
    deadline = Deadline(timeout) if isinstance(timeout, (int, float)) else Deadline(30)
    stage = "input"
    location: dict = {}
    try:
        if not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        if not isinstance(max_violations, int) or max_violations < 1:
            raise ValueError("max_violations must be a positive integer")
        if sqlite3.sqlite_version_info < (3, 37, 0):
            raise ValueError("SQLite 3.37 or later is required")
        if prepare_connection is not None and not callable(prepare_connection):
            raise ValueError("prepare_connection must be callable")
        entries = load_policy(policy)
        source = Path(database).resolve(strict=True)
        if not source.is_file():
            raise ValueError("Database must be an existing regular file")
        paths = [Path(p).resolve(strict=True) for p in migrations]
        if not paths:
            raise ValueError("At least one migration SQL file is required")
        report["source"] = str(source)
        report["migration"]["files"] = [str(p) for p in paths]
        with tempfile.TemporaryDirectory(prefix="rebuild-check-", dir=temp_dir) as directory:
            with closing(sqlite3.connect(Path(directory) / "replica.db", isolation_level=None,
                                          timeout=min(timeout, 1), cached_statements=0)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA cache_size=-4096")
                conn.set_progress_handler(deadline.progress, 1000)
                stage = "backup"
                with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True,
                                            timeout=min(timeout, 1))) as original:
                    original.backup(conn, pages=256, progress=deadline.check, sleep=0.02)
                if prepare_connection is not None:
                    stage = "preparation"
                    conn.execute("PRAGMA query_only=ON")
                    try:
                        prepare_connection(conn)
                    except Exception as exc:
                        raise ValueError(f"Connection preparation failed: {exc}") from exc
                    finally:
                        conn.execute("PRAGMA query_only=OFF")
                    if conn.in_transaction:
                        raise ValueError("Connection preparation left a transaction open")
                    # Preserve the execution/inspection invariants even when a
                    # host plugin configures its normal application connection.
                    conn.row_factory = sqlite3.Row
                    conn.isolation_level = None
                    conn.set_progress_handler(deadline.progress, 1000)
                    deadline.check()
                    report["connection_prepared"] = True
                stage = "baseline"
                conn.execute("PRAGMA foreign_keys=ON")
                enabled = conn.execute("PRAGMA foreign_keys").fetchone()
                if not enabled or enabled[0] != 1:
                    raise ValueError("This SQLite build cannot enable foreign-key enforcement")
                report["before"] = snapshot(conn, count_rows=count_rows, deadline=deadline)
                report["validation"]["before"] = validate(conn, report["before"], deadline, max_violations)
                conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_enabled else 'OFF'}")
                conn.set_authorizer(migration_authorizer)
                stage = "migration"
                for path in paths:
                    location = {"file": str(path), "line": 1}
                    with path.open("r", encoding="utf-8-sig", newline="") as stream:
                        for line, sql in statements(stream, deadline):
                            location["line"] = line
                            deadline.check()
                            # Match executescript's completion semantics without
                            # its implicit-commit behavior between chunks. A
                            # SELECT may raise only after its first result row.
                            with closing(conn.execute(sql)) as cursor:
                                for _ in cursor:
                                    deadline.check()
                            report["migration"]["statements_executed"] += 1
                if conn.in_transaction:
                    raise ValueError("Migration left a transaction open; add COMMIT or ROLLBACK explicitly")
                report["migration"]["completed"] = True
                report["migration"]["final_foreign_keys_enabled"] = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
                stage = "validation"
                location = {}
                # Python 3.10 accepts None but subsequent statements may fail
                # with SQLITE_AUTH. None became supported in Python 3.11. A
                # permissive callback works consistently on all supported APIs.
                conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA ignore_check_constraints=OFF")
                report["after"] = snapshot(conn, count_rows=count_rows, deadline=deadline)
                report["validation"]["after"] = validate(conn, report["after"], deadline, max_violations)
                compare(report, report["before"], report["after"])
                for category, code in (("integrity", "integrity_failed"), ("foreign_keys", "foreign_key_violation"),
                                       ("references", "reference_invalid")):
                    baseline = Counter(validation_identity(category, v) for v in report["validation"]["before"][category])
                    for detail in report["validation"]["after"][category]:
                        identity = validation_identity(category, detail)
                        preexisting = baseline[identity] > 0
                        baseline[identity] -= int(preexisting)
                        obj = detail.get("object", f"table:{detail.get('table', '?')}") if isinstance(detail, dict) else "database"
                        finding(report, code, obj, f"SQLite {category} validation failed",
                                result=detail, preexisting=preexisting)
                apply_policy(report, entries)
                deadline.check()
                failed = any(f["severity"] == "error" and not f["accepted"] for f in report["findings"])
                report["status"], report["exit_code"] = ("failed", 1) if failed else ("passed", 0)
    except (sqlite3.Error, OSError, ValueError, DeadlineExceeded) as exc:
        expired = time.monotonic() >= deadline.end
        code = "timeout" if expired else f"{stage}_error"
        finding(report, code, "database", "The check could not complete", error=str(exc), stage=stage, **location)
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return report
