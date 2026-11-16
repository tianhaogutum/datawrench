"""Restricted empty-schema fixtures and an isolated, offline DuckDB binder."""
from __future__ import annotations

import re
from pathlib import Path

import duckdb
import sqlglot
import yaml
from sqlglot import exp

MAX_BYTES = 2 * 1024 * 1024


class InputError(ValueError):
    pass


def read_text(path: Path) -> str:
    if not path.is_file():
        raise InputError(f'{path}: expected a regular file')
    with path.open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise InputError(f'{path}: input exceeds {MAX_BYTES} bytes')
    if b'\0' in data:
        raise InputError(f'{path}: binary input (NUL byte)')
    return data.decode('utf-8-sig')


def quote(name: str) -> str:
    if not name or '\0' in name or len(name) > 256:
        raise InputError('identifiers must contain 1–256 characters without NUL')
    return '"' + name.replace('"', '""') + '"'


def identifier_key(name: str) -> str:
    # DuckDB folds ASCII identifier case, but preserves non-ASCII case.
    return name.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))


def table_key(table: exp.Table) -> tuple[str, str]:
    if table.catalog:
        raise InputError('catalog-qualified tables are not supported; use schema.table')
    if not isinstance(table.this, exp.Identifier):
        raise InputError('table functions are not schema table names')
    return identifier_key(table.db or 'main'), identifier_key(table.name)


def table_name(key: tuple[str, str]) -> str:
    return '.'.join(quote(part) for part in key)


def safe_type(value: str | exp.DataType, dialect: str) -> str:
    if isinstance(value, str):
        if not re.fullmatch(r'[A-Za-z0-9_\s(),\[\]<>]+', value):
            raise InputError('fixture types must contain only type names and numeric parameters')
        dtype = sqlglot.parse_one(value, read=dialect, into=exp.DataType)
    else:
        dtype = value
    # No UDTs, expressions, struct field names, enums or executable type parameters.
    allowed = {'BOOLEAN', 'TINYINT', 'SMALLINT', 'INT', 'BIGINT', 'INT128', 'UTINYINT',
               'USMALLINT', 'UINT', 'UBIGINT', 'FLOAT', 'DOUBLE', 'DECIMAL', 'VARCHAR',
               'CHAR', 'TEXT', 'BINARY', 'VARBINARY', 'BLOB', 'DATE', 'TIME', 'TIMETZ',
               'TIMESTAMP', 'TIMESTAMPNTZ', 'TIMESTAMPLTZ', 'TIMESTAMPTZ', 'TIMESTAMP_S',
               'TIMESTAMP_MS', 'TIMESTAMP_NS', 'INTERVAL', 'UUID', 'JSON', 'ARRAY'}
    for node in dtype.walk():
        if isinstance(node, exp.DataType):
            if node.this.name not in allowed:
                raise InputError(f'unsupported fixture type: {node.this.name}')
        elif not isinstance(node, (exp.DataTypeParam, exp.Literal)):
            raise InputError('fixture type contains unsupported parameters')
        elif isinstance(node, exp.Literal) and (node.is_string or not node.this.isdigit()):
            raise InputError('fixture type parameters must be positive integers')
    result = dtype.sql(dialect='duckdb')
    if not re.fullmatch(r'[A-Za-z0-9_\s(),\[\]]+', result):
        raise InputError(f'unsafe fixture type: {result}')
    return result


