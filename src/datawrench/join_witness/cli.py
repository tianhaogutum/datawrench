from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import ExitStack, suppress
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from tableproof import JoinSpec, TableProofError
from tableproof.config import load_config

from . import __version__
from .engine import analyze
from .sql import sql_script


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="datawrench join-witness", description="Explain TableProof join inflation before building a wide join."
    )
    result.add_argument("--version", action="version", version=__version__)
    utility = result.add_mutually_exclusive_group()
    utility.add_argument("--demo", action="store_true", help="Run the packaged four-order example; intentional policy failure")
    utility.add_argument("--print-schema", action="store_true", help="Print the bundled JSON report schema and exit")
    result.add_argument("--config", type=Path, help="Existing TableProof v1 TOML contract")
    result.add_argument("--audit", help="Join name to select from --config")
    result.add_argument("--left", type=Path, help="Local UTF-8 CSV or TSV")
    result.add_argument("--right", type=Path, help="Local UTF-8 CSV or TSV")
    result.add_argument("--key", action="append", help="Key on both sides; repeat for a composite key")
    result.add_argument("--left-key", action="append", help="Left key; repeat in component order")
    result.add_argument("--right-key", action="append", help="Right key; repeat in component order")
    result.add_argument("--expect", choices=["one-to-one", "one-to-many", "many-to-one", "many-to-many"])
    result.add_argument("--how", choices=["inner", "left", "right", "full"], help="Defaults to contract join_type or left")
    result.add_argument("--left-measure", action="append", default=[], help="Numeric sum to trace; repeat")
    result.add_argument("--right-measure", action="append", default=[], help="Numeric sum to trace; repeat")
    result.add_argument("--null-keys", choices=["error", "warn", "ignore"])
    result.add_argument("--left-unmatched", choices=["error", "warn", "ignore"])
    result.add_argument("--right-unmatched", choices=["error", "warn", "ignore"])
    result.add_argument("--fail-on", choices=["error", "warning"], help="Override configured exit threshold")
    result.add_argument("--top", type=int, default=10, help="Number of ranked key groups (0..100)")
    result.add_argument("--rank-by", default="rows", help="rows (default), left:COLUMN, or right:COLUMN; metrics rank by absolute repeated value")
    result.add_argument("--sample-rows", type=int, default=3, help="Original records per side/key (0..20)")
    result.add_argument("--sample-bytes", type=int, default=4 * 1024 * 1024,
                        help="Total raw record-value JSON budget in bytes (0..16777216; default 4 MiB)")
    disclosure = result.add_mutually_exclusive_group()
    disclosure.add_argument("--redact", dest="redact", action="store_const", const=True, default=None,
                            help="Omit raw key and record values; keep locators and sums")
    disclosure.add_argument("--show-rows", dest="redact", action="store_const", const=False,
                            help="Include raw keys and original records, overriding config redaction")
    result.add_argument("--format", choices=["text", "json"], default="text")
    result.add_argument("--output", type=Path, help="Write the report atomically instead of stdout")
    result.add_argument("--assertions-output", type=Path, help="Write runnable DuckDB SQL assertions")
    return result


