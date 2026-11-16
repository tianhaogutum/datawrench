"""Explicit types must preserve decoded fields, regardless of inferred candidates."""
import csv
import json

import pytest

from .test_cli_check import cli


def files(tmp_path, data, columns):
    path = tmp_path / 'data.csv'
    path.write_text(data, encoding='utf-8', newline='')
    schema = tmp_path / 'types.json'
    schema.write_text(json.dumps({'version': 1, 'columns': columns}), encoding='utf-8')
    return path, schema


def validate(path, schema, *options):
    return cli('validate-types', path, '--types', schema, '--format', 'json', *options)


def test_explicit_zip_and_amount_contract(tmp_path):
    path, schema = files(tmp_path, 'zip,amount,note\n12,1.10,"two\nlines"\n007,1.10,end\n',
                         {'amount': 'DECIMAL(3,2)', 'zip': 'BIGINT'})
    result = validate(path, schema)
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        'version': 1, 'path': str(path), 'rows': 2, 'delimiter': ',', 'null_tokens': [],
        'valid': False, 'notices': [], 'columns': [
            {'name': 'zip', 'requested_type': 'BIGINT', 'checked': 2, 'non_null': 2,
             'nulls': 0, 'valid': False, 'rejection': {
                 'line': 4, 'code': 'leading_zeros', 'reason': 'leading zeros would be removed'}},
            {'name': 'amount', 'requested_type': 'DECIMAL(3,2)', 'checked': 2,
             'non_null': 2, 'nulls': 0, 'valid': True, 'rejection': None}]}
    assert '007' not in result.stdout and '1.10' not in result.stdout
    schema.write_text(json.dumps({'version': 1, 'columns': {'zip': 'VARCHAR', 'amount': 'DECIMAL(3,2)'}}))
    result = validate(path, schema)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['valid'] is True


@pytest.mark.parametrize('dtype,value,code,rendered', [
    ('BIGINT', '007', 'leading_zeros', '7'),
    ('BIGINT', '-00', 'leading_zeros', '0'),
    ('BIGINT', '-0', 'integer_negative_zero', '0'),
    ('BIGINT', '9223372036854775808', 'integer_range', None),
    ('BIGINT', '-9223372036854775809', 'integer_range', None),
    ('BIGINT', '+1', 'round_trip', '1'),
    ('BIGINT', '1.0', 'round_trip', '1'),
    ('DECIMAL(3,2)', '1.105', 'decimal_scale', '1.11'),
    ('DECIMAL(3,2)', '1.1', 'decimal_scale', '1.10'),
    ('DECIMAL(3,2)', '10.00', 'decimal_precision', None),
    ('DECIMAL(3,2)', '007.00', 'leading_zeros', '7.00'),
    ('DECIMAL(3,2)', '-0.00', 'decimal_negative_zero', '0.00'),
    ('DECIMAL(3,2)', '1e0', 'decimal_notation', '1.00'),
    ('DECIMAL(2,2)', '0.10', 'round_trip', '.10'),
    ('DECIMAL(38,38)', '0.' + '1' * 38, 'decimal_integer_zero', '.' + '1' * 38),
    ('DECIMAL(38,0)', '1' * 39, 'decimal_precision', None),
    ('BOOLEAN', 'TRUE', 'round_trip', 'true'),
    ('BOOLEAN', '1', 'round_trip', 'true'),
    ('DATE', '2024-2-29', 'date_notation', '2024-02-29'),
    ('DATE', '0000-01-01', 'date_notation', '0001-01-01 (BC)'),
    ('DATE', '2023-02-29', 'invalid_calendar', None),
    ('TIMESTAMP', '2024-02-29T00:00:00', 'timestamp_notation', '2024-02-29 00:00:00'),
    ('TIMESTAMP', '2024-02-29 00:00:00.100', 'round_trip', '2024-02-29 00:00:00.1'),
    ('TIMESTAMP', '2024-02-29 00:00:00.0000001', 'timestamp_notation', '2024-02-29 00:00:00'),
    ('TIMESTAMP', '2024-02-29 24:00:00', 'round_trip', '2024-03-01 00:00:00'),
    ('TIMESTAMP', '2023-02-29 00:00:00', 'invalid_calendar', None),
])
def test_exact_requested_type_rejections(tmp_path, dtype, value, code, rendered):
    path, schema = files(tmp_path, 'x\n' + value + '\n', {'x': dtype})
    result = validate(path, schema, '--show-examples')
    assert result.returncode == 1, result.stderr
    col = json.loads(result.stdout)['columns'][0]
    assert col['requested_type'] == dtype
    assert col['checked'] == col['non_null'] == 1
    assert col['rejection']['line'] == 2
    assert col['rejection']['code'] == code
    assert col['rejection']['value'] == value
    assert col['rejection']['rendered'] == rendered


