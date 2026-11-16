"""Command line entry points. No SQLMesh plans, runs or external commands."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

from . import __version__
from .exporter import build_manifest, serialize
from .diff import compare_manifests, render_diff
from .diff_validation import diff_validation_result, validate_diff
from .loading import ExportError, load_project
from .lineage import build_lineage_report
from .report_validation import ReportValidationError, validate_report, validation_result
from .validation import ManifestError, manifest_validation_result, read_manifest, read_manifest_input, validate_manifest


def _check_destination(path: Path) -> None:
    # The caller explicitly chooses this file. Refuse symlink destinations and
    # symlink ancestors rather than following them into an unexpected directory.
    absolute = path.absolute()
    if any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise ExportError("Output path may not contain symlinks")
    if absolute.exists() and not absolute.is_file():
        raise ExportError("Output path is not a regular file")


def _check_output(path: Path, project_root: Path) -> None:
    _check_destination(path)
    output = path.resolve()
    if output.is_relative_to(project_root):
        relative = output.relative_to(project_root)
        protected = {"config.yaml", "config.yml", "config.py", "external_models.yaml", "schema.yaml", ".env",
                     "models", "seeds", "audits", "macros", "signals", "materializations", "linter", "metrics", "tests", "external_models"}
        if relative.parts and relative.parts[0] in protected:
            raise ExportError("Output may not overwrite project inputs; use target/manifest.json")


def _write_atomic(path: Path, content: str) -> None:
    _check_destination(path)
    absolute = path.absolute()
    absolute.parent.mkdir(parents=True, exist_ok=True)
    scratch = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=absolute.parent,
                                         prefix=".manifest-", suffix=".tmp", delete=False) as stream:
            scratch = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(scratch, absolute)
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)


def _check_diff_output(path: Path, inputs: tuple[Path, Path]) -> None:
    _check_destination(path)
    for source in inputs:
        if path.resolve() == source.resolve() or (path.exists() and source.exists() and path.samefile(source)):
            raise ExportError("Diff output may not overwrite either input manifest")


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(prog="datawrench sqlmesh-manifest",
                                  description="Export local SQLMesh metadata to a dbt-compatible manifest")
    app.add_argument("--version", action="version", version=__version__)
    sub = app.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="Load local metadata without warehouse connections")
    export.add_argument("--project", required=True, type=Path)
    export.add_argument("--out", required=True, type=Path)
    export.add_argument("--report", type=Path, help="Write a JSON provenance and untranslated-semantics report")
    export.add_argument("--gateway")
    export.add_argument("--project-name")
    export.add_argument("--schema-version", default="12", choices=["12"])
    export.add_argument("--deterministic", action="store_true")
    export.add_argument("--trust-project", action="store_true", help="Allow executable project Python and macros; this is not a sandbox")
    validate = sub.add_parser("validate")
    validate.add_argument("file", help="Saved manifest JSON file, or - for stdin (./- for a literal dash file)")
    validate.add_argument("--format", choices=("text", "json"), default="text")
    sub.add_parser("summary").add_argument("file", help="Saved manifest JSON file, or - for stdin (./- for a literal dash file)")
    report = sub.add_parser("validate-report", help="Validate a provenance report against exact manifest bytes")
    report.add_argument("report", help="Saved provenance report JSON file, or - for stdin (./- for a literal dash file)")
    report.add_argument("--manifest", required=True, type=Path)
    report.add_argument("--format", choices=("text", "json"), default="text")
    validate_diff_cmd = sub.add_parser("validate-diff", help="Validate saved diff-v1 JSON structure offline")
    validate_diff_cmd.add_argument("file", help="Saved diff JSON file, or - for stdin (./- for a literal dash file)")
    validate_diff_cmd.add_argument("--format", choices=("text", "json"), default="text")
    diff = sub.add_parser("diff", help="Compare offline model/source lifecycle, model dependencies and columns in two v12 manifests")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument("--format", choices=("text", "json"), default="text")
    diff.add_argument("--out", type=Path, help="Write the result atomically instead of printing to stdout")
    diff.add_argument("--include-descriptions", action="store_true",
                      help="Compare exact top-level descriptions on surviving enabled models and sources")
    return app


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "export":
            project_root = args.project.resolve()
            _check_output(args.out, project_root)
            if args.report is not None:
                _check_output(args.report, project_root)
                output, report_path = args.out.resolve(), args.report.resolve()
                if output.is_relative_to(report_path) or report_path.is_relative_to(output):
                    raise ExportError("Manifest and report paths must be distinct files, not ancestors of one another")
            with load_project(args.project, args.gateway, trusted=args.trust_project) as context:
                manifest, warnings = build_manifest(context, project_name=args.project_name,
                                                    deterministic=args.deterministic)
                validate_manifest(manifest)
                report = build_lineage_report(context, manifest) if args.report is not None else None
            manifest_text = serialize(manifest)
            report_text = serialize(report) if report is not None else None
            _write_atomic(args.out, manifest_text)
            if report_text is not None:
                _write_atomic(args.report, report_text)
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
            print(f"Exported {len(manifest['nodes'])} nodes and {len(manifest['sources'])} sources to {args.out}")
        elif args.command == "validate-report":
            try:
                counts = validate_report(args.report, args.manifest)
            except Exception as exc:
                if args.format != "json":
                    raise
                diagnostic = (exc.diagnostic if isinstance(exc, ReportValidationError)
                              else {"input": "pair", "path": "$", "rule": "internal_error"})
                print(serialize(validation_result(diagnostic=diagnostic)), end="")
                return 1
            if args.format == "json":
                print(serialize(validation_result(counts=counts)), end="")
            else:
                print("Valid provenance report v1 for supplied manifest")
        elif args.command == "validate-diff":
            if args.format == "json":
                result = diff_validation_result(args.file)
                print(serialize(result), end="")
                return 0 if result["valid"] else 1
            try:
                validate_diff(args.file)
            except Exception:
                # Do not expose schema messages, arbitrary metadata keys, input
                # paths or exception details, even for unexpected failures.
                print("validation error: Invalid or unsupported semantic diff report", file=sys.stderr)
                return 1
            print("Valid semantic diff v1 (structural validation only)")
        elif args.command == "diff":
            if args.out is not None:
                _check_diff_output(args.out, (args.before, args.after))
            report = compare_manifests(read_manifest(args.before), read_manifest(args.after),
                                       include_descriptions=args.include_descriptions)
            content = serialize(report) if args.format == "json" else render_diff(report)
            if args.out is not None:
                _write_atomic(args.out, content)
            else:
                print(content, end="")
            return 1 if report["has_changes"] else 0
        elif args.command == "validate" and args.format == "json":
            result = manifest_validation_result(args.file)
            print(serialize(result), end="")
            return 0 if result["valid"] else 1
        else:
            manifest = read_manifest_input(args.file)
            validate_manifest(manifest)
            if args.command == "validate":
                print("Valid dbt manifest (dbt-artifacts-parser; v12 also checked against vendored official schema)")
            else:
                counts = Counter()
                for group in ("nodes", "sources", "macros", "docs", "exposures", "metrics", "groups", "saved_queries", "semantic_models", "unit_tests"):
                    for node in (manifest.get(group) or {}).values():
                        # Legacy documentation has no resource_type; validation
                        # has already checked its version-specific shape.
                        resource = node.get("resource_type", "doc") if group == "docs" else node["resource_type"]
                        counts[resource] += 1
                edges = sum(len(v) for v in (manifest.get("parent_map") or {}).values())
                fields = [f"models={counts['model']}", f"seeds={counts['seed']}", f"sources={counts['source']}", f"tests={counts['test']}"]
                for resource in sorted(counts.keys() - {"model", "seed", "source", "test"}):
                    label = "analyses" if resource == "analysis" else resource + "s"
                    fields.append(f"{label}={counts[resource]}")
                fields.append(f"edges={edges}")
                print(" ".join(fields))
        return 0
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ManifestError as exc:
        print(f"validation error: {exc}", file=sys.stderr)
        return 2 if args.command in ("export", "diff") else 1
    except Exception as exc:
        # Upstream exceptions can include connection passwords, env vars, raw
        # templates or arbitrary project exception strings. Report only type.
        print(f"error: {type(exc).__name__} while {args.command}ing; check local project syntax and gateway configuration", file=sys.stderr)
        return 2 if args.command in ("export", "diff") else 1


if __name__ == "__main__":
    sys.exit(main())
