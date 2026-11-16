"""Saved eager row provenance reports at consumption names, never creation."""
import ast
import textwrap

import pytest

from datawrench.polars_vet import analyze
from datawrench.polars_vet.analysis import RULES, analyze_with_suppressions
from datawrench.polars_vet.plugin import PolarsVetChecker

HEADER = 'import polars as pl\ndf = pl.read_csv("x.csv")\n'
SAVED = HEADER + 'rows = df.iter_rows()\n'
LOOP = 'for row in rows:\n    pass\n'


@pytest.mark.parametrize('method', ['iter_rows(named=True)', 'rows()', 'to_dicts()'])
@pytest.mark.parametrize('iterator', ['rows', 'enumerate(rows)', 'enumerate(rows, 1)', 'enumerate(rows, start=offset)'])
@pytest.mark.parametrize('template', [
    'for row in {iterator}:\n    pass',
    '[row for row in {iterator}]', '{{row for row in {iterator}}}',
    '{{i: row for i, row in {iterator}}}', '(row for row in {iterator})',
])
def test_saved_row_consumption(method, iterator, template):
    source = HEADER + f'rows = df.{method}\n' + template.format(iterator=iterator)
    diagnostic, = analyze(source)
    assert diagnostic.code == 'PLV003'
    assert diagnostic.message == RULES['PLV003'] and diagnostic.edits == ()
    assert diagnostic.line == diagnostic.end_line == 4
    line = source.splitlines()[3]
    assert line[diagnostic.column - 1:diagnostic.end_column - 1] == 'rows'


@pytest.mark.parametrize('setup', [
    'rows = df.rows()', 'rows: object = df.rows()', 'rows = other = df.rows()',
    'original = df.rows()\nrows = original',
    'original = df.rows()\nrows: object = original',
    '(rows := df.rows())',
    'from polars import read_csv as read\nframe = read("x.csv")\ncopy = frame\nrows = copy.to_dicts()',
    'p = pl\nframe = p.DataFrame()\nrows = frame.iter_rows()',
    'rows = pl.scan_csv("x.csv").collect().filter(pl.col("x") > 0).rows()',
    'rows = df.rows()\ndf = unknown',
    'original = df.rows()\nrows = original\noriginal = unknown',
    'if ready:\n    rows = df.rows()\nelse:\n    rows = df.to_dicts()',
])
def test_simple_assignments_aliases_and_retained_values(setup):
    assert analyze(HEADER + setup + '\n', selected={'PLV003'}) == []
    assert [d.code for d in analyze(HEADER + setup + '\n' + LOOP, selected={'PLV003'})] == ['PLV003']


@pytest.mark.parametrize('setup', [
    'rows = unknown.iter_rows()', 'rows = pl.scan_csv("x.csv").rows()',
    'rows = pl.LazyFrame().to_dicts()', 'rows = df.unknown().iter_rows()',
    'rows = helper(df.iter_rows())', 'rows = list(df.rows())',
    'rows = enumerate(df.rows())', 'rows = iter(df.rows())',
    'fn = df.rows\nrows = fn()', 'rows, other = df.rows()',
    '[rows] = df.rows()', 'box.rows = df.rows()\nrows = box.rows',
    'box[0] = df.rows()\nrows = box[0]',
    'def helper():\n    return df.rows()\nrows = helper()',
    'read = pl.read_csv\nread = unknown\nrows = read("x.csv").rows()',
    'p = pl\np.read_csv = unknown\nrows = pl.read_csv("x.csv").rows()',
])
def test_unknown_sources_do_not_report(setup):
    assert analyze(HEADER + setup + '\n' + LOOP) == []


@pytest.mark.parametrize('mutation', [
    'rows = unknown', 'rows: object = unknown', 'rows: object', 'rows += other',
    '(rows := unknown)', 'del rows', 'del rows, df',
    'import helpers as rows', 'from helpers import rows', 'from helpers import *',
    'if ready:\n    rows = unknown',
    'if ready:\n    rows = df.rows()\nelse:\n    rows = unknown',
    'for rows in things:\n    pass', 'for x in things:\n    rows = unknown',
    'while ready:\n    rows = unknown',
    'try:\n    rows = df.rows()\nexcept Exception:\n    pass',
    'try:\n    pass\nexcept Exception as rows:\n    pass',
    'try:\n    pass\nfinally:\n    rows = unknown',
    'with context() as rows:\n    pass',
    'match obj:\n    case {"value": rows}:\n        pass',
    'def rows():\n    pass', 'class rows:\n    pass',
])
def test_binding_invalidation(mutation):
    assert analyze(SAVED + mutation + '\n' + LOOP) == []


@pytest.mark.parametrize('body', [
    'def f(rows):\n' + textwrap.indent(LOOP, '    '),
    'def f(*rows):\n' + textwrap.indent(LOOP, '    '),
    'def f(**rows):\n' + textwrap.indent(LOOP, '    '),
    'def f():\n' + textwrap.indent(LOOP + 'rows = unknown\n', '    '),
    'def f():\n    global rows\n' + textwrap.indent(LOOP, '    '),
    'lambda rows: [r for r in rows]',
    '[r for rows in things for r in rows]',
    '[r for x in things for r in rows for rows in things]',
])
def test_local_shadowing(body):
    assert analyze(SAVED + body) == []


@pytest.mark.parametrize('body', [
    '[rows for rows in rows]', '[r for x in things for r in rows]',
    '[0 for rows in things]\n' + LOOP,
    'def f(rows):\n    pass\n' + LOOP,
    'def f():\n    rows = df.rows()\n' + textwrap.indent(LOOP, '    '),
    'async def f():\n    async for row in rows:\n        pass',
])
def test_scope_restore_and_local_creation(body):
    assert [d.code for d in analyze(SAVED + body)] == ['PLV003']


