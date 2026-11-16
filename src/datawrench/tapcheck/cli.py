from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from . import __version__
from .convert import convert
from .engine import StopValidation, Validator
from .io import Diagnostic, InputError, MAX_LINE_BYTES, decode, dumps
from .output import atomic_output


def positive(value):
    number = int(value)
    if not 1 <= number <= 2**31 - 1: raise argparse.ArgumentTypeError("must be between 1 and 2147483647")
    return number


def example_limit(value):
    number = int(value)
    if not 0 <= number <= 100: raise argparse.ArgumentTypeError("must be between 0 and 100")
    return number


def output_budget(value):
    number = int(value)
    if not 0 <= number <= 2**63 - 1:
        raise argparse.ArgumentTypeError("must be between 0 and 9223372036854775807")
    return number


def parser():
    root = argparse.ArgumentParser(prog="datawrench tapcheck", description="Offline streaming Singer / Airbyte contract checks")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a JSONL file or stdin")
    quality = commands.add_parser("quality", help="JSON per-stream quality counts with bounded, value-free examples")
    conversion = commands.add_parser("convert", help="convert the shared subset; stdout may be partial on failure")
    for cmd in (validate, conversion, quality):
        cmd.add_argument("file", nargs="?", default="-")
        cmd.add_argument("--protocol", choices=["auto", "singer", "airbyte"], default="auto")
        cmd.add_argument("--catalog", type=Path)
        cmd.add_argument("--max-input-bytes", type=output_budget, metavar="N",
                         help="total raw main-input bytes, excluding external catalog/BATCH files (0-9223372036854775807; default unlimited)")
    conversion.add_argument("--to", choices=["singer", "airbyte"], required=True)
    conversion.add_argument("--output", type=Path, metavar="FILE",
                            help="atomically publish a complete file; omitted or '-' streams stdout (may be partial)")
    conversion.add_argument("--max-output-bytes", type=output_budget, metavar="N",
                            help="total serialized output bytes including newlines (0-9223372036854775807; default unlimited)")
    for cmd in (validate, quality):
        cmd.add_argument("--max-declarations", type=output_budget, metavar="N",
                         help="cumulative schema registration attempts, including repeats and invalid schemas (0-9223372036854775807; default unlimited)")
        cmd.add_argument("--max-messages", type=output_budget, metavar="N",
                         help="cumulative nonblank main-input processing events, including parse diagnostics (0-9223372036854775807; default unlimited)")
        cmd.add_argument("--max-records", type=output_budget, metavar="N",
                         help="cumulative declared-stream object record evaluations (0-9223372036854775807; default unlimited)")
        cmd.add_argument("--max-batch-files", type=output_budget, metavar="N",
                         help="cumulative local-BATCH manifest occurrences, including invalid paths (0-9223372036854775807; default unlimited)")
        cmd.add_argument("--max-batch-raw-bytes", type=output_budget, metavar="N",
                         help="cumulative raw local-BATCH bytes including gzip metadata (0-9223372036854775807; default unlimited)")
        cmd.add_argument("--max-batch-bytes", type=output_budget, metavar="N",
                         help="cumulative decompressed local-BATCH payload bytes (0-9223372036854775807; default unlimited)")
        cmd.add_argument("--strict", action="store_true")
        cmd.add_argument("--check-order", action="store_true")
        cmd.add_argument("--check-duplicates", action="store_true")
        cmd.add_argument("--duplicate-limit", type=positive, default=100_000)
        cmd.add_argument("--max-line-bytes", type=positive, default=MAX_LINE_BYTES)
        cmd.add_argument("--batch-root", type=Path, help="BATCH allowlisted directory (default: input directory, or cwd for stdin)")
    validate.add_argument("--max-errors", type=positive, default=100)
    validate.add_argument("--format", choices=["text", "json", "junit"], default="text")
    quality.add_argument("--example-limit", type=example_limit, default=5, metavar="N",
                         help="first N error locations per stream and N unassigned (0-100; default 5)")
    quality.add_argument("--key-example-limit", type=example_limit, default=0, metavar="N",
                         help="first N missing/null locations per declared key-path occurrence (0-100; default 0)")
    return root


def xml_safe(text):
    # XML 1.0 cannot represent all JSON string codepoints.
    return "".join(c if c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF else "\\u%04x" % ord(c) for c in str(text))


