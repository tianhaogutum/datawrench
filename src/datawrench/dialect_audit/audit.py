from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import contextmanager
import logging
import re
from threading import Timer
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import Dialect, ErrorLevel, exp
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

from .schema import Binder, InputError, read_text, table_key, table_name, identifier_key

SEVERITIES = {
    'PARSE_ERROR': 'error', 'COMMAND_FALLBACK': 'error', 'UNSUPPORTED': 'error',
    'ANONYMOUS_FUNCTION': 'warn', 'ROUNDTRIP_UNSTABLE': 'warn',
    'BIND_ERROR': 'error', 'UNKNOWN_TABLE': 'error',
}


def dialect_names() -> list[str]:
    return sorted(name for name in Dialect.classes if name)


def validate_dialect(name: str) -> None:
    if name not in dialect_names():
        raise InputError(f'unknown dialect {name!r}; available: {", ".join(dialect_names())}')


@contextmanager
def capture_warnings():
    messages = []
    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())
    logger = logging.getLogger('sqlglot')
    handler = Capture()
    # Avoid interleaving upstream warnings with machine-readable stdout/stderr.
    previous_handlers, previous_propagate = logger.handlers[:], logger.propagate
    logger.handlers, logger.propagate = [handler], False
    try:
        yield messages
    finally:
        logger.handlers, logger.propagate = previous_handlers, previous_propagate


def split_statements(text: str, dialect: str) -> list[tuple[str, int, int, int, int]]:
    """Return SQL plus inclusive start/end lines and code-point columns."""
    tokens = Dialect.get_or_raise(dialect).tokenize(text)
    chunks, group = [], []
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            if group:
                chunks.append(group)
                group = []
        else:
            group.append(token)
    if group:
        chunks.append(group)
    newlines = [i for i, char in enumerate(text) if char == '\n']
    def position(offset):
        line = bisect_right(newlines, offset)
        return line + 1, offset - (newlines[line - 1] if line else -1)

    statements = []
    for group in chunks:
        start, end = group[0].start, group[-1].end
        line_start, column_start = position(start)
        line_end, column_end = position(end)
        statements.append((text[start:end + 1], line_start, line_end, column_start, column_end))
    return statements


def diagnostic(category: str, message: str, location: dict | None = None,
               related_locations: list | None = None) -> dict:
    message = re.sub(r'\x1b\[[0-9;]*m', '', message)
    result = {'category': category, 'severity': SEVERITIES[category], 'message': message, 'location': location}
    if related_locations is not None:
        result['related_locations'] = related_locations
    return result


def parser_locations(exc: Exception, source: str, line_start: int,
                     column_start: int | None) -> tuple[dict | None, list | None]:
    """Keep aggregate entries as evidence without promoting any to primary."""
    if not isinstance(exc, sqlglot.errors.ParseError) or not isinstance(exc.errors, list):
        return None, None
    entries = exc.errors
    if len(entries) == 1:
        return parser_entry_location(entries[0], source, line_start, column_start), None
    if not entries:
        return None, None
    related = []
    for error in entries:
        description = error.get('description') if isinstance(error, dict) else None
        related.append({
            'description': re.sub(r'\x1b\[[0-9;]*m', '', description) if isinstance(description, str) else None,
            'location': parser_entry_location(error, source, line_start, column_start),
        })
    return None, related


