"""Standalone validation of a strict, bounded document of exact requested types."""
from __future__ import annotations

import json
from pathlib import Path
import re

import duckdb

from .core import DATE, DECIMAL, LEADING_ZEROS, TIMESTAMP, connection, rejection_reason, short
from .csvio import CSVSource, MAX_COLUMNS, SeedtypesError, batch_csv

MAX_TYPES_BYTES = 65_536
SIMPLE_TYPES = {'VARCHAR', 'BOOLEAN', 'BIGINT', 'DATE', 'TIMESTAMP'}
DECIMAL_TYPE = re.compile(r'DECIMAL\(([1-9][0-9]?),([0-9]|[1-9][0-9])\)\Z', re.ASCII)


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise SeedtypesError('types JSON keys must be unique')
        obj[key] = value
    return obj


def _invalid_constant(_value):
    raise SeedtypesError('types document must be strict JSON')


def load_types(path) -> dict[str, str]:
    """Only validated whitelist spellings can reach a SQL cast expression."""
    path = Path(path)
    if (any(p.is_symlink() for p in (path.absolute(), *path.absolute().parents))
            or not path.is_file() or path.suffix.lower() != '.json'):
        raise SeedtypesError('types document must be a regular local .json file (no symlinks)')
    with path.open('rb') as stream:
        raw = stream.read(MAX_TYPES_BYTES + 1)
    if len(raw) > MAX_TYPES_BYTES:
        raise SeedtypesError(f'types JSON exceeds {MAX_TYPES_BYTES} bytes')
    try:
        doc = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                         parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, SeedtypesError):
            raise
        raise SeedtypesError('types document must be strict UTF-8 JSON') from None
    if (not isinstance(doc, dict) or set(doc) != {'version', 'columns'}
            or type(doc['version']) is not int or doc['version'] != 1):
        raise SeedtypesError('types document must contain exactly version (integer 1) and columns')
    columns = doc['columns']
    if not isinstance(columns, dict) or not 1 <= len(columns) <= MAX_COLUMNS:
        raise SeedtypesError(f'types columns must map 1..{MAX_COLUMNS} exact column names to types')
    return validate_requested_types(columns)


def validate_requested_types(columns, *, allow_empty=False):
    """Validate an in-memory mapping before any type string reaches SQL."""
    if not isinstance(columns, dict) or not (0 if allow_empty else 1) <= len(columns) <= MAX_COLUMNS:
        raise SeedtypesError(f'types columns must map at most {MAX_COLUMNS} exact column names to types')
    for name, dtype in columns.items():
        if not isinstance(name, str) or not name or not isinstance(dtype, str):
            raise SeedtypesError('types columns must map nonempty exact column names to type strings')
        if dtype in SIMPLE_TYPES:
            continue
        match = DECIMAL_TYPE.fullmatch(dtype)
        if match and 1 <= int(match[1]) <= 38 and 0 <= int(match[2]) <= int(match[1]):
            continue
        raise SeedtypesError('requested types must be VARCHAR, BOOLEAN, BIGINT, DECIMAL(P,S) '
                             '(1<=P<=38, 0<=S<=P), DATE or TIMESTAMP; use exact uppercase spellings')
    return dict(columns)


def _rejection(dtype, value, rendered):
    """Canonical lexical rules supplement the exact, requested-type cast back."""
    candidate = 'DECIMAL' if dtype.startswith('DECIMAL(') else dtype
    if candidate == 'DECIMAL':
        match = DECIMAL.fullmatch(value)
        if match is None:
            if LEADING_ZEROS.fullmatch(value):
                return rejection_reason(candidate, value, rendered)
            return 'decimal_notation', 'not canonical fixed-point notation'
        scale = int(DECIMAL_TYPE.fullmatch(dtype)[2])
        if len(match[2] or '') != scale:
            return 'decimal_scale', 'field scale differs from requested DECIMAL scale'
        if rendered is None:
            return 'decimal_precision', 'outside requested DECIMAL precision'
    lexical_ok = True
    if candidate in ('DATE', 'TIMESTAMP'):
        pattern = DATE if candidate == 'DATE' else TIMESTAMP
        lexical_ok = bool(pattern.fullmatch(value)) and not value.startswith('0000')
    if rendered != value or not lexical_ok:
        return rejection_reason(candidate, value, rendered)
    return None