@pytest.mark.parametrize('iterator', [
    'wrapper(rows)', 'zip(rows)', 'iter(iter(rows))', 'list(rows)',
    'enumerate(wrapper(rows))', 'enumerate(enumerate(rows))',
    'enumerate(*[rows])', 'enumerate(rows, **options)', 'enumerate(iterable=rows)',
    'enumerate(rows, stop=1)', 'builtins.enumerate(rows)', 'enum(rows)', '(copy := rows)',
])
def test_unsupported_consumption(iterator):
    assert analyze(SAVED + f'for item in {iterator}:\n    pass') == []


@pytest.mark.parametrize('mutation', [
    'enumerate = unknown', 'from builtins import enumerate',
    'import helpers as enumerate', 'del enumerate',
    'if ready:\n    enumerate = unknown', 'for enumerate in things:\n    pass',
    'try:\n    enumerate = unknown\nexcept Exception:\n    pass',
])
def test_enumerate_shadow_keeps_direct_consumption(mutation):
    source = SAVED + mutation + '\nfor i, row in enumerate(rows):\n    pass\n' + LOOP
    finding, = analyze(source)
    assert finding.line == len(source.splitlines()) - 1


def test_unicode_span_multiline_suppression_and_plugin():
    comment = '  # polars-vet: ignore[PLV003] -- intentional'
    source = SAVED + '文本 = "你好"; result = [r for r in rows]\n'
    diagnostic, = analyze(source)
    column = source.splitlines()[3].index('rows') + 1
    assert (diagnostic.line, diagnostic.column, diagnostic.end_line, diagnostic.end_column) == (4, column, 4, column + 4)
    # Creation and outer wrapper comments cannot suppress a later name line.
    multiline = SAVED.rstrip() + comment + '\nfor item in enumerate(' + comment + '\n    rows, start=1):\n    pass\n'
    result = analyze_with_suppressions(multiline)
    assert [(d.line, d.column, d.end_column) for d in result.diagnostics] == [(5, 5, 9)]
    assert [s.status for s in result.suppressions] == ['unused', 'unused']
    suppressed = multiline.replace('rows, start=1):', 'rows,' + comment + '\n    start=1):')
    result = analyze_with_suppressions(suppressed)
    assert result.diagnostics == []
    assert [s.status for s in result.suppressions] == ['unused', 'unused', 'used']
    assert analyze(source, selected={'PLV001'}) == []
    assert analyze(source, selected={'PLV003'}) == [diagnostic]
    findings = list(PolarsVetChecker(ast.parse(source), 'rows.py', source.splitlines(keepends=True)).run())
    assert [(f[0], f[1], f[2]) for f in findings] == [(4, column - 1, 'PLV003 ' + RULES['PLV003'])]


@pytest.mark.parametrize('comment', ['# noqa', '# noqa: PLV003', '# polars-vet: ignore[PLV003] -- intentional'])
def test_suppression_use_start_line(comment):
    assert analyze(SAVED + LOOP.replace(':\n', ':  ' + comment + '\n'), report_unused_suppressions=True) == []


def test_repeated_uses_and_no_creation_or_wrapper_duplicates():
    source = SAVED + LOOP + '[r for i, r in enumerate(rows, start=1)]\n'
    assert [(d.code, d.line) for d in analyze(source)] == [('PLV003', 4), ('PLV003', 6)]


@pytest.mark.parametrize('iteration', [LOOP, '[r for i, r in enumerate(rows, start=1)]\n'])
def test_baseline_identity_status_and_path_counts(tmp_path, iteration):
    from .test_baseline import report, run

    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src/job.py'
    source.write_text(SAVED + iteration)
    flags = ('--select', 'PLV003', '--show-baseline-path-counts', '--show-baseline-rule-counts')
    created = report(run(tmp_path, *flags, '--update-baseline'), 0)
    assert [d['baselineStatus'] for d in created['diagnostics']] == ['new']
    baseline = tmp_path / 'baseline.json'
    saved = baseline.read_bytes()
    source.write_text('# shifted\n' + SAVED + iteration.replace('rows', '( rows )'))
    existing = report(run(tmp_path, *flags), 0)
    assert [d['baselineStatus'] for d in existing['diagnostics']] == ['existing']
    counts = dict(existing=1, new=0, resolved=0, uncertain=0, error=0)
    assert existing['baseline']['counts'] == counts
    assert existing['baseline']['pathCounts'] == [dict(path='src/job.py', **counts)]
    assert existing['baseline']['ruleCounts'] == [dict(code='PLV003', **counts)]
    # For a multiline loop the diagnostic starts at the for line.
    source.write_text(SAVED + (iteration.replace(':\n', ':  # noqa: PLV003\n') if iteration == LOOP else iteration.rstrip() + '  # noqa: PLV003\n'))
    resolved = report(run(tmp_path, *flags), 0)
    assert [d['baselineStatus'] for d in resolved['diagnostics']] == ['resolved']
    assert resolved['baseline']['pathCounts'][0]['resolved'] == 1
    assert baseline.read_bytes() == saved


def test_repeated_identical_baseline_uses_remain_uncertain(tmp_path):
    from .test_baseline import report, run

    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/job.py').write_text(SAVED + LOOP + LOOP)
    result = report(run(tmp_path, '--select', 'PLV003', '--show-baseline-path-counts', '--update-baseline'), 1)
    assert [d['baselineStatus'] for d in result['diagnostics']] == ['uncertain', 'uncertain']
    assert result['baseline']['pathCounts'][0]['uncertain'] == 2
    assert not (tmp_path / 'baseline.json').exists()
