"""Disk-backed exact identity audit; report sampling never affects decisions."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from datawrench import __version__
from .csvio import (
    AuditError,
    CSVOptions,
    file_sha256,
    new_atomic_file,
    open_records,
    snapshot_source,
)
from .evidence import EvidenceBudget, describe_key, key_changes
from .rules import RULES, engine_identity, validate_rules

SCHEMA = "keyaudit.review/v1"
MAX_REVIEW_BYTES = 32 * 1024 * 1024


def pack(key) -> str:
    # JSON arrays preserve column boundaries, null bytes and leading zeroes.
    return json.dumps(key, ensure_ascii=True, separators=(",", ":"))


def column_preview(names: list[str]) -> str:
    preview = [name[:80] + ("..." if len(name) > 80 else "") for name in names[:20]]
    suffix = f" ({len(names)} columns total; preview truncated)" if len(names) > 20 else ""
    return repr(preview) + suffix


def review_id(report: dict) -> str:
    contract = {
        "schema": report["schema"],
        "engine": report["engine"],
        "source": {k: report["source"][k] for k in ("sha256", "bytes")},
        "plan": report["plan"],
    }
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class DetailLimits:
    groups: int = 100
    variants: int = 20
    samples: int = 5

    def __post_init__(self):
        for field in ("groups", "variants", "samples"):
            n = getattr(self, field)
            if type(n) is not int or not 1 <= n <= 1000:
                raise AuditError(f"detail {field} must be an integer between 1 and 1000")


class Audit:
    def __init__(
        self,
        source: Path,
        columns: list[str],
        rules: list[str] | tuple[str, ...],
        options: CSVOptions | None = None,
        limits: DetailLimits | None = None,
        work_dir: Path | None = None,
        expected_source: dict | None = None,
    ):
        self.source = source
        self.columns = columns
        self.rules = validate_rules(rules)
        self.options = options or CSVOptions()
        self.limits = limits or DetailLimits()
        self.work_dir = work_dir
        self.expected_source = expected_source
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(c, str) and c for c in columns)
        ):
            raise AuditError("select at least one nonempty key column name")
        if len(set(columns)) != len(columns):
            raise AuditError("key column names must not repeat")
        if len(columns) > 32:
            raise AuditError("at most 32 key columns can be reviewed together")
        self.connection = None
        self.temporary = None

    def __enter__(self):
        try:
            if sqlite3.sqlite_version_info < (3, 35, 0):
                raise AuditError("SQLite 3.35 or newer is required; install a current Python build")
            self.temporary = tempfile.TemporaryDirectory(prefix="keyaudit-", dir=self.work_dir)
            self.directory = Path(self.temporary.name)
            self.snapshot = self.directory / "source.csv"
            self.source_info = snapshot_source(self.source, self.snapshot)
            if self.expected_source is not None and any(
                self.source_info[k] != self.expected_source[k] for k in ("sha256", "bytes")
            ):
                raise AuditError(
                    "input bytes differ from the reviewed source; audit the new input first"
                )
            self.connection = sqlite3.connect(self.directory / "index.sqlite")
            self.connection.row_factory = sqlite3.Row
            self.connection.executescript("""
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                PRAGMA cache_size=-8192;
                PRAGMA temp_store=FILE;
                CREATE TABLE variants (
                    id INTEGER PRIMARY KEY, raw TEXT NOT NULL UNIQUE, n INTEGER NOT NULL,
                    first_record INTEGER NOT NULL, first_line INTEGER NOT NULL,
                    empty_before INTEGER NOT NULL, empty_after INTEGER NOT NULL DEFAULT 0,
                    newly_empty INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE samples (
                    raw_id INTEGER NOT NULL, record INTEGER NOT NULL, line INTEGER NOT NULL,
                    PRIMARY KEY (raw_id, record)
                ) WITHOUT ROWID;
                CREATE TABLE keys (
                    stage INTEGER NOT NULL, raw_id INTEGER NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY (stage, raw_id)
                ) WITHOUT ROWID;
            """)
            self._scan()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.connection is not None:
            self.connection.close()
        if self.temporary is not None:
            self.temporary.cleanup()

    @property
    def db(self):
        if self.connection is None:
            raise RuntimeError("use Audit as a context manager")
        return self.connection

    def _scan(self):
        self.rows = 0
        with open_records(self.snapshot, self.options) as records:
            self.header = records.header
            missing = [c for c in self.columns if c not in self.header]
            if missing:
                raise AuditError(
                    f"key columns not found: {column_preview(missing)}; "
                    f"available: {column_preview(self.header)}"
                )
            self.indices = [self.header.index(c) for c in self.columns]
            for record, line, row in records:
                self.rows = record
                original = tuple(row[i] for i in self.indices)
                if any(len(s) > self.options.max_key_chars for s in original):
                    raise AuditError(
                        f"key in data record {record} exceeds max_key_chars={self.options.max_key_chars}"
                    )
                raw = pack(original)
                raw_id, n = self.db.execute(
                    """
                    INSERT INTO variants(raw,n,first_record,first_line,empty_before) VALUES (?, 1, ?, ?, ?)
                    ON CONFLICT(raw) DO UPDATE SET n=n+1 RETURNING id,n
                """,
                    (raw, record, line, int("" in original)),
                ).fetchone()
                if n <= self.limits.samples:
                    self.db.execute("INSERT INTO samples VALUES (?, ?, ?)", (raw_id, record, line))
                if n == 1:
                    current = original
                    values = [(0, raw_id, raw)]
                    for stage, name in enumerate(self.rules, 1):
                        current = tuple(RULES[name].transform(s) for s in current)
                        if any(len(s) > self.options.max_key_chars for s in current):
                            raise AuditError(
                                f"rule {name!r} produces a key beyond max_key_chars="
                                f"{self.options.max_key_chars} in data record {record}; "
                                "increase the limit to review this expansion"
                            )
                        values.append((stage, raw_id, pack(current)))
                    self.db.execute(
                        "UPDATE variants SET empty_after=?, newly_empty=? WHERE id=?",
                        (
                            int("" in current),
                            int(
                                any(old != "" and new == "" for old, new in zip(original, current))
                            ),
                            raw_id,
                        ),
                    )
                    self.db.executemany("INSERT INTO keys VALUES (?, ?, ?)", values)
        self.db.execute("CREATE INDEX by_value ON keys(stage, value)")
        self.db.commit()
        self.stages = [self._stage(stage) for stage in range(len(self.rules) + 1)]

    def _stage(self, stage: int) -> dict:
        stats = self.db.execute(
            """
            SELECT COUNT(*) AS distinct_keys,
                   COALESCE(SUM(variants>1), 0) AS collision_groups,
                   COALESCE(SUM(CASE WHEN variants>1 THEN rows ELSE 0 END), 0) AS collision_rows
            FROM (
                SELECT k.value, COUNT(*) AS variants, SUM(v.n) AS rows
                FROM keys k JOIN variants v ON v.id=k.raw_id WHERE k.stage=?
                GROUP BY k.value
            )
        """,
            (stage,),
        ).fetchone()
        result = dict(stats)
        result["rule"] = self.rules[stage - 1] if stage else "original"
        if stage:
            changed = self.db.execute(
                """
                SELECT COALESCE(SUM(v.n), 0) FROM variants v
                JOIN keys a ON a.raw_id=v.id AND a.stage=?
                JOIN keys b ON b.raw_id=v.id AND b.stage=?
                WHERE a.value != b.value
            """,
                (stage - 1, stage),
            ).fetchone()[0]
            result["changed_rows"] = changed
        return result

    def summary(self) -> dict:
        baseline = self.db.execute("""
            SELECT COUNT(*) AS groups, COALESCE(SUM(n),0) AS rows,
                   COALESCE(SUM(n-1),0) AS excess FROM variants WHERE n>1
        """).fetchone()
        changed = self.db.execute(
            """
            SELECT COUNT(*) AS keys, COALESCE(SUM(v.n),0) AS rows
            FROM variants v JOIN keys k ON k.raw_id=v.id AND k.stage=?
            WHERE k.value != v.raw
        """,
            (len(self.rules),),
        ).fetchone()
        empties = self.db.execute("""
            SELECT COALESCE(SUM(n*empty_before),0), COALESCE(SUM(n*empty_after),0),
                   COALESCE(SUM(n*newly_empty),0), COALESCE(SUM(newly_empty),0)
            FROM variants
        """).fetchone()
        return {
            "rows": self.rows,
            "original_distinct_keys": self.stages[0]["distinct_keys"],
            "normalized_distinct_keys": self.stages[-1]["distinct_keys"],
            "baseline_duplicate_groups": baseline["groups"],
            "baseline_duplicate_rows": baseline["rows"],
            "baseline_duplicate_excess_rows": baseline["excess"],
            "new_collision_groups": self.stages[-1]["collision_groups"],
            "new_collision_rows": self.stages[-1]["collision_rows"],
            "original_keys_merged": self.stages[0]["distinct_keys"]
            - self.stages[-1]["distinct_keys"],
            "changed_distinct_keys": changed["keys"],
            "changed_rows": changed["rows"],
            "empty_key_rows_before": empties[0],
            "empty_key_rows_after": empties[1],
            "newly_empty_key_rows": empties[2],
            "newly_empty_distinct_keys": empties[3],
        }

    def _variant(self, row: sqlite3.Row, value: str) -> dict:
        before = json.loads(row["raw"])
        after = json.loads(value)
        samples = [
            dict(r)
            for r in self.db.execute(
                "SELECT record, line FROM samples WHERE raw_id=? ORDER BY record", (row["id"],)
            )
        ]
        return {
            "original_key_id": hashlib.sha256(row["raw"].encode("ascii")).hexdigest(),
            "original_key": describe_key(before),
            "row_count": row["n"],
            "row_samples": samples,
            "omitted_row_samples": row["n"] - len(samples),
            "changes": key_changes(before, after, self.columns),
        }

    def report(self) -> dict:
        summary = self.summary()
        budget = EvidenceBudget()
        collisions = []
        for group in self.db.execute(
            """
            SELECT k.value, COUNT(*) AS variant_count, SUM(v.n) AS row_count,
                   MIN(v.first_record) AS first_record
            FROM keys k JOIN variants v ON v.id=k.raw_id WHERE k.stage=?
            GROUP BY k.value HAVING COUNT(*)>1
            ORDER BY first_record LIMIT ?
        """,
            (len(self.rules), self.limits.groups),
        ):
            item = {
                "normalized_key": describe_key(json.loads(group["value"])),
                "row_count": group["row_count"],
                "variant_count": group["variant_count"],
            }
            if not budget.include(item):
                break
            variants = []
            for row in self.db.execute(
                """
                SELECT v.* FROM variants v JOIN keys k ON k.raw_id=v.id
                WHERE k.stage=? AND k.value=? ORDER BY v.first_record LIMIT ?
            """,
                (len(self.rules), group["value"], self.limits.variants),
            ):
                variant = self._variant(row, group["value"])
                if not budget.include(variant):
                    break
                variants.append(variant)
            collisions.append(
                {
                    **item,
                    "omitted_variants": group["variant_count"] - len(variants),
                    "variants": variants,
                }
            )
        newly_empty = []
        for row in self.db.execute(
            """
            SELECT v.*, k.value FROM variants v JOIN keys k ON k.raw_id=v.id
            WHERE k.stage=? AND v.newly_empty=1 ORDER BY v.first_record LIMIT ?
        """,
            (len(self.rules), self.limits.groups),
        ):
            item = self._variant(row, row["value"])
            item["normalized_key"] = describe_key(json.loads(row["value"]))
            if not budget.include(item):
                break
            newly_empty.append(item)
        changed = []
        for row in self.db.execute(
            """
            SELECT v.*, k.value FROM variants v JOIN keys k ON k.raw_id=v.id
            WHERE k.stage=? AND k.value!=v.raw ORDER BY v.first_record LIMIT ?
        """,
            (len(self.rules), self.limits.groups),
        ):
            item = self._variant(row, row["value"])
            item["normalized_key"] = describe_key(json.loads(row["value"]))
            current = json.loads(row["raw"])
            steps = []
            for name in self.rules:
                after = [RULES[name].transform(s) for s in current]
                if current != after:
                    steps.append(
                        {"rule": name, "changes": key_changes(current, after, self.columns)}
                    )
                current = after
            item["steps"] = steps
            if not budget.include(item):
                break
            changed.append(item)
        baseline = []
        for row in self.db.execute(
            "SELECT * FROM variants WHERE n>1 ORDER BY first_record LIMIT ?", (self.limits.groups,)
        ):
            item = self._variant(row, row["raw"])
            if not budget.include(item):
                break
            baseline.append(item)
        stages = []
        previous = self.stages[0]["distinct_keys"]
        for stage in self.stages:
            stages.append({**stage, "additional_keys_merged": previous - stage["distinct_keys"]})
            previous = stage["distinct_keys"]
        report = {
            "schema": SCHEMA,
            "tool_version": __version__,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "engine": engine_identity(),
            "source": self.source_info,
            "plan": {
                "key_columns": self.columns,
                "rules": list(self.rules),
                "csv": self.options.as_dict(),
            },
            "summary": summary,
            "stages": stages,
            "baseline_duplicates": {
                "total_groups": summary["baseline_duplicate_groups"],
                "omitted_groups": summary["baseline_duplicate_groups"] - len(baseline),
                "groups": baseline,
            },
            "new_collisions": {
                "total_groups": summary["new_collision_groups"],
                "omitted_groups": summary["new_collision_groups"] - len(collisions),
                "groups": collisions,
            },
            "changed_keys": {
                "total_keys": summary["changed_distinct_keys"],
                "omitted_keys": summary["changed_distinct_keys"] - len(changed),
                "keys": changed,
            },
            "newly_empty_keys": {
                "total_keys": summary["newly_empty_distinct_keys"],
                "omitted_keys": summary["newly_empty_distinct_keys"] - len(newly_empty),
                "keys": newly_empty,
            },
            "evidence_budget": {
                "compact_bytes_limit": budget.limit,
                "compact_bytes_used": budget.used,
                "exhausted": budget.exhausted,
            },
            "detail_limits": {
                "groups": self.limits.groups,
                "variants": self.limits.variants,
                "samples": self.limits.samples,
            },
            "semantics": {
                "row_samples": "1-based data record and first physical line; header is not a data record",
                "baseline": "same exact original composite key repeated before any rule",
                "new_collision": "two or more distinct original composite keys reach the same final key",
                "overlap": "baseline rows may also belong to a new collision group",
                "empty": "any key component is the empty string; empty is not SQL NULL",
                "sampling": "details are bounded; counts and export guards cover the entire input",
            },
        }
        report["review_id"] = review_id(report)
        return report

    def export(self, destination: Path, *, encoding: str = "utf-8") -> dict:
        if encoding not in ("utf-8", "utf-8-sig"):
            raise AuditError("output encoding must be utf-8 or utf-8-sig")
        with new_atomic_file(destination, encoding=encoding) as (target, temporary):
            with open_records(self.snapshot, self.options) as records:
                writer = csv.writer(target, lineterminator="\r\n", **self.options.dialect())
                writer.writerow(records.header)
                for _, _, row in records:
                    for i in self.indices:
                        value = row[i]
                        for name in self.rules:
                            value = RULES[name].transform(value)
                        row[i] = value
                    writer.writerow(row)
            target.flush()
            digest = file_sha256(temporary)
        return {
            "source_sha256": self.source_info["sha256"],
            "output": str(destination.resolve()),
            "output_sha256": digest,
            "output_encoding": encoding,
            "applied_rules": list(self.rules),
            "summary": self.summary(),
        }

    def export_map(self, destination: Path, *, scope: str = "collisions") -> dict:
        """Stream complete full-length key mappings, independent of evidence caps."""
        if scope not in ("collisions", "changed", "all"):
            raise AuditError("mapping scope must be collisions, changed or all")
        condition = {
            "collisions": "g.original_keys>1",
            "changed": "v.raw!=k.value",
            "all": "1=1",
        }[scope]
        query = f"""
            WITH groups AS (
                SELECT value, COUNT(*) AS original_keys FROM keys WHERE stage=? GROUP BY value
            )
            SELECT v.*, k.value, g.original_keys FROM variants v
            JOIN keys k ON k.raw_id=v.id AND k.stage=?
            JOIN groups g ON g.value=k.value
            WHERE {condition} ORDER BY v.first_record
        """
        count = 0
        with new_atomic_file(destination) as (target, temporary):
            writer = csv.writer(target, lineterminator="\r\n")
            writer.writerow(
                [
                    "collision_id",
                    "original_key_json",
                    "normalized_key_json",
                    "original_rows",
                    "normalized_original_keys",
                    "first_record",
                    "first_line",
                    "newly_empty_key",
                ]
            )
            for row in self.db.execute(query, (len(self.rules), len(self.rules))):
                count += 1
                writer.writerow(
                    [
                        hashlib.sha256(row["value"].encode("ascii")).hexdigest()
                        if row["original_keys"] > 1
                        else "",
                        row["raw"],
                        row["value"],
                        row["n"],
                        row["original_keys"],
                        row["first_record"],
                        row["first_line"],
                        row["newly_empty"],
                    ]
                )
            target.flush()
            digest = file_sha256(temporary)
        return {
            "source_sha256": self.source_info["sha256"],
            "output": str(destination.resolve()),
            "output_sha256": digest,
            "scope": scope,
            "mapping_rows": count,
            "rules": list(self.rules),
            "key_columns": self.columns,
        }
