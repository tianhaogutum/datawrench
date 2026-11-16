"""A bounded JSON cache with atomic replacement and conservative maintenance."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable

from .fingerprint import SCHEMA, InputError, digest

ENTRY = re.compile(r"[0-9a-f]{64}\.json\Z")
HISTORY = re.compile(r"\.inputs-[0-9a-f]{64}\Z")
COMPONENTS = ("content", "settings", "sqlfluff_version", "dependencies")
DEPENDENCY_COMPONENTS = ("template_files", "python_plugins", "environment")
READ_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError)
MARKER = ".sqlfluff-cache"
MAGIC = "sqlfluff-cache:1\n"
MAX_ENTRY = 16 * 1024 * 1024
MAX_HISTORY = 16 * 1024
CORRUPT_CODES = ("non_regular", "oversized", "unreadable", "invalid_data")
GC_REASONS = ("corrupt", "source_missing", "expired", "entry_missing", "entry_removed", "over_budget")


def validate_record(record: dict) -> None:
    if not isinstance(record, dict) or set(record) != {"violations", "statistics", "timings"}:
        raise ValueError("invalid record fields")
    if not isinstance(record["violations"], list):
        raise ValueError("invalid violations")
    for v in record["violations"]:
        for k in ("code", "description", "name"):
            if not isinstance(v.get(k), str):
                raise ValueError("invalid violation text")
        for k in ("start_line_no", "start_line_pos"):
            if type(v.get(k)) is not int or v[k] < 1:
                raise ValueError("invalid violation position")
        if type(v.get("warning")) is not bool or not isinstance(v.get("fixes", []), list):
            raise ValueError("invalid violation flags/fixes")
    for field in ("statistics", "timings"):
        if not isinstance(record[field], dict) or not all(
                isinstance(k, str) and type(v) in (int, float) and v >= 0
                for k, v in record[field].items()):
            raise ValueError("invalid statistics/timings")


class Cache:
    def __init__(self, directory: Path, warn: Callable[[str], None] = lambda msg: None):
        self.directory = directory.absolute()
        self.warn = warn

    def prepare(self, *, create: bool = False) -> bool:
        # Do not allow a cache path to redirect writes through symlink components.
        for path in (self.directory, *self.directory.parents):
            if path.is_symlink():
                raise InputError(f"Cache directory symlinks are unsupported: {path}")
        if not self.directory.exists():
            if not create:
                return False
            self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not self.directory.is_dir():
            raise InputError(f"Cache path is not a directory: {self.directory}")
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            return self._prepare_marker(create)
        finally:
            os.close(directory_fd)

    def _prepare_marker(self, create: bool) -> bool:
        marker = self.directory / MARKER
        if marker.is_symlink():
            raise InputError("Cache marker must not be a symlink")
        if not marker.exists():
            if any(self.directory.iterdir()):
                raise InputError(f"Refusing unmarked, nonempty cache directory: {self.directory}")
            if not create:
                return False
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(MAGIC)
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != len(MAGIC) or handle.read() != MAGIC:
                raise InputError("Unrecognized cache marker; use a separate cache directory")
        return True

    def _read(self, path: Path) -> dict:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENTRY:
                raise ValueError("entry is not a bounded regular file")
            entry = json.load(handle)
        if not isinstance(entry, dict) or entry.get("schema") != SCHEMA:
            raise ValueError("invalid schema")
        if entry.get("key") + ".json" != path.name:
            raise ValueError("key mismatch")
        if not isinstance(entry.get("source"), str) or not Path(entry["source"]).is_absolute():
            raise ValueError("invalid source path")
        validate_record(entry["record"])
        if digest(entry["record"]) != entry["checksum"]:
            raise ValueError("record checksum mismatch")
        return entry

    def _entry_path(self, key: str) -> Path:
        if not isinstance(key, str) or not ENTRY.fullmatch(key + ".json"):
            raise InputError("Cache key must be a SHA-256 hex digest")
        return self.directory / (key + ".json")

    def get(self, key: str, source: Path) -> dict | None:
        return self.lookup(key, source)[0]

    def lookup(self, key: str, source: Path) -> tuple[dict | None, str]:
        path = self._entry_path(key)
        try:
            entry = self._read(path)
            if entry["source"] != str(source.absolute()):
                raise ValueError("source mismatch")
            return entry["record"], "cache_hit"
        except FileNotFoundError:
            # A dangling symlink is corrupt, not a missing cache file.
            if not path.is_symlink():
                return None, "entry_missing"
            self.warn(f"Corrupt cache entry {path.name}; recomputing (FileNotFoundError)")
            return None, "corrupt_entry"
        except READ_ERRORS as exc:
            self.warn(f"Corrupt cache entry {path.name}; recomputing ({type(exc).__name__})")
            return None, "corrupt_entry"

    def put(self, key: str, source: Path, record: dict) -> bool:
        destination = self._entry_path(key)
        validate_record(record)
        entry = {"schema": SCHEMA, "key": key, "source": str(source.absolute()),
                 "record": record, "checksum": digest(record)}
        payload = json.dumps(entry, ensure_ascii=True, allow_nan=False).encode()
        if len(payload) > MAX_ENTRY:
            self.warn("Result exceeds 16 MiB; not cached")
            return False
        self._write(destination, payload)
        return True

    def _write(self, destination: Path, payload: bytes) -> None:
        fd, temp = tempfile.mkstemp(prefix=".entry-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, destination)
        finally:
            Path(temp).unlink(missing_ok=True)

    def _history_path(self, source: Path) -> Path:
        token = digest([str(source.absolute()), os.getcwd()])
        return self.directory / (".inputs-" + token)

    def _read_history(self, path: Path) -> dict:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HISTORY:
                raise ValueError("history is not a bounded regular file")
            entry = json.load(handle)
        checksum = entry.pop("checksum")
        fields = {"schema", "source", "cwd", "key", "components"}
        if (set(entry) not in (fields, fields | {"dependency_components"})
                or entry["schema"] != 1 or digest(entry) != checksum
                or not isinstance(entry["source"], str) or not Path(entry["source"]).is_absolute()
                or not isinstance(entry["cwd"], str) or not Path(entry["cwd"]).is_absolute()
                or path.name != ".inputs-" + digest([entry["source"], entry["cwd"]])
                or not isinstance(entry["key"], str) or not ENTRY.fullmatch(entry["key"] + ".json")
                or set(entry["components"]) != set(COMPONENTS)
                or any(not isinstance(v, str) or not ENTRY.fullmatch(v + ".json")
                       for v in entry["components"].values())):
            raise ValueError("invalid input history")
        if "dependency_components" in entry:
            details = entry["dependency_components"]
            if (not isinstance(details, dict) or set(details) != set(DEPENDENCY_COMPONENTS)
                    or any(not isinstance(v, str) or not ENTRY.fullmatch(v + ".json")
                           for v in details.values())):
                raise ValueError("invalid dependency history")
        return entry

    def miss_reasons(self, source: Path, components: dict, lookup_reason: str) -> list[str]:
        return self.miss_explanation(source, components, lookup_reason)[0]

    def miss_explanation(self, source: Path, components: dict, lookup_reason: str,
                         dependency_components: dict | None = None) -> tuple[list[str], list[str]]:
        # Exact-key corruption is known even if comparison history is absent.
        if lookup_reason == "corrupt_entry":
            return [lookup_reason], []
        path = self._history_path(source)
        try:
            previous = self._read_history(path)
        except FileNotFoundError:
            return ["not_cached"], []
        except READ_ERRORS as exc:
            self.warn(f"Input history unavailable {path.name} ({type(exc).__name__})")
            return ["history_unavailable"], []
        reasons = [name + "_changed" for name in COMPONENTS
                   if previous["components"][name] != components[name]] or ["entry_missing"]
        changes = []
        if dependency_components is not None and "dependencies_changed" in reasons:
            baseline = previous.get("dependency_components")
            changes = ([name for name in DEPENDENCY_COMPONENTS
                        if baseline[name] != dependency_components[name]] if baseline else [])
            # Legacy history has only the aggregate; do not infer a category.
            changes = changes or ["unavailable"]
        return reasons, changes

    def remember(self, key: str, source: Path, components: dict,
                 dependency_components: dict | None = None) -> None:
        path = self._history_path(source)
        entry = {"schema": 1, "source": str(source.absolute()), "cwd": os.getcwd(),
                 "key": key, "components": components}
        if dependency_components is not None:
            entry["dependency_components"] = dependency_components
        try:
            if self._read_history(path) == entry:
                return  # A warm hit need not write or fsync unchanged history.
        except READ_ERRORS:
            pass
        try:
            self._write(path, json.dumps({**entry, "checksum": digest(entry)}, allow_nan=False).encode())
        except OSError as exc:
            # Auxiliary comparison metadata must not break a valid cache replay
            # (e.g. a read-only legacy cache without history).
            self.warn(f"Could not save input history {path.name} ({type(exc).__name__})")

    def histories(self) -> list[Path]:
        return sorted(p for p in self.directory.iterdir() if HISTORY.fullmatch(p.name))

    def _remove(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            raise InputError(f"Refusing to remove a cache subdirectory: {path}")
        path.unlink(missing_ok=True)

    def entries(self) -> list[Path]:
        return sorted(p for p in self.directory.iterdir() if ENTRY.fullmatch(p.name))

    def _inventory(self) -> tuple[list[Path], list[Path], dict[Path, os.stat_result]]:
        """Share maintenance accounting and preflight without following links."""
        entries, histories = self.entries(), self.histories()
        info = {}
        for path in [*entries, *histories]:
            info[path] = path.lstat()
            if stat.S_ISDIR(info[path].st_mode):
                raise InputError(f"Unsupported cache subdirectory: {path}")
        return entries, histories, info

    def status(self, *, details: bool = False, corrupt_reasons: bool = False,
               corrupt_counts: bool = False, corrupt_bytes: bool = False) -> dict:
        """Report logical owned bytes and structural corruption, without repair.

        Valid orphan/expired histories are included, not marked corrupt. Like
        GC, this is a snapshot; run with lint writers idle for comparable totals.
        Opt-in details identify owned names, sizes and structural corruption,
        without exposing contents or source paths. Reads may update access time.
        Optional corrupt_reason codes require details and never expose errors.
        Independent corrupt_counts summarize those codes without listing names.
        Optional corrupt_bytes require counts and sum the same inventory sizes.
        """
        if corrupt_reasons and not details:
            raise InputError("--corrupt-reasons requires --details")
        if corrupt_bytes and not corrupt_counts:
            raise InputError("--corrupt-bytes requires --corrupt-counts")
        report = {"entries": 0, "bytes": 0, "corrupt": 0, "histories": 0,
                  "history_bytes": 0, "history_corrupt": 0, "managed_total_bytes": 0}
        if details:
            report["details"] = []
        if corrupt_counts:
            report["corrupt_counts"] = {kind: dict.fromkeys(CORRUPT_CODES, 0)
                                        for kind in ("entries", "histories")}
        if corrupt_bytes:
            report["corrupt_bytes"] = {kind: dict.fromkeys(CORRUPT_CODES, 0)
                                       for kind in ("entries", "histories")}
        if not self.prepare():
            return report
        entries, histories, info = self._inventory()
        for paths, read, limit, count, byte_count, corrupt_count in (
                (entries, self._read, MAX_ENTRY, "entries", "bytes", "corrupt"),
                (histories, self._read_history, MAX_HISTORY,
                 "histories", "history_bytes", "history_corrupt")):
            report[count] = len(paths)
            report[byte_count] = sum(info[path].st_size for path in paths)
            for path in paths:
                corrupt = False
                reason = None
                try:
                    read(path)
                except READ_ERRORS as exc:
                    corrupt = True
                    report[corrupt_count] += 1
                    if corrupt_reasons or corrupt_counts:
                        # Reuse this snapshot and validation failure; no second
                        # content read or disclosure of validator error text.
                        if not stat.S_ISREG(info[path].st_mode):
                            reason = "non_regular"
                        elif info[path].st_size > limit:
                            reason = "oversized"
                        elif isinstance(exc, OSError):
                            reason = "unreadable"
                        else:
                            reason = "invalid_data"
                        if corrupt_counts:
                            report["corrupt_counts"][count][reason] += 1
                        if corrupt_bytes:
                            report["corrupt_bytes"][count][reason] += info[path].st_size
                if details:
                    item = {"name": path.name,
                            "kind": "entry" if count == "entries" else "history",
                            "bytes": info[path].st_size, "corrupt": corrupt}
                    if corrupt_reasons:
                        item["corrupt_reason"] = reason
                    report["details"].append(item)
        report["managed_total_bytes"] = report["bytes"] + report["history_bytes"]
        if details:
            report["details"].sort(key=lambda item: item["name"])
        return report

    def plan_clear(self, *, summary: bool = False) -> dict:
        """Preview all owned deletions without reading contents or changing files.

        Logical bytes use lstat, including links themselves. This snapshot does
        not reserve a later clear; run maintenance with writers idle.
        Optional summary omits only the per-name plan, keeping the same counts
        and bytes without reading managed contents or symlink targets.
        """
        report = {"dry_run": True, "removed": 0, "histories_removed": 0,
                  "planned_entries": 0, "planned_histories": 0,
                  "managed_total_bytes": 0, "plan": []}
        if self.prepare():
            entries, histories, info = self._inventory()
            report.update(planned_entries=len(entries), planned_histories=len(histories),
                          managed_total_bytes=sum(item.st_size for item in info.values()),
                          plan=[{"name": path.name,
                                 "kind": "entry" if ENTRY.fullmatch(path.name) else "history",
                                 "bytes": info[path].st_size} for path in sorted(info)])
        if summary:
            report.pop("plan")
        return report

    def clear(self) -> int:
        """Preflight all owned names, then unlink; return the result-only count.

        Refuse managed directories or inventory errors before deleting anything.
        Unlink failures can still leave partial deletions; this is not a transaction.
        """
        if not self.prepare():
            return 0
        paths, histories, _ = self._inventory()
        for path in [*paths, *histories]:
            self._remove(path)
        return len(paths)

    def gc(self, max_age_days: float = 30, *, max_bytes: int | None = None) -> int:
        """Compatibility API: return the number of removed result entries."""
        return self.collect(max_age_days, max_bytes=max_bytes)["removed"]

    def collect(self, max_age_days: float = 30, *, max_bytes: int | None = None,
                dry_run: bool = False, summary: bool = False, reason_bytes: bool = False) -> dict:
        """Plan then optionally apply GC over managed results AND input histories.

        Bytes are logical lstat sizes, excluding the marker and unknown files.
        Eviction uses write time, not access time; hits do not refresh it. The
        returned bytes_after is projected in dry runs. Run maintenance while
        lint writers are idle: this is a snapshot plan, not a transaction.
        Planned counts include both kinds, even when dry-run removals are zero.
        Optional summary replaces the per-name plan with fixed reason counts
        from that same plan, without another content read or disclosing names.
        Optional reason_bytes requires summary and sums that plan's logical
        bytes by the same reasons, using the existing inventory snapshot.
        """
        if reason_bytes and not summary:
            raise InputError("--reason-bytes requires --summary")
        if (not isinstance(max_age_days, (int, float))
                or not math.isfinite(max_age_days) or max_age_days < 0):
            raise InputError("--max-age-days must be finite and nonnegative")
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
            raise InputError("--max-bytes must be a nonnegative integer")
        report = {"dry_run": dry_run, "max_age_days": max_age_days, "max_bytes": max_bytes,
                  "bytes_before": 0, "bytes_after": 0, "removed": 0,
                  "histories_removed": 0, "planned_entries": 0, "planned_histories": 0,
                  "plan": []}
        if summary:
            del report["plan"]
            report["planned_reasons"] = {kind: dict.fromkeys(GC_REASONS, 0)
                                         for kind in ("entries", "histories")}
        if reason_bytes:
            report["planned_reason_bytes"] = {kind: dict.fromkeys(GC_REASONS, 0)
                                              for kind in ("entries", "histories")}
        if not self.prepare():
            return report
        # Preflight everything before deleting anything, including directories
        # disguised as managed names. lstat never measures a symlink target.
        entries, histories, info = self._inventory()
        sizes = {path: item.st_size for path, item in info.items()}
        written = {path: item.st_mtime_ns for path, item in info.items()}
        order = lambda path: (written[path], path.name)
        reasons: dict[Path, str] = {}
        cutoff = time.time() - max_age_days * 86400
        for path in entries:
            try:
                entry = self._read(path)
                if not Path(entry["source"]).is_file():
                    reasons[path] = "source_missing"
                elif written[path] / 10**9 < cutoff:
                    reasons[path] = "expired"
            except READ_ERRORS:
                reasons[path] = "corrupt"
        linked: dict[Path, list[Path]] = {}
        for path in histories:
            try:
                history = self._read_history(path)
                target = self._entry_path(history["key"])
                if not Path(history["source"]).is_file():
                    reasons[path] = "source_missing"
                elif target in reasons:
                    reasons[path] = "entry_removed"
                elif target not in sizes:
                    reasons[path] = "entry_missing"
                elif written[path] / 10**9 < cutoff:
                    reasons[path] = "expired"
                else:
                    linked.setdefault(target, []).append(path)
            except READ_ERRORS:
                reasons[path] = "corrupt"
        before = sum(sizes.values())
        remaining = before - sum(sizes[path] for path in reasons)
        # Remove mandatory garbage first, then oldest live result groups until
        # the result + history budget fits. Never leave newly orphaned history.
        if max_bytes is not None:
            for path in sorted(entries, key=order):
                if remaining <= max_bytes:
                    break
                if path in reasons:
                    continue
                reasons[path] = "over_budget"
                remaining -= sizes[path]
                for history in linked.get(path, []):
                    reasons[history] = "entry_removed"
                    remaining -= sizes[history]
        plan = [{"name": path.name, "kind": "entry" if ENTRY.fullmatch(path.name) else "history",
                 "bytes": sizes[path], "reason": reasons[path]}
                for path in sorted(reasons, key=order)]
        report.update(bytes_before=before, bytes_after=remaining,
                      planned_entries=sum(item["kind"] == "entry" for item in plan),
                      planned_histories=sum(item["kind"] == "history" for item in plan))
        if summary:
            for item in plan:
                kind = "entries" if item["kind"] == "entry" else "histories"
                report["planned_reasons"][kind][item["reason"]] += 1
                if reason_bytes:
                    report["planned_reason_bytes"][kind][item["reason"]] += item["bytes"]
        else:
            report["plan"] = plan
        if not dry_run:
            for item in plan:
                self._remove(self.directory / item["name"])
            report["removed"] = sum(item["kind"] == "entry" for item in plan)
            report["histories_removed"] = sum(item["kind"] == "history" for item in plan)
        return report
