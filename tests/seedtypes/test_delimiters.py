"""Explicit dialects, using synthetic inputs and the real locked reader engines."""
import csv
import json

import pytest

from datawrench.seedtypes.compare import compare
from datawrench.seedtypes.core import connection, identifier, infer
from datawrench.seedtypes.csvio import CSVSource, SeedtypesError, discover
from datawrench.seedtypes.emit import emit
from .test_cli_check import cli

DIALECTS = [('comma', ',', '.csv'), ('tab', '\t', '.tsv'), ('semicolon', ';', '.csv')]
HEADERS = ['zip', 'amount', 'flag', 'date', 'time', 'note']
VALUES = [
    ['123', '1.10', 'true', '0001-01-01', '0001-01-01 00:00:00', 'é,;\t"quoted"\r\nline'],
    ['007', '-0.20', 'false', '2024-02-29', '2024-02-29 01:02:03.000001', '  spaced  '],
    ['NULL'] * 6,
]


def write_fixture(path, delimiter, values=VALUES):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(HEADERS)
        writer.writerows(values)
    return path


def assert_recipes(result, expected):
    with connection() as con:
        rel = con.sql(emit([result], 'duckdb'))
        actual = rel.project(', '.join(f'CAST({identifier(c.name)} AS VARCHAR)' for c in result.columns)).fetchall()
    assert actual == [tuple(row) for row in expected]
    namespace = {}
    exec(compile(emit([result], 'polars'), '<trusted generated reader>', 'exec'), namespace)
    assert namespace['rendered'].rows() == [tuple(row) for row in expected]


@pytest.mark.parametrize('name,delimiter,suffix', DIALECTS)
def test_same_inference_and_import_values(tmp_path, name, delimiter, suffix):
    path = write_fixture(tmp_path / ('data' + suffix), delimiter)
    result = infer(path, ['NULL'], delimiter=delimiter)
    baseline = infer(write_fixture(tmp_path / 'baseline.csv', ','), ['NULL'])
    assert result.json()['columns'] == baseline.json()['columns']
    assert [c.type for c in result.columns] == ['VARCHAR', 'DECIMAL(3,2)', 'BOOLEAN', 'DATE', 'TIMESTAMP', 'VARCHAR']
    assert result.columns[0].rejected['BIGINT']['line'] == 4
    assert result.json()['delimiter'] == delimiter
    assert_recipes(result, [*VALUES[:2], [None] * 6])
    report = cli('infer', path, '--delimiter', name, '--null-token', 'NULL', '--format', 'json')
    assert report.returncode == 0, report.stderr
    assert json.loads(report.stdout)[0] == result.json()
    assert '007' not in report.stdout


@pytest.mark.parametrize('name,delimiter,suffix', DIALECTS)
def test_compare_uses_selected_dialect_and_original_lines(tmp_path, name, delimiter, suffix):
    path = write_fixture(tmp_path / ('comparison' + suffix), delimiter, VALUES[:2])
    report = compare(path, delimiter=delimiter)
    baseline = compare(write_fixture(tmp_path / 'baseline.csv', ',', VALUES[:2]))
    assert report['engines'] == baseline['engines']
    assert report['engines']['polars']['columns']['zip']['first_mismatch_line'] == 4
    response = cli('compare', path, '--delimiter', name, '--format', 'json')
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout) == report


@pytest.mark.parametrize('name,delimiter,suffix', DIALECTS)
@pytest.mark.parametrize('tail', ['"unclosed{d}field\r\nsecond line\r\n', '"bad\r\nfield"tail{d}2\r\n', 'ragged\r\n'])
def test_malformed_records_report_original_start_line(tmp_path, name, delimiter, suffix, tail):
    path = tmp_path / ('broken' + suffix)
    path.write_bytes(('a{d}b\r\n1{d}"good\r\nmultiline"\r\n' + tail).format(d=delimiter).encode())
    response = cli('infer', path, '--delimiter', name, '--format', 'json')
    assert response.returncode == 2
    assert response.stdout == ''
    assert f'{path}: line 4:' in response.stderr
    assert 'malformed CSV' in response.stderr or 'ragged row' in response.stderr


@pytest.mark.parametrize('name,delimiter,suffix', DIALECTS)
def test_header_only_and_discovery(tmp_path, name, delimiter, suffix):
    path = write_fixture(tmp_path / ('header' + suffix), delimiter, [])
    assert discover([str(tmp_path)]) == [path]
    result = infer(path, delimiter=delimiter)
    assert [c.name for c in result.columns] == HEADERS
    assert_recipes(result, [])
    response = cli('emit', tmp_path, '--delimiter', name, '--target', 'json')
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout) == [result.json()]


