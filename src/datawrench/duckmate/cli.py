"""Small offline CLI; all commands share the same target lock and SQL boundary."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import duckdb

from . import __version__
from .core import (Error, applied, atomic_write, check_history, connect, diff, history_report, locked,
                   migrate, migration_sources, migrations, plan, schema_dump, statements, table_name,
                   target_path, target_plan, target_plan_report)


def parser():
    p = argparse.ArgumentParser(prog='datawrench duckmate',
                                description='dbmate-format migrations for local DuckDB files')
    p.add_argument('--version', action='version', version=__version__)

    def common(dest, defaults=False):
        d = lambda v: v if defaults else argparse.SUPPRESS
        dest.add_argument('-u', '--url', default=d(None), help='duckdb:path or DATABASE_URL')
        dest.add_argument('--migrations-dir', type=Path, default=d(Path('db/migrations')))
        dest.add_argument('--schema-file', type=Path, default=d(Path('db/schema.sql')))
        dest.add_argument('--migrations-table', default=d('schema_migrations'))
        dest.add_argument('--no-dump-schema', action='store_true', default=d(False))
        dest.add_argument('--strict', action='store_true', default=d(False))
        dest.add_argument('--dry-run', action='store_true', default=d(False))
        dest.add_argument('--json', action='store_true', default=d(False),
                          help='machine-readable report (status or plan --to only)')
        dest.add_argument('--to', default=d(None), metavar='VERSION',
                          help='ordered plan to a numeric version, without executing SQL (plan only; 0 = empty)')
    common(p, True)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('new', 'up', 'migrate', 'rollback', 'status', 'dump', 'load', 'verify', 'plan'):
        c = sub.add_parser(name)
        common(c)
        if name == 'new':
            c.add_argument('name', help='ASCII letters, numbers, underscore or hyphen')
        if name in ('rollback', 'plan'):
            c.add_argument('--count', type=int, default=1 if name == 'rollback' else None,
                           help='number of applied migrations to roll back (plan defaults to up)')
    return p


def load(path: Path, schema_file: Path, table: str):
    if path.exists():
        raise Error('load requires a nonexistent target; it never overwrites a database', 2)
    text = schema_file.read_text(encoding='utf-8')
    # Build next to the target; failed load leaves no target or partial database.
    fd, name = tempfile.mkstemp(prefix='.duckmate-load-', suffix='.duckdb', dir=path.parent)
    os.close(fd)
    os.unlink(name)  # DuckDB needs a missing file, not an empty placeholder.
    temporary = Path(name)
    try:
        with connect(temporary) as con:
            parsed = statements(con, text, str(schema_file))
            con.execute('BEGIN')
            try:
                for n, stmt in enumerate(parsed, 1):
                    try:
                        con.execute(stmt)
                    except duckdb.Error as e:
                        raise Error(f'{schema_file}: load statement {n} failed:\n{stmt.query}\n{e}') from e
                schema_dump(con, table)  # Confirm all objects can be reconstructed.
                con.execute('COMMIT')
            except BaseException:
                con.execute('ROLLBACK')
                raise
        os.chmod(temporary, 0o600)
        os.link(temporary, path)  # Atomic publication without overwriting an existing file.
    finally:
        temporary.unlink(missing_ok=True)
        Path(name + '.wal').unlink(missing_ok=True)


def run(args, json_plan=None):
    table = table_name(args.migrations_table)
    if json_plan is not None:
        json_plan['migrations_table'] = table
    if args.to is not None:
        if args.command != 'plan':
            raise Error('--to is supported for plan only', 2)
        if args.count is not None or args.dry_run:
            raise Error('--to cannot be combined with --count or --dry-run', 2, reason='incompatible_options')
        if not re.fullmatch(r'[0-9]+', args.to):
            raise Error('--to requires a nonnegative numeric version (0 = empty baseline)', 2, reason='invalid_target')
    if args.json and args.command != 'status' and not (args.command == 'plan' and args.to is not None):
        raise Error('--json is supported for status or plan --to only', 2)
    if args.command == 'new':
        if args.dry_run:
            raise Error('new --dry-run is unsupported; no file was created', 2)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', args.name):
            raise Error('migration name must contain only ASCII letters, numbers, _ or -', 2)
        args.migrations_dir.mkdir(parents=True, exist_ok=True)
        version = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        if list(args.migrations_dir.glob(version + '_*.sql')):
            raise Error('a migration already exists for this UTC second; retry in the next second', 2)
        path = args.migrations_dir / f'{version}_{args.name}.sql'
        with path.open('x', encoding='utf-8') as out:
            out.write('-- migrate:up\n\n-- migrate:down\n')
        print(f'Created: {path}')
        return
    path = target_path(args.url if args.url is not None else os.environ.get('DATABASE_URL'))
    if json_plan is not None:
        json_plan['database'] = str(path)
    reserved = {path, path.with_name(path.name + '.wal'), path.with_name(path.name + '.duckmate.lock')}
    if args.schema_file.resolve().is_relative_to(args.migrations_dir.resolve()):
        raise Error('schema file must be outside the migrations directory', 2)
    if args.schema_file.resolve() in reserved:
        raise Error('schema file must be distinct from database, WAL and lock paths', 2)
    if args.command in ('up', 'migrate', 'rollback', 'status', 'verify', 'plan'):
        inventory_only = (args.command == 'status' and args.json) or args.to is not None
        files = ([file for file, _ in migration_sources(args.migrations_dir)]
                 if inventory_only else migrations(args.migrations_dir))
        if args.schema_file.resolve() in {m.path.resolve() for m in files}:
            raise Error('schema file must be distinct from migration files', 2)
    else:
        files = []
    count = getattr(args, 'count', None) or 0
    if count < 0 or (args.command == 'rollback' and count == 0):
        raise Error('--count must be positive for rollback, nonnegative for plan', 2)
    if args.dry_run and args.command not in ('up', 'migrate', 'rollback', 'plan'):
        raise Error('--dry-run is supported for up, migrate, rollback and plan only', 2)
    with locked(path):
        if args.to is not None:
            if json_plan is not None:
                json_plan.update(target_plan_report(path, files, table, args.to, strict=args.strict,
                                                    directory=args.migrations_dir))
                print(json.dumps(json_plan, indent=2, sort_keys=True))
                return json_plan['exit_code']
            print(target_plan(path, files, table, args.to, strict=args.strict,
                              directory=args.migrations_dir), end='')
            return
        if args.command == 'plan' or args.dry_run:
            print(plan(path, files, table, strict=args.strict, rollback=count,
                       directory=args.migrations_dir), end='')
            return
        if args.command == 'load':
            load(path, args.schema_file, table)
            print(f'Loaded: {path}')
            return
        readonly = args.command in ('status', 'dump', 'verify')
        if readonly and not path.exists():
            raise Error(f'database does not exist: {path}', 2)
        with connect(path, readonly=readonly) as con:
            if args.command in ('up', 'migrate', 'rollback'):
                done = migrate(con, files, table, strict=args.strict, rollback=count,
                               directory=args.migrations_dir)
                print(f'{"Rolled back" if count else "Applied"}: {done}')
                if not args.no_dump_schema:
                    try:
                        snapshot = schema_dump(con, table)
                        atomic_write(args.schema_file, snapshot)
                    except (Error, OSError, duckdb.Error) as e:
                        raise Error(f'Migrations committed, but schema snapshot failed: {e}. '
                                    'Fix the unsupported object/output path and run dump.') from e
                    print(f'Writing: {args.schema_file}')
            elif args.command == 'status':
                if args.json:
                    report = {'schema_version': 1, 'database': str(path), 'migrations_table': table,
                              'strict': args.strict,
                              'migrations_directory': str(args.migrations_dir.absolute()),
                              **history_report(con, files, table, args.migrations_dir, strict=args.strict)}
                    print(json.dumps(report, indent=2, sort_keys=True))
                    return 0 if report['ok'] else 1
                recorded = applied(con, table)
                disk = {m.version for m in files}
                for m in files:
                    print(f'[{"X" if m.version in recorded else " "}] {m.path.name}')
                for v in sorted(recorded - disk, key=int):
                    print(f'[!] {v} (file missing)')
                print(f'Applied: {len(recorded)}, Pending: {sum(m.version not in recorded for m in files)}')
            elif args.command == 'dump':
                atomic_write(args.schema_file, schema_dump(con, table))
                print(f'Writing: {args.schema_file}')
            elif args.command == 'verify':
                check_history(con, files, table, require_checksum=True, directory=args.migrations_dir)
                expected = args.schema_file.read_text(encoding='utf-8')
                actual = schema_dump(con, table)
                if expected != actual:
                    raise Error('schema drift:\n' + diff(expected, actual))
                print('Verified: migration checksums and schema match.')


def main(argv=None):
    args = parser().parse_args(argv)
    json_plan = None
    if args.command == 'plan' and args.to is not None and args.json:
        json_plan = {'schema_version': 1, 'command': 'plan', 'database': None, 'migrations_table': None,
                     'migrations_directory': str(args.migrations_dir.absolute()), 'strict': args.strict,
                     'requested_target': args.to, 'current_version': None, 'target_version': None,
                     'sql_execution': 'not_checked', 'direction': None, 'steps': [], 'problems': [],
                     'ok': False, 'exit_code': 2}

    def failure(error, exit_code, reason, *, version=None, path=None):
        if json_plan is None:
            print(f'datawrench duckmate: {error}', file=sys.stderr)
        else:
            json_plan.update(ok=False, exit_code=exit_code, direction=None, steps=[], problems=[
                {'code': reason, 'version': version, 'path': path, 'message': str(error)}])
            print(json.dumps(json_plan, indent=2, sort_keys=True))
        return exit_code

    try:
        result = run(args, json_plan)
    except Error as e:
        return failure(e, e.code, e.reason or ('invalid_input' if e.code == 2 else 'plan_error'),
                       version=e.version, path=e.path)
    except (OSError, UnicodeError, duckdb.Error) as e:
        return failure(e, 1, 'database_error' if isinstance(e, duckdb.Error) else 'io_error')
    return result if result is not None else 0


if __name__ == '__main__':
    sys.exit(main())
