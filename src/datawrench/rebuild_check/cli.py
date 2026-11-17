"""``datawrench rebuild-check``: rehearse SQL migrations on a backup and check what survived.

The same check is available as the ``sqlite-utils rebuild-check`` plugin command
(see ``plugin.py``); both share ``load_policy_file``, ``run`` and ``render_text``.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

from . import __version__
from .core import check_migration

POLICY_LIMIT = 1024 * 1024
DESCRIPTION = """Rehearse MIGRATIONS on a backup of DATABASE and check dependency retention.

The source is opened read-only. Exit 0: pass, 1: failed checks, 2: incomplete
run. SQL files execute in order, with their own transaction boundaries."""


def prepare_plugins(conn):
    # Defer discovery to the core's structured preparation-error boundary.
    from sqlite_utils.plugins import ensure_plugins_loaded, pm
    ensure_plugins_loaded()
    pm.hook.prepare_connection(conn=conn)


def load_policy_file(policy: Path | None):
    """The parsed policy JSON (None without --policy); raises OSError / ValueError."""
    if not policy:
        return None
    with Path(policy).open(encoding="utf-8-sig") as stream:
        text = stream.read(POLICY_LIMIT + 1)
    if len(text) > POLICY_LIMIT:
        raise ValueError("Policy exceeds the 1 MiB character limit")
    return json.loads(text)


def run(database, migrations, *, timeout=30.0, foreign_keys="on", count_rows=True, max_violations=100,
        policy=None, temp_dir=None, connection_plugins=False) -> dict:
    """Load the policy file and run the check; a bad policy becomes a ``policy_error`` report."""
    try:
        policy_data = load_policy_file(policy)
    except (OSError, ValueError) as exc:
        return {"schema_version": 1, "status": "error", "exit_code": 2, "findings": [
            {"code": "policy_error", "object": "policy", "severity": "error", "message": str(exc),
             "details": {}, "accepted": False}]}
    preparer = prepare_plugins if connection_plugins else None
    return check_migration(database, migrations, timeout=timeout, foreign_keys_enabled=foreign_keys == "on",
                           count_rows=count_rows, max_violations=max_violations, policy=policy_data,
                           temp_dir=temp_dir, prepare_connection=preparer)


def render_text(report: dict) -> list[str]:
    lines = [f"{report['status'].upper()}: {len(report['findings'])} finding(s)"]
    for item in report["findings"]:
        marker = "ACCEPTED" if item["accepted"] else item["severity"].upper()
        lines.append(f"{marker} {item['code']} {item['object']}: {item['message']}")
        if "error" in item["details"]:
            lines.append(f"  {item['details']['error']}")
        result = item["details"].get("result")
        if result:
            lines.append("  " + json.dumps(result, ensure_ascii=False))
    if "changes" in report:
        lines.append(f"{len(report['changes'])} definition change(s); use --format json for before/after details.")
    return lines


def render(report: dict, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return "".join(line + "\n" for line in render_text(report))


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid float") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not in the range x>0")
    return number


def _at_least_one(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value} is not in the range x>=1")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="datawrench rebuild-check", description=DESCRIPTION,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("database", type=Path, metavar="DATABASE", help="SQLite database file (opened read-only, never changed)")
    p.add_argument("migrations", nargs="+", type=Path, metavar="MIGRATIONS",
                   help="SQL files, executed in argument order on a disposable backup")
    p.add_argument("--format", dest="output_format", choices=["text", "json"], default="text",
                   help="output format (default: text)")
    p.add_argument("--timeout", type=_positive_float, default=30.0,
                   help="total deadline in seconds, including the backup (default: 30.0)")
    p.add_argument("--foreign-keys", choices=["on", "off"], default="on",
                   help="initial migration connection setting; SQL may change it (default: on)")
    p.add_argument("--count-rows", action=argparse.BooleanOptionalAction, default=True,
                   help="count rows of ordinary tables before and after; FK and integrity checks run either way")
    p.add_argument("--max-violations", type=_at_least_one, default=100,
                   help="foreign-key violation samples kept per check (default: 100)")
    p.add_argument("--policy", type=Path, help="JSON allowlist with exact findings and reasons")
    p.add_argument("--temp-dir", type=Path, help="parent directory for the disposable backup")
    p.add_argument("--connection-plugins", action="store_true",
                   help="run installed sqlite-utils prepare_connection hooks on the replica")
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    if args.temp_dir is not None and args.temp_dir.exists() and not args.temp_dir.is_dir():
        p.error(f"--temp-dir: directory {str(args.temp_dir)!r} is a file")
    report = run(args.database, args.migrations, timeout=args.timeout, foreign_keys=args.foreign_keys,
                 count_rows=args.count_rows, max_violations=args.max_violations, policy=args.policy,
                 temp_dir=args.temp_dir, connection_plugins=args.connection_plugins)
    sys.stdout.write(render(report, args.output_format))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