def parser_entry_location(error: object, source: str, line_start: int,
                          column_start: int | None) -> dict | None:
    """Map one verified SQLGlot highlight endpoint in sliced SQL to the file.

    SQLGlot line/col describe the last character of the highlighted token,
    including multiline tokens. Never derive coordinates from diagnostic text.
    """
    if not isinstance(error, dict) or column_start is None:
        return None
    line, col, highlight = (error.get(k) for k in ('line', 'col', 'highlight'))
    if (type(line) is not int or type(col) is not int or line <= 0 or col <= 0
            or not isinstance(highlight, str) or not highlight):
        return None
    lines = source.split('\n')
    if line > len(lines) or col > len(lines[line - 1]):
        return None
    end = sum(len(part) + 1 for part in lines[:line - 1]) + col
    start = end - len(highlight)
    if start < 0 or source[start:end] != highlight or source[end - 1] in '\r\n':
        return None
    # Context is optional upstream metadata, but it must agree when supplied.
    for key, context in [('start_context', source[:start]), ('end_context', source[end:])]:
        value = error.get(key)
        if value is not None and (not isinstance(value, str) or not (
            context.endswith(value) if key == 'start_context' else context.startswith(value)
        )):
            return None

    def position(offset):
        local_line = source.count('\n', 0, offset)
        local_col = offset - source.rfind('\n', 0, offset)
        return line_start + local_line, local_col + (column_start - 1 if local_line == 0 else 0)

    first_line, first_col = position(start)
    last_line, last_col = position(end - 1)
    return {'line_start': first_line, 'line_end': last_line,
            'column_start': first_col, 'column_end': last_col}


def physical_sources(tree: exp.Expression):
    """Scope-aware physical tables: excludes CTEs and derived table aliases."""
    for scope in traverse_scope(tree):
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                yield scope, alias, source


def bind_statement(tree: exp.Expression, sql: str, binder: Binder, auto_stub: bool) -> tuple[list, list, str]:
    findings, stubs = [], []
    if not isinstance(tree, exp.Query) or any(
        isinstance(node, (exp.DML, exp.DDL, exp.Into, exp.Command, exp.Lock))
        for node in tree.walk()
    ):
        return [diagnostic('UNSUPPORTED', 'DuckDB binding supports read-only queries only; statement was not executed')], [], 'not-attempted'
    missing: dict[tuple[str, str], set[str]] = {}
    try:
        for scope, alias, table in physical_sources(tree):
            if not isinstance(table.this, exp.Identifier):
                # Table functions bind under DuckDB's disabled external-access policy.
                continue
            key = table_key(table)
            if key not in binder.tables:
                columns = missing.setdefault(key, set())
                if auto_stub:
                    columns.update(identifier_key(column.name) for column in scope.columns
                                   if identifier_key(column.table) == identifier_key(alias) and column.name != '*')
    except (InputError, sqlglot.errors.SqlglotError) as exc:
        findings.append(diagnostic('UNSUPPORTED', str(exc)))
        return findings, stubs, 'not-attempted'
    binder.conn.execute('BEGIN TRANSACTION')
    try:
        for key, columns in sorted(missing.items()):
            if columns:
                binder.create(key, {name: 'DOUBLE' for name in sorted(columns)})
                stubs.append({'table': '.'.join(key), 'columns': sorted(columns), 'type': 'DOUBLE'})
            else:
                findings.append(diagnostic('UNKNOWN_TABLE', f'{table_name(key)} is absent from the schema fixture; '
                                           'auto-stub requires qualified column references'))
        timer = Timer(3.0, binder.conn.interrupt)
        timer.daemon = True
        timer.start()
        try:
            binder.conn.execute('EXPLAIN ' + sql).fetchall()
            binding_status = 'bound'
        except duckdb.Error as exc:
            findings.append(diagnostic('BIND_ERROR', str(exc)))
            binding_status = 'failed'
        finally:
            timer.cancel()
            timer.join()
    finally:
        binder.conn.execute('ROLLBACK')
    return findings, stubs, binding_status


