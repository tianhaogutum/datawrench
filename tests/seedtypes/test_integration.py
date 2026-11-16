import csv
from pathlib import Path

import pytest
import yaml

from datawrench.seedtypes.compare import compare
from datawrench.seedtypes.core import connection, identifier, infer
from datawrench.seedtypes.emit import emit

EXAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'seedtypes'


def raw_rows(path, tokens=()):
    with path.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    return [[None if v in tokens else v for v in row] for row in rows[1:]]


def execute_recipes(path, tokens=None):
    result = infer(path, tokens)
    sql = emit([result], 'duckdb')
    with connection() as con:
        rel = con.sql(sql)
        actual = rel.project(', '.join(f'CAST({identifier(c.name)} AS VARCHAR)' for c in result.columns)).fetchall()
    assert [list(row) for row in actual] == raw_rows(path, tokens or [])
    namespace = {}
    exec(compile(emit([result], 'polars'), '<generated trusted recipe>', 'exec'), namespace)
    assert namespace['rendered'].rows() == [tuple(row) for row in raw_rows(path, tokens or [])]
    return namespace['df']


@pytest.mark.parametrize('name', ['ledger.csv', 'zip_codes.csv'])
def test_real_fixture_recipes(name):
    execute_recipes(EXAMPLES / 'seeds' / name)


def test_nulls_quotes_empty_and_big_numbers(tmp_path):
    path = tmp_path / "input'; select 1; --.csv"
    with path.open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['big', "quote'\";column", 'bool', 'fraction', 'date', 'time', 'empty'])
        w.writerow(['9' * 38, 'comma,quote"', 'true', '0.' + '1' * 38,
                    '0001-01-01', '0001-01-01 00:00:00.000001', ''])
        w.writerow(['NULL'] * 6 + [''])
    execute_recipes(path, ['NULL'])
    assert not (tmp_path / 'pwned').exists()


def test_bom_and_quoted_nulls(tmp_path):
    path = tmp_path / 'bom.csv'
    path.write_text('\ufeffa,b\n"1","NULL"\n"2",""\n', encoding='utf-8')
    execute_recipes(path, ['NULL'])


def test_late_leading_zero_and_default_comparison(tmp_path):
    path = tmp_path / 'late.csv'
    # Physical line 21000 is outside DuckDB's 20480-record sample and Polars' 100.
    path.write_text('zip\n' + '2134\n' * 20998 + '02134\n' + '2134\n' * 4000)
    result = compare(path, show_examples=True)
    col = result['inference']['columns'][0]
    assert col['type'] == 'VARCHAR'
    assert col['rejected']['BIGINT']['line'] == 21000
    for engine in ('duckdb', 'polars'):
        report = result['engines'][engine]
        assert report['status'] == 'LOSSY'
        assert report['rows_compared'] == 24999
        example = report['columns']['zip']['examples'][0]
        assert example == {'line': 21000, 'value': '02134', 'rendered': '2134'}
    execute_recipes(path)


def test_default_reader_error_not_silently_safe(tmp_path):
    path = tmp_path / 'late_string.csv'
    path.write_text('id\n' + '123\n' * 24999 + 'oops\n')
    result = compare(path)
    assert result['inference']['columns'][0]['type'] == 'VARCHAR'
    assert result['engines']['polars']['status'] == 'ERROR'
    assert 'oops' not in result['engines']['polars']['error']
    assert 'ComputeError' in result['engines']['polars']['error']
    assert result['engines']['duckdb']['status'] == 'ERROR'


def test_defaults_nullify_empty_fields(tmp_path):
    path = tmp_path / 'empty_strings.csv'
    path.write_text('text,n\na,1\n,2\n')
    result = compare(path, show_examples=True)
    for report in result['engines'].values():
        assert report['columns']['text']['status'] == 'LOSSY'
        assert report['columns']['text']['examples'][0]['rendered'] is None
    execute_recipes(path)


