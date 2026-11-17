"""The integration layer: selected failure oracle, evidence, and portable bundle."""

import hashlib
import importlib.metadata
import json
import math
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from array import array
from collections import OrderedDict
from pathlib import Path

import dfshrink
import polars as pl
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from . import __version__
from .table import CsvTable, InputError
from .validator import ValidationError, Worker, validate_options


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validator_versions() -> dict[str, str]:
    """Pin the installed runtime graph, without unrelated development packages."""
    pending = [Requirement("pandas"), Requirement("pandera[pandas,io]")]
    visited = {}
    versions = {}
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        extras = {"", *requirement.extras} - visited.get(name, set())
        if not extras:
            continue
        visited.setdefault(name, set()).update(extras)
        distribution = importlib.metadata.distribution(name)
        versions[distribution.metadata["Name"]] = distribution.version
        for expression in distribution.requires or []:
            dependency = Requirement(expression)
            if dependency.marker is None or any(
                dependency.marker.evaluate({"extra": extra}) for extra in extras
            ):
                pending.append(dependency)
    return dict(sorted(versions.items()))


def ranges(positions):
    """Compact, one-based inclusive intervals for exact audit reconstruction."""
    result = []
    for position in positions:
        value = position + 1
        if result and result[-1][1] + 1 == value:
            result[-1][1] = value
        else:
            result.append([value, value])
    return result


class Oracle:
    def __init__(self, table, candidate, worker, target, stability_runs, log):
        self.table = table
        self.candidate = candidate
        self.worker = worker
        self.target = target
        self.stability_runs = stability_runs
        self.log = log
        self.validation_runs = 0
        self.unresolved = 0
        self.cached_candidates = 0
        self.cache = OrderedDict()
        self.event_counter = 0
        self.deadline = None
        self.best_positions = None
        self.reduction_requests = 0

    def _record(self, event):
        self.event_counter += 1
        event["event"] = self.event_counter
        self.log.write(json.dumps(event, sort_keys=True) + "\n")
        self.log.flush()
        return self.event_counter

    def evaluate(self, positions, stage="reduce", *, required=False):
        if stage == "reduce":
            self.reduction_requests += 1
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise ReductionDeadline
        positions = list(positions)
        key = hashlib.sha256(array("Q", positions)).digest()
        if stage == "reduce" and key in self.cache:
            matched, event = self.cache[key]
            self.cache.move_to_end(key)
            self.cached_candidates += 1
            self._record(
                {
                    "stage": stage,
                    "rows": len(positions),
                    "source_record_ranges": ranges(positions),
                    "target_present": matched,
                    "observed_failure_ids": [],
                    "cache_hit": True,
                    "reused_event": event,
                    "elapsed_ms": 0,
                }
            )
            return matched
        self.table.write_subset(positions, self.candidate)
        outcomes = []
        observed = []
        started = time.monotonic()
        error = None
        for _ in range(self.stability_runs):
            response = self.worker.validate(self.candidate)
            self.validation_runs += 1
            if response["status"] == "ok" and response.get("parsed_rows") != len(positions):
                response = {
                    "status": "error",
                    "kind": "RecordCountMismatch",
                    "message": "CSV parser changed the logical record count",
                }
            if response["status"] != "ok":
                error = {"kind": response["kind"], "message": response["message"]}
                break
            ids = sorted(f["id"] for f in response["failures"])
            observed.append(ids)
            outcomes.append(self.target in ids)
        unstable = len(set(outcomes)) > 1
        matched = error is None and not unstable and all(outcomes)
        event = {
            "stage": stage,
            "rows": len(positions),
            "source_record_ranges": ranges(positions),
            "target_present": matched,
            "observed_failure_ids": observed,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        }
        if error:
            event["error"] = error
            self.unresolved += 1
        if unstable:
            event["error"] = {"kind": "Nondeterminism", "message": "target presence changed"}
        event_id = self._record(event)
        if unstable:
            raise ValidationError("selected failure is nondeterministic; no bundle was published")
        if required and error:
            raise ValidationError(f"validator {error['kind']}: {error['message']}")
        if matched and (self.best_positions is None or len(positions) < len(self.best_positions)):
            self.best_positions = positions
        if error is None and stage != "final":
            self.cache[key] = matched, event_id
            self.cache.move_to_end(key)
            if len(self.cache) > 128:
                self.cache.popitem(last=False)
        return matched

    def __call__(self, frame):
        return self.evaluate(frame["record"].to_list())


class ReductionDeadline(Exception):
    """Stop the existing engine and preserve the last independently checked hit."""


def list_failures(
    data,
    schema,
    delimiter,
    options,
    timeout,
    max_bytes,
    *,
    max_rows=250_000,
    max_columns=2048,
):
    table = CsvTable.scan(data, delimiter, max_bytes, max_rows=max_rows, max_columns=max_columns)
    with Worker(schema, delimiter, options, timeout) as worker:
        result = worker.validate(data)
    if result["status"] != "ok":
        raise ValidationError(f"validator {result['kind']}: {result['message']}")
    if result.get("parsed_rows") != table.rows:
        raise ValidationError("CSV parser changed the logical record count")
    return result["failures"]


