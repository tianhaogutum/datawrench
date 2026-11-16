"""DECIMAL sign-loss explanation with real casts and original physical lines."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from datawrench.seedtypes.core import infer
from datawrench.seedtypes.csvio import BATCH_ROWS
from datawrench.seedtypes.emit import emit
from .test_cli_check import cli


REASON = 'casting negative zero to DECIMAL removes the minus sign'


@pytest.mark.parametrize('value', ['-0', '-0.0', '-0.00', '-0.000000', '-0.' + '0' * 37])
def test_negative_zero_specific_reason_and_actual_evidence(tmp_path, value):
    path = tmp_path / 'amount.csv'
    path.write_text(f'amount\n{value}\n')
    private = infer(path).columns[0]
    assert private.type == 'VARCHAR'
    expected = {'line': 2, 'code': 'decimal_negative_zero', 'reason': REASON}
    assert private.rejected['DECIMAL'] == expected
    assert all(set(d) == {'line', 'code', 'reason'} for d in private.rejected.values())
    shown = infer(path, show_examples=True).columns[0]
    assert shown.rejected['DECIMAL'] == {**expected, 'value': value, 'rendered': value[1:]}


@pytest.mark.parametrize('value,expected,code', [
    ('0.00', 'DECIMAL(3,2)', None),
    ('-0.10', 'DECIMAL(3,2)', None),
    ('-0.000001', 'DECIMAL(7,6)', None),
    ('-00.00', 'VARCHAR', 'leading_zeros'),
    ('-.00', 'VARCHAR', 'decimal_notation'),
    ('-0.' + '0' * 38, 'VARCHAR', 'decimal_integer_zero'),
    ('-0.' + '0' * 39, 'VARCHAR', 'decimal_precision'),
])
def test_negative_zero_explanation_keeps_other_boundaries(tmp_path, value, expected, code):
    path = tmp_path / 'boundary.csv'
    path.write_text(f'amount\n{value}\n')
    column = infer(path).columns[0]
    assert column.type == expected
    if code is None:
        assert 'DECIMAL' not in column.rejected
    else:
        rejection = column.rejected['DECIMAL']
        assert (rejection['code'], rejection['line']) == (code, 2)


@pytest.mark.parametrize('value', ['-0', '-0.0', '-0.00', '-0.000000', '-0.' + '0' * 38])
@pytest.mark.parametrize('all_null', [False, True])
def test_negative_zero_nulls_are_excluded(tmp_path, value, all_null):
    path = tmp_path / 'null.csv'
    # Include both quoted and unquoted designated nulls.
    path.write_text(f'amount\n"{value}"\n{value}\n' + ('' if all_null else '0.00\n-0.10\n'))
    column = infer(path, [value]).columns[0]
    assert (column.type, column.nulls, column.non_null) == (
        'VARCHAR' if all_null else 'DECIMAL(3,2)', 2, 0 if all_null else 2)
    assert 'DECIMAL' not in column.rejected


@pytest.mark.parametrize('other,code', [
    ('9' * 39, 'decimal_precision'),
    ('1.2', 'decimal_mixed_scales'),
    ('1e0', 'decimal_notation'),
])
@pytest.mark.parametrize('zero_first', [False, True])
@pytest.mark.parametrize('gap', [0, BATCH_ROWS])
def test_negative_zero_keeps_first_rejection(tmp_path, other, code, zero_first, gap):
    first, second = ('-0.00', other) if zero_first else (other, '-0.00')
    path = tmp_path / 'precedence.csv'
    path.write_text('amount\n1.00\n' + first + '\n' + '1.00\n' * gap + second + '\n')
    rejection = infer(path).columns[0].rejected['DECIMAL']
    assert (rejection['code'], rejection['line']) == (
        'decimal_negative_zero' if zero_first else code, 3)


def test_late_negative_zero_after_multiline_has_exact_line(tmp_path):
    path = tmp_path / 'late.csv'
    script = Path(__file__).resolve().parents[2] / 'examples' / 'seedtypes' / 'make_negative_zero_fixture.py'
    with path.open('w') as stream:
        subprocess.run([sys.executable, str(script)], stdout=stream, check=True, timeout=30)
    result = infer(path)
    column = result.columns[0]
    assert (result.rows, column.type, column.non_null) == (25000, 'VARCHAR', 25000)
    expected = {'line': 21000, 'code': 'decimal_negative_zero', 'reason': REASON}
    assert column.rejected['DECIMAL'] == expected
    assert '-0.00' not in emit([result], 'json')
    shown = infer(path, show_examples=True).columns[0]
    assert shown.rejected['DECIMAL'] == {**expected, 'value': '-0.00', 'rendered': '0.00'}


@pytest.mark.parametrize('command,extra', [
    ('infer', ['--format', 'json']), ('infer', []), ('emit', ['--target', 'json'])])
def test_negative_zero_cli_evidence_requires_opt_in(tmp_path, command, extra):
    path = tmp_path / 'private.csv'
    path.write_text('amount\n1.00\n-0.00\n-0.00\n')
    private = cli(command, path, *extra)
    assert private.returncode == 0, private.stderr
    assert REASON in private.stdout and '-0.00' not in private.stdout + private.stderr
    shown = cli(command, path, *extra, '--show-examples')
    assert shown.returncode == 0, shown.stderr
    assert '-0.00' in shown.stdout and '0.00' in shown.stdout
    if 'json' in extra:
        column = json.loads(shown.stdout)[0]['columns'][0]
        assert column['rejected']['DECIMAL'] == {
            'line': 3, 'code': 'decimal_negative_zero', 'reason': REASON,
            'value': '-0.00', 'rendered': '0.00'}