def validate_types(path, types_path, null_tokens=None, *, show_examples=False, delimiter=','):
    return validate_columns(path, load_types(types_path), null_tokens,
                            show_examples=show_examples, delimiter=delimiter)


def validate_columns(path, requested, null_tokens=None, *, show_examples=False, delimiter=','):
    """Exact round trips from a bounded mapping; empty mappings still scan the CSV."""
    requested = validate_requested_types(requested, allow_empty=True)
    tokens = list(dict.fromkeys(null_tokens or []))
    if len(tokens) > 64 or any(len(t) > 1024 or '\x00' in t for t in tokens):
        raise SeedtypesError('at most 64 null tokens, each at most 1024 characters without NUL')
    null_set = set(tokens)
    source = CSVSource(path, delimiter=delimiter)
    columns = []
    indices = []

    def prepare_columns():
        if requested.keys() - set(source.headers):
            raise SeedtypesError('types document contains column names not in CSV header (exact match required)')
        for i, name in enumerate(source.headers):
            if name in requested:
                indices.append(i)
                columns.append({'name': name, 'requested_type': requested[name], 'checked': 0,
                                'non_null': 0, 'nulls': 0, 'valid': True, 'rejection': None})

    count = 0
    prepared = False
    try:
        with connection() as con:
            for batch in source.batches():
                if not prepared:
                    prepare_columns()
                    prepared = True
                if not columns:
                    count += len(batch)
                    continue
                relation = con.read_csv(batch_csv(batch), header=True, auto_detect=False,
                                        columns={f'c{i}': 'VARCHAR' for i in range(len(source.headers))},
                                        na_values=[], strict_mode=True, parallel=False, escapechar='"',
                                        max_line_size=9_000_000, buffer_size=18_000_000)
                relation.create_view('_seedtypes_validation_batch', replace=True)
                expressions = [f'TRY_CAST(TRY_CAST(c{i} AS {col["requested_type"]}) AS VARCHAR)'
                               for i, col in zip(indices, columns, strict=True)]
                rendered_rows = con.execute('SELECT ' + ', '.join(expressions)
                                            + ' FROM _seedtypes_validation_batch').fetchall()
                if len(rendered_rows) != len(batch):
                    raise SeedtypesError('DuckDB changed the normalized row count')
                for row, rendered in zip(batch, rendered_rows, strict=True):
                    for i, col, text in zip(indices, columns, rendered, strict=True):
                        col['checked'] += 1
                        value = row.values[i]
                        if value in null_set:
                            col['nulls'] += 1
                            continue
                        col['non_null'] += 1
                        if col['rejection'] is not None:
                            continue
                        rejection = _rejection(col['requested_type'], value, text)
                        if rejection is not None:
                            code, reason = rejection
                            detail = {'line': row.line, 'code': code, 'reason': reason}
                            if show_examples:
                                detail.update(value=short(value), rendered=short(text))
                            col.update(valid=False, rejection=detail)
                count += len(batch)
    except duckdb.Error:
        # Engine diagnostics can contain source text, even for normalized input.
        raise SeedtypesError('DuckDB could not validate the normalized CSV batch') from None
    if not prepared:
        if source.empty and requested:
            raise SeedtypesError(f'{source.path}: validation requires a CSV header')
        prepare_columns()
    notices = [f'{col["name"]}: no non-null values; requested type has no evidence'
               for col in columns if col['non_null'] == 0]
    return {'version': 1, 'path': str(source.path), 'rows': count, 'delimiter': source.delimiter,
            'null_tokens': tokens, 'valid': all(col['valid'] for col in columns),
            'notices': notices, 'columns': columns}


def validation_text(result):
    lines = [f'{result["path"]}: {result["rows"]} data rows; '
             + ('VALID' if result['valid'] else 'INVALID')]
    lines.extend(f'  NOTICE: {notice}' for notice in result['notices'])
    for col in result['columns']:
        lines.append(f'  {col["name"]}: {col["requested_type"]}: '
                     + ('VALID' if col['valid'] else 'INVALID')
                     + f' ({col["checked"]} checked, {col["non_null"]} non-null, {col["nulls"]} nulls)')
        detail = col['rejection']
        if detail:
            evidence = f' {detail["value"]!r} -> {detail["rendered"]!r}' if 'value' in detail else ''
            lines.append(f'    line {detail["line"]}: {detail["code"]}: {detail["reason"]}{evidence}')
    return '\n'.join(lines)