class Binder:
    def __init__(self) -> None:
        self.conn = duckdb.connect(':memory:', config={
            'enable_external_access': 'false', 'autoinstall_known_extensions': 'false',
            'autoload_known_extensions': 'false', 'threads': '1', 'memory_limit': '128MB',
            'allow_persistent_secrets': 'false', 'allow_unsigned_extensions': 'false',
            'allow_community_extensions': 'false', 'python_enable_replacements': 'false',
            'temp_directory': '',
        })
        self.conn.execute('SET max_expression_depth = 200')
        self.conn.execute('SET lock_configuration = true')
        self.tables: dict[tuple[str, str], dict[str, str]] = {}

    def close(self) -> None:
        self.conn.close()

    def create(self, key: tuple[str, str], columns: dict[str, str]) -> None:
        if not columns:
            raise InputError(f'{table_name(key)}: empty column definitions')
        self.conn.execute(f'CREATE SCHEMA IF NOT EXISTS {quote(key[0])}')
        definitions = ', '.join(f'{quote(name)} {dtype}' for name, dtype in columns.items())
        self.conn.execute(f'CREATE TABLE {table_name(key)} ({definitions})')

    def load(self, path: Path, dialect: str) -> None:
        try:
            text = read_text(path)
            if path.suffix.lower() in {'.yaml', '.yml'}:
                self._yaml(text, dialect)
            else:
                self._ddl(text, dialect)
        except Exception as exc:
            raise InputError(f'schema {path}: {exc}') from exc

    def _add(self, table: exp.Table, columns: dict[str, str]) -> None:
        key = table_key(table)
        if key in self.tables:
            raise InputError(f'duplicate schema table {table_name(key)}')
        self.create(key, columns)
        self.tables[key] = columns

    def _yaml(self, text: str, dialect: str) -> None:
        # Disallow aliases: schema fixtures do not need recursive/shared YAML structures.
        if any(isinstance(event, yaml.events.AliasEvent) for event in yaml.parse(text)):
            raise InputError('YAML aliases are not supported')
        class UniqueLoader(yaml.SafeLoader):
            pass
        def mapping(loader, node):
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node)
                if not isinstance(key, str) or key in result:
                    raise InputError('YAML keys must be unique strings')
                result[key] = loader.construct_object(value_node)
            return result
        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        data = yaml.load(text, Loader=UniqueLoader)
        if not isinstance(data, dict) or not data:
            raise InputError('YAML must map table names to nonempty column/type mappings')
        for name, columns in data.items():
            try:
                if ';' in name:
                    raise InputError('table names cannot contain statement separators')
                table = sqlglot.parse_one(name, read=dialect, into=exp.Table)
                if any(value for key, value in table.args.items() if key not in {'this', 'db', 'catalog'}):
                    raise InputError('expected a plain table name without aliases/modifiers')
                if not isinstance(columns, dict) or not columns:
                    raise InputError('columns must be a nonempty mapping')
                converted = {}
                for column, dtype in columns.items():
                    if not isinstance(dtype, str):
                        raise InputError(f'column {column}: type must be a string')
                    if identifier_key(column) in converted:
                        raise InputError(f'duplicate column {column}')
                    converted[identifier_key(column)] = safe_type(dtype, dialect)
                self._add(table, converted)
            except Exception as exc:
                raise InputError(f'table {name!r}: {exc}') from exc

    def _ddl(self, text: str, dialect: str) -> None:
        statements = sqlglot.parse(text, read=dialect)
        if not any(statements):
            raise InputError('schema fixture is empty')
        for index, stmt in enumerate(statements, 1):
            try:
                if not isinstance(stmt, exp.Create) or stmt.kind != 'TABLE':
                    raise InputError('only CREATE TABLE column definitions are accepted')
                if any(value for key, value in stmt.args.items() if key not in {'this', 'kind'}):
                    raise InputError('CTAS, properties, replacement and other CREATE options are unsupported')
                if not isinstance(stmt.this, exp.Schema) or not isinstance(stmt.this.this, exp.Table):
                    raise InputError('expected CREATE TABLE name (column type, ...)')
                columns = {}
                for column in stmt.this.expressions:
                    if not isinstance(column, exp.ColumnDef) or not isinstance(column.kind, exp.DataType):
                        raise InputError('only typed columns are supported')
                    if any(value for key, value in column.args.items() if key not in {'this', 'kind'}):
                        raise InputError('column constraints/defaults are unsupported')
                    if identifier_key(column.name) in columns:
                        raise InputError(f'duplicate column {column.name}')
                    columns[identifier_key(column.name)] = safe_type(column.kind, dialect)
                self._add(stmt.this.this, columns)
            except Exception as exc:
                source = stmt.sql(dialect=dialect) if stmt is not None else '<empty>'
                raise InputError(f'statement {index} ({source}): {exc}') from exc