@pytest.mark.parametrize('dtype,values', [
    ('VARCHAR', ['007', '', 'TRUE', 'é;\t"\nPRIVATE']),
    ('BIGINT', ['0', '-1', '9223372036854775807', '-9223372036854775808']),
    ('DECIMAL(3,2)', ['0.10', '1.10', '-0.10', '9.99']),
    ('DECIMAL(38,0)', ['1' * 38, '-' + '9' * 38]),
    ('DECIMAL(38,37)', ['0.' + '1' * 37]),
    ('DECIMAL(1,0)', ['0', '9', '-9']),
    ('BOOLEAN', ['true', 'false']),
    ('DATE', ['0001-01-01', '2024-02-29', '9999-12-31']),
    ('TIMESTAMP', ['0001-01-01 00:00:00', '2024-02-29 12:34:56.000001',
                   '9999-12-31 23:59:59.123456', '2024-01-01 00:00:00.1']),
])
def test_exact_requested_types_pass(tmp_path, dtype, values):
    path, schema = files(tmp_path, '', {'x': dtype})
    with path.open('w', newline='') as stream:
        csv.writer(stream).writerows([['x'], *[[v] for v in values]])
    result = validate(path, schema)
    assert result.returncode == 0, result.stderr
    col = json.loads(result.stdout)['columns'][0]
    assert col['rejection'] is None and col['valid'] is True
    assert col['checked'] == col['non_null'] == len(values)


@pytest.mark.parametrize('dtype', ['DECIMAL(2,2)', 'DECIMAL(3,1)', 'DECIMAL(2,0)'])
def test_inferred_decimal_does_not_certify_requested_type(tmp_path, dtype):
    path, schema = files(tmp_path, 'x\n1.10\n', {'x': dtype})
    inferred = cli('infer', path, '--format', 'json')
    assert json.loads(inferred.stdout)[0]['columns'][0]['type'] == 'DECIMAL(3,2)'
    assert validate(path, schema).returncode == 1


@pytest.mark.parametrize('raw', [
    '{}', '[]', 'null', '{"version":1}', '{"columns":{"x":"BIGINT"}}',
    '{"version":2,"columns":{"x":"BIGINT"}}',
    '{"version":true,"columns":{"x":"BIGINT"}}',
    '{"version":1.0,"columns":{"x":"BIGINT"}}',
    '{"version":1,"columns":{"x":"BIGINT"},"extra":"PRIVATE_SCHEMA"}',
    '{"version":1,"version":1,"columns":{"x":"BIGINT"}}',
    '{"version":1,"columns":{"x":"BIGINT","x":"VARCHAR"}}',
    '{"version":1,"columns":{"x":"BIGINT"},"columns":{"x":"VARCHAR"}}',
    '{"version":1,"columns":{}}', '{"version":1,"columns":[]}',
    '{"version":1,"columns":{"x":null}}',
    '{"version":1,"columns":{"x":NaN}}',
    '{"version":Infinity,"columns":{"x":"BIGINT"}}',
    '{"version":1,"columns":{"x":1}}',
    '{"version":1,"columns":{"x":{"nested":"PRIVATE_SCHEMA"}}}',
    '{"version":1,"columns":{"":"BIGINT"}}',
    '{"version":1,"columns":{"X":"BIGINT"}}',
    '{"version":1,"columns":{"missing":"BIGINT"}}',
    '{"version":1,"columns":{"x":"BIGINT",}}',
    '{"version":1,"columns":{"x":"PRIVATE_SCHEMA"}',
    '{"version":1,"columns":{"x":"VARCHAR"}} trailing PRIVATE_SCHEMA',
    '[' * 1100 + '0' + ']' * 1100,
])
def test_strict_schema_errors_are_atomic_private(tmp_path, raw):
    path, schema = files(tmp_path, 'x\nPRIVATE_FIELD\n', {'x': 'VARCHAR'})
    schema.write_text(raw)
    result = validate(path, schema, '--show-examples')
    assert result.returncode == 2, result.stdout
    assert result.stdout == ''
    assert 'Traceback' not in result.stderr
    assert 'PRIVATE_SCHEMA' not in result.stderr and 'PRIVATE_FIELD' not in result.stderr


