"""One bounded file pass: raw VARCHAR batches, then DuckDB TRY_CAST round trips."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import re
from typing import Any

import duckdb

from .csvio import CSVSource, SeedtypesError, batch_csv

CANDIDATES = ('BOOLEAN', 'BIGINT', 'DECIMAL', 'DATE', 'TIMESTAMP')
DECIMAL = re.compile(r'-?(0|[1-9][0-9]*)(?:\.([0-9]+))?\Z', re.ASCII)
DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\Z', re.ASCII)
TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\Z', re.ASCII)
LEADING_ZEROS = re.compile(r'-?0[0-9]+(?:\.[0-9]+)?\Z', re.ASCII)


def connection():
    # No extension downloads or parallel worker pool; no DB or temp files created.
    return duckdb.connect(config={'threads': 1, 'memory_limit': '128MB',
                                  'max_temp_directory_size': '0B',
                                  'autoinstall_known_extensions': False,
                                  'autoload_known_extensions': False})


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def short(value: str | None) -> str | None:
    if value is None or len(value) <= 160:
        return value
    return value[:157] + '...'


def rejection_reason(candidate, value, rendered):
    """Static explanations only: never interpolate a source value into a reason."""
    if candidate in ('BIGINT', 'DECIMAL') and LEADING_ZEROS.fullmatch(value):
        return 'leading_zeros', 'leading zeros would be removed'
    if candidate == 'BIGINT' and value == '-0' and rendered == '0':
        return ('integer_negative_zero',
                'casting negative zero to BIGINT removes the minus sign')
    if candidate == 'DECIMAL':
        match = DECIMAL.fullmatch(value)
        if match and match[1] == '0' and len(match[2] or '') == 38:
            return ('decimal_integer_zero',
                    'preserving the integer zero at scale 38 requires precision 39; maximum DECIMAL precision is 38')
        if (match and value.startswith('-') and match[1] == '0'
                and not (match[2] or '').strip('0') and rendered == value[1:]):
            return ('decimal_negative_zero',
                    'casting negative zero to DECIMAL removes the minus sign')
    if candidate in ('DATE', 'TIMESTAMP'):
        pattern = DATE if candidate == 'DATE' else TIMESTAMP
        if not pattern.fullmatch(value) or value.startswith('0000'):
            if candidate == 'DATE':
                return 'date_notation', 'not canonical YYYY-MM-DD date text'
            return 'timestamp_notation', 'not canonical microsecond timestamp text'
        if rendered is None:
            return 'invalid_calendar', 'invalid calendar date or time'
    if candidate == 'BIGINT' and DECIMAL.fullmatch(value) and '.' not in value and rendered is None:
        return 'integer_range', 'outside signed 64-bit integer range'
    if rendered is None:
        return 'cast_failed', 'cast failed'
    return 'round_trip', 'round trip differs'


@dataclass
class Column:
    name: str
    type: str = 'VARCHAR'
    non_null: int = 0
    nulls: int = 0
    rejected: dict[str, dict[str, Any]] = field(default_factory=dict)
    scale: int | None = field(default=None, repr=False)
    integer_digits: int = field(default=0, repr=False)
    show_examples: bool = field(default=False, repr=False)

    def reject(self, candidate, line, value, rendered, code=None, reason=None):
        if candidate not in self.rejected or line < self.rejected[candidate]['line']:
            if code is None:
                code, reason = rejection_reason(candidate, value, rendered)
            detail = {'line': line, 'code': code, 'reason': reason}
            if self.show_examples:
                detail.update(value=short(value), rendered=short(rendered))
            self.rejected[candidate] = detail

    def json(self):
        obj = asdict(self)
        del obj['scale'], obj['integer_digits'], obj['show_examples']
        return obj


@dataclass
class Inference:
    path: str
    rows: int
    columns: list[Column]
    null_tokens: list[str]
    notices: list[str]
    delimiter: str = ','

    def json(self):
        return {'path': self.path, 'rows': self.rows, 'null_tokens': self.null_tokens,
                'delimiter': self.delimiter, 'notices': self.notices,
                'columns': [col.json() for col in self.columns]}


def infer(path, null_tokens: list[str] | None = None, *, show_examples: bool = False,
          delimiter: str = ',') -> Inference:
    tokens = list(dict.fromkeys(null_tokens or []))
    if len(tokens) > 64 or any(len(t) > 1024 or '\x00' in t for t in tokens):
        raise SeedtypesError('at most 64 null tokens, each at most 1024 characters without NUL')
    null_set = set(tokens)
    source = CSVSource(path, delimiter=delimiter)
    columns: list[Column] = []
    count = 0
    with connection() as con:
        for batch in source.batches():
            if not columns:
                columns = [Column(name, show_examples=show_examples) for name in source.headers]
            # Parsing source once in Python supplies exact physical positions, including
            # multiline records. DuckDB sees a bounded all-VARCHAR CSV with no sniffing.
            relation = con.read_csv(batch_csv(batch), header=True, auto_detect=False,
                                    columns={f'c{i}': 'VARCHAR' for i in range(len(columns))},
                                    na_values=[], strict_mode=True, parallel=False, escapechar='"',
                                    max_line_size=9_000_000, buffer_size=18_000_000)
            relation.create_view('_seedtypes_batch', replace=True)
            expressions: list[str] = []
            checks: list[tuple[int, str]] = []
            for i, col in enumerate(columns):
                previously_rejected = set(col.rejected)
                for row in batch:
                    value = row.values[i]
                    if value in null_set:
                        col.nulls += 1
                        continue
                    col.non_null += 1
                    if 'DECIMAL' not in col.rejected:
                        match = DECIMAL.fullmatch(value)
                        if match is None:
                            if LEADING_ZEROS.fullmatch(value):
                                col.reject('DECIMAL', row.line, value, None)
                            else:
                                col.reject('DECIMAL', row.line, value, None,
                                           'decimal_notation', 'not canonical fixed-point notation')
                        else:
                            scale = len(match[2] or '')
                            digits = 0 if match[1] == '0' else len(match[1])
                            if scale > 38 or digits + scale > 38:
                                col.reject('DECIMAL', row.line, value, None,
                                           'decimal_precision', 'exceeds DECIMAL precision/scale 38')
                            elif col.scale is not None and col.scale != scale:
                                col.reject('DECIMAL', row.line, value, None,
                                           'decimal_mixed_scales', 'mixed scales cannot round-trip as one DECIMAL')
                            else:
                                col.scale = scale
                                col.integer_digits = max(col.integer_digits, digits)
                for candidate in CANDIDATES:
                    if candidate in col.rejected and (candidate != 'DECIMAL' or candidate in previously_rejected):
                        continue
                    dtype = f'DECIMAL(38,{col.scale or 0})' if candidate == 'DECIMAL' else candidate
                    expressions.append(f'TRY_CAST(TRY_CAST(c{i} AS {dtype}) AS VARCHAR)')
                    checks.append((i, candidate))
            if expressions:
                rendered_rows = con.execute('SELECT ' + ', '.join(expressions) + ' FROM _seedtypes_batch').fetchall()
                if len(rendered_rows) != len(batch):
                    raise SeedtypesError(f'{source.path}: line {batch[0].line}: DuckDB changed the normalized row count')
                for row, rendered in zip(batch, rendered_rows, strict=True):
                    for (i, candidate), text in zip(checks, rendered, strict=True):
                        value = row.values[i]
                        if value in null_set:
                            continue
                        lexical_ok = True
                        if candidate in ('DATE', 'TIMESTAMP'):
                            pattern = DATE if candidate == 'DATE' else TIMESTAMP
                            lexical_ok = bool(pattern.fullmatch(value)) and not value.startswith('0000')
                        if text != value or not lexical_ok:
                            columns[i].reject(candidate, row.line, value, text)
            else:
                # Validate every raw VARCHAR batch, even after all candidates fail.
                if len(relation.fetchall()) != len(batch):
                    raise SeedtypesError(f'{source.path}: line {batch[0].line}: DuckDB changed the normalized row count')
            count += len(batch)
    notices = []
    if count == 0:
        columns = [Column(name, show_examples=show_examples) for name in source.headers]
        notices.append('empty file' if source.empty else 'header only; all columns are VARCHAR')
    for col in columns:
        if col.non_null == 0:
            if count:
                notices.append(f'{col.name}: all values are designated nulls; VARCHAR')
            continue
        for candidate in CANDIDATES:
            if candidate not in col.rejected:
                if candidate == 'DECIMAL':
                    scale = col.scale or 0
                    precision = max(scale + 1, col.integer_digits + scale)
                    col.type = f'DECIMAL({precision},{scale})'
                else:
                    col.type = candidate
                break
    return Inference(str(source.path), count, columns, tokens, notices, source.delimiter)