def reduce_to_bundle(
    data: Path,
    schema: Path,
    output: Path,
    target: str,
    *,
    delimiter=",",
    options=None,
    timeout=10.0,
    max_evals=1000,
    stability_runs=2,
    max_bytes=32 * 1024 * 1024,
    max_rows=250_000,
    max_columns=2048,
    max_seconds=300.0,
    _parent_bundle=None,
    _expected_files=None,
):
    options = validate_options({} if options is None else options)
    if max_evals < 1 or not 2 <= stability_runs <= 10:
        raise InputError("max-evals >= 1 and stability-runs between 2 and 10 are required")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise InputError("timeout must be finite and between 0 and 300 seconds")
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise InputError("max-seconds must be finite and positive")
    if output.exists() or output.is_symlink():
        raise InputError("output already exists; choose a new bundle directory")
    if not output.parent.is_dir():
        raise InputError("output parent directory does not exist")
    if schema.suffix.lower() not in {".yaml", ".yml", ".py"}:
        raise InputError("schema must be Pandera YAML or a self-contained Python .py file")
    if data.stat().st_size > max_bytes:
        raise InputError(f"input exceeds the {max_bytes}-byte limit")
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        work = Path(temporary)
        source = work / "source.csv"
        shutil.copyfile(data, source)
        table = CsvTable.scan(
            source,
            delimiter,
            max_bytes,
            max_rows=max_rows,
            max_columns=max_columns,
        )
        schema_name = "schema.py" if schema.suffix.lower() == ".py" else "schema.yaml"
        shutil.copyfile(schema, work / schema_name)
        source_digest = sha256(source)
        schema_digest = sha256(work / schema_name)
        if _expected_files and (
            source_digest != _expected_files["data"] or schema_digest != _expected_files["schema"]
        ):
            raise ValidationError("parent bundle data or schema changed before snapshotting")
        candidate = work / "candidate.csv"
        original = list(range(table.rows))
        with (work / "reduction.jsonl").open("w", encoding="utf-8") as log:
            with Worker(work / schema_name, delimiter, options, timeout) as worker:
                response = worker.validate(source)
                if response["status"] != "ok":
                    raise ValidationError(f"validator {response['kind']}: {response['message']}")
                if response.get("parsed_rows") != table.rows:
                    raise ValidationError("CSV parser changed the logical record count")
                targets = {failure["id"]: failure for failure in response["failures"]}
                if target not in targets:
                    raise ValidationError(
                        "target failure is absent; use 'datawrench failurepack list' to choose one"
                    )
                oracle = Oracle(table, candidate, worker, target, stability_runs, log)
                oracle.validation_runs += 1
                if not oracle.evaluate(original, "baseline", required=True):
                    raise ValidationError("target failure disappeared during baseline verification")
                empty_fails = oracle.evaluate([], "empty")
                reduction_started = time.monotonic()
                stop_reason = "completed"
                if empty_fails:
                    selected, calls, minimal = [], 0, True
                elif not original:
                    raise ValidationError("empty input changed its validation result")
                else:
                    ids = pl.DataFrame({"record": pl.Series(range(table.rows), dtype=pl.UInt64)})
                    # Reuse the established engine; do not implement a second reducer.
                    oracle.deadline = reduction_started + max_seconds
                    try:
                        result = dfshrink.shrink_rows(ids, oracle, max_evals=max_evals)
                    except ReductionDeadline:
                        selected = oracle.best_positions
                        calls, minimal = oracle.reduction_requests, False
                        stop_reason = "time_budget"
                        oracle._record(
                            {
                                "stage": "stop",
                                "reason": stop_reason,
                                "rows": len(selected),
                                "source_record_ranges": ranges(selected),
                                "observed_failure_ids": [],
                                "target_present": True,
                                "reuses_prior_evidence": True,
                            }
                        )
                    else:
                        if result is None:
                            raise ValidationError("target failure disappeared before reduction")
                        selected = result.frame["record"].to_list()
                        calls, minimal = result.predicate_calls, result.minimality_proven
                        if not minimal:
                            stop_reason = "evaluation_budget"
                reduction_elapsed = time.monotonic() - reduction_started
                unresolved = oracle.unresolved
                worker_starts = worker.starts
                worker_peak = worker.peak_rss_bytes
            # Fresh interpreter for final evidence, independent of the reduction worker.
            with Worker(work / schema_name, delimiter, options, timeout) as fresh_worker:
                oracle.worker = fresh_worker
                if not oracle.evaluate(selected, "final", required=True):
                    raise ValidationError("final CSV did not reproduce the selected failure")
                total_runs = oracle.validation_runs
                worker_starts += fresh_worker.starts
                worker_peak = max(worker_peak, fresh_worker.peak_rss_bytes)
        if sha256(source) != source_digest or sha256(work / schema_name) != schema_digest:
            raise ValidationError("fixed input or schema snapshot changed during validation")
        table.write_subset(selected, work / "data.csv")
        write_json(work / "read-options.json", {"delimiter": delimiter, "pandas_options": options})
        package = Path(__file__).parent
        shutil.copyfile(package / "_worker.py", work / "_validator.py")
        shutil.copyfile(package / "replay_template.py.txt", work / "replay.py")
        versions = validator_versions()
        (work / "requirements.txt").write_text(
            "# Replay needs only the existing validators, not datawrench or dfshrink.\n"
            + "\n".join(
                f"{name}{'[pandas,io]' if name == 'pandera' else ''}=={version}"
                for name, version in versions.items()
            )
            + "\n",
            encoding="utf-8",
        )
        members = [
            "data.csv",
            schema_name,
            "read-options.json",
            "reduction.jsonl",
            "_validator.py",
            "replay.py",
            "requirements.txt",
        ]
        manifest = {
            "format_version": 1,
            "tool": {"name": "failurepack", "version": __version__},
            "target": targets[target],
            "schema_file": schema_name,
            "source": {
                "name": data.name,
                "sha256": source_digest,
                "bytes": source.stat().st_size,
                "rows": table.rows,
            },
            "result": {"rows": len(selected), "source_record_ranges": ranges(selected)},
            "reduction": {
                "engine": "dfshrink.shrink_rows",
                "engine_version": dfshrink.__version__,
                "max_evals": max_evals,
                "max_seconds": max_seconds,
                "elapsed_seconds": round(reduction_elapsed, 6),
                "stop_reason": stop_reason,
                "predicate_calls": calls,
                "validation_runs": total_runs,
                "cached_candidates": oracle.cached_candidates,
                "worker_starts": worker_starts,
                "worker_max_requests": 128,
                "worker_rss_recycle_threshold_bytes": 192 * 1024 * 1024,
                "worker_peak_rss_bytes": worker_peak,
                "stability_runs": stability_runs,
                "timeout_seconds": timeout,
                "unresolved_candidates": unresolved,
                "one_minimal": minimal and not unresolved,
                "global_minimum_guaranteed": False,
            },
            "validator_versions": versions,
            "environment": {"python": platform.python_version(), "polars": pl.__version__},
            "files": {name: sha256(work / name) for name in members},
        }
        if _parent_bundle is not None:
            manifest["parent_bundle"] = _parent_bundle
        write_json(work / "manifest.json", manifest)
        publish = work / "bundle"
        publish.mkdir()
        for name in [*members, "manifest.json"]:
            (work / name).rename(publish / name)
        # Publish only the fully checked bundle; never overwrite an existing output.
        if output.exists():
            raise InputError("output appeared while reducing; refusing to overwrite it")
        publish.rename(output)
    return manifest


