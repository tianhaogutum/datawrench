"""Offline CLI and bounded, atomic source rewriting."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import tokenize

from . import __version__
from .analysis import Diagnostic, RULES, Suppression, analyze_with_suppressions, fix_source
from . import baseline

MAX_BYTES = 2 * 1024 * 1024
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}


def error(path: str, message: str) -> Diagnostic:
    return Diagnostic(path, 1, 1, 1, 2, "PLV000", message)


def no_symlinks(path: Path):
    absolute = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise ValueError("symbolic link paths are unsupported")


def discover(paths: list[str]) -> tuple[list[Path], list[Diagnostic]]:
    files: dict[str, Path] = {}
    errors = []
    for raw in paths:
        root = Path(raw)
        try:
            no_symlinks(root)
            if root.is_file():
                files[os.path.abspath(root)] = root
            elif root.is_dir():
                def walk_error(exc):
                    errors.append(error(str(exc.filename), "Cannot traverse directory"))
                for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
                    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(directory) / d).is_symlink())
                    for name in sorted(names):
                        if name.endswith(".py"):
                            path = Path(directory) / name
                            files[os.path.abspath(path)] = path
            else:
                errors.append(error(raw, "Path does not exist or is not a regular file/directory"))
        except (OSError, ValueError) as exc:
            errors.append(error(raw, str(exc) if isinstance(exc, ValueError) else "Cannot inspect path"))
    return sorted(files.values(), key=str), errors


def read_source(path: Path) -> tuple[str, str, bytes, os.stat_result]:
    no_symlinks(path)
    # O_NOFOLLOW plus regular-file check prevents blocking on FIFOs/devices.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Only regular Python source files are supported")
        if info.st_size > MAX_BYTES:
            raise ValueError("Source exceeds 2 MiB analysis limit")
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Source exceeds 2 MiB analysis limit")
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding), encoding, raw, info


def atomic_write(path: Path, text: str, encoding: str, raw: bytes, original: os.stat_result):
    data = text.encode(encoding)
    # Validate rewritten syntax before touching the original.
    compile(text, str(path), "exec", flags=0, dont_inherit=True, optimize=0)
    atomic_replace(path, data, raw, original)


def atomic_replace(path: Path, data: bytes, raw: bytes | None, original: os.stat_result | None):
    no_symlinks(path)
    current = path.stat(follow_symlinks=False) if path.exists() else None
    if original is None:
        if current is not None:
            raise ValueError("Destination changed during analysis; refusing to overwrite")
    elif current is None:
        raise ValueError("Source changed during analysis; refusing to overwrite")
    if original is not None and (
        (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
            original.st_dev, original.st_ino, original.st_mtime_ns, original.st_size
        ) or path.read_bytes() != raw
    ):
        raise ValueError("Source changed during analysis; refusing to overwrite")
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix=".polars-vet-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), stat.S_IMODE(original.st_mode) if original else 0o644)
            os.fsync(handle.fileno())
        no_symlinks(path)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def codes(value: str) -> set[str]:
    result = {part.strip().upper() for part in value.split(",")}
    unknown = result - RULES.keys()
    if unknown:
        raise argparse.ArgumentTypeError("Unknown rule code(s): " + ", ".join(sorted(unknown)))
    return result


def github_escape(text: str, *, property: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if property else text


def suppression_audit(states: list[tuple[str, str]]) -> dict:
    priority = {status: i for i, status in enumerate((
        'complete', 'invalid-directive', 'syntax-error', 'analysis-error', 'read-error', 'discovery-error'
    ))}
    paths = {}
    for path, status in states:
        key = os.path.abspath(path)
        if key in paths:
            previous = paths[key]
            path = min(path, previous['path'])
            status = max(status, previous['status'], key=priority.__getitem__)
        paths[key] = {'path': path, 'status': status}
    rows = sorted(paths.values(), key=lambda row: row['path'])
    return {'complete': all(row['status'] == 'complete' for row in rows), 'paths': rows}


def render(diagnostics: list[Diagnostic], format: str, fixed: int, *,
           rows: list[baseline.Classified] | None = None, baseline_info: dict | None = None,
           suppressions: list[Suppression] | None = None,
           suppression_states: list[tuple[str, str]] | None = None) -> str:
    if rows is not None:
        diagnostics = [row.diagnostic for row in rows]

    def label(i):
        return f"[{rows[i].status}] " if rows is not None else ""

    def level(i):
        if rows is not None:
            return {'existing': 'note', 'resolved': 'note', 'uncertain': 'error', 'error': 'error', 'new': 'warning'}[rows[i].status]
        return 'error' if diagnostics[i].code == 'PLV000' else 'warning'

    def reason(i):
        return f" ({rows[i].reason})" if rows is not None and rows[i].reason else ""

    if format == "json":
        data = {"version": __version__, "diagnostics": [r.to_dict() for r in rows] if rows is not None else [d.to_dict() for d in diagnostics],
                "fixedFiles": fixed}
        if baseline_info is not None:
            data['baseline'] = baseline_info
        if suppressions is not None:
            data['suppressions'] = [s.to_dict() for s in sorted(suppressions, key=lambda s: (s.path, s.line, s.code))]
        if suppression_states is not None:
            data['suppressionAudit'] = suppression_audit(suppression_states)
        return json.dumps(data, ensure_ascii=True, indent=2)
    if format == "sarif":
        all_rules = {"PLV000": "Source, suppression, or safe-rewrite error", **RULES}
        return json.dumps({"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0", "runs": [{
            "tool": {"driver": {"name": "polars-vet", "version": __version__, "rules": [
                {"id": code, "shortDescription": {"text": message}} for code, message in all_rules.items()
            ]}},
            "results": [{"ruleId": d.code, "level": level(i),
                         **({'baselineState': {'existing': 'unchanged', 'new': 'new', 'resolved': 'absent'}[rows[i].status]}
                            if rows is not None and rows[i].status in {'existing', 'new', 'resolved'} else {}),
                         **({'properties': {'baselineStatus': rows[i].status, 'baselineReason': rows[i].reason}}
                            if rows is not None else {}),
                         "message": {"text": label(i) + d.message + reason(i)}, "locations": [{"physicalLocation": {
                             "artifactLocation": {"uri": Path(os.path.abspath(d.path)).as_uri()},
                             "region": {"startLine": d.line, "startColumn": d.column,
                                        "endLine": d.end_line, "endColumn": d.end_column}
                         }}]} for i, d in enumerate(diagnostics)]
        }]}, indent=2)
    if format == "github":
        return "\n".join(
            f"::{ 'notice' if level(i) == 'note' else level(i) } file={github_escape(d.path, property=True)},line={d.line},col={d.column},title={d.code}::{github_escape(label(i) + d.message + reason(i))}"
            for i, d in enumerate(diagnostics)
        )
    return "\n".join(f"{d.path}:{d.line}:{d.column} {d.code} {label(i)}{d.message}{reason(i)}" + (" [fixable]" if d.edits else "") for i, d in enumerate(diagnostics))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datawrench polars-vet", description="Static Polars performance checks; never executes source.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Check Python files or recursively scan directories")
    check.add_argument("paths", nargs="+")
    check.add_argument("--select", type=codes, default=set(RULES))
    check.add_argument("--ignore", type=codes, default=set())
    check.add_argument("--format", choices=["text", "json", "sarif", "github"], default="text")
    check.add_argument("--fix", action="store_true", help="Apply conservative PLV001 fixes; report remaining diagnostics")
    check.add_argument("--report-unused-suppressions", action="store_true",
                       help="Report unused reasoned suppression codes among selected rules as PLV000")
    check.add_argument("--show-suppressions", action="store_true",
                       help="Include valid reasoned suppressions and usage in JSON (discloses reasons)")
    check.add_argument("--exit-zero", action="store_true", help="Return zero for diagnostics (usage errors remain 2)")
    check.add_argument("--baseline", type=Path, help="Compare against a JSON baseline; its directory anchors relative input paths")
    check.add_argument("--update-baseline", action="store_true", help="Explicitly create/replace the baseline without changing source")
    check.add_argument("--baseline-path-map", action="append", default=[], metavar="OLD=NEW",
                       help="Map an intentional file rename relative to the baseline directory (repeatable)")
    check.add_argument("--show-baseline-policy", action="store_true",
                       help="Include current normalized scan scope and rule policy in baseline JSON")
    check.add_argument("--show-baseline-rule-counts", action="store_true",
                       help="Include per-rule classification counts in baseline JSON")
    check.add_argument("--show-baseline-path-counts", action="store_true",
                       help="Include per-path classification counts in baseline JSON")
    args = parser.parse_args(argv)
    if args.show_suppressions and args.format != 'json':
        parser.error("--show-suppressions requires --format json")
    if args.show_baseline_policy and not args.baseline:
        parser.error("--show-baseline-policy requires --baseline FILE")
    if args.show_baseline_policy and args.format != 'json':
        parser.error("--show-baseline-policy requires --format json")
    if args.show_baseline_rule_counts and not args.baseline:
        parser.error("--show-baseline-rule-counts requires --baseline FILE")
    if args.show_baseline_rule_counts and args.format != 'json':
        parser.error("--show-baseline-rule-counts requires --format json")
    if args.show_baseline_path_counts and not args.baseline:
        parser.error("--show-baseline-path-counts requires --baseline FILE")
    if args.show_baseline_path_counts and args.format != 'json':
        parser.error("--show-baseline-path-counts requires --format json")
    if args.update_baseline and not args.baseline:
        parser.error("--update-baseline requires --baseline FILE")
    if args.baseline_path_map and not args.baseline:
        parser.error("--baseline-path-map requires --baseline FILE")
    if args.baseline and args.fix:
        parser.error("--baseline and --fix cannot be combined; baseline checks never rewrite source")
    selected = args.select - args.ignore

    def check_source(text, path):
        return analyze_with_suppressions(text, str(path), selected=selected,
                                         report_unused_suppressions=args.report_unused_suppressions)

    previous = []
    mapping = {}
    baseline_raw = baseline_stat = None
    if args.baseline:
        args.baseline = Path(os.path.abspath(args.baseline))
        baseline_root = args.baseline.parent
        try:
            no_symlinks(args.baseline)
            if args.baseline.suffix != '.json':
                raise ValueError("Baseline file must use a .json extension")
            current_policy = baseline.policy(args.paths, baseline_root, selected, args.report_unused_suppressions)
            mapping = baseline.path_maps(args.baseline_path_map)
            if args.baseline.exists():
                baseline_text, _, baseline_raw, baseline_stat = read_source(args.baseline)
                try:
                    previous = baseline.load(baseline_text, baseline.prior_policy(current_policy, mapping))
                except ValueError:
                    if not args.update_baseline or mapping:
                        raise  # Only an explicit refresh can reset schema/scope/policy.
                if mapping:
                    previous = baseline.remap(previous, mapping)
            elif mapping:
                raise ValueError("Baseline path maps require an existing valid baseline")
            elif not args.update_baseline:
                raise ValueError("Baseline does not exist; use --update-baseline to create it explicitly")
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            parser.error(str(exc) if isinstance(exc, ValueError) else "Cannot read baseline (" + type(exc).__name__ + ")")

    files, diagnostics = discover(args.paths)
    discovery_failed = bool(diagnostics)
    if args.baseline and any(Path(os.path.abspath(p)) == args.baseline for p in files):
        parser.error("Baseline file cannot also be a checked source file")
    if mapping:
        scanned = {os.path.abspath(path) for path in files}
        try:
            for old, new in mapping.items():
                old_path, new_path = baseline_root / old, baseline_root / new
                no_symlinks(old_path)
                no_symlinks(new_path)
                if old_path.exists():
                    raise ValueError("OLD must no longer exist")
                if str(new_path) not in scanned or not new_path.is_file():
                    raise ValueError("NEW must be a regular file discovered by this check")
        except (OSError, ValueError) as exc:
            parser.error("Invalid baseline path map: " + (str(exc) if isinstance(exc, ValueError) else "cannot inspect paths"))
    entries = [None] * len(diagnostics)
    suppressions = [] if args.show_suppressions else None
    suppression_states = [(d.path, 'discovery-error') for d in diagnostics] if args.show_suppressions else None
    fixed = 0
    for path in files:
        result = None
        failure_status = 'read-error'
        try:
            text, encoding, raw, info = read_source(path)
            failure_status = 'analysis-error'
            result = check_source(text, path)
            found = result.diagnostics
            if args.fix:
                updated = fix_source(text, found)
                if updated != text:
                    atomic_write(path, updated, encoding, raw, info)
                    fixed += 1
                    result = check_source(updated, path)
                    found = result.diagnostics
            if args.baseline:
                entries.extend(baseline.identities(text, found, baseline_root))
            diagnostics.extend(found)
            if suppressions is not None:
                suppressions.extend(result.suppressions)
                suppression_states.append((str(path), result.suppression_status))
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "Cannot read or safely rewrite Python source (" + type(exc).__name__ + ")"
            diagnostics.append(error(str(path), message))
            entries.append(None)
            if suppressions is not None:
                suppression_states.append((str(path), failure_status))
                if result is not None:
                    suppressions.extend(Suppression(s.path, s.line, s.code, s.reason,
                                                    'disabled' if s.status == 'disabled' else 'unknown')
                                        for s in result.suppressions)
    if args.baseline:
        rows = baseline.compare(diagnostics, entries, previous, baseline_root, discovery_failed=discovery_failed,
                                scanned_paths={os.path.abspath(path) for path in files})
        counts = {status: sum(row.status == status for row in rows)
                  for status in ('existing', 'new', 'resolved', 'uncertain', 'error')}
        updated = False
        if args.update_baseline and not (counts['error'] or counts['uncertain']):
            try:
                data = baseline.snapshot(entries, current_policy).encode('utf-8')
                if len(data) > MAX_BYTES:
                    raise ValueError("Baseline exceeds 2 MiB limit")
                atomic_replace(args.baseline, data, baseline_raw, baseline_stat)
                updated = True
            except (OSError, ValueError) as exc:
                parser.error(str(exc) if isinstance(exc, ValueError) else "Cannot write baseline (" + type(exc).__name__ + ")")
        baseline_info = {'path': str(args.baseline), 'updated': updated, 'counts': counts}
        if args.show_baseline_policy:
            baseline_info['policy'] = current_policy
        if args.show_baseline_rule_counts:
            by_code = {}
            for row in rows:
                code = row.diagnostic.code
                if code not in by_code:
                    by_code[code] = {'code': code, **dict.fromkeys(counts, 0)}
                by_code[code][row.status] += 1
            baseline_info['ruleCounts'] = [by_code[code] for code in sorted(by_code)]
        if args.show_baseline_path_counts:
            by_path = {}
            for row in rows:
                path = baseline.relative(row.diagnostic.path, baseline_root)
                if path not in by_path:
                    by_path[path] = {'path': path, **dict.fromkeys(counts, 0)}
                by_path[path][row.status] += 1
            baseline_info['pathCounts'] = [by_path[path] for path in sorted(by_path)]
        if mapping:
            baseline_info['pathMaps'] = [{'oldPath': old, 'newPath': new}
                                         for old, new in sorted(mapping.items())]
        output = render(diagnostics, args.format, fixed, rows=rows,
                        baseline_info=baseline_info,
                        suppressions=suppressions, suppression_states=suppression_states)
        if output:
            print(output)
        if args.update_baseline:
            return 0 if updated else 1
        return 1 if (counts['new'] or counts['uncertain'] or counts['error']) and not args.exit_zero else 0
    diagnostics.sort(key=lambda d: (d.path, d.line, d.column, d.code))
    output = render(diagnostics, args.format, fixed, suppressions=suppressions, suppression_states=suppression_states)
    if output:
        print(output)
    return 1 if diagnostics and not args.exit_zero else 0


if __name__ == "__main__":
    sys.exit(main())
