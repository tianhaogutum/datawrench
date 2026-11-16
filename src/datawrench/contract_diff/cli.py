"""Read-only CLI. stdout is the report; invalid input exits 2."""
from __future__ import annotations

import argparse
import html
import json
import sys

from . import __version__
from .diff import Report, compare_manifests
from .manifest import InputError, load_manifest
from .policy import load_policy
from .types import DIALECTS


def _plain(value: object) -> str:
    # Escape control characters including ANSI escapes and Unicode bidi controls.
    return "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in str(value))


def _md(value: object) -> str:
    return html.escape(_plain(value)).replace("|", "&#124;").replace("`", "&#96;")


def _gh(value: object, prop: bool = False) -> str:
    value = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    value = _plain(value)
    return value.replace(":", "%3A").replace(",", "%2C") if prop else value


def _detail_description(detail: dict, arrow: str = "->") -> str:
    # Nullability retains legacy boolean before/after in JSON, but displays the leaf types.
    before = detail.get("before_type", detail["before"])
    after = detail.get("after_type", detail["after"])
    value = f"{detail['path']}: {before} {arrow} {after} ({detail['classification']})"
    if detail.get("rule"):
        value += f"; Rule: {detail['rule']} [{detail['certainty']}]; {detail['reason']}; Action: {detail['remediation']}"
    if detail.get("policy"):
        policy = detail["policy"]
        value += f"; Policy: {policy['source']}#{policy['pointer']} severity={policy['severity']} (default breaking={policy['default_breaking']})"
    return value


def _description(change) -> str:
    location = change.model + (f" column {change.column}" if change.column else "")
    details = "; ".join(_detail_description(x) for x in change.details)
    return f"{location}: {change.message}" + (f"; {details}" if details else "")


def render(report: Report, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report.to_dict(), indent=2, ensure_ascii=True)
    scopes = [label + " models (exact unique_ids): " + json.dumps(keys, ensure_ascii=False)
              for label, keys in (("Selected", report.selected_models), ("Excluded", report.excluded_models))
              if keys is not None]
    summary = f"{report.models_compared} models compared; {len(report.changes)} changes; {report.breaking_count} breaking; {len(report.equivalents)} alias-equivalent"
    if fmt == "markdown":
        lines = ["# dbt contract diff", "", _md(summary), "", "| Impact | Model | Column | Change |", "| --- | --- | --- | --- |"]
        if scopes:
            lines[4:4] = [*[_md(scope) for scope in scopes], ""]
        for c in report.changes:
            details = "; ".join(_detail_description(d, "→") for d in c.details)
            lines.append(f"| {'BREAKING' if c.breaking else 'non-breaking'} | {_md(c.model)} | {_md(c.column or '—')} | {_md(c.message + ('; ' + details if details else ''))} |")
        lines.extend(["", *[f"- Warning: {_md(w)}" for w in report.warnings], *[f"- {_md(n)}" for n in report.notices]])
        return "\n".join(lines).rstrip()
    if fmt == "github":
        lines = [f"::notice title=dbt contract diff::{_gh(summary)}"]
        lines.extend(f"::notice title=dbt contract diff::{_gh(scope)}" for scope in scopes)
        lines.extend(f"::{'error' if c.breaking else 'notice'} title={_gh(c.kind, True)}::{_gh(_description(c))}" for c in report.changes)
        lines.extend(f"::warning title=dbt contract diff::{_gh(w)}" for w in report.warnings)
        lines.extend(f"::notice title=dbt contract diff::{_gh(n)}" for n in report.notices)
        return "\n".join(lines)
    lines = [_plain(summary)]
    lines.extend(_plain(scope) for scope in scopes)
    lines.extend(f"{'BREAKING' if c.breaking else 'NON-BREAKING'} {_plain(_description(c))}" for c in report.changes)
    lines.extend("WARNING " + _plain(w) for w in report.warnings)
    lines.extend("NOTICE " + _plain(n) for n in report.notices)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datawrench contract-diff",
                                     description="Offline compatibility checks between two dbt manifest contracts.")
    parser.add_argument("--base", required=True, help="base manifest.json path")
    parser.add_argument("--head", required=True, help="head manifest.json path")
    parser.add_argument("--dialect", choices=("auto", *DIALECTS), default="auto")
    parser.add_argument("--format", choices=("text", "json", "markdown", "github"), default="text")
    parser.add_argument("--fail-on", choices=("breaking", "any", "none"), default="breaking")
    parser.add_argument("--fail-on-empty-scope", action="store_true",
                        help="exit 1 when zero eligible models are compared, independently of --fail-on")
    parser.add_argument("--include-unenforced", action="store_true")
    parser.add_argument("--model", action="append", dest="selected_models", metavar="UNIQUE_ID",
                        help="compare only this exact case-sensitive model unique_id (repeatable; does not include unenforced)")
    parser.add_argument("--exclude-model", action="append", dest="excluded_models", metavar="UNIQUE_ID",
                        help="exclude this exact case-sensitive model unique_id after eligibility filtering (repeatable)")
    parser.add_argument("--strict-widening", action="store_true")
    parser.add_argument("--policy", help="explicit project policy JSON path (exact known-rule impact overrides)")
    parser.add_argument("--fail-on-unused-policy", action="store_true",
                        help="exit 1 for unused policy entries in this comparison, independently of --fail-on")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy) if args.policy is not None else None
        report = compare_manifests(load_manifest(args.base), load_manifest(args.head), dialect=args.dialect,
                                   include_unenforced=args.include_unenforced, strict_widening=args.strict_widening, policy=policy,
                                   selected_models=args.selected_models, excluded_models=args.excluded_models)
        print(render(report, args.format))
        if args.fail_on_empty_scope and report.models_compared == 0:
            return 1
        if args.fail_on_unused_policy and any(d["code"] == "unused_policy_entry" for d in report.policy_diagnostics):
            return 1
        return report.exit_code(args.fail_on)
    except InputError as e:
        print("datawrench contract-diff: input error: " + _plain(e), file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 2


if __name__ == "__main__":
    sys.exit(main())