def resume_bundle(parent: Path, output: Path, **budgets):
    """Continue a verified bundle using dfshrink again, without the full source."""
    if output.exists() or output.is_symlink():
        raise InputError("output already exists; choose a new bundle directory")
    parent = parent.resolve()
    manifest_path = parent / "manifest.json"
    if manifest_path.stat().st_size > 16 * 1024 * 1024:
        raise InputError("parent bundle manifest exceeds the 16 MiB limit")
    before = sha256(manifest_path)
    # Use this installation's verifier, so no unverified replay script is run.
    verifier = Path(__file__).with_name("replay_template.py.txt")
    result = subprocess.run(
        [sys.executable, str(verifier), "--bundle", str(parent)],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:1000]
        raise ValidationError("parent bundle did not verify and reproduce: " + detail)
    encoded = manifest_path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != before:
        raise ValidationError("parent bundle manifest changed during verification")
    manifest = json.loads(encoded)
    settings_bytes = (parent / "read-options.json").read_bytes()
    if hashlib.sha256(settings_bytes).hexdigest() != manifest["files"]["read-options.json"]:
        raise ValidationError("parent bundle parser settings changed during verification")
    settings = json.loads(settings_bytes)
    timeout = budgets.pop("timeout", None)
    return reduce_to_bundle(
        parent / "data.csv",
        parent / manifest["schema_file"],
        output,
        manifest["target"]["id"],
        delimiter=settings["delimiter"],
        options=settings["pandas_options"],
        timeout=manifest["reduction"]["timeout_seconds"] if timeout is None else timeout,
        stability_runs=manifest["reduction"]["stability_runs"],
        _expected_files={
            "data": manifest["files"]["data.csv"],
            "schema": manifest["files"][manifest["schema_file"]],
        },
        _parent_bundle={
            "manifest_sha256": before,
            "source": manifest["source"],
            "result": manifest["result"],
            "target_id": manifest["target"]["id"],
        },
        **budgets,
    )