@pytest.mark.parametrize('dtype', [
    'varchar', 'INT', 'DOUBLE', 'VARCHAR(4)', 'BOOLEAN ', ' BIGINT',
    'DECIMAL', 'DECIMAL(0,0)', 'DECIMAL(39,0)', 'DECIMAL(2,3)',
    'DECIMAL(3,-1)', 'DECIMAL(03,2)', 'DECIMAL(3,02)', 'DECIMAL(3, 2)',
    'DECIMAL(٣,٢)', 'TIMESTAMP WITH TIME ZONE',
    'BIGINT); SELECT PRIVATE_SCHEMA; --', '',
])
def test_type_whitelist(tmp_path, dtype):
    path, schema = files(tmp_path, 'x\nPRIVATE_FIELD\n', {'x': dtype})
    result = validate(path, schema)
    assert result.returncode == 2 and result.stdout == ''
    assert 'PRIVATE_SCHEMA' not in result.stderr and 'PRIVATE_FIELD' not in result.stderr


@pytest.mark.parametrize('data,options,rows,non_null,nulls,valid', [
    ('x\n', [], 0, 0, 0, True),
    ('x\nNULL\n"NULL"\n', ['--null-token', 'NULL'], 2, 0, 2, True),
    ('x\n""\n', [], 1, 1, 0, False),
    ('x\n""\n', ['--null-token', ''], 1, 0, 1, True),
    ('x\n-0\n007\n', ['--null-token', '-0', '--null-token', '007'], 2, 0, 2, True),
])
def test_no_evidence_and_null_policy(tmp_path, data, options, rows, non_null, nulls, valid):
    path, schema = files(tmp_path, data, {'x': 'BIGINT'})
    result = validate(path, schema, *options)
    assert result.returncode == int(not valid), result.stderr
    report = json.loads(result.stdout)
    col = report['columns'][0]
    assert (report['rows'], col['checked'], col['non_null'], col['nulls']) == (rows, rows, non_null, nulls)
    assert bool(report['notices']) == (non_null == 0)
    assert report['valid'] is valid


@pytest.mark.parametrize('delimiter,character,suffix', [('comma', ',', '.csv'), ('tab', '\t', '.tsv'), ('semicolon', ';', '.csv')])
def test_exact_unusual_headers_delimiters_and_quoted_nulls(tmp_path, delimiter, character, suffix):
    names = ['a"; SELECT', "  Zip's é  ", 'unused']
    path, schema = files(tmp_path, '', {names[1]: 'BIGINT', names[0]: 'VARCHAR'})
    path = path.rename(path.with_suffix(suffix))
    token = 'NULL' + character + 'TOKEN'
    with path.open('w', newline='') as stream:
        csv.writer(stream, delimiter=character).writerows([
            names, ['private\r\nstring', '7', 'anything'], ['007', token, 'tail']])
    result = validate(path, schema, '--delimiter', delimiter, '--null-token', token, '--null-token', token)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['delimiter'] == character and report['null_tokens'] == [token]
    assert [c['name'] for c in report['columns']] == names[:2]
    assert report['columns'][1]['non_null'] == report['columns'][1]['nulls'] == 1


def test_late_failure_and_counts_after_failure(tmp_path):
    path, schema = files(tmp_path, 'x,note\nNULL,start\n' + '12,ok\n' * 1024
                         + '7,"multi\nline"\n007,end\n-0,end\nNULL,end\n8,end\n', {'x': 'BIGINT'})
    result = validate(path, schema, '--null-token', 'NULL')
    assert result.returncode == 1, result.stderr
    col = json.loads(result.stdout)['columns'][0]
    assert col['rejection']['line'] == 1029
    assert col['rejection']['code'] == 'leading_zeros'
    assert col['checked'] == 1030 and col['non_null'] == 1028 and col['nulls'] == 2


