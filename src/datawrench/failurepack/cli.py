"""``datawrench failurepack``: command line interface for the dfshrink/Pandera integration."""

import argparse
import importlib.util
import json
import math
import signal
import subprocess
import sys
from pathlib import Path


def _require(*modules):
    """Fail at import time, like a missing import, so the dispatcher prints the install hint.

    pandas and Pandera run only in the validator subprocess; check them here so a missing
    extra is reported before any work starts.
    """
    for module in modules:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            raise ModuleNotFoundError(f"No module named {module!r}", name=module)


_require("pandas", "pandera")

from . import __version__  # noqa: E402
from .bundle import list_failures, reduce_to_bundle, resume_bundle  # noqa: E402
from .table import InputError  # noqa: E402
from .validator import ValidationError, read_options  # noqa: E402

PROG = "datawrench failurepack"


def parser():
    root = argparse.ArgumentParser(
        prog=PROG,
        description="Reduce CSV records while preserving one selected Pandera failure.",
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("list", "reduce"):
        command = commands.add_parser(
            name, help="inspect failures" if name == "list" else "build a bundle"
        )
        command.add_argument("data", type=Path, help="local UTF-8 CSV with header")
        command.add_argument(
            "--schema", required=True, type=Path, help="Pandera .yaml or .py schema"
        )
        command.add_argument("--delimiter", default=",")
        command.add_argument(
            "--read-options", type=Path, help="JSON object of pandas parser options"
        )
        command.add_argument("--timeout", type=float, default=10, help="seconds per validator run")
        command.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
        command.add_argument("--max-rows", type=int, default=250_000)
        command.add_argument("--max-columns", type=int, default=2048)
        if name == "reduce":
            command.add_argument("--target", required=True, help="exact failure id from 'list'")
            command.add_argument("--out", required=True, type=Path)
            command.add_argument("--max-evals", type=int, default=1000)
            command.add_argument(
                "--max-seconds",
                type=float,
                default=300,
                help="soft reduction time budget; baseline and final evidence are additional",
            )
            command.add_argument("--stability-runs", type=int, default=2)
    replay = commands.add_parser("replay", help="verify and replay an existing bundle")
    replay.add_argument("bundle", type=Path)
    replay.add_argument("--allow-version-drift", action="store_true")
    resume = commands.add_parser("resume", help="continue reduction from a verified bundle")
    resume.add_argument("bundle", type=Path)
    resume.add_argument("--out", required=True, type=Path)
    resume.add_argument("--max-evals", type=int, default=1000)
    resume.add_argument("--max-seconds", type=float, default=300)
    resume.add_argument("--timeout", type=float)
    resume.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    resume.add_argument("--max-rows", type=int, default=250_000)
    resume.add_argument("--max-columns", type=int, default=2048)
    return root


def main(argv: list[str] | None = None) -> int:
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        return _main(argv)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "replay":
            if not (args.bundle / "manifest.json").is_file():
                raise InputError("bundle has no manifest.json")
            verifier = Path(__file__).with_name("replay_template.py.txt")
            command = [sys.executable, str(verifier), "--bundle", str(args.bundle.resolve())]
            if args.allow_version_drift:
                command.append("--allow-version-drift")
            return subprocess.run(command, timeout=3600).returncode
        if args.command == "resume":
            manifest = resume_bundle(
                args.bundle,
                args.out,
                max_evals=args.max_evals,
                max_seconds=args.max_seconds,
                timeout=args.timeout,
                max_bytes=args.max_bytes,
                max_rows=args.max_rows,
                max_columns=args.max_columns,
            )
            print_summary(args.out, manifest)
            return 0
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 300 or args.max_bytes < 1:
            raise InputError("timeout must be finite, in (0, 300]; max-bytes must be positive")
        options = read_options(args.read_options)
        if args.command == "list":
            failures = list_failures(
                args.data,
                args.schema,
                args.delimiter,
                options,
                args.timeout,
                args.max_bytes,
                max_rows=args.max_rows,
                max_columns=args.max_columns,
            )
            print(json.dumps({"failures": failures}, indent=2, sort_keys=True))
            return 0
        manifest = reduce_to_bundle(
            args.data,
            args.schema,
            args.out,
            args.target,
            delimiter=args.delimiter,
            options=options,
            timeout=args.timeout,
            max_evals=args.max_evals,
            stability_runs=args.stability_runs,
            max_bytes=args.max_bytes,
            max_rows=args.max_rows,
            max_columns=args.max_columns,
            max_seconds=args.max_seconds,
        )
        print_summary(args.out, manifest)
        return 0
    except (OSError, ValueError, ValidationError, subprocess.TimeoutExpired) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted; no incomplete bundle was published", file=sys.stderr)
        return 130


def print_summary(output, manifest):
    print(
        json.dumps(
            {
                "bundle": str(output),
                "source_rows": manifest["source"]["rows"],
                "result_rows": manifest["result"]["rows"],
                "one_minimal": manifest["reduction"]["one_minimal"],
                "stop_reason": manifest["reduction"]["stop_reason"],
                "validation_runs": manifest["reduction"]["validation_runs"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