@pytest.mark.parametrize('name,delimiter,suffix', DIALECTS[1:])
def test_noncomma_dbt_properties_pin_separator(tmp_path, name, delimiter, suffix):
    import yaml
    path = write_fixture(tmp_path / 'data.csv', delimiter)
    response = cli('emit', path, '--delimiter', name, '--target', 'dbt')
    assert response.returncode == 0, response.stderr
    config = yaml.safe_load(response.stdout)['seeds'][0]['config']
    assert config['delimiter'] == delimiter
    assert list(config['column_types']) == HEADERS


@pytest.mark.parametrize('delimiter', ['|', '::', '', '，', 'tab', None])
def test_api_rejects_unsupported_delimiters(tmp_path, delimiter):
    path = write_fixture(tmp_path / 'data.csv', ',')
    for call in (lambda: infer(path, delimiter=delimiter),
                 lambda: compare(path, delimiter=delimiter),
                 lambda: CSVSource(path, delimiter=delimiter)):
        with pytest.raises(SeedtypesError, match='delimiter'):
            call()


def test_cli_rejects_unsupported_delimiter(tmp_path):
    path = write_fixture(tmp_path / 'data.csv', ',')
    response = cli('infer', path, '--delimiter', '|')
    assert response.returncode == 2 and not response.stdout
    assert 'invalid choice' in response.stderr


def test_default_is_comma_without_extension_sniffing(tmp_path):
    path = tmp_path / 'data.tsv'
    path.write_text('a\tb\n1\t2\n')
    default = infer(path)
    assert [c.name for c in default.columns] == ['a\tb']
    assert default.json() == infer(path, delimiter=',').json()
    assert [c.type for c in infer(path, delimiter='\t').columns] == ['BIGINT', 'BIGINT']


@pytest.mark.parametrize('name,delimiter,suffix', DIALECTS)
def test_non_utf8_is_never_transcoded(tmp_path, name, delimiter, suffix):
    path = tmp_path / ('invalid' + suffix)
    path.write_bytes(f'a{delimiter}b\n1{delimiter}'.encode() + b'\xff\n')
    response = cli('infer', path, '--delimiter', name)
    assert response.returncode == 2 and not response.stdout
    assert 'non-UTF-8' in response.stderr


def test_default_comma_multiline_syntax_error_starts_at_record(tmp_path):
    # This fails independently of the new option on the preceding implementation:
    # csv.reader.line_num is 5, but the malformed record begins on physical line 4.
    path = tmp_path / 'broken.csv'
    path.write_bytes(b'a,b\r\n1,"good\r\nmultiline"\r\n"unclosed,field\r\nsecond line\r\n')
    response = cli('infer', path, '--format', 'json')
    assert response.returncode == 2 and not response.stdout
    assert f'{path}: line 4: malformed CSV' in response.stderr


@pytest.mark.parametrize('name,delimiter,suffix', DIALECTS)
def test_late_records_and_separator_in_null_token(tmp_path, name, delimiter, suffix):
    path = tmp_path / ('late' + suffix)
    token = f'null{delimiter}token'
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(['zip', 'note'])
        writer.writerows([['123', 'plain']] * 1024)
        writer.writerow(['123', f'quoted{delimiter}field\r\nline'])
        writer.writerow(['007', token])
    result = infer(path, [token], delimiter=delimiter)
    assert result.rows == 1026
    assert result.columns[0].rejected['BIGINT']['line'] == 1028
    assert result.columns[1].nulls == 1
    expected = [['123', 'plain']] * 1024 + [['123', f'quoted{delimiter}field\r\nline'], ['007', None]]
    assert_recipes(result, expected)
    # Full-source validation still runs beyond the inference batch boundary.
    with path.open('a', encoding='utf-8') as stream:
        stream.write('ragged\n')
    with pytest.raises(SeedtypesError, match='line 1029: ragged'):
        infer(path, [token], delimiter=delimiter)


@pytest.mark.parametrize('name,delimiter,suffix', DIALECTS)
def test_separator_null_policy_preserves_typed_values(tmp_path, name, delimiter, suffix):
    token = f'null{delimiter}token'
    values = [*VALUES, [token] * 6, [''] * 6]
    path = write_fixture(tmp_path / ('typed_nulls' + suffix), delimiter, values)
    result = infer(path, ['NULL', token, ''], delimiter=delimiter)
    assert [c.type for c in result.columns] == ['VARCHAR', 'DECIMAL(3,2)', 'BOOLEAN', 'DATE', 'TIMESTAMP', 'VARCHAR']
    assert all(c.nulls == 3 for c in result.columns)
    assert_recipes(result, [*VALUES[:2], *[[None] * 6] * 3])