def render_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [f"{report['verdict'].upper()}: {report['join_type']} join, expected {report['expected_relationship']}",
             f"Predicted rows: {summary['predicted_join_rows']:,}; excess matched rows above contract caps: {summary['contract_excess_matched_rows']:,}",
             f"Added copies: left {summary['left_added_row_copies']:,}; right {summary['right_added_row_copies']:,}",
             f"Multiplying keys shown: {summary['reported_key_groups']}/{summary['contributing_key_groups']}"]
    if report["ranking"]["by"] != "rows":
        lines.append(f"Ranking: largest absolute value added by repetition of {report['ranking']['by']}.")
    for side in ("left", "right"):
        for column, values in report["measures"][side].items():
            lines.append(f"{side}.{column}: source {values['source_sum']}; retained once {values['retained_once_sum']}; predicted SUM {values['predicted_join_sum']}; added by repetition {values['added_by_repetition']}; dropped {values['dropped_sum']}")
    for group in report["top_keys"]:
        key = json.dumps(group["key"], ensure_ascii=False) if "key" in group else group["key_id"]
        status = "violates relationship" if group["violates_relationship"] else "allowed by relationship"
        lines.append(f"\nKey {key}: {group['left_count']} × {group['right_count']} = {group['joined_rows']} joined rows; contract excess {group['contract_excess_rows']}; {status}")
        for side in ("left", "right"):
            for column, measure in group[side + "_measures"].items():
                lines.append(f"  {side}[{json.dumps(column, ensure_ascii=False)}]: source {measure['source_sum']}; predicted SUM {measure['predicted_join_sum']}; added {measure['added_by_repetition']}")
            for record in group[side + "_records"]:
                values = " " + json.dumps(record["values"], ensure_ascii=False) if "values" in record else ""
                if "values_omitted" in record:
                    values = " (values omitted: sample byte budget)"
                lines.append(f"  {side} record {record['record_number']} (lines {record['line_start']}-{record['line_end']}, {record['join_copies']} copies){values}")
            if group[side + "_records_omitted"]:
                lines.append(f"  {side}: {group[side + '_records_omitted']} further records omitted")
    for finding in report["upstream_audit"]["findings"]:
        lines.append(f"{finding['severity'].upper()} {finding['code']}: {finding['message']}")
    lines.append(f"SQL assertions: {len(report['assertions'])}; use --assertions-output PATH to save.")
    sampling = report["sampling"]
    if sampling["records_without_values"]:
        lines.append(f"Raw sample values omitted for {sampling['records_without_values']} records (byte budget); source locators remain.")
    if not report["redacted"]:
        lines.append("Report includes raw source records. Use --redact to omit their values.")
    if "exit_status" in report:
        lines.append(f"Exit policy: fail on {report['fail_on']}; status {report['exit_status']}.")
    return "\n".join(lines) + "\n"


def _request(args: argparse.Namespace) -> tuple[JoinSpec, str, bool, list[Path]]:
    if args.config:
        if not args.config.is_file():
            raise TableProofError(f"Configuration must be a regular file: {args.config}")
        direct = (args.left, args.right, args.key, args.left_key, args.right_key, args.expect,
                  args.null_keys, args.left_unmatched, args.right_unmatched)
        if any(value is not None for value in direct):
            raise TableProofError("Use --config or direct table/key/policy options, not both")
        try:
            specs, fail_on = load_config(args.config)
        except (TypeError, ValueError) as exc:
            raise TableProofError(f"Invalid TableProof configuration: {exc}") from exc
        matches = [spec for spec in specs if args.audit is None or spec.name == args.audit]
        if len(matches) != 1:
            raise TableProofError("Select exactly one configured join with --audit NAME")
        spec = matches[0]
        redact = not spec.show_raw_keys if args.redact is None else args.redact
        protected = [args.config]
    else:
        if args.audit:
            raise TableProofError("--audit requires --config")
        if not args.left or not args.right or not args.expect:
            raise TableProofError("Specify --left, --right, and --expect (or use --config)")
        if args.key and (args.left_key or args.right_key):
            raise TableProofError("Use --key or --left-key/--right-key, not both")
        left_keys = tuple(args.key or args.left_key or ())
        right_keys = tuple(args.key or args.right_key or ())
        if not left_keys or not right_keys:
            raise TableProofError("Specify --key, or both --left-key and --right-key")
        spec = JoinSpec(name="join-witness", left=args.left, right=args.right,
                        left_keys=left_keys, right_keys=right_keys,
                        relationship=args.expect, null_keys=args.null_keys or "error",
                        left_unmatched=args.left_unmatched or "warn",
                        right_unmatched=args.right_unmatched or "warn")
        fail_on, redact, protected = "error", bool(args.redact), []
    return spec, args.fail_on or fail_on, redact, protected + [spec.left, spec.right] + ([spec.result] if spec.result is not None else [])


