"""Public explanation contracts using local synthetic CSVs and real pinned readers."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from datawrench.seedtypes.core import infer
from datawrench.seedtypes.emit import emit
from .test_cli_check import cli
from .test_integration import execute_recipes


ZERO_PRECISION_REASON = 'preserving the integer zero at scale 38 requires precision 39; maximum DECIMAL precision is 38'


@pytest.mark.parametrize('value', [
    '0.' + '1' * 38, '-0.' + '1' * 38,
    '0.' + '0' * 38, '0.' + '1' * 37 + '0',
])
def test_scale_38_integer_zero_has_specific_explanation(tmp_path, value):
    path = tmp_path / 'zero.csv'
    path.write_text(f'fraction\n{value}\n')
    column = infer(path, show_examples=True).columns[0]
    assert column.type == 'VARCHAR'
    assert column.rejected['DECIMAL'] == {
        'line': 2, 'code': 'decimal_integer_zero', 'reason': ZERO_PRECISION_REASON,
        'value': value, 'rendered': value.replace('0.', '.', 1)}


@pytest.mark.parametrize('values,expected,code,line', [
    (['0.' + '1' * 37, '-0.' + '2' * 37], 'DECIMAL(38,37)', None, None),
    (['0.' + '0' * 37], 'DECIMAL(38,37)', None, None),
    (['0.' + '1' * 39], 'VARCHAR', 'decimal_precision', 2),
    (['1.' + '1' * 38], 'VARCHAR', 'decimal_precision', 2),
    (['00.' + '1' * 38], 'VARCHAR', 'leading_zeros', 2),
    (['.' + '1' * 38], 'VARCHAR', 'decimal_notation', 2),
    (['0.10', '0.' + '1' * 38], 'VARCHAR', 'decimal_mixed_scales', 3),
    (['0.' + '1' * 38, '0.10'], 'VARCHAR', 'decimal_integer_zero', 2),
    (['-0.00', '0.' + '1' * 38], 'VARCHAR', 'decimal_negative_zero', 2),
    (['text', '0.' + '1' * 38], 'VARCHAR', 'decimal_notation', 2),
])
def test_integer_zero_explanation_keeps_other_boundaries(tmp_path, values, expected, code, line):
    path = tmp_path / 'boundary.csv'
    path.write_text('fraction\n' + '\n'.join(values) + '\n')
    column = infer(path).columns[0]
    assert column.type == expected
    rejection = column.rejected.get('DECIMAL')
    if code is None:
        assert rejection is None
    else:
        assert (rejection['code'], rejection['line']) == (code, line)


def test_scale_38_designated_null_does_not_reject_decimal(tmp_path):
    value = '0.' + '1' * 38
    path = tmp_path / 'null.csv'
    path.write_text(f'fraction\n{value}\n0.10\n')
    column = infer(path, [value]).columns[0]
    assert (column.type, column.nulls, column.non_null) == ('DECIMAL(3,2)', 1, 1)
    assert 'DECIMAL' not in column.rejected


def test_late_scale_38_integer_zero_explains_and_preserves(tmp_path):
    path = tmp_path / 'late_zero.csv'
    script = Path(__file__).resolve().parents[2] / 'examples' / 'seedtypes' / 'make_decimal_zero_fixture.py'
    with path.open('w') as stream:
        subprocess.run([sys.executable, str(script)], stdout=stream, check=True, timeout=30)
    result = infer(path, ['NULL'])
    column = result.columns[0]
    assert (result.rows, column.type, column.nulls, column.non_null) == (25000, 'VARCHAR', 20997, 4003)
    assert column.rejected['DECIMAL'] == {
        'line': 21000, 'code': 'decimal_integer_zero', 'reason': ZERO_PRECISION_REASON}
    assert '0.' + '1' * 38 not in emit([result], 'json')
    shown = infer(path, ['NULL'], show_examples=True).columns[0].rejected['DECIMAL']
    assert shown == {**column.rejected['DECIMAL'],
                     'value': '0.' + '1' * 38, 'rendered': '.' + '1' * 38}
    execute_recipes(path, ['NULL'])


def test_late_precision_and_leading_zeros_explain_and_preserve(tmp_path):
    path = tmp_path / 'late_explanations.csv'
    precise = '1.' + '1234567890' * 3 + '12345678'
    path.write_text('zip,amount\n' + '2134,1.00\n' * 20998 + f'02134,{precise}\n')
    result = infer(path)
    assert [c.type for c in result.columns] == ['VARCHAR', 'VARCHAR']
    zip_col, amount = result.json()['columns']
    assert zip_col['rejected']['BIGINT'] == {
        'line': 21000, 'code': 'leading_zeros', 'reason': 'leading zeros would be removed'}
    assert zip_col['rejected']['DECIMAL']['code'] == 'leading_zeros'
    assert amount['rejected']['DECIMAL'] == {
        'line': 21000, 'code': 'decimal_precision', 'reason': 'exceeds DECIMAL precision/scale 38'}
    assert '02134' not in json.dumps(result.json())
    assert precise not in emit([result], 'json')
    # Both executable pinned outputs must retain the late lexical values.
    execute_recipes(path)


def test_date_string_mixture_has_decisive_line(tmp_path):
    path = tmp_path / 'date_mixture.csv'
    path.write_text('event\n2026-11-11\nprivate-event-name\n')
    column = infer(path).json()['columns'][0]
    assert column['type'] == 'VARCHAR'
    assert column['rejected']['DATE'] == {
        'line': 3, 'code': 'date_notation', 'reason': 'not canonical YYYY-MM-DD date text'}
    assert 'private-event-name' not in json.dumps(column)


def test_opt_in_evidence_is_bounded_and_truncated(tmp_path):
    path = tmp_path / 'private.csv'
    path.write_text('value\n' + 'secret-' + 'x' * 200 + '\n' + 'other-secret\n' * 10)
    private = infer(path)
    assert all(set(d) == {'line', 'code', 'reason'} for d in private.columns[0].rejected.values())
    shown = infer(path, show_examples=True).json()['columns'][0]['rejected']
    assert len(shown) == 5  # One counterexample per candidate, never per input row.
    assert all(d['line'] == 2 and len(d['value']) == 160 for d in shown.values())
    assert all('other-secret' not in json.dumps(d) for d in shown.values())


@pytest.mark.parametrize('command,extra,fmt', [
    ('infer', [], 'text'), ('infer', [], 'json'), ('emit', ['--target', 'json'], 'json')])
def test_cli_private_default_and_explicit_examples(tmp_path, command, extra, fmt):
    path = tmp_path / 'private.csv'
    path.write_text('id\n0012345\n')
    args = [command, path, *extra]
    if command == 'infer':
        args += ['--format', fmt]
    default = cli(*args)
    assert default.returncode == 0, default.stderr
    assert '0012345' not in default.stdout + default.stderr
    shown = cli(*args, '--show-examples')
    assert shown.returncode == 0, shown.stderr
    assert '0012345' in shown.stdout
    if fmt == 'json':
        assert json.loads(default.stdout)[0]['columns'][0]['rejected']['BIGINT']['line'] == 2
    else:
        assert 'leading zeros' in default.stdout and 'line 2' in default.stdout


@pytest.mark.parametrize('command', ['compare', 'check'])
@pytest.mark.parametrize('show', [[], ['--show-examples']])
def test_nested_reports_and_engine_errors_do_not_leak_data(tmp_path, command, show):
    path = tmp_path / 'seeds' / 'late.csv'
    path.parent.mkdir()
    path.write_text('id\n' + '123\n' * 20998 + 'secret-sensitive-value\n')
    (tmp_path / 'dbt_project.yml').write_text('name: private\n')
    result = cli(command, path if command == 'compare' else tmp_path, '--format', 'json', *show)
    assert result.returncode == 1, result.stderr
    if not show:
        assert 'secret-sensitive-value' not in result.stdout + result.stderr
    report = json.loads(result.stdout)
    if command == 'check':
        report = report['seeds'][0]['comparison']
    assert report['engines']['polars']['status'] == 'ERROR'
    assert report['engines']['polars']['error']
    assert 'secret-sensitive-value' not in json.dumps(report['engines'])


@pytest.mark.parametrize('values,candidate,line,code', [
    (['1.00', '-0.00', '1.2'], 'DECIMAL', 3, 'decimal_negative_zero'),
    (['1.00', '1.2'], 'DECIMAL', 3, 'decimal_mixed_scales'),
    (['2026-11-11', '2026-02-30'], 'DATE', 3, 'invalid_calendar'),
    (['9223372036854775808'], 'BIGINT', 2, 'integer_range'),
])
def test_specific_reasons_keep_first_counterexample(tmp_path, values, candidate, line, code):
    path = tmp_path / 'boundary.csv'
    path.write_text('value\n' + '\n'.join(values) + '\n')
    rejection = infer(path).columns[0].rejected[candidate]
    assert rejection['line'] == line and rejection['code'] == code


def test_multiline_record_does_not_shift_private_line_numbers(tmp_path):
    path = tmp_path / 'multiline.csv'
    path.write_bytes(b'zip,note\r\n123,"private\r\nmultiline"\r\n0012345,done\r\n')
    result = cli('infer', path, '--format', 'json')
    assert result.returncode == 0, result.stderr
    col = json.loads(result.stdout)[0]['columns'][0]
    assert col['rejected']['BIGINT']['line'] == 4
    assert col['rejected']['BIGINT']['code'] == 'leading_zeros'
    assert '0012345' not in result.stdout and 'private' not in json.dumps(col)


@pytest.mark.parametrize('command', ['compare', 'check'])
def test_comparison_examples_require_opt_in_and_are_capped(tmp_path, command):
    path = tmp_path / 'seeds' / 'zips.csv'
    path.parent.mkdir()
    path.write_text('zip\n' + '0012345\n' * 10)
    (tmp_path / 'dbt_project.yml').write_text('name: private\n')
    args = [command, path if command == 'compare' else tmp_path, '--format', 'json']
    private = cli(*args)
    assert private.returncode in (0, 1), private.stderr
    assert '0012345' not in private.stdout
    shown = cli(*args, '--show-examples')
    assert shown.returncode in (0, 1), shown.stderr
    report = json.loads(shown.stdout)
    if command == 'check':
        report = report['seeds'][0]['comparison']
    assert len(report['engines']['polars']['columns']['zip']['examples']) == 3
    private_report = json.loads(private.stdout)
    if command == 'check':
        private_report = private_report['seeds'][0]['comparison']
    detail = private_report['engines']['polars']['columns']['zip']
    assert detail['examples'] == [] and detail['mismatches'] == 10
    assert detail['first_mismatch_line'] == 2
    text_report = cli(command, path if command == 'compare' else tmp_path)
    assert '0012345' not in text_report.stdout and 'line 2' in text_report.stdout


def test_bad_csv_remains_an_error_without_partial_report(tmp_path):
    path = tmp_path / 'broken.csv'
    path.write_text('a,b\n0012345,1\nragged\n')
    result = cli('infer', path, '--format', 'json')
    assert result.returncode == 2 and not result.stdout
    assert 'line 3' in result.stderr and '0012345' not in result.stderr