@pytest.mark.parametrize('tail,error', [('one\n', 'ragged'), ('"bad\n', 'malformed'), ('\x00,x\n', 'NUL')])
def test_trailing_malformed_records_after_first_failure(tmp_path, tail, error):
    path, schema = files(tmp_path, 'x,note\n007,"multi\nline"\n' + '12,ok\n' * 1026 + tail,
                         {'x': 'BIGINT'})
    result = validate(path, schema, '--show-examples')
    assert result.returncode == 2 and result.stdout == ''
    assert 'line 1030' in result.stderr and error in result.stderr
    assert '007' not in result.stderr and 'Traceback' not in result.stderr


@pytest.mark.parametrize('data', ['', 'x,x\n1,2\n', 'x,X\n1,2\n', ',x\n1,2\n', '"multi\nname"\n1\n', 'x\n\xff\n'])
def test_csv_source_boundaries(tmp_path, data):
    path, schema = files(tmp_path, '', {'x': 'VARCHAR'})
    path.write_bytes(data.encode('latin1'))
    result = validate(path, schema)
    assert result.returncode == 2 and result.stdout == ''
    assert 'Traceback' not in result.stderr


def test_evidence_private_in_both_formats_and_bounded(tmp_path):
    secret = 'PRIVATE_FIELD_' + 'x' * 300
    path, schema = files(tmp_path, 'x\n' + secret + '\nSECOND_PRIVATE\n', {'x': 'BIGINT'})
    for form in ['text', 'json']:
        base = ['validate-types', path, '--types', schema, '--format', form]
        private = cli(*base)
        assert private.returncode == 1
        assert 'PRIVATE_FIELD' not in private.stdout + private.stderr
        assert 'SECOND_PRIVATE' not in private.stdout + private.stderr
        public = cli(*base, '--show-examples')
        assert public.returncode == 1
        assert 'PRIVATE_FIELD' in public.stdout and secret not in public.stdout
        assert 'SECOND_PRIVATE' not in public.stdout
        if form == 'json':
            detail = json.loads(public.stdout)['columns'][0]['rejection']
            assert len(detail['value']) == 160 and detail['rendered'] is None


def test_schema_and_option_resource_bounds(tmp_path):
    path, schema = files(tmp_path, 'x\n7\n', {'x': 'BIGINT'})
    good = schema.read_text()
    for raw in [' ' * 65537, json.dumps({'version': 1, 'columns': {str(i): 'VARCHAR' for i in range(257)}})]:
        schema.write_text(raw)
        result = validate(path, schema)
        assert result.returncode == 2 and result.stdout == ''
    schema.write_bytes(b'\xff')
    assert validate(path, schema).returncode == 2
    schema.write_text(good)
    for options in [['--delimiter', '|'], ['--format', 'yaml'], ['--null-token', 'a' * 1025],
                    sum([['--null-token', str(i)] for i in range(65)], [])]:
        result = validate(path, schema, *options)
        assert result.returncode == 2 and result.stdout == ''
    assert cli('validate-types', path).returncode == 2
    assert validate(tmp_path, schema).returncode == 2
    link = tmp_path / 'linked.json'
    link.symlink_to(schema)
    assert validate(path, link).returncode == 2
    assert validate(path, tmp_path / 'missing.json').returncode == 2


def test_duckdb_errors_never_echo_normalized_fields(tmp_path, monkeypatch):
    import duckdb
    from datawrench.seedtypes.csvio import SeedtypesError
    from datawrench.seedtypes import validate as module
    path, schema = files(tmp_path, 'x\nPRIVATE_FIELD\n', {'x': 'BIGINT'})

    def broken():
        raise duckdb.Error('PRIVATE_FIELD')

    monkeypatch.setattr(module, 'connection', broken)
    with pytest.raises(SeedtypesError, match='DuckDB could not validate') as exc:
        module.validate_types(path, schema, show_examples=True)
    assert 'PRIVATE_FIELD' not in str(exc.value)