def _demo(args: argparse.Namespace, resources: ExitStack) -> None:
    if args.config or args.audit or any(value is not None for value in
                                       (args.left, args.right, args.key, args.left_key, args.right_key, args.expect)):
        raise TableProofError("Use --demo without direct sources, keys, --expect, or --config")
    data = files(__package__).joinpath("data")
    args.left = resources.enter_context(as_file(data.joinpath("orders.csv")))
    args.right = resources.enter_context(as_file(data.joinpath("customers.csv")))
    args.key, args.expect = ["customer_id"], "many-to-one"
    if not args.left_measure:
        args.left_measure = ["amount"]


def _resolved(path: Path) -> Path:
    try:
        str(path).encode("utf-8")
        return path.resolve()
    except UnicodeError as exc:
        raise TableProofError("File paths must be valid UTF-8") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise TableProofError(f"Cannot resolve path {path!s}: {exc}") from exc


def _check_outputs(inputs: list[Path], outputs: list[Path]) -> None:
    resolved = [_resolved(path) for path in outputs]
    source_paths = [_resolved(path) for path in inputs]
    if len(set(resolved)) != len(resolved):
        raise TableProofError("Report and assertion output must be different files")
    for output, output_path in zip(outputs, resolved):
        if not output.parent.is_dir() or output.is_dir():
            raise TableProofError(f"Output needs an existing parent directory and a file path: {output}")
        if output.exists() and not output.is_file():
            raise TableProofError(f"Output must be a regular file: {output}")
        for source, source_path in zip(inputs, source_paths):
            if output_path == source_path or (output.exists() and source.exists() and output.samefile(source)):
                raise TableProofError(f"Output would overwrite an input: {output}")


def _write_outputs(outputs: list[tuple[Path, str]]) -> None:
    """Stage every output before replacing any existing destination.

    Each replacement is atomic. Cross-file replacement cannot be transactional;
    staging still prevents partial publication on parse or preparation failures.
    """
    staged = []
    try:
        for path, content in outputs:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                             prefix=".join-witness-", delete=False) as stream:
                temporary = Path(stream.name)
                staged.append((temporary, path))
                stream.write(content)
        for temporary, path in staged:
            os.replace(temporary, path)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    resources = ExitStack()
    try:
        if args.print_schema:
            sys.stdout.write(files(__package__).joinpath("data").joinpath("report-v1.schema.json").read_text(encoding="utf-8"))
            sys.stdout.flush()
            return 0
        if args.demo:
            _demo(args, resources)
        spec, fail_on, redact, protected = _request(args)
        outputs = [path for path in (args.output, args.assertions_output) if path is not None]
        _check_outputs(protected, outputs)
        report = analyze(spec, how=args.how, left_measures=tuple(args.left_measure),
                         right_measures=tuple(args.right_measure), top=args.top,
                         sample_rows=args.sample_rows, redact=redact, sample_bytes=args.sample_bytes,
                         rank_by=args.rank_by)
        exit_status = int(report["verdict"] == "fail" or (fail_on == "warning" and report["upstream_audit"]["summary"]["warnings"] > 0))
        report["exit_status"], report["fail_on"] = exit_status, fail_on
        content = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n" if args.format == "json" else render_text(report)
        payloads = []
        if args.assertions_output:
            payloads.append((args.assertions_output, sql_script(report["assertions"])))
        if args.output:
            payloads.append((args.output, content))
        _write_outputs(payloads)
        if not args.output:
            sys.stdout.write(content)
            sys.stdout.flush()
        return exit_status
    except BrokenPipeError:
        close = getattr(sys.stdout, "close", None)
        if close is not None:
            with suppress(OSError, ValueError):
                close()
        return 0
    except (TableProofError, OSError) as exc:
        print(f"datawrench join-witness: {exc}", file=sys.stderr)
        return 2
    finally:
        resources.close()