def render(report, format):
    if format == "json": return json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False)
    if format == "junit":
        suite = ET.Element("testsuite", name="tapcheck", tests=str(max(1, report["errors"])), failures=str(report["errors"] if report["exit_code"] != 2 else 0), errors=str(report["errors"] if report["exit_code"] == 2 else 0))
        if report["stopped_early"]:
            properties = ET.SubElement(suite, "properties")
            for name, value in (("stopped_early", "true"), ("input_lines", report["input_lines"]),
                                ("records", sum(s["records"] for s in report["streams"]))):
                ET.SubElement(properties, "property", name=name, value=str(value))
        for d in report["diagnostics"]:
            case = ET.SubElement(suite, "testcase", name=xml_safe(f"{d['code']} line {d['line']}"), classname=xml_safe(d["stream"] or "protocol"))
            issue = ET.SubElement(case, "error" if report["exit_code"] == 2 else "failure", type=d["code"], message=xml_safe(d["message"]))
            issue.text = xml_safe(f"{d['source']}:{d['line']} path={d['path']}")
        if not report["diagnostics"]: ET.SubElement(suite, "testcase", name="valid stream", classname="tapcheck")
        return ET.tostring(suite, encoding="unicode")
    output = []
    for d in report["diagnostics"]:
        # repr escapes terminal control sequences in user-controlled paths/names.
        stream = f" stream {d['stream']!r}" if d["stream"] is not None else ""
        path = f" path {d['path']!r}" if d["path"] else ""
        output.append(f"line {d['line']} {d['code']}{stream}: {d['message']}{path}")
    records = sum(s["records"] for s in report["streams"])
    output.append(f"{report['errors']} error(s), {records} record(s), {len(report['streams'])} stream(s); exit {report['exit_code']}" + (" (stopped early)" if report["stopped_early"] else ""))
    return "\n".join(output)


def read_catalog(path):
    if path is None: return None
    with path.open("rb") as handle:
        raw = handle.read(MAX_LINE_BYTES + 1)
    if len(raw) > MAX_LINE_BYTES: raise InputError("catalog exceeds 8 MiB", "TC020")
    try: value = decode(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError): raise InputError("invalid catalog JSON/UTF-8", "TC004") from None
    # None means "no --catalog" downstream, so a JSON null catalog must fail here.
    if value is None: raise InputError("catalog must contain a streams array", "TC003")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    engine = None
    is_quality = args.command == "quality"
    report_format = "json" if is_quality else getattr(args, "format", "text")
    file_output = args.command == "convert" and args.output not in (None, Path("-"))
    try:
        catalog = read_catalog(args.catalog)
        context = nullcontext(sys.stdin.buffer) if args.file == "-" else open(args.file, "rb")
        with context as handle:
            if args.command == "convert":
                output_context = nullcontext(sys.stdout.buffer)
                if file_output:
                    protected = [os.fstat(handle.fileno())]
                    if args.catalog is not None:
                        protected.append(args.catalog.stat())
                    output_context = atomic_output(args.output, protected=protected)
                with output_context as output:
                    output_bytes = 0
                    for msg in convert(handle, args.to, protocol=args.protocol, catalog=catalog,
                                       source_name=args.file, max_input_bytes=args.max_input_bytes):
                        # Write the exact bytes counted, independent of stdout's
                        # text encoding or newline translation settings.
                        encoded = (dumps(msg) + "\n").encode("utf-8")
                        line_bytes = len(encoded)
                        if line_bytes > MAX_LINE_BYTES:
                            raise InputError("converted output line exceeds 8 MiB", "TC020")
                        if args.max_output_bytes is not None and output_bytes + line_bytes > args.max_output_bytes:
                            raise InputError("converted output exceeds --max-output-bytes budget", "TC020")
                        output.write(encoded)
                        output_bytes += line_bytes
                    output.flush()
                return 0
            batch_root = args.batch_root or (Path.cwd() if args.file == "-" else Path(args.file).resolve().parent)
            engine = Validator(protocol=args.protocol, strict=args.strict, check_order=args.check_order,
                check_duplicates=args.check_duplicates, max_errors=getattr(args, "max_errors", 100),
                duplicate_limit=args.duplicate_limit, batch_root=batch_root, max_line_bytes=args.max_line_bytes,
                quality=is_quality, example_limit=getattr(args, "example_limit", 5),
                key_example_limit=getattr(args, "key_example_limit", 0), max_input_bytes=args.max_input_bytes,
                max_batch_bytes=args.max_batch_bytes, max_batch_raw_bytes=args.max_batch_raw_bytes,
                max_batch_files=args.max_batch_files, max_records=args.max_records, max_messages=args.max_messages,
                max_declarations=args.max_declarations)
            report = engine.run(handle, source=args.file, catalog=catalog,
                                catalog_source=str(args.catalog) if args.catalog is not None else "<catalog>")
        print(render(report, report_format))
        return report["exit_code"]
    except BrokenPipeError:
        if args.command == "convert":
            print("TC021: cannot write output" + ("" if file_output else "; stdout may contain partial output"), file=sys.stderr)
        return 2
    except (InputError, OSError) as exc:
        code = exc.code if isinstance(exc, InputError) else "TC021"
        message = str(exc) if isinstance(exc, InputError) else "cannot open/read input or catalog, or write/publish output"
        if is_quality:
            engine = engine or Validator(protocol=args.protocol, quality=True,
                example_limit=args.example_limit, key_example_limit=args.key_example_limit,
                strict=args.strict, check_order=args.check_order,
                check_duplicates=args.check_duplicates)
            engine.source = args.file
            try: engine.error(code, message, operational=True)
            except StopValidation: pass
            print(render(engine.report(), "json"))
        elif args.command == "validate":
            report = (engine or Validator(protocol=args.protocol)).report()
            report.update(exit_code=2, valid=False, errors=report["errors"] + 1, stopped_early=True)
            report["diagnostics"].append(Diagnostic(code, message, source=args.file).as_dict())
            print(render(report, report_format))
        else:
            suffix = "" if file_output else "; stdout may contain partial output"
            print(f"{code}: {message}{suffix}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
