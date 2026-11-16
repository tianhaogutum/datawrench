import csv
from pathlib import Path

import pytest

from datawrench.seedtypes.core import infer
from datawrench.seedtypes.csvio import CSVSource, SeedtypesError


def csv_file(tmp_path, values, name='data.csv', header='value'):
    path = tmp_path / name
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([header])
        writer.writerows([v] for v in values)
    return path


@pytest.mark.parametrize('values,expected', [
    (['1', '-2', '0'], 'BIGINT'),
    (['9223372036854775807', '-9223372036854775808'], 'BIGINT'),
    (['9223372036854775808'], 'DECIMAL(19,0)'),
    (['9' * 38], 'DECIMAL(38,0)'),
    (['9' * 39], 'VARCHAR'),
    (['007', '123'], 'VARCHAR'),
    (['+1'], 'VARCHAR'),
    (['-0'], 'VARCHAR'),
    ([' 1'], 'VARCHAR'),
    (['1e3'], 'VARCHAR'),
    (['NaN', 'Inf'], 'VARCHAR'),
    (['1.10', '12.20'], 'DECIMAL(4,2)'),
    (['0.10', '-0.20'], 'DECIMAL(3,2)'),
    (['1.10', '2.2'], 'VARCHAR'),
    (['1', '2.0'], 'VARCHAR'),
    (['-0.00'], 'VARCHAR'),
    (['0.' + '1' * 38], 'VARCHAR'),
    (['0.' + '1' * 39], 'VARCHAR'),
    (['true', 'false'], 'BOOLEAN'),
    (['TRUE', 'FALSE'], 'VARCHAR'),
    (['yes', 'no'], 'VARCHAR'),
    (['2024-02-29', '2025-01-01'], 'DATE'),
    (['2025-02-29'], 'VARCHAR'),
    (['0000-01-01'], 'VARCHAR'),
    (['2025-01-01 12:30:00', '2025-01-02 12:30:00.12'], 'TIMESTAMP'),
    (['2025-01-01T12:30:00'], 'VARCHAR'),
    (['2025-01-01 12:30:00.120'], 'VARCHAR'),
    (['2025-01-01 12:30:00.1234567'], 'VARCHAR'),
    (['2025-01-01 12:30:00+00'], 'VARCHAR'),
    (['2025-01-01', '2025-01-01 00:00:00'], 'VARCHAR'),
    (['', '1'], 'VARCHAR'),
])
def test_typing_rules(tmp_path, values, expected):
    result = infer(csv_file(tmp_path, values))
    assert result.columns[0].type == expected
    assert result.rows == len(values)


def test_null_tokens_and_all_null(tmp_path):
    p = csv_file(tmp_path, ['1', 'NULL', '2', ''])
    c = infer(p, ['', 'NULL']).columns[0]
    assert (c.type, c.non_null, c.nulls) == ('BIGINT', 2, 2)
    c = infer(csv_file(tmp_path, ['NULL', 'NULL']), ['NULL']).columns[0]
    assert (c.type, c.non_null, c.nulls) == ('VARCHAR', 0, 2)


def test_bom_quoted_number_and_physical_lines(tmp_path):
    path = tmp_path / 'bom.csv'
    path.write_text('\ufeffzip,note\r\n"123","multi\r\nline"\r\n"007",last\r\n', encoding='utf-8')
    result = infer(path, show_examples=True)
    assert [c.name for c in result.columns] == ['zip', 'note']
    assert result.columns[0].type == 'VARCHAR'
    assert result.columns[0].rejected['BIGINT']['line'] == 4
    assert result.columns[0].rejected['BIGINT']['rendered'] == '7'


def test_first_decimal_offender_within_batch(tmp_path):
    col = infer(csv_file(tmp_path, ['1.00', '-0.00', '1.2'])).columns[0]
    assert col.rejected['DECIMAL']['line'] == 3


@pytest.mark.parametrize('data,error', [
    ('a,b\n1\n', 'line 2: ragged'),
    ('a,b\n1,2,3\n', 'line 2: ragged'),
    ('a,b\n"unclosed,2\n', 'line 2: malformed'),
    ('a,a\n1,2\n', 'duplicate headers'),
    ('"a\nb"\n1\n', 'multiline column names'),
    ('A,a\n1,2\n', 'duplicate headers'),
    (',b\n1,2\n', 'empty or NUL'),
    ('a\n\n', 'line 2: ragged'),
    ('a\n"x"tail\n', 'malformed CSV'),
    ('a\nx\x00y\n', 'NUL bytes'),
])
def test_invalid_csv(tmp_path, data, error):
    path = tmp_path / 'bad.csv'
    path.write_text(data)
    with pytest.raises(SeedtypesError, match=error):
        infer(path)


def test_invalid_utf8(tmp_path):
    path = tmp_path / 'bad.csv'
    path.write_bytes(b'a\n\xff\n')
    with pytest.raises(SeedtypesError, match='bad.csv: non-UTF-8'):
        infer(path)


@pytest.mark.parametrize('data,types,notice', [('', [], 'empty file'), ('a,b\n', ['VARCHAR', 'VARCHAR'], 'header only')])
def test_no_rows(tmp_path, data, types, notice):
    path = tmp_path / 'empty.csv'
    path.write_text(data)
    result = infer(path)
    assert result.rows == 0
    assert [c.type for c in result.columns] == types
    assert notice in result.notices[0]


def test_source_change_detected(tmp_path):
    path = csv_file(tmp_path, ['1'])
    source = CSVSource(path)
    path.write_text('value\n2\n')
    with pytest.raises(SeedtypesError, match='changed during scan'):
        list(source.batches())


def test_bounded_batches(tmp_path):
    path = csv_file(tmp_path, ['1'] * 3000)
    batches = list(CSVSource(path).batches())
    assert [len(b) for b in batches] == [1024, 1024, 952]


def test_late_malformed_row_is_not_skipped(tmp_path):
    path = tmp_path / 'late_bad.csv'
    path.write_text('a,b\n' + 'text,text\n' * 3000 + 'ragged\n')
    with pytest.raises(SeedtypesError, match='line 3002: ragged'):
        infer(path)


def test_field_and_column_limits(tmp_path):
    path = tmp_path / 'field.csv'
    path.write_text('a\n' + 'x' * (1_048_576 + 1) + '\n')
    with pytest.raises(SeedtypesError, match='field larger'):
        infer(path)
    path.write_text(','.join(f'c{i}' for i in range(257)) + '\n')
    with pytest.raises(SeedtypesError, match='more than 256 columns'):
        infer(path)


def test_record_limit_before_csv_buffering(tmp_path):
    path = tmp_path / 'record.csv'
    path.write_text('a\n' + 'x' * 9_000_001 + '\n')
    with pytest.raises(SeedtypesError, match='record exceeds'):
        infer(path)


def test_excessive_null_tokens(tmp_path):
    path = csv_file(tmp_path, ['1'])
    with pytest.raises(SeedtypesError, match='at most 64'):
        infer(path, [str(i) for i in range(65)])
