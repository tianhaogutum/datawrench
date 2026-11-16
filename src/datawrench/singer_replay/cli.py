"""Small argparse CLIs; stdout is exclusively machine-readable JSON/JSONL."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import zlib
from pathlib import Path

from . import __version__
from .cassette import Cassette, record, scrub, scrub_rules, trim, trim_limits, verify
from .common import ReplayError, encode, read_json, require, timestamp
from .replay import InjectedFailure, replay

PROG = "datawrench singer-replay"
TAP_PROG = f"{PROG} tap"

SETTINGS = {
    "type": "object", "additionalProperties": False, "required": ["cassette_path"],
    "properties": {
        "cassette_path": {"type": "string", "description": "Local cassette directory; relative to current working directory"},
        "time_shift": {"default": False, "description": "false, true/now, or a fixed RFC3339 destination timestamp",
                       "anyOf": [{"type": "boolean"}, {"const": "now"}, {"type": "string", "format": "date-time"}]},
        "fail_after_records": {"type": ["integer", "null"], "minimum": 0, "default": None},
        "fail_exit_code": {"type": "integer", "minimum": 1, "maximum": 125, "default": 1},
        "state_message_frequency": {"type": "integer", "minimum": 0, "default": 1000},
        "delay_ms": {"type": "integer", "minimum": 0, "maximum": 60000, "default": 0},
    },
}


def config_from(value):
    require(isinstance(value, dict), "config must be an object")
    require(not (set(value) - set(SETTINGS["properties"])), "unknown config setting")
    path = value.get("cassette_path")
    require(isinstance(path, str) and path and "\x00" not in path, "cassette_path must be a nonempty local path")
    cfg = {k: prop.get("default") for k, prop in SETTINGS["properties"].items()}
    cfg.update(value)
    shift = cfg["time_shift"]
    require(type(shift) is bool or isinstance(shift, str), "time_shift must be boolean, now, or RFC3339 timestamp")
    if shift is True:
        cfg["time_shift"] = "now"
    if isinstance(shift, str) and shift != "now":
        timestamp(shift)
    for key, lower, upper in [("fail_after_records", 0, None), ("fail_exit_code", 1, 125),
                              ("state_message_frequency", 0, None), ("delay_ms", 0, 60000)]:
        n = cfg[key]
        if key == "fail_after_records" and n is None:
            continue
        require(type(n) is int and n >= lower and (upper is None or n <= upper),
                f"invalid {key}")
    return cfg


def _error(exc, prefix):
    # Never echo raw input records, config contents, or command arguments.
    if isinstance(exc, ReplayError):
        message = str(exc)
    elif isinstance(exc, OSError):
        message = exc.strerror or "filesystem/pipe error"
    else:
        message = "corrupt gzip, invalid input structure, or out-of-range value"
    print(f"{prefix}: {message}", file=sys.stderr)
    return 2


ERRORS = (ReplayError, OSError, EOFError, zlib.error, ValueError, TypeError,
          KeyError, OverflowError, RecursionError)


def record_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Record, trim, verify and scrub Singer cassettes; replay one with "
                    f"'{TAP_PROG}' (formerly the separate 'tap-replay' command).",
        epilog=f"Also: '{TAP_PROG} --config CONFIG [--discover | --about] [--catalog FILE] "
               f"[--state FILE]' acts as a Singer tap backed by a cassette (see '{TAP_PROG} --help').")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("record", help="record stdin or an explicitly provided tap command")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--scrub", action="append", default=[], metavar="STREAM:PATH=MODE",
                   help="hash|mask|drop; STREAM:profile.phone, STREAM:/JSON/Pointer "
                        "(~1 for /, ~0 for ~), or legacy STREAM.FIELD")
    p.add_argument("--salt-file", help="32+ byte private salt for stable hashes across cassettes")
    p.add_argument("--limit", type=int, help="stop after this many RECORDs (must be positive)")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- TAP COMMAND ARG ... (no shell)")
    p = sub.add_parser("trim", help="create a reproducible cassette with at most N records per stream")
    p.add_argument("source", help="existing local cassette directory (never modified)")
    p.add_argument("-o", "--output", help="new destination directory; required unless --dry-run")
    p.add_argument("--dry-run", action="store_true",
                   help="print a read-only JSON plan; cannot be combined with --output")
    p.add_argument("--per-stream", type=int, required=True, metavar="N",
                   help="default cap: retain the first N RECORDs of each stream; zero keeps schemas only")
    p.add_argument("--max-records", type=int, metavar="N",
                   help="cap total retained RECORDs after stream filters and per-stream caps; zero keeps schemas only")
    p.add_argument("--stream-limit", action="append", default=[], metavar="STREAM=N",
                   help="override the default cap for an exact stream ID; repeat for different streams")
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--include-stream", action="append", metavar="STREAM",
                   help="retain only these exact stream IDs; repeat to include multiple streams")
    selection.add_argument("--exclude-stream", action="append", metavar="STREAM",
                   help="omit these exact stream IDs; repeat to exclude multiple streams")
    p.add_argument("--compact-states", action="store_true",
                   help="collapse adjacent identical STATE messages after rebuilding checkpoints")
    p.add_argument("--schema-once", action="store_true",
                   help="keep only the first identical SCHEMA for each retained stream")
    boundaries = p.add_mutually_exclusive_group()
    boundaries.add_argument("--final-state-only", action="store_true",
                   help="omit source STATE boundaries and emit one safe final STATE")
    boundaries.add_argument("--state-every", type=int, metavar="N",
                   help="replace source STATE boundaries with checkpoints every N retained RECORDs (positive)")
    p.add_argument("--drop-empty-streams", action="store_true",
                   help="omit streams with zero retained records; reject if no schema would remain")
    p = sub.add_parser("verify", help="validate a whole cassette and print its actual contents summary")
    p.add_argument("source", help="existing local cassette directory (never modified)")
    p = sub.add_parser("scrub", help="mask or drop fields in an existing cassette without a tap")
    p.add_argument("source", help="existing local cassette directory (never modified)")
    p.add_argument("-o", "--output", required=True, help="new destination outside the source")
    p.add_argument("--scrub", action="append", required=True, metavar="STREAM:PATH=MODE",
                   help="mask|drop; legacy STREAM.FIELD, STREAM:object.field or STREAM:/JSON/Pointer")
    args = parser.parse_args(argv)
    proc = None
    try:
        if args.action == "scrub":
            manifest = scrub(args.source, args.output, rules=args.scrub)
            print(f"datawrench singer-replay: scrubbed {sum(manifest['counts'].values())} records; "
                  f"truncated={manifest['truncated']}", file=sys.stderr)
            return 0
        if args.action == "verify":
            print(encode(verify(args.source)))
            return 0
        if args.action == "trim":
            manifest = trim(args.source, args.output, per_stream=args.per_stream,
                            stream_limits=trim_limits(args.stream_limit),
                            include_streams=args.include_stream, exclude_streams=args.exclude_stream,
                            compact_states=args.compact_states, max_records=args.max_records,
                            drop_empty_streams=args.drop_empty_streams,
                            final_state_only=args.final_state_only, state_every=args.state_every,
                            schema_once=args.schema_once, dry_run=args.dry_run)
            if args.dry_run:
                print(encode(manifest))
                return 0
            print(f"datawrench singer-replay: trimmed to {sum(manifest['counts'].values())} records; "
                  f"truncated={manifest['truncated']}", file=sys.stderr)
            return 0
        rules = scrub_rules(args.scrub)
        require(args.limit is None or args.limit > 0, "limit must be positive")
        require(not Path(args.output).exists(), "output directory already exists; refusing to overwrite")
        salt = None
        if args.salt_file:
            with open(args.salt_file, "rb") as src:
                salt = src.read(4097)
            require(32 <= len(salt) <= 4096, "salt-file must contain 32..4096 bytes")
        command = args.command
        if command and command[0] == "--":
            command = command[1:]
        require(not args.command or bool(command), "missing tap command after --")
        if command:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            source = proc.stdout
        else:
            source = sys.stdin.buffer

        def finish(stop):
            if proc is None:
                return None
            proc.stdout.close()
            if stop:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                return proc.wait(timeout=2 if stop else 10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                return proc.returncode or 2

        manifest = record(source, args.output, rules=rules, salt=salt, limit=args.limit, finish=finish)
        print(f"datawrench singer-replay: recorded {sum(manifest['counts'].values())} records; "
              f"truncated={manifest['truncated']}", file=sys.stderr)
        return 0
    except ERRORS as exc:
        return _error(exc, PROG)
    except KeyboardInterrupt:
        return 130
    finally:
        if proc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            proc.stdout.close()


def tap_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=TAP_PROG,
        description="Replay a verified local cassette as a Singer tap (formerly the separate 'tap-replay' command).")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--discover", action="store_true")
    modes.add_argument("--about", action="store_true")
    parser.add_argument("--format", choices=["json"], default="json")
    parser.add_argument("--catalog")
    parser.add_argument("--state")
    args = parser.parse_args(argv)
    try:
        if args.about:
            print(encode({"name": "tap-replay", "version": __version__,
                          "capabilities": ["catalog", "discover", "state", "about"],
                          "settings": SETTINGS}))
            return 0
        require(args.config, "--config is required")
        cfg = config_from(read_json(args.config))
        catalog = read_json(args.catalog) if args.catalog else None
        require(catalog is None or isinstance(catalog, dict), "catalog must be an object")
        state = read_json(args.state) if args.state else None
        require(state is None or isinstance(state, dict), "state must be an object")
        with Cassette(cfg["cassette_path"]) as cassette:
            if args.discover:
                print(encode(cassette.catalog))
            else:
                def emit(msg):
                    print(encode(msg), flush=True)
                replay(cassette, cfg, emit, catalog=catalog, state=state)
        return 0
    except InjectedFailure as exc:
        print(f"{TAP_PROG}: injected failure after configured record count", file=sys.stderr)
        return exc.code
    except BrokenPipeError:
        # A consumer such as head may finish before us; avoid shutdown tracebacks.
        sys.stdout = open(os.devnull, "w")
        return 0
    except ERRORS as exc:
        return _error(exc, TAP_PROG)
    except KeyboardInterrupt:
        return 130


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``datawrench singer-replay``.

    ``datawrench singer-replay tap ...`` (formerly the ``tap-replay`` console script) is
    routed to :func:`tap_main` before parsing; everything else (``record``, ``trim``,
    ``verify``, ``scrub``) goes to :func:`record_main` (formerly ``singer-replay``).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "tap":
        return tap_main(argv[1:])
    return record_main(argv)
