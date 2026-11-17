"""A streaming audit CLI with atomic output files and useful CI exit statuses."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from datawrench import __version__

from .audit import AuditError, AuditOptions, audit, canonical_type
from .config import load_config, read_json_object


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="datawrench csv-rewind",
        description="Find changed CSV cell text before accepting a DuckDB import schema.",
        epilog="Exit: 0 = exact round trips; 1 = losses found; 2 = incomplete audit/input error.",
    )
    command.add_argument("input", type=Path, help="one local CSV/TSV, optionally compressed")
    command.add_argument("--version", action="version", version=__version__)
    command.add_argument(
        "--type",
        action="append",
        default=[],
        metavar="COLUMN=TYPE",
        help="override one inferred type; repeatable; quote DECIMAL(18,2)",
    )
    command.add_argument(
        "--types", type=Path, help="JSON object mapping column names to DuckDB types"
    )
    command.add_argument("--read-config", type=Path, help="audit using a saved frozen JSON recipe")
    command.add_argument("--header", choices=["auto", "yes", "no"])
    command.add_argument("--delimiter", help="override the detected delimiter (use a literal tab)")
    command.add_argument(
        "--quote", help="override the quote character; empty string disables quoting"
    )
    command.add_argument("--escape", help="override the escape character")
    command.add_argument(
        "--new-line",
        choices=["lf", "crlf", "cr"],
        help="override line-ending inference, useful with multiline fields",
    )
    command.add_argument("--skip", type=int, help="explicitly exclude N initial lines")
    command.add_argument("--date-format", help="DuckDB strptime format for DATE parsing")
    command.add_argument("--timestamp-format", help="DuckDB strptime format for TIMESTAMP parsing")
    command.add_argument("--encoding", help="DuckDB CSV encoding; default utf-8")
    nulls = command.add_mutually_exclusive_group()
    nulls.add_argument(
        "--null",
        action="append",
        dest="null_values",
        metavar="TOKEN",
        help="candidate null tokens; replaces the default empty-string token",
    )
    nulls.add_argument("--no-nulls", action="store_true", help="do not propose any null coercion")
    command.add_argument(
        "--sample-size",
        type=int,
        default=20_480,
        help="sniffing rows only; -1 sniffs all; the audit always scans all rows",
    )
    command.add_argument("--max-examples", type=int, default=20, help="bounded report examples")
    command.add_argument(
        "--max-cell-chars",
        type=int,
        default=512,
        help="truncate displayed evidence; --events always retains full text",
    )
    command.add_argument(
        "--memory-mb", type=int, default=128, help="DuckDB memory budget, minimum 32"
    )
    command.add_argument("--json", action="store_true", help="emit the report JSON on stdout")
    command.add_argument("--report", type=Path, help="write a JSON report atomically")
    command.add_argument("--events", type=Path, help="stream ALL changed cells as full-text JSONL")
    command.add_argument("--freeze", type=Path, help="write conservative DuckDB read SQL")
    command.add_argument("--write-config", type=Path, help="write a reusable frozen JSON recipe")
    return command


def _types(args: argparse.Namespace) -> dict[str, str]:
    result: dict[str, str] = {}
    if args.types:
        result = read_json_object(args.types)
    for pair in args.type:
        if "=" not in pair:
            raise AuditError(f"Expected COLUMN=TYPE, got {pair!r}")
        name, value = pair.rsplit("=", 1)
        if not name:
            raise AuditError("A candidate column name cannot be empty")
        result[name] = value
    return {key: canonical_type(value) for key, value in result.items()}


def _options(args: argparse.Namespace) -> AuditOptions:
    base = load_config(args.read_config) if args.read_config else AuditOptions()
    overrides = {}
    for name, value in {
        "header": {"auto": None, "yes": True, "no": False}.get(args.header),
        "delimiter": args.delimiter,
        "quote": args.quote,
        "escape": args.escape,
        "new_line": {"lf": r"\n", "crlf": r"\r\n", "cr": r"\r"}.get(args.new_line),
        "skip": args.skip,
        "date_format": args.date_format,
        "timestamp_format": args.timestamp_format,
        "encoding": args.encoding,
    }.items():
        if value is not None:
            overrides[name] = value
    if args.header == "auto":
        overrides["header"] = None
    if args.no_nulls:
        overrides["null_values"] = ()
    elif args.null_values is not None:
        overrides["null_values"] = tuple(args.null_values)
    return replace(
        base,
        **overrides,
        types={**base.types, **_types(args)},
        sample_size=args.sample_size,
        max_examples=args.max_examples,
        max_cell_chars=args.max_cell_chars,
        memory_limit_mb=args.memory_mb,
    )


def _summary(report: dict[str, Any]) -> str:
    lines = [
        f"{report['rows_audited']} rows, {report['cells_audited']} cells audited; "
        f"{report['lossy_cells']} changed cells.",
    ]
    for column in report["columns"]:
        lines.append(
            f"  {column['name']!r}: {column['candidate_type']} -> "
            f"{column['recommended_type']} ({column['lossy_cells']} changed)"
        )
    for example in report["examples"]:
        lines.append(
            f"  row {example['data_row']}, {example['column']!r}: "
            f"{example['raw']['text']!r} -> {example['rendered']['text']!r} "
            f"[{example['reason']}]"
        )
    if report["examples_omitted"]:
        lines.append(
            f"  {report['examples_omitted']} further examples omitted; use --events for all."
        )
    lines.extend("Note: " + warning for warning in report["warnings"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    temporary: list[tuple[Path, Path]] = []
    try:
        options = _options(args)
        output_paths = [
            path.expanduser().resolve()
            for path in (args.report, args.events, args.freeze, args.write_config)
            if path is not None
        ]
        protected = {args.input.expanduser().resolve()}
        if args.types:
            protected.add(args.types.expanduser().resolve())
        if args.read_config:
            protected.add(args.read_config.expanduser().resolve())
        if len(set(output_paths)) != len(output_paths):
            raise AuditError("Report, events, freeze and config must use distinct output paths")
        if protected.intersection(output_paths):
            raise AuditError("An output path would overwrite an input file")
        if any(path.is_dir() for path in output_paths):
            raise AuditError("An output path points to a directory")
        with ExitStack() as stack:
            streams = {}
            for name in ("report", "events", "freeze", "write_config"):
                target = getattr(args, name)
                if target is None:
                    continue
                target = target.expanduser().resolve()
                stream = stack.enter_context(
                    tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        newline="\n",
                        dir=target.parent,
                        prefix=".csv-rewind-",
                        suffix=".tmp",
                        delete=False,
                    )
                )
                temporary.append((Path(stream.name), target))
                streams[name] = stream
            report = audit(args.input, options, events=streams.get("events"))
            if "report" in streams:
                json.dump(report, streams["report"], ensure_ascii=False, indent=2)
                streams["report"].write("\n")
            if "freeze" in streams:
                streams["freeze"].write(report["frozen"]["sql"])
            if "write_config" in streams:
                json.dump(
                    report["frozen"]["config"],
                    streams["write_config"],
                    ensure_ascii=False,
                    indent=2,
                )
                streams["write_config"].write("\n")
        for temporary_path, target in temporary:
            os.replace(temporary_path, target)
        temporary.clear()
        if args.json:
            json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(_summary(report))
        return 0 if report["lossless"] else 1
    except KeyboardInterrupt:
        print("datawrench csv-rewind: interrupted; temporary outputs discarded", file=sys.stderr)
        return 130
    except (AuditError, OSError, json.JSONDecodeError) as error:
        print(f"datawrench csv-rewind: {error}", file=sys.stderr)
        return 2
    finally:
        for temporary_path, _ in temporary:
            temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