@pytest.mark.parametrize('adapter,expected', [
    ('duckdb', ['TEXT', 'BIGINT', 'DECIMAL(3, 2)', 'DATE', 'TIMESTAMP', 'BOOLEAN']),
    ('postgres', ['TEXT', 'BIGINT', 'DECIMAL(3, 2)', 'DATE', 'TIMESTAMP', 'BOOLEAN']),
    ('snowflake', ['VARCHAR', 'BIGINT', 'DECIMAL(3, 2)', 'DATE', 'DATETIME', 'BOOLEAN']),
    ('bigquery', ['STRING', 'INT64', 'NUMERIC(3, 2)', 'DATE', 'DATETIME', 'BOOL']),
    ('redshift', ['VARCHAR(MAX)', 'BIGINT', 'DECIMAL(3, 2)', 'DATE', 'TIMESTAMP', 'BOOLEAN']),
    ('databricks', ['STRING', 'BIGINT', 'DECIMAL(3, 2)', 'DATE', 'TIMESTAMP_NTZ', 'BOOLEAN']),
])
def test_dbt_adapter_mappings(tmp_path, adapter, expected):
    path = tmp_path / 'all_types.csv'
    path.write_text('text,n,money,date,time,bool\n007,1,1.10,2025-01-01,2025-01-01 00:00:00,true\n')
    doc = yaml.safe_load(emit([infer(path)], 'dbt', adapter))
    assert doc['version'] == 2
    seed = doc['seeds'][0]
    assert seed['name'] == 'all_types'
    assert list(seed['config']['column_types'].values()) == [t.lower() for t in expected]


def test_bigquery_scale_and_integer_capacity(tmp_path):
    path = tmp_path / 'bigquery.csv'
    path.write_text('fraction,huge\n0.1234567891,' + '9' * 38 + '\n')
    cols = yaml.safe_load(emit([infer(path)], 'dbt', 'bigquery'))['seeds'][0]['config']['column_types']
    assert cols == {'fraction': 'bignumeric(11, 10)', 'huge': 'bignumeric(38, 0)'}


def test_late_decimal_reports_real_engine_behaviour(tmp_path):
    path = tmp_path / 'late_decimal.csv'
    path.write_text('amount\n' + '1\n' * 20998 + '1.10\n' + '1\n' * 4000)
    result = compare(path, show_examples=True)
    assert result['inference']['columns'][0]['type'] == 'VARCHAR'
    duck = result['engines']['duckdb']['columns']['amount']
    assert duck['status'] == 'LOSSY'
    assert duck['examples'][0] == {'line': 21000, 'value': '1.10', 'rendered': '1'}
    assert result['engines']['polars']['status'] == 'ERROR'
    execute_recipes(path)


def test_header_only_recipes(tmp_path):
    path = tmp_path / 'header.csv'
    path.write_text('a,b\n')
    execute_recipes(path)


@pytest.mark.parametrize('value', ['é漢字🙂', 'line\r\nline', 'line\nline', 'line\rline', '  spaced  ', '"quoted"', '\t', 'NULL', '2025-01-01Z'])
def test_string_format_boundaries(tmp_path, value):
    path = tmp_path / 'text.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['field'])
        w.writerow([value])
    execute_recipes(path)


def test_multiple_special_null_tokens(tmp_path):
    tokens = ['', 'NULL', "N'A", 'line\nnull']
    path = tmp_path / 'nulls.csv'
    with path.open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['number'])
        w.writerows([[v] for v in ['1', *tokens, '2']])
    df = execute_recipes(path, tokens)
    assert str(df.schema['number']) == 'Int64'


def test_compare_size_limit_without_allocating_large_memory(tmp_path):
    from datawrench.seedtypes.csvio import SeedtypesError
    path = tmp_path / 'oversized.csv'
    with path.open('wb') as f:
        f.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(SeedtypesError, match='64 MiB'):
        compare(path)
