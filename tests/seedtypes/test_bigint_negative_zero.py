"""BIGINT sign-loss explanations from real casts, preserving first physical lines."""
import csv
import json
from pathlib import Path
import subprocess
import sys

import pytest

from datawrench.seedtypes.core import infer
from datawrench.seedtypes.csvio import BATCH_ROWS
from datawrench.seedtypes.emit import emit
from .test_cli_check import cli


REASON = 'casting negative zero to BIGINT removes the minus sign'


@pytest.mark.parametrize('prefix,zero,line', [
    ('', '-0', 2), ('12\n0\n-7\n', '-0', 5), ('12\n', '"-0"', 3),
])
def test_bigint_negative_zero_reason_and_actual_evidence(tmp_path, prefix, zero, line):
    path = tmp_path / 'integer.csv'
    path.write_text('integer\n' + prefix + zero + '\n')
    private = infer(path).columns[0]
    expected = {'line': line, 'code': 'integer_negative_zero', 'reason': REASON}
    assert private.type == 'VARCHAR'
    assert private.rejected['BIGINT'] == expected
    assert all(set(d) == {'line', 'code', 'reason'} for d in private.rejected.values())
    assert private.rejected['DECIMAL']['code'] == 'decimal_negative_zero'
    shown = infer(path, show_examples=True).columns[0]
    assert shown.rejected['BIGINT'] == {**expected, 'value': '-0', 'rendered': '0'}


@pytest.mark.parametrize('values,expected,code', [
    (['0', '-7', '9223372036854775807', '-9223372036854775808'], 'BIGINT', None),
    (['00'], 'VARCHAR', 'leading_zeros'),
    (['-00'], 'VARCHAR', 'leading_zeros'),
    (['-0.0'], 'VARCHAR', 'round_trip'),
    (['+0'], 'VARCHAR', 'round_trip'),
    ([' -0'], 'VARCHAR', 'round_trip'),
    (['9223372036854775808'], 'DECIMAL(19,0)', 'integer_range'),
    (['-9223372036854775809'], 'DECIMAL(19,0)', 'integer_range'),
])
def test_bigint_negative_zero_keeps_other_boundaries(tmp_path, values, expected, code):
    path = tmp_path / 'boundary.csv'
    path.write_text('integer\n' + '\n'.join(values) + '\n')
    column = infer(path).columns[0]
    assert column.type == expected
    if code is None:
        assert 'BIGINT' not in column.rejected
    else:
        assert (column.rejected['BIGINT']['code'], column.rejected['BIGINT']['line']) == (code, 2)


@pytest.mark.parametrize('all_null', [False, True])
def test_bigint_negative_zero_designated_nulls_are_excluded(tmp_path, all_null):
    path = tmp_path / 'null.csv'
    path.write_text('integer\n"-0"\n-0\n' + ('' if all_null else '0\n-7\n'))
    column = infer(path, ['-0']).columns[0]
    assert (column.type, column.nulls, column.non_null) == (
        'VARCHAR' if all_null else 'BIGINT', 2, 0 if all_null else 2)
    assert 'BIGINT' not in column.rejected and 'DECIMAL' not in column.rejected


@pytest.mark.parametrize('other,code', [
    ('007', 'leading_zeros'), ('-00', 'leading_zeros'),
    ('9223372036854775808', 'integer_range'),
    ('-9223372036854775809', 'integer_range'),
    ('+1', 'round_trip'), ('1e0', 'round_trip'), ('private-text', 'cast_failed'),
])
@pytest.mark.parametrize('zero_first', [False, True])
@pytest.mark.parametrize('gap', [0, BATCH_ROWS])
def test_bigint_negative_zero_keeps_first_rejection(tmp_path, other, code, zero_first, gap):
    first, second = ('-0', other) if zero_first else (other, '-0')
    path = tmp_path / 'precedence.csv'
    path.write_text('integer\n12\n' + first + '\n' + '12\n' * gap + second + '\n')
    column = infer(path, show_examples=True).columns[0]
    rejection = column.rejected['BIGINT']
    assert column.type == 'VARCHAR'
    assert (rejection['code'], rejection['line'], rejection['value']) == (
        'integer_negative_zero' if zero_first else code, 3, first)
    if zero_first:
        assert (rejection['reason'], rejection['rendered']) == (REASON, '0')


def test_late_bigint_negative_zero_after_multiline_has_exact_line(tmp_path):
    path = tmp_path / 'late.csv'
    script = Path(__file__).resolve().parents[2] / 'examples' / 'seedtypes' / 'make_integer_negative_zero_fixture.py'
    with path.open('w') as stream:
        subprocess.run([sys.executable, str(script)], stdout=stream, check=True, timeout=30)
    with path.open(newline='') as stream:
        reader = csv.reader(stream)
        assert next(reader) == ['integer', 'note']
        end = reader.line_num
        for row in reader:
            if row[0] == '-0':
                assert end + 1 == 21000
            end = reader.line_num
    result = infer(path)
    column = result.columns[0]
    assert (result.rows, column.type, column.non_null) == (25000, 'VARCHAR', 25000)
    expected = {'line': 21000, 'code': 'integer_negative_zero', 'reason': REASON}
    assert column.rejected['BIGINT'] == expected
    assert '"-0"' not in emit([result], 'json')
    shown = infer(path, show_examples=True).columns[0]
    assert shown.rejected['BIGINT'] == {**expected, 'value': '-0', 'rendered': '0'}


@pytest.mark.parametrize('command,extra', [
    ('infer', ['--format', 'json']), ('infer', []), ('emit', ['--target', 'json']),
    ('compare', ['--format', 'json']), ('check', ['--format', 'json']),
])
def test_bigint_negative_zero_cli_evidence_requires_opt_in(tmp_path, command, extra):
    path = tmp_path / 'seeds' / 'integer.csv'
    path.parent.mkdir()
    path.write_text('integer\n12\n-0\n-0\n')
    (tmp_path / 'dbt_project.yml').write_text('name: private\n')
    target = tmp_path if command == 'check' else path
    for show in [False, True]:
        result = cli(command, target, *extra, *(['--show-examples'] if show else []))
        assert result.returncode == (1 if command == 'check' else 0), result.stderr
        assert REASON in result.stdout
        if not show:
            assert '"-0"' not in result.stdout + result.stderr
            assert "'-0'" not in result.stdout + result.stderr
        if 'json' in extra:
            data = json.loads(result.stdout)
            if command == 'check':
                data = data['seeds'][0]['comparison']
            if command in ('check', 'compare'):
                data = data['inference']
            else:
                data = data[0]
            detail = {'line': 3, 'code': 'integer_negative_zero', 'reason': REASON}
            if show:
                detail.update(value='-0', rendered='0')
            assert data['columns'][0]['rejected']['BIGINT'] == detail
        elif show:
            assert "'-0' -> '0'" in result.stdout