def audit_statement(source: str, file: str, index: int, start: int, end: int,
                    read: str, write: str, binder: Binder | None = None,
                    auto_stub: bool = False, *, column_start: int | None = None,
                    column_end: int | None = None) -> dict:
    result = {'file': file, 'index': index, 'line_start': start, 'line_end': end,
              'column_start': column_start, 'column_end': column_end,
              'source_sql': source, 'target_sql': None, 'diagnostics': [],
              'functions': {}, 'auto_stubs': [],
              'binding_status': 'not-attempted' if write == 'duckdb' else 'static-only'}
    findings = result['diagnostics']
    with capture_warnings() as messages:
        try:
            parsed = sqlglot.parse(source, read=read, error_level=ErrorLevel.RAISE)
            if len(parsed) != 1 or parsed[0] is None:
                raise InputError('expected exactly one nonempty statement')
            tree = parsed[0]
        except (sqlglot.errors.SqlglotError, InputError, RecursionError) as exc:
            location, related = parser_locations(exc, source, start, column_start)
            findings.append(diagnostic('PARSE_ERROR', str(exc), location, related))
            result['categories'] = ['PARSE_ERROR']
            return result
        inventory = Counter()
        for function in tree.find_all(exp.Func):
            name = function.name.lower() if isinstance(function, exp.Anonymous) else function.sql_name().lower()
            inventory[name] += 1
            if isinstance(function, exp.Anonymous):
                item = diagnostic('ANONYMOUS_FUNCTION', f'{name}: unknown to SQLGlot; passed through without a verified translation')
                if item not in findings:
                    findings.append(item)
        result['functions'] = dict(sorted(inventory.items()))
        if any(isinstance(node, exp.Command) for node in tree.walk()):
            findings.append(diagnostic('COMMAND_FALLBACK', 'SQLGlot parsed an opaque Command; target SQL is not verified'))
        else:
            try:
                generator = Dialect.get_or_raise(write).generator(unsupported_level=ErrorLevel.IGNORE, comments=False)
                target = generator.generate(tree)
                result['target_sql'] = target
                findings.extend(diagnostic('UNSUPPORTED', msg) for msg in generator.unsupported_messages)
                try:
                    reparsed = sqlglot.parse_one(target, read=write)
                    regenerated = reparsed.sql(dialect=write, comments=False, unsupported_level=ErrorLevel.RAISE)
                    if isinstance(reparsed, exp.Command) or regenerated != target:
                        findings.append(diagnostic('ROUNDTRIP_UNSTABLE', f'target regeneration changed SQL: {regenerated}'))
                except (sqlglot.errors.SqlglotError, RecursionError) as exc:
                    findings.append(diagnostic('ROUNDTRIP_UNSTABLE', f'target SQL cannot round-trip: {exc}'))
                if binder is not None:
                    # Resolve scopes in the target AST (generator rewrites can add tables).
                    bind_tree = sqlglot.parse_one(target, read=write)
                    bound, stubs, binding_status = bind_statement(bind_tree, target, binder, auto_stub)
                    findings.extend(bound)
                    result['auto_stubs'] = stubs
                    result['binding_status'] = binding_status
            except (sqlglot.errors.SqlglotError, InputError, duckdb.Error, RecursionError, ValueError) as exc:
                findings.append(diagnostic('UNSUPPORTED', str(exc)))
        for message in messages:
            if 'Falling back' not in message:
                item = diagnostic('UNSUPPORTED', message)
                if item not in findings:
                    findings.append(item)
    result['categories'] = list(dict.fromkeys(item['category'] for item in findings)) or ['OK']
    return result


def collect_paths(paths: list[str]) -> list[Path]:
    files = {}
    for value in paths:
        path = Path(value)
        if not path.exists():
            raise InputError(f'input does not exist: {path}')
        candidates = sorted(p for p in path.rglob('*') if p.suffix.lower() == '.sql' and p.is_file()) if path.is_dir() else [path]
        if not path.is_dir() and not path.is_file():
            raise InputError(f'expected a regular SQL file: {path}')
        if not candidates:
            raise InputError(f'no .sql files in {path}')
        for candidate in candidates:
            if candidate.is_symlink():
                raise InputError(f'symlink SQL inputs are unsupported: {candidate}')
            files.setdefault(candidate.resolve(), candidate)
    return list(files.values())


def summarize_files(files: list[Path], statements: list[dict], notices: list[dict]) -> dict:
    by_file, file_notices = defaultdict(list), defaultdict(list)
    for stmt in statements:
        by_file[stmt['file']].append(stmt)
    for notice in notices:
        file_notices[notice['file']].append(notice)
    summaries = {}
    for path in files:
        file = str(path)
        items, skipped = by_file[file], file_notices[file]
        diagnostics = [d for stmt in items for d in stmt['diagnostics']] + skipped
        errors = sum(d['severity'] == 'error' for d in diagnostics)
        warnings = sum(d['severity'] == 'warn' for d in diagnostics)
        categories = Counter(c for stmt in items for c in stmt['categories'])
        ok = categories['OK']
        categories.update(n['category'] for n in skipped)
        bindings = Counter(stmt['binding_status'] for stmt in items)
        summaries[file] = {
            'status': 'error' if errors else 'warn' if warnings else 'ok',
            'statements': len(items), 'ok': ok, 'flagged': len(items) - ok,
            'skipped': bool(skipped), 'errors': errors, 'warnings': warnings,
            'categories': dict(sorted(categories.items())),
            'binding_statuses': dict(sorted(bindings.items())),
        }
    return summaries


