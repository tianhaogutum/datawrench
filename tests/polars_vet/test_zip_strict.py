"""Only a sole literal-boolean strict keyword extends supported builtin zip."""
import ast
import textwrap

import pytest

from datawrench.polars_vet import analyze
from datawrench.polars_vet.analysis import RULES, analyze_with_suppressions
from datawrench.polars_vet.plugin import PolarsVetChecker
from .test_zip import HEADER, spans


@pytest.mark.parametrize('strict', ['True', 'False'])
@pytest.mark.parametrize('args, expected', [
    ('rows, labels', ['rows']), ('labels, rows', ['rows']),
    ('df.iter_rows(), labels', ['df.iter_rows()']),
    ('labels, df.rows(named=True)', ['df.rows(named=True)']),
    ('df.to_dicts(), rows', ['df.to_dicts()', 'rows']),
    ('rows, rows', ['rows', 'rows']),
    ('wrapper(rows), df.rows()', ['df.rows()']),
])
@pytest.mark.parametrize('template', [
    'for item in {iterator}:\n    pass',
    '[row for row, label in {iterator}]',
    '{{label: row for row, label in {iterator}}}',
    '{{label for row, label in {iterator}}}',
    '(row for row, label in {iterator})',
])
def test_literal_boolean_strict(strict, args, expected, template):
    source = HEADER + template.format(iterator=f'zip({args}, strict={strict})')
    findings = analyze(source)
    assert spans(source, findings) == expected
    assert all(d.code == 'PLV003' and d.message == RULES['PLV003'] and d.edits == () for d in findings)


@pytest.mark.parametrize('iterator', [
    'zip(rows, labels, strict=flag)', 'zip(rows, labels, strict=explode())',
    'zip(rows, labels, strict=1)', 'zip(rows, labels, strict=0)',
    'zip(rows, labels, strict=1.0)', 'zip(rows, labels, strict="True")',
    'zip(rows, labels, strict=None)', 'zip(rows, labels, strict=[])',
    'zip(rows, labels, strict=not False)', 'zip(rows, labels, strict=(flag := True))',
    'zip(rows, labels, unknown=True)', 'zip(rows, labels, strict=True, other=False)',
    'zip(rows, labels, strict=False, **{})', 'zip(rows, labels, **{"strict": True})',
    'zip(strict=True)', 'zip(rows, strict=False)', 'zip(rows, labels, other, fourth, strict=True)',
    'zip(*[rows, labels], strict=True)', 'zip(rows, *labels, strict=False)',
    'builtins.zip(rows, labels, strict=True)', 'alias(rows, labels, strict=False)',
    'enumerate(zip(rows, labels, strict=True))', 'iter(zip(rows, labels, strict=False))',
    'zip(iter(rows), enumerate(df.rows()), strict=True)',
    'zip(zip(rows, labels), wrapper(df.rows()), strict=False)',
    'zip(unknown.rows(), pl.LazyFrame().rows(), strict=True)',
])
def test_rejected_strict_signatures_and_sources(iterator):
    assert analyze(HEADER + f'alias = zip\nfor pair in {iterator}:\n    pass\n') == []


def test_duplicate_strict_retains_syntax_error():
    source = HEADER + 'for pair in zip(rows, labels, strict=True, strict=False):\n    pass\n'
    assert [d.code for d in analyze(source)] == ['PLV000']


@pytest.mark.parametrize('strict', ['True', 'False'])
@pytest.mark.parametrize('setup', [
    'zip = custom', 'del zip', 'from builtins import zip', 'from helpers import zip',
    'if ready:\n    zip = custom', 'for zip in things:\n    pass',
    'try:\n    zip = custom\nexcept Exception:\n    pass',
    'rows = unknown', 'if ready:\n    rows = unknown',
    'from helpers import *\nimport polars as pl\ndf = pl.DataFrame()\nrows = df.rows()',
])
def test_existing_proof_invalidation(strict, setup):
    assert analyze(HEADER + setup + f'\nfor pair in zip(rows, labels, strict={strict}):\n    pass') == []


