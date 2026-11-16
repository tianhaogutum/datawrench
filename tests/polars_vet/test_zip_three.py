"""Exactly three explicit zip sources reuse independent eager row proofs."""
import ast
import textwrap

import pytest

from datawrench.polars_vet import analyze
from datawrench.polars_vet.analysis import RULES, analyze_with_suppressions
from datawrench.polars_vet.plugin import PolarsVetChecker
from .test_zip import HEADER, spans


@pytest.mark.parametrize('keyword', ['', ', strict=True', ', strict=False'])
@pytest.mark.parametrize('args, expected', [
    ('rows, labels, weights', ['rows']),
    ('labels, rows, weights', ['rows']),
    ('labels, weights, rows', ['rows']),
    ('labels, df.rows(), rows', ['df.rows()', 'rows']),
    ('df.iter_rows(), df.rows(named=True), df.to_dicts()',
     ['df.iter_rows()', 'df.rows(named=True)', 'df.to_dicts()']),
    ('rows, rows, rows', ['rows', 'rows', 'rows']),
    ('unknown.rows(), pl.LazyFrame().rows(), rows', ['rows']),
    ('iter(rows), rows, enumerate(df.rows())', ['rows']),
    ('zip(rows, labels), wrapper(df.rows()), df.to_dicts()', ['df.to_dicts()']),
])
@pytest.mark.parametrize('template', [
    'for row, label, weight in {iterator}:\n    pass',
    'async def f():\n    async for item in {iterator}:\n        pass',
    '[p for p in {iterator}]', '{{p for p in {iterator}}}',
    '{{p: p for p in {iterator}}}', '(p for p in {iterator})',
])
def test_three_source_zip(keyword, args, expected, template):
    source = HEADER + template.format(iterator=f'zip({args}{keyword})')
    findings = analyze(source)
    assert spans(source, findings) == expected
    assert all(d.code == 'PLV003' and d.message == RULES['PLV003'] and d.edits == () for d in findings)
    assert len({(d.line, d.column, d.end_column) for d in findings}) == len(expected)


@pytest.mark.parametrize('keyword', ['', ', strict=True', ', strict=False'])
@pytest.mark.parametrize('setup', [
    'zip = custom', 'del zip', 'from builtins import zip', 'from helpers import zip',
    'def zip(*args):\n    pass', 'if ready:\n    zip = custom',
    'for zip in things:\n    pass', 'while ready:\n    zip = custom',
    'try:\n    zip = custom\nexcept Exception:\n    pass',
    'from helpers import *\nimport polars as pl\ndf = pl.DataFrame()\nrows = df.rows()',
    'rows = unknown', 'del rows', 'if ready:\n    rows = unknown',
    'for rows in things:\n    pass', 'try:\n    rows = unknown\nexcept Exception:\n    pass',
    'df = unknown\nrows = df.rows()', 'rows = pl.scan_csv("x.csv").rows()',
])
def test_existing_binding_and_source_invalidation(keyword, setup):
    assert analyze(HEADER + setup + f'\nfor p in zip(labels, rows, weights{keyword}):\n    pass') == []


@pytest.mark.parametrize('keyword', ['', ', strict=True', ', strict=False'])
def test_local_scope_and_restore(keyword):
    loop = f'for p in zip(rows, labels, weights{keyword}):\n    pass\n'
    for body in [
        'def f(zip):\n' + textwrap.indent(loop, '    '),
        'def f():\n' + textwrap.indent(loop + 'zip = custom\n', '    '),
        f'lambda zip: [p for p in zip(rows, labels, weights{keyword})]',
        f'[p for zip in things for p in zip(rows, labels, weights{keyword})]',
        f'[p for rows in things for p in zip(rows, labels, weights{keyword})]',
        f'[p for x in things for p in zip(rows, labels, weights{keyword}) for zip in things]',
    ]:
        assert analyze(HEADER + body) == []
    source = HEADER + f'[zip for zip in zip(rows, labels, weights{keyword})]\n' + loop
    assert spans(source, analyze(source)) == ['rows', 'rows']


@pytest.mark.parametrize('iterator', [
    'zip()', 'zip(rows)', 'zip(rows, labels, weights, extra)',
    'zip(rows, labels, weights, extra, fifth, strict=True)',
    'zip(*[rows, labels, weights])', 'zip(rows, *labels, weights, strict=True)',
    'zip(rows, labels, weights, **{})', 'zip(rows, labels, weights, **{"strict": True})',
    'zip(rows, labels, weights, strict=True, **{})',
    'zip(rows, labels, weights, other=True)', 'zip(rows, labels, weights, strict=True, other=False)',
    *[f'zip(rows, labels, weights, strict={value})' for value in
      ['flag', 'explode()', '1', '0', '1.0', '"True"', 'None', '[]', 'not False', '(flag := True)']],
    'builtins.zip(rows, labels, weights)', 'alias(rows, labels, weights, strict=False)',
    'enumerate(zip(rows, labels, weights))', 'iter(zip(rows, labels, weights, strict=True))',
    'zip(iter(rows), enumerate(rows), wrapper(df.rows()))',
    'zip(unknown.rows(), pl.LazyFrame().rows(), pl.scan_csv("x.csv").rows())',
    'zip((copy := rows), labels, weights)',
])
def test_unsupported_signatures_and_wrappers(iterator):
    assert analyze(HEADER + f'alias = zip\nfor p in {iterator}:\n    pass') == []


