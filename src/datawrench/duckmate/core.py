"""Migration engine. SQL is parsed by DuckDB, never by splitting on semicolons."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import difflib
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import unquote, urlsplit

import duckdb


class Error(Exception):
    def __init__(self, message: str, code: int = 1, *, reason: str | None = None,
                 version: str | None = None, path: Path | None = None):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.version = version
        self.path = str(path.absolute()) if path is not None else None


def ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def table_name(s: str) -> str:
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', s) or s.lower().startswith('_duckmate'):
        raise Error('migrations table must be a simple identifier, outside the _duckmate namespace', 2)
    return s.lower()


def target_path(url: str | None) -> Path:
    if not url or not url.startswith('duckdb:'):
        raise Error('use a local duckdb:path.duckdb or duckdb:///absolute/path URL', 2)
    raw = url[len('duckdb:'):]
    if raw.startswith('//') and not raw.startswith('///'):
        raise Error('URL authorities and network shares are unsupported', 2)
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment or parsed.netloc:
        raise Error('URL authority, query and fragment are unsupported', 2)
    raw = unquote(raw)
    if raw.startswith('///'):
        raw = raw[2:]
    if (not raw or raw == ':memory:' or raw.startswith('//') or '\\' in raw
            or re.search(r'(^|/)[^/]*:', raw) or '\x00' in raw):
        raise Error('only local filesystem database paths are supported', 2)
    path = Path(raw).absolute()
    if path.is_symlink():
        raise Error('database symlinks are unsupported; use the canonical path', 2)
    # Resolve parent aliases so two spellings share one advisory lock.
    path = path.parent.resolve() / path.name
    if path.exists() and not path.is_file():
        raise Error('target must be a regular database file', 2)
    return path


def connect(path: Path | str = ':memory:', *, readonly: bool = False):
    return duckdb.connect(str(path), read_only=readonly, config={
        'enable_external_access': 'false', 'autoload_known_extensions': 'false',
        'autoinstall_known_extensions': 'false', 'allow_unsigned_extensions': 'false',
        'allow_community_extensions': 'false', 'allow_persistent_secrets': 'false',
        'allow_unredacted_secrets': 'false',
        'lock_configuration': 'true', 'threads': '1', 'memory_limit': '256MB',
        'max_temp_directory_size': '0B',
    })


@contextmanager
def locked(path: Path):
    """Persistent lock inode: never unlink a lock that another process may hold."""
    lock = path.with_name(path.name + '.duckmate.lock')
    if not path.parent.is_dir():
        raise Error(f'database parent directory does not exist: {path.parent}', 2)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error(f'another datawrench duckmate run holds the lock: {lock}', reason='lock_busy') from None
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class MigrationFile:
    path: Path
    version: str
    checksum: str


@dataclass(frozen=True)
class Migration(MigrationFile):
    up: str
    down: str
    up_transaction: bool
    down_transaction: bool


def migration_sources(directory: Path) -> list[tuple[MigrationFile, bytes]]:
    """Inventory file bytes without decoding markers or parsing migration SQL."""
    if not directory.is_dir():
        raise Error(f'migrations directory does not exist: {directory}', 2,
                    reason='migration_directory_not_found', path=directory)
    result = []
    seen = set()
    # pathlib.glob suppresses listing errors, which could look like an empty
    # inventory. Finish enumeration before validating/reading any file.
    with os.scandir(directory) as entries:
        paths = sorted(directory / entry.name for entry in entries if entry.name.endswith('.sql'))
    for path in paths:
        match = re.fullmatch(r'([0-9]+)_[A-Za-z0-9_-]+\.sql', path.name)
        if not match:
            raise Error(f'{path}: expected NUMERIC_VERSION_name.sql', 2,
                        reason='invalid_migration_filename', path=path)
        version = match[1]
        if int(version) in seen:
            raise Error(f'{path}: duplicate numeric migration version {version}', 2,
                        reason='duplicate_migration_version', version=version, path=path)
        seen.add(int(version))
        try:
            raw = path.read_bytes()
        except OSError as e:
            raise Error(str(e), reason='io_error', version=version, path=path) from e
        result.append((MigrationFile(path, version, hashlib.sha256(raw).hexdigest()), raw))
    return sorted(result, key=lambda item: int(item[0].version))


def migrations(directory: Path) -> list[Migration]:
    result = []
    for file, raw in migration_sources(directory):
        path = file.path
        try:
            source = raw.decode('utf-8')
        except UnicodeDecodeError as e:
            raise Error(f'{path}: migration must be UTF-8', 2) from e
        blocks = list(re.finditer(r'^\s*--\s*migrate:(up|down)([^\r\n]*)$', source, re.M))
        if len(blocks) != 2 or [m[1] for m in blocks] != ['up', 'down']:
            raise Error(f'{path}: require exactly one -- migrate:up followed by -- migrate:down', 2)
        if re.sub(r'--[^\n]*', '', source[:blocks[0].start()]).strip():
            raise Error(f'{path}: SQL before migrate:up is unsupported', 2)
        transactions = []
        for block in blocks:
            option = block[2].strip()
            if option not in ('', 'transaction:true', 'transaction:false'):
                raise Error(f'{path}: unsupported migration option {option!r}', 2)
            transactions.append(option != 'transaction:false')
        result.append(Migration(path, file.version, file.checksum,
                                source[blocks[0].end():blocks[1].start()],
                                source[blocks[1].end():], *transactions))
    return sorted(result, key=lambda m: int(m.version))


# In addition to the engine's locked external-access settings, reject workflow,
# transaction and configuration statements. SELECT may use local tables only.
ALLOWED = {'CREATE', 'ALTER', 'DROP', 'INSERT', 'UPDATE', 'DELETE', 'SELECT', 'COMMENT_ON'}


def sql_words(sql: str):
    """Lexical words only, for policy checks (not statement splitting).

    Respect strings, dollar quotes, quoted identifiers and nested comments so a
    literal such as 'temporary' cannot accidentally become a policy keyword.
    """
    i = 0
    while i < len(sql):
        if sql.startswith('--', i):
            end = sql.find('\n', i)
            i = len(sql) if end < 0 else end + 1
        elif sql.startswith('/*', i):
            depth = 1
            i += 2
            while depth and i < len(sql):
                if sql.startswith('/*', i):
                    depth += 1
                    i += 2
                elif sql.startswith('*/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif sql[i] in "'\"":
            quote = sql[i]
            i += 1
            word = ''
            while i < len(sql):
                if sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        word += quote
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    word += sql[i]
                    i += 1
            if quote == '\"':
                yield word.lower(), True
        elif sql[i] == '$' and (m := re.match(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$', sql[i:])):
            delimiter = m[0]
            end = sql.find(delimiter, i + len(delimiter))
            i = len(sql) if end < 0 else end + len(delimiter)
        elif m := re.match(r'[A-Za-z_][A-Za-z0-9_$]*', sql[i:]):
            yield m[0].lower(), False
            i += len(m[0])
        else:
            i += 1


def statements(con, sql: str, label: str, *, protected: str | None = None):
    try:
        parsed = con.extract_statements(sql)
    except duckdb.Error as e:
        raise Error(f'{label}: SQL parse failed: {e}') from e
    for n, stmt in enumerate(parsed, 1):
        kind = stmt.type.name
        if kind not in ALLOWED:
            raise Error(f'{label}: statement {n}: {kind} is unsupported (local SQL only)', 2)
        # CREATE SECRET includes credentials and has out-of-database effects.
        # Temporary objects cannot survive reopening or be faithfully dumped.
        words = list(sql_words(stmt.query))
        if any(w in {'secret', 'temp', 'temporary'} and not quoted for w, quoted in words):
            raise Error(f'{label}: statement {n}: secrets and temporary objects are unsupported', 2)
        if protected and any(w == protected.lower() or w.startswith('_duckmate') for w, _ in words):
            raise Error(f'{label}: statement {n}: migration metadata identifiers are reserved', 2)
    return parsed


def rows(con, sql: str) -> list[dict]:
    cursor = con.execute(sql)
    keys = [d[0] for d in cursor.description]
    return [dict(zip(keys, r)) for r in cursor.fetchall()]


def metadata(con, table: str):
    con.execute(f'CREATE TABLE IF NOT EXISTS {ident(table)} (version VARCHAR PRIMARY KEY)')
    con.execute('CREATE TABLE IF NOT EXISTS _duckmate_checksums '
                '(migrations_table VARCHAR, version VARCHAR, checksum VARCHAR NOT NULL, '
                'PRIMARY KEY (migrations_table, version))')


def exists(con, name: str) -> bool:
    return bool(con.execute('SELECT count(*) FROM duckdb_tables() '
                           "WHERE schema_name='main' AND lower(table_name)=lower(?) AND NOT temporary", [name]).fetchone()[0])


def applied(con, table: str) -> set[str]:
    if not exists(con, table):
        return set()
    # Keep every recorded row until numeric uniqueness has been validated, even
    # when externally supplied legacy metadata lacks a primary key.
    versions = [str(r[0]) for r in con.execute(f'SELECT version FROM {ident(table)}').fetchall()]
    if any(not re.fullmatch('[0-9]+', v) for v in versions):
        raise Error(f'{table}: migration versions must be numeric', 2)
    try:
        numeric_versions = {int(v) for v in versions}
    except ValueError as e:
        # Syntax is already checked; respect Python's configured decimal limit
        # without changing it or letting oversized legacy history escape the CLI.
        raise Error(f'{table}: recorded migration versions exceed the supported numeric conversion limit', 2) from e
    if len(numeric_versions) != len(versions):
        raise Error(f'{table}: duplicate numeric migration versions', 2)
    return set(versions)


def checksums(con, table: str) -> dict[str, str]:
    if not exists(con, '_duckmate_checksums'):
        return {}
    recorded = con.execute('SELECT version, checksum FROM _duckmate_checksums WHERE migrations_table=?', [table]).fetchall()
    # A malformed external table may omit the PK's NOT NULL constraint. Reject
    # selected NULL keys before duplicate detection, dict conversion or sorting.
    if any(version is None for version, _ in recorded):
        raise Error(f'{table}: checksum versions must not be NULL in _duckmate_checksums', 2)
    # Preserve stored text spelling; casting/decoding malformed external keys
    # could adopt evidence. Check before hashing potentially unhashable values.
    if any(not isinstance(version, str) for version, _ in recorded):
        raise Error(f'{table}: checksum versions must be text in _duckmate_checksums', 2)
    # Validate original row multiplicity before a dict can discard conflicting
    # or identical evidence in externally supplied metadata without its PK.
    if len({version for version, _ in recorded}) != len(recorded):
        raise Error(f'{table}: duplicate checksum rows in _duckmate_checksums', 2)
    # Validate original selected values after keys, before drift comparison or
    # report serialization. Decoding/casting could adopt malformed evidence.
    if any(not isinstance(checksum, str) for _, checksum in recorded):
        raise Error(f'{table}: checksum values must be text in _duckmate_checksums', 2)
    return dict(recorded)


def history_report(con, files: list[MigrationFile], table: str, directory: Path, *, strict: bool = False) -> dict:
    """Read-only checksum evidence shared by CI status and migration preflight."""
    recorded = applied(con, table)
    highest = max(recorded, key=int) if recorded else None
    sums = checksums(con, table)
    by_version = {m.version: m for m in files}
    entries = []
    problems = []

    def problem(code, version, path, message):
        problems.append({'code': code, 'version': version, 'path': path, 'message': message})

    for version in sorted(set(sums) - recorded):
        file = by_version.get(version)
        path = str(file.path.absolute()) if file else None
        problem('orphan_checksum', version, path, f'orphan checksum without an applied version: {version}')
    for version in sorted(recorded | by_version.keys(), key=int):
        file = by_version.get(version)
        path = str(file.path.absolute()) if file else None
        state = 'pending'
        if version in recorded:
            if file is None:
                state = 'missing'
                expected = str(directory.absolute() / f'{version}_*.sql')
                problem('missing_file', version, None,
                        f'applied migration missing from disk: {version} (expected {expected})')
            elif version not in sums:
                state = 'unverified'
                problem('missing_checksum', version, path,
                        f'no recorded checksum for version {version} (legacy dbmate history): {path}')
            elif sums[version] != file.checksum:
                state = 'changed'
                problem('checksum_drift', version, path, f'checksum drift: version {version}: {path}')
            else:
                state = 'applied'
        out_of_order = state == 'pending' and highest is not None and int(version) <= int(highest)
        if strict and out_of_order:
            problem('out_of_order', version, path,
                    f'strict mode refuses out-of-order migration: version {version} <= '
                    f'highest applied version {highest}: {path}')
        entry = {'version': version, 'path': path, 'state': state, 'applied': version in recorded,
                 'out_of_order': out_of_order,
                 'checksum': file.checksum if file else None, 'recorded_checksum': sums.get(version)}
        if state == 'missing':
            entry['expected_path'] = expected
        entries.append(entry)
    summary = {state: sum(e['state'] == state for e in entries)
               for state in ('pending', 'changed', 'missing', 'unverified')}
    summary['applied'] = len(recorded)
    summary['out_of_order'] = sum(e['out_of_order'] for e in entries)
    return {'ok': not problems, 'summary': summary, 'migrations': entries, 'problems': problems}


def check_history(con, files: list[MigrationFile], table: str, *, require_checksum: bool = False,
                  directory: Path | None = None):
    directory = directory if directory is not None else (files[0].path.parent if files else Path('db/migrations'))
    report = history_report(con, files, table, directory)
    problems = [p['message'] for p in report['problems']
                if require_checksum or p['code'] != 'missing_checksum']
    if problems:
        raise Error('\n'.join(problems))


def run_migration(con, m: Migration, table: str, *, down: bool = False):
    sql = m.down if down else m.up
    transactional = m.down_transaction if down else m.up_transaction
    parsed = statements(con, sql, str(m.path), protected=table)
    if transactional:
        con.execute('BEGIN TRANSACTION')
    try:
        metadata(con, table)
        for n, stmt in enumerate(parsed, 1):
            try:
                con.execute(stmt)
            except duckdb.Error as e:
                raise Error(f'{m.path}: statement {n} failed:\n{stmt.query}\n{e}'
                            + ('\ntransaction:false: earlier statements may be committed; repair before retry.'
                               if not transactional else '\nMigration rolled back.')) from e
        if down:
            con.execute(f'DELETE FROM {ident(table)} WHERE version=?', [m.version])
            con.execute('DELETE FROM _duckmate_checksums WHERE migrations_table=? AND version=?', [table, m.version])
        else:
            con.execute(f'INSERT INTO {ident(table)} VALUES (?)', [m.version])
            con.execute('INSERT INTO _duckmate_checksums VALUES (?, ?, ?)', [table, m.version, m.checksum])
        if transactional:
            try:
                con.execute('COMMIT')
            except duckdb.Error as error:
                raise Error(f'{m.path}: COMMIT failed:\n{error}') from error
    except BaseException:
        if transactional:
            try:
                con.execute('ROLLBACK')
            except duckdb.Error:
                # COMMIT can already abort the transaction; preserve its error.
                pass
        raise


def migrate(con, files: list[Migration], table: str, *, strict: bool = False,
            rollback: int = 0, emit=print, directory: Path | None = None):
    check_history(con, files, table, directory=directory)
    recorded = applied(con, table)
    selected = [m for m in files if (m.version in recorded if rollback else m.version not in recorded)]
    if rollback:
        selected = list(reversed(selected))[:rollback]
    elif strict and recorded:
        old = [m.version for m in selected if int(m.version) <= max(map(int, recorded))]
        if old:
            raise Error('strict mode refuses out-of-order migrations: ' + ', '.join(old), 2)
    # Validate all sections before modifying the target, including nontransactional migrations.
    for m in selected:
        statements(con, m.down if rollback else m.up, str(m.path), protected=table)
    for m in selected:
        emit(f'{"Rolling back" if rollback else "Applying"}: {m.path.name}'
             + (' (transaction:false)' if not (m.down_transaction if rollback else m.up_transaction) else ''))
        run_migration(con, m, table, down=bool(rollback))
    return len(selected)


def schema_dump(con, table: str) -> str:
    """Catalog -> deterministic, dependency-ordered, schema-only SQL.

    Replay on a sandboxed empty database both orders dependencies and rejects
    objects we cannot reconstruct. User table rows never enter the snapshot.
    """
    current = con.execute('SELECT current_database()').fetchone()[0]
    if con.execute('SELECT count(*) FROM duckdb_databases() WHERE NOT internal').fetchone()[0] != 1:
        raise Error('attached databases are unsupported', 2)
    ddl: list[tuple[tuple, str]] = []
    comments: list[str] = []

    def add_comment(kind, name, comment):
        if comment is not None:
            comments.append(f'COMMENT ON {kind} {name} IS {literal(comment)};')

    def catalog(function):
        return rows(con, f'SELECT * FROM {function}() WHERE NOT internal')

    for r in catalog('duckdb_schemas'):
        if r['database_name'] != current:
            continue
        name = r['schema_name']
        if name != 'main':
            ddl.append(((0, name), f'CREATE SCHEMA {ident(name)};'))
        add_comment('SCHEMA', ident(name), r['comment'])
        if r['tags']:
            raise Error('catalog tags cannot be dumped in v0.1', 2)
    for r in catalog('duckdb_types'):
        name = f'{ident(r["schema_name"])}.{ident(r["type_name"])}'
        definition = ('ENUM (' + ', '.join(map(literal, r['labels'])) + ')' if r['labels'] is not None
                      else con.execute(f'SELECT typeof(NULL::{name})').fetchone()[0])
        ddl.append(((1, name), f'CREATE TYPE {name} AS {definition};'))
        add_comment('TYPE', name, r['comment'])
        if r['tags']:
            raise Error('catalog tags cannot be dumped in v0.1', 2)
    for r in rows(con, 'SELECT * FROM duckdb_sequences()'):
        if r['temporary']:
            raise Error('temporary sequences are unsupported', 2)
        name = f'{ident(r["schema_name"])}.{ident(r["sequence_name"])}'
        # Sequence cursor is data. Reset to the bound rather than capture live state.
        start = r['min_value'] if r['increment_by'] > 0 else r['max_value']
        sql = (f'CREATE SEQUENCE {name} INCREMENT BY {r["increment_by"]} '
               f'MINVALUE {r["min_value"]} MAXVALUE {r["max_value"]} START {start} '
               f'{"CYCLE" if r["cycle"] else "NO CYCLE"};')
        ddl.append(((2, name), sql))
        add_comment('SEQUENCE', name, r['comment'])
        if r['tags']:
            raise Error('catalog tags cannot be dumped in v0.1', 2)
    functions = [r for r in catalog('duckdb_functions') if r['function_type'] in ('macro', 'table_macro')]
    signatures = set()
    for r in functions:
        name = f'{ident(r["schema_name"])}.{ident(r["function_name"])}'
        if name in signatures:
            raise Error(f'overloaded macros are unsupported in schema dumps: {name}', 2)
        signatures.add(name)
        if any(t is not None for t in r['parameter_types']):
            raise Error(f'typed macros are unsupported in schema dumps: {name}', 2)
        # DuckDB 1.4.2 does not expose macro defaults in its catalog. Detect
        # them via binding (DESCRIBE does not execute the macro) and fail closed.
        if r['parameters']:
            try:
                query = f'SELECT * FROM {name}()' if r['function_type'] == 'table_macro' else f'SELECT {name}()'
                con.execute('DESCRIBE ' + query)
            except duckdb.Error as e:
                if ':=' in str(e):
                    raise Error(f'macro default arguments cannot be dumped: {name}', 2) from e
                if 'does not support the supplied arguments' not in str(e):
                    raise Error(f'cannot validate macro signature: {name}: {e}', 2) from e
            else:
                raise Error(f'macro default arguments cannot be dumped: {name}', 2)
        params = ', '.join(map(ident, r['parameters']))
        body = r['macro_definition']
        ddl.append(((3, name), f'CREATE MACRO {name}({params}) AS '
                    + ('TABLE ' if r['function_type'] == 'table_macro' else '') + body + ';'))
        add_comment('MACRO', name, r['comment'])
        if r['tags']:
            raise Error('catalog tags cannot be dumped in v0.1', 2)
    for priority, function, field, kind in [(4, 'duckdb_tables', 'table_name', 'TABLE'),
                                          (5, 'duckdb_views', 'view_name', 'VIEW'),
                                          (6, 'duckdb_indexes', 'index_name', 'INDEX')]:
        for r in catalog(function) if function != 'duckdb_indexes' else rows(con, 'SELECT * FROM duckdb_indexes()'):
            if r.get('temporary'):
                raise Error('temporary tables/views are unsupported', 2)
            name = f'{ident(r["schema_name"])}.{ident(r[field])}'
            if not r['sql']:
                raise Error(f'catalog has no reproducible SQL for {name}', 2)
            ddl.append(((priority, name), r['sql']))
            add_comment(kind, name, r['comment'])
            if r['tags']:
                raise Error('catalog tags cannot be dumped in v0.1', 2)
    for r in catalog('duckdb_columns'):
        name = '.'.join(ident(r[k]) for k in ('schema_name', 'table_name', 'column_name'))
        add_comment('COLUMN', name, r['comment'])

    pending = [s for _, s in sorted(ddl)]
    ordered = []
    with connect() as scratch:
        while pending:
            failed = []
            for sql in pending:
                try:
                    scratch.execute(sql)
                except duckdb.Error as e:
                    failed.append((sql, str(e)))
                else:
                    ordered.append(sql)
            if len(failed) == len(pending):
                sql, why = failed[0]
                raise Error(f'cannot produce a self-contained schema dump:\n{sql}\n{why}', 2)
            pending = [s for s, _ in failed]
        for sql in sorted(comments):
            scratch.execute(sql)
    ordered.extend(sorted(comments))
    if exists(con, table):
        for version in sorted(applied(con, table), key=int):
            ordered.append(f'INSERT INTO {ident(table)} (version) VALUES ({literal(version)});')
    if exists(con, '_duckmate_checksums'):
        exported = con.execute('SELECT * FROM _duckmate_checksums ORDER BY migrations_table, version').fetchall()
        # Dumps include foreign history tables too. Validate original keys before
        # values, without decoding/casting or filtering the rows we will render.
        if any(version is None for _, version, _ in exported):
            raise Error('_duckmate_checksums: checksum versions must not be NULL in schema dumps', 2)
        if any(not isinstance(version, str) for _, version, _ in exported):
            raise Error('_duckmate_checksums: checksum versions must be text in schema dumps', 2)
        if any(not isinstance(checksum, str) for _, _, checksum in exported):
            raise Error('_duckmate_checksums: checksum values must be text in schema dumps', 2)
        for a, b, c in exported:
            ordered.append('INSERT INTO "_duckmate_checksums" VALUES (' + ', '.join(map(literal, (a, b, c))) + ');')
    return '-- duckmate schema-only snapshot v1 (sequence cursors and user rows omitted)\n' + '\n'.join(ordered) + '\n'


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.duckmate-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def diff(before: str, after: str) -> str:
    return ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile='current/schema.sql', tofile='planned/schema.sql'))


@contextmanager
def planning_connection(path: Path, *, readonly: bool = False):
    # Never open the source through DuckDB: even read-only opening can recover
    # a WAL. A quiescent, checkpointed source is required for byte-safe copying.
    if path.with_name(path.name + '.wal').exists():
        raise Error('plan refuses a database with a WAL; close other writers/checkpoint it first',
                    reason='wal_present')
    with tempfile.TemporaryDirectory(prefix='duckmate-plan-') as tmp:
        copy = Path(tmp) / 'plan.duckdb'
        if path.exists():
            shutil.copyfile(path, copy)
        with connect(copy, readonly=readonly and copy.exists()) as con:
            yield con


def target_plan_report(path: Path, files: list[MigrationFile], table: str, target: str, *, strict=False,
                       directory: Path | None = None) -> dict:
    """Schedule versions from inventory/history only; never parse or execute migration SQL."""
    report = {'ok': False, 'exit_code': 2, 'current_version': None, 'target_version': None,
              'direction': None, 'steps': [], 'problems': []}

    def refuse(code, message, *, version=None, path=None):
        report['problems'] = [{'code': code, 'version': version, 'path': path, 'message': message}]
        return report

    if not re.fullmatch(r'[0-9]+', target):
        return refuse('invalid_target', '--to requires a nonnegative numeric version (0 = empty baseline)')
    normalized = target.lstrip('0') or '0'
    ordered = sorted(files, key=lambda m: int(m.version))
    directory = directory if directory is not None else (files[0].path.parent if files else Path('db/migrations'))
    with planning_connection(path, readonly=True) as con:
        history = history_report(con, ordered, table, directory)
        recorded = applied(con, table)
    current = max(recorded, key=int) if recorded else '0'
    report['current_version'] = current
    # Retain migration preflight's legacy compatibility without adopting checksums.
    problems = [p for p in history['problems'] if p['code'] != 'missing_checksum']
    if problems:
        report.update(exit_code=1, problems=problems)
        return report
    zero = next((m for m in ordered if int(m.version) == 0), None)
    if zero is not None:
        return refuse('reserved_version_zero',
                      'target planning: version 0 is reserved for the empty baseline; use positive migration versions',
                      version=zero.version, path=str(zero.path.absolute()))
    highest = ordered[-1].version if ordered else '0'
    maximum = str(int(highest))
    # Compare decimal strings before conversion, including targets beyond Python's
    # integer-string limit and arbitrarily many leading zeroes.
    if (len(normalized), normalized) > (len(maximum), maximum):
        return refuse('target_out_of_range', f'target version {target} is outside available range 0..{highest}')
    number = int(normalized)
    goal = next((m.version for m in ordered if int(m.version) == number), None)
    if number and goal is None:
        return refuse('target_not_found',
                      f'target version {target} has no migration file; choose 0 or an existing version')
    goal = goal if number else '0'
    report['target_version'] = goal
    down = number < int(current)
    if down and number and goal not in recorded:
        return refuse('target_not_applied', f'target version {goal} is not applied; rollback cannot reach it',
                      version=goal, path=str(next(m.path.absolute() for m in ordered if m.version == goal)))
    if down:
        selected = [m for m in reversed(ordered) if m.version in recorded and int(m.version) > number]
    else:
        selected = [m for m in ordered if m.version not in recorded and int(m.version) <= number]
        old = [m for m in selected if int(m.version) <= int(current)]
        if strict and old:
            report['problems'] = [
                {'code': 'out_of_order', 'version': m.version, 'path': str(m.path.absolute()),
                 'message': f'strict mode refuses out-of-order migrations: {m.version} <= highest applied version {current}'}
                for m in old]
            return report
    direction = ('down' if down else 'up') if selected else 'none'
    recorded_checksums = {entry['version']: entry['recorded_checksum']
                          for entry in history['migrations'] if entry['applied']}
    report.update(ok=True, exit_code=0, direction=direction, steps=[
        {'position': n, 'direction': direction, 'version': m.version, 'path': str(m.path.absolute()),
         'checksum': m.checksum, 'recorded_checksum': recorded_checksums.get(m.version)}
        for n, m in enumerate(selected, 1)])
    return report


def target_plan(path: Path, files: list[MigrationFile], table: str, target: str, *, strict=False,
                directory: Path | None = None) -> str:
    report = target_plan_report(path, files, table, target, strict=strict, directory=directory)
    if not report['ok']:
        raise Error('\n'.join(p['message'] for p in report['problems']), report['exit_code'])
    lines = ['Migration plan (no SQL executed)', f'Current version: {report["current_version"]}',
             f'Target version: {report["target_version"]}', f'Direction: {report["direction"]}']
    lines.extend(f'{s["position"]}. {s["direction"]} {s["version"]} {s["path"]}' for s in report['steps'])
    lines.extend([f'Steps: {len(report["steps"])}',
                  'SQL execution: not checked; a generated plan does not guarantee executable SQL.'])
    return '\n'.join(lines) + '\n'


def plan(path: Path, files: list[Migration], table: str, *, strict=False, rollback=0,
         directory: Path | None = None) -> str:
    with planning_connection(path) as con:
        before = schema_dump(con, table)
        migrate(con, files, table, strict=strict, rollback=rollback, directory=directory)
        after = schema_dump(con, table)
    return diff(before, after) or 'No schema changes.\n'