@pytest.mark.parametrize('strict', ['True', 'False'])
def test_local_binding_and_comprehension_scope(strict):
    loop = f'for pair in zip(rows, labels, strict={strict}):\n    pass\n'
    for body in [
        'def f(zip):\n' + textwrap.indent(loop, '    '),
        'def f():\n' + textwrap.indent(loop + 'zip = custom\n', '    '),
        f'lambda zip: [p for p in zip(rows, labels, strict={strict})]',
        f'[p for zip in things for p in zip(rows, labels, strict={strict})]',
        f'[p for rows in things for p in zip(rows, labels, strict={strict})]',
    ]:
        assert analyze(HEADER + body) == []
    source = HEADER + f'[zip for zip in zip(rows, labels, strict={strict})]\n' + loop
    assert spans(source, analyze(source)) == ['rows', 'rows']


@pytest.mark.parametrize('strict', ['True', 'False'])
def test_creation_stays_silent(strict):
    source = HEADER + f'pairs = zip(rows, df.rows(), strict={strict})\nfor pair in pairs:\n    pass\n'
    assert analyze(source) == []


@pytest.mark.parametrize('strict', ['True', 'False'])
def test_unicode_plugin_selection_and_inner_suppression(strict):
    source = HEADER + f'文本 = "你好"; result = [p for p in zip(rows, df.rows(), strict={strict})]\n'
    findings = analyze(source)
    assert spans(source, findings) == ['rows', 'df.rows()']
    assert analyze(source, selected={'PLV003'}) == findings
    assert analyze(source, selected={'PLV001'}) == []
    plugin = list(PolarsVetChecker(ast.parse(source), 'rows.py', source.splitlines(keepends=True)).run())
    assert [(f[0], f[1], f[2]) for f in plugin] == [(d.line, d.column - 1, 'PLV003 ' + RULES['PLV003']) for d in findings]
    comment = '  # polars-vet: ignore[PLV003] -- intentional'
    multiline = HEADER.rstrip() + comment + '\nfor pair in zip(' + comment + f'\n    rows,\n    df.rows(), strict={strict}):\n    pass\n'
    result = analyze_with_suppressions(multiline)
    assert [(d.line, d.column, d.end_column) for d in result.diagnostics] == [(5, 5, 9), (6, 5, 14)]
    assert [s.status for s in result.suppressions] == ['unused', 'unused']
    result = analyze_with_suppressions(multiline.replace('rows,\n', 'rows,' + comment + '\n'))
    assert [d.line for d in result.diagnostics] == [6]
    assert [s.status for s in result.suppressions] == ['unused', 'unused', 'used']
    assert analyze(source.rstrip() + '  # noqa: PLV003\n') == []


@pytest.mark.parametrize('strict', ['True', 'False'])
def test_baseline_statuses_counts_and_read_only(strict, tmp_path):
    from .test_baseline import report, run

    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src/job.py'
    iteration = f'for pair in zip(rows, df.rows(), strict={strict}):\n    pass\n'
    source.write_text(HEADER + iteration)
    flags = ('--select', 'PLV003', '--show-baseline-path-counts', '--show-baseline-rule-counts')
    created = report(run(tmp_path, *flags, '--update-baseline'), 0)
    assert [d['baselineStatus'] for d in created['diagnostics']] == ['new', 'new']
    baseline = tmp_path / 'baseline.json'
    saved = baseline.read_bytes()
    source.write_text('# moved\n' + HEADER + iteration.replace('zip(', 'zip( '))
    before = source.read_bytes()
    existing = report(run(tmp_path, *flags), 0)
    counts = dict(existing=2, new=0, resolved=0, uncertain=0, error=0)
    assert [d['baselineStatus'] for d in existing['diagnostics']] == ['existing', 'existing']
    assert existing['baseline']['counts'] == counts
    assert existing['baseline']['pathCounts'] == [dict(path='src/job.py', **counts)]
    assert existing['baseline']['ruleCounts'] == [dict(code='PLV003', **counts)]
    assert source.read_bytes() == before and baseline.read_bytes() == saved
    source.write_text(HEADER + iteration.replace(':\n', ':  # noqa: PLV003\n'))
    resolved = report(run(tmp_path, *flags), 0)
    assert [d['baselineStatus'] for d in resolved['diagnostics']] == ['resolved', 'resolved']
    source.write_text(HEADER + f'for pair in zip(rows, rows, strict={strict}):\n    pass\n')
    uncertain = report(run(tmp_path, *flags, '--update-baseline'), 1)
    assert uncertain['baseline']['counts'] == dict(existing=0, new=0, resolved=2, uncertain=2, error=0)
    assert not uncertain['baseline']['updated'] and baseline.read_bytes() == saved