def test_duplicate_keyword_is_still_a_syntax_error():
    source = HEADER + 'for p in zip(rows, labels, weights, strict=True, strict=False):\n    pass'
    assert [d.code for d in analyze(source)] == ['PLV000']


@pytest.mark.parametrize('keyword', ['', ', strict=True', ', strict=False'])
def test_creation_alias_and_eager_provenance(keyword):
    source = HEADER + f'pairs = zip(rows, df.rows(), df.to_dicts(){keyword})\nfor p in pairs:\n    pass'
    assert analyze(source) == []
    source = HEADER + 'from polars import read_csv as read\nframe = read("x.csv")\ncopy = frame\n'
    source += 'original = copy.filter(pl.col("x") > 0).to_dicts()\nrows: object = original\n'
    source += f'for p in zip(labels, rows, pl.scan_csv("x.csv").collect().iter_rows(){keyword}):\n    pass'
    assert spans(source, analyze(source)) == ['rows', 'pl.scan_csv("x.csv").collect().iter_rows()']


@pytest.mark.parametrize('keyword', ['', ', strict=True', ', strict=False'])
def test_unicode_plugin_selection_and_inner_start_suppression(keyword):
    source = HEADER + f'文本 = "你好"; result = [p for p in zip(rows, df.rows(), rows{keyword})]\n'
    findings = analyze(source)
    assert spans(source, findings) == ['rows', 'df.rows()', 'rows']
    assert analyze(source, selected={'PLV003'}) == findings
    assert analyze(source, selected={'PLV001'}) == []
    plugin = list(PolarsVetChecker(ast.parse(source), 'rows.py', source.splitlines(keepends=True)).run())
    assert [(f[0], f[1], f[2]) for f in plugin] == [(d.line, d.column - 1, 'PLV003 ' + RULES['PLV003']) for d in findings]
    assert analyze(source.rstrip() + '  # noqa: PLV003\n') == []
    comment = '  # polars-vet: ignore[PLV003] -- intentional'
    multiline = HEADER.rstrip() + comment + '\nfor p in zip(' + comment + '\n    rows,\n    df.rows(\n        named=True),\n    rows' + keyword + '):\n    pass'
    result = analyze_with_suppressions(multiline)
    assert [(d.line, d.column, d.end_line, d.end_column) for d in result.diagnostics] == [
        (5, 5, 5, 9), (6, 5, 7, 20), (8, 5, 8, 9)]
    assert [s.status for s in result.suppressions] == ['unused', 'unused']
    suppressed = multiline.replace('df.rows(\n', 'df.rows(' + comment + '\n')
    result = analyze_with_suppressions(suppressed)
    assert [d.line for d in result.diagnostics] == [5, 8]
    assert [s.status for s in result.suppressions] == ['unused', 'unused', 'used']


@pytest.mark.parametrize('keyword', ['', ', strict=True', ', strict=False'])
def test_baseline_classification_counts_and_read_only(keyword, tmp_path):
    from .test_baseline import report, run

    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src/job.py'
    iteration = f'for p in zip(rows, df.rows(), df.to_dicts(){keyword}):\n    pass\n'
    source.write_text(HEADER + iteration)
    flags = ('--select', 'PLV003', '--show-baseline-path-counts', '--show-baseline-rule-counts')
    created = report(run(tmp_path, *flags, '--update-baseline'), 0)
    assert [d['baselineStatus'] for d in created['diagnostics']] == ['new'] * 3
    baseline = tmp_path / 'baseline.json'
    saved = baseline.read_bytes()
    source.write_text('# shifted\n' + HEADER + iteration.replace('zip(', 'zip( '))
    before = source.read_bytes()
    existing = report(run(tmp_path, *flags), 0)
    counts = dict(existing=3, new=0, resolved=0, uncertain=0, error=0)
    assert [d['baselineStatus'] for d in existing['diagnostics']] == ['existing'] * 3
    assert existing['baseline']['counts'] == counts
    assert existing['baseline']['pathCounts'] == [dict(path='src/job.py', **counts)]
    assert existing['baseline']['ruleCounts'] == [dict(code='PLV003', **counts)]
    assert source.read_bytes() == before and baseline.read_bytes() == saved
    source.write_text(HEADER + iteration.replace(':\n', ':  # noqa: PLV003\n'))
    resolved = report(run(tmp_path, *flags), 0)
    assert [d['baselineStatus'] for d in resolved['diagnostics']] == ['resolved'] * 3
    source.write_text(HEADER + f'for p in zip(rows, rows, rows{keyword}):\n    pass\n')
    uncertain = report(run(tmp_path, *flags, '--update-baseline'), 1)
    assert uncertain['baseline']['counts'] == dict(existing=0, new=0, resolved=3, uncertain=3, error=0)
    assert not uncertain['baseline']['updated'] and baseline.read_bytes() == saved
