"""Execute both real default readers; inspect every row, including past samples."""
from __future__ import annotations

from itertools import zip_longest
import os
import duckdb

from .core import connection, identifier, infer, short
from .csvio import CSVSource, SeedtypesError

# Set before importing Polars: respect an explicitly configured caller environment.
os.environ.setdefault('POLARS_MAX_THREADS', '1')

MAX_COMPARE_BYTES = 64 * 1024 * 1024


def _inspect(source, engine_rows, types, null_tokens, show_examples):
    reports = {name: {'type': dtype, 'status': 'OK', 'mismatches': 0,
                      'first_mismatch_line': None, 'examples': []}
               for name, dtype in types.items()}
    raw = (row for batch in source.batches() for row in batch)
    sentinel = object()
    count = 0
    for original, rendered in zip_longest(raw, engine_rows, fillvalue=sentinel):
        if original is sentinel or rendered is sentinel:
            raise SeedtypesError('default reader changed the number of CSV records')
        if list(types) != source.headers or len(rendered) != len(source.headers):
            raise SeedtypesError('default reader changed the CSV column names/count')
        count += 1
        for name, value, text in zip(source.headers, original.values, rendered, strict=True):
            expected = None if value in null_tokens else value
            if text != expected:
                col = reports[name]
                col['status'] = 'LOSSY'
                col['mismatches'] += 1
                if col['first_mismatch_line'] is None:
                    col['first_mismatch_line'] = original.line
                if show_examples and len(col['examples']) < 3:
                    col['examples'].append({'line': original.line, 'value': short(value), 'rendered': short(text)})
    return {'status': 'LOSSY' if any(c['status'] == 'LOSSY' for c in reports.values()) else 'OK',
            'rows_compared': count, 'columns': reports}


def reader_error(engine, exc):
    # Reader exceptions can embed whole sensitive CSV rows and several values.
    # Even opt-in examples must use structured, bounded evidence, not this text.
    return f'{engine} default reader failed ({type(exc).__name__}); pin types with an emitted recipe'


def compare(path, null_tokens=None, *, show_examples=False, delimiter=','):
    source = CSVSource(path, delimiter=delimiter)
    if source.path.stat().st_size > MAX_COMPARE_BYTES:
        raise SeedtypesError(f'{source.path}: compare is limited to 64 MiB; infer remains streaming')
    inference = infer(source.path, null_tokens, show_examples=show_examples, delimiter=source.delimiter)
    source.unchanged()
    if not inference.rows:
        return {'inference': inference.json(), 'engines': {
            name: {'status': 'NOT_RUN', 'error': 'no data rows'} for name in ('duckdb', 'polars')}}
    engines = {}
    try:
        with connection() as con:
            relation = con.read_csv(str(source.path), header=True, delimiter=source.delimiter, quotechar='"',
                                    escapechar='"', parallel=False)
            types = dict(zip(relation.columns, map(str, relation.types), strict=True))
            relation = relation.project(', '.join(f'CAST({identifier(name)} AS VARCHAR) AS {identifier(name)}'
                                                  for name in types))

            def rows():
                while batch := relation.fetchmany(1024):
                    yield from batch

            engines['duckdb'] = _inspect(source, rows(), types, inference.null_tokens, show_examples)
    except (duckdb.Error, SeedtypesError) as exc:
        source.unchanged()
        engines['duckdb'] = {'status': 'ERROR', 'error': reader_error('DuckDB', exc)}
    try:
        import polars as pl
        df = pl.read_csv(source.path, separator=source.delimiter, n_threads=1, glob=False)
        types = {name: str(dtype) for name, dtype in df.schema.items()}
        rendered = df.select(pl.all().cast(pl.String)).iter_rows()
        engines['polars'] = _inspect(source, rendered, types, inference.null_tokens, show_examples)
    except (pl.exceptions.PolarsError, SeedtypesError) as exc:
        source.unchanged()
        engines['polars'] = {'status': 'ERROR', 'error': reader_error('Polars', exc)}
    source.unchanged()
    return {'inference': inference.json(), 'engines': engines}
