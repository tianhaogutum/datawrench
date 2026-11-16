from __future__ import annotations

import argparse
import sys

import duckdb

from . import __version__
from .audit import run_audit
from .baseline import compare, load_baseline, render_diff, save_baseline, snapshot
from .report import emit, exit_code, render
from .schema import InputError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='datawrench dialect-audit', description='Audit SQL migration readiness without executing the statements under audit.')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command, help_text in [('run', 'Audit SQL files or directories (recursive *.sql)'),
                               ('baseline', 'Explicitly save a portable audit baseline'),
                               ('diff', 'Audit and compare against a baseline without changing it')]:
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument('paths', nargs='+', metavar='PATHS')
        sub.add_argument('--read', required=True, help='SQLGlot source dialect')
        sub.add_argument('--write', required=True, help='SQLGlot target dialect (duckdb enables binding)')
        sub.add_argument('--schema', help='Empty-table DDL or YAML table -> column -> type fixture')
        sub.add_argument('--schema-read', help='DDL/type dialect; defaults to --read')
        sub.add_argument('--auto-stub', action='store_true', help='Infer missing tables from qualified columns as DOUBLE')
        if command == 'run':
            sub.add_argument('--format', choices=['text', 'json', 'md', 'junit', 'sarif'], default='text')
            sub.add_argument('--fail-on', choices=['error', 'warn', 'never'], default='error')
            sub.add_argument('--emit-dir', help='Write generated SQL and report.json; refuse existing output files')
        else:
            sub.add_argument('--root', default='.', help='Project root for portable input/schema paths (default: cwd)')
            if command == 'baseline':
                sub.add_argument('--output', required=True, help='Baseline JSON file to create')
                sub.add_argument('--update', action='store_true', help='Explicitly replace an existing valid baseline')
            else:
                sub.add_argument('--baseline', required=True, help='Saved baseline JSON file to compare')
                sub.add_argument('--format', choices=['text', 'json'], default='text')
    args = parser.parse_args(argv)
    try:
        previous = load_baseline(args.baseline) if args.command == 'diff' else None
        report = run_audit(args.paths, args.read, args.write, args.schema, args.auto_stub, args.schema_read)
        if args.command in ('baseline', 'diff'):
            current = snapshot(report, args.root)
            if args.command == 'baseline':
                protected = list(report['file_summaries']) + ([args.schema] if args.schema else [])
                save_baseline(current, args.output, args.update, protected)
                print(f'Baseline saved to {args.output}; findings acknowledged, compatibility not verified.')
                return 0
            delta = compare(previous, current)
            sys.stdout.write(render_diff(delta, args.format))
            return int(delta['changed'])
        if args.emit_dir:
            emit(report, args.emit_dir)
        sys.stdout.write(render(report, args.format, args.fail_on))
        return exit_code(report, args.fail_on)
    except (InputError, OSError, duckdb.Error) as exc:
        print(f'datawrench dialect-audit: {exc}', file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