def run_audit(paths: list[str], read: str, write: str, schema: str | None = None,
              auto_stub: bool = False, schema_read: str | None = None) -> dict:
    validate_dialect(read)
    validate_dialect(write)
    validate_dialect(schema_read or read)
    if auto_stub and write != 'duckdb':
        raise InputError('--auto-stub requires --write duckdb')
    if schema and write != 'duckdb':
        raise InputError('--schema binding requires --write duckdb')
    files = collect_paths(paths)
    report = {'version': 1, 'read': read, 'write': write, 'schema': schema,
              'schema_read': schema_read or read, 'auto_stub': auto_stub,
              'binding': 'duckdb-explain' if write == 'duckdb' else 'not-available',
              'statements': [], 'notices': [], 'file_function_inventory': {}, 'function_inventory': {}}
    binder = Binder() if write == 'duckdb' else None
    total_functions = Counter()
    try:
        if schema and binder:
            binder.load(Path(schema), schema_read or read)
        for path in files:
            file = str(path)
            file_functions = Counter()
            report['file_function_inventory'][file] = {}
            try:
                text = read_text(path)
            except (OSError, UnicodeError, InputError) as exc:
                report['notices'].append({'file': file, 'category': 'INPUT_SKIPPED', 'severity': 'error', 'message': str(exc)})
                continue
            if any(marker in text for marker in ('{{', '{%', '{#')):
                report['notices'].append({'file': file, 'category': 'JINJA_SKIPPED', 'severity': 'warn',
                                          'message': 'Jinja markers found; supply compiled SQL instead'})
                continue
            try:
                statements = split_statements(text, read)
            except (sqlglot.errors.SqlglotError, RecursionError) as exc:
                report['statements'].append({
                    'file': file, 'index': 1, 'line_start': 1, 'line_end': text.count('\n') + 1,
                    'column_start': None, 'column_end': None,
                    'source_sql': text, 'target_sql': None, 'functions': {}, 'auto_stubs': [],
                    'binding_status': 'not-attempted' if write == 'duckdb' else 'static-only',
                    'categories': ['PARSE_ERROR'], 'diagnostics': [diagnostic('PARSE_ERROR', str(exc))]})
                continue
            if not statements:
                report['notices'].append({'file': file, 'category': 'EMPTY_INPUT', 'severity': 'warn',
                                          'message': 'no SQL statements found'})
            for index, (source, start, end, column_start, column_end) in enumerate(statements, 1):
                result = audit_statement(source, file, index, start, end, read, write, binder, auto_stub,
                                         column_start=column_start, column_end=column_end)
                report['statements'].append(result)
                file_functions.update(result['functions'])
            report['file_function_inventory'][file] = dict(sorted(file_functions.items()))
            total_functions.update(file_functions)
    finally:
        if binder:
            binder.close()
    report['function_inventory'] = dict(sorted(total_functions.items()))
    report['file_summaries'] = summarize_files(files, report['statements'], report['notices'])
    categories = Counter(category for result in report['statements'] for category in result['categories'])
    diagnostics = [item for result in report['statements'] for item in result['diagnostics']] + report['notices']
    report['summary'] = {'files': len(files), 'statements': len(report['statements']),
                         'ok': categories['OK'], 'flagged': len(report['statements']) - categories['OK'],
                         'skipped_files': len(report['notices']),
                         'errors': sum(item['severity'] == 'error' for item in diagnostics),
                         'warnings': sum(item['severity'] == 'warn' for item in diagnostics),
                         'categories': dict(sorted(categories.items()))}
    return report
