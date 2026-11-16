from __future__ import annotations

import argparse
import json
import sys

import duckdb

from . import __version__
from .core import infer
from .csvio import DELIMITERS, SeedtypesError, discover
from .emit import ADAPTERS, emit


def parser():
    p = argparse.ArgumentParser(prog='datawrench seedtypes', description='Infer only types that preserve every decoded CSV field.')
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='command', required=True)
    for command in ('infer', 'compare', 'emit', 'validate-types'):
        cmd = sub.add_parser(command)
        if command in ('compare', 'validate-types'):
            cmd.add_argument('file')
        else:
            cmd.add_argument('inputs', nargs='+', metavar='FILES|DIR')
        if command == 'validate-types':
            cmd.add_argument('--types', required=True, metavar='TYPES.json')
        cmd.add_argument('--null-token', action='append', default=[], metavar='TEXT', help='repeatable; default: no null tokens')
        cmd.add_argument('--delimiter', choices=DELIMITERS, default='comma',
                         help='explicit input separator; default: comma (never guessed from contents or extension)')
        cmd.add_argument('--show-examples', action='store_true',
                         help='include sensitive field evidence: at most one per rejected candidate and three per comparison column')
        if command != 'emit':
            cmd.add_argument('--format', choices=('text', 'json'), default='text')
        else:
            cmd.add_argument('--target', required=True, choices=('dbt', 'duckdb', 'polars', 'json'))
            cmd.add_argument('--adapter', choices=ADAPTERS, default='duckdb')
    check = sub.add_parser('check')
    check.add_argument('project_dir')
    check.add_argument('--format', choices=('text', 'json'), default='text')
    check.add_argument('--validate-column-types', action='store_true',
                       help='validate effective canonical column types with exact field round trips')
    check.add_argument('--null-token', action='append', default=[], metavar='TEXT',
                       help='repeatable; requires --validate-column-types; default: no null tokens')
    check.add_argument('--show-examples', action='store_true',
                       help='include bounded sensitive field evidence in nested JSON reports')
    return p


def inference_text(result):
    lines = [f'{result.path}: {result.rows} data rows']
    lines.extend(f'  NOTICE: {notice}' for notice in result.notices)
    for col in result.columns:
        lines.append(f'  {col.name}: {col.type} ({col.non_null} values, {col.nulls} nulls)')
        for candidate, detail in col.rejected.items():
            evidence = (f' {detail["value"]!r} -> {detail["rendered"]!r}' if 'value' in detail else '')
            lines.append(f'    reject {candidate}: line {detail["line"]}{evidence} ({detail["reason"]})')
    return '\n'.join(lines)


def compare_text(result):
    lines = [f'{result["inference"]["path"]}: {result["inference"]["rows"]} data rows']
    for col in result['inference']['columns']:
        lines.append(f'{col["name"]}: lossless={col["type"]}')
        for engine, report in result['engines'].items():
            detail = report.get('columns', {}).get(col['name'])
            if detail is None:
                lines.append(f'  {engine}-default: {report["status"]}: {report.get("error", "")}')
                continue
            lines.append(f'  {engine}-default={detail["type"]}: {detail["status"]} '
                         f'({detail["mismatches"]} changed values)')
            if detail['first_mismatch_line'] is not None:
                lines.append(f'    first mismatch: line {detail["first_mismatch_line"]}')
            for example in detail['examples']:
                lines.append(f'    line {example["line"]}: {example["value"]!r} -> {example["rendered"]!r}')
    return '\n'.join(lines)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        exit_code = 0
        if args.command == 'infer':
            results = [infer(path, args.null_token, show_examples=args.show_examples,
                             delimiter=DELIMITERS[args.delimiter]) for path in discover(args.inputs)]
            payload = [r.json() for r in results]
            text = '\n\n'.join(inference_text(r) for r in results)
        elif args.command == 'emit':
            results = [infer(path, args.null_token, show_examples=args.show_examples,
                             delimiter=DELIMITERS[args.delimiter]) for path in discover(args.inputs)]
            print(emit(results, args.target, args.adapter), end='')
            return 0
        elif args.command == 'compare':
            from .compare import compare
            payload = compare(args.file, args.null_token, show_examples=args.show_examples,
                              delimiter=DELIMITERS[args.delimiter])
            text = compare_text(payload)
            exit_code = int(any(e['status'] == 'ERROR' for e in payload['engines'].values()))
        elif args.command == 'validate-types':
            from .validate import validate_types, validation_text
            payload = validate_types(args.file, args.types, args.null_token,
                                     show_examples=args.show_examples,
                                     delimiter=DELIMITERS[args.delimiter])
            text = validation_text(payload)
            exit_code = int(not payload['valid'])
        else:
            from .check import check_project
            payload = check_project(args.project_dir, show_examples=args.show_examples,
                                    validate_column_types=args.validate_column_types,
                                    null_tokens=args.null_token)
            failures = payload.get('column_type_failures', [])
            exit_code = int(bool(payload['issues'] or failures))
            text = f'{payload["seeds_checked"]} seeds checked; {len(payload["issues"])} unpinned risks'
            for issue in payload['issues']:
                line = f' at line {issue["line"]}' if 'line' in issue else ''
                text += f'\n  {issue["path"]}: {issue["column"]} ({issue["engine"]}): {issue["reason"]}{line}'
            if args.validate_column_types:
                from .validate import validation_text
                text += f'\n{len(failures)} explicit column type failures'
                for seed in payload['seeds']:
                    validation = seed['column_type_validation']
                    text += '\n' + validation_text(validation)
                    for col in validation['columns']:
                        text += f'\n  {col["name"]} declaration: {col["source"]}: {col["config_path"]}'
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.format == 'json' else text)
        return exit_code
    except (SeedtypesError, duckdb.Error, OSError, UnicodeError) as exc:
        print(f'datawrench seedtypes: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
