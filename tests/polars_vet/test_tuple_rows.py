"""Only flat two-name/two-value tuples retain independent saved-row proofs."""
import ast
import textwrap

import pytest

from datawrench.polars_vet import analyze
from datawrench.polars_vet.analysis import RULES, analyze_with_suppressions
from datawrench.polars_vet.plugin import PolarsVetChecker
from .test_saved_rows import HEADER, LOOP
from .test_zip import spans


@pytest.mark.parametrize('method', ['iter_rows(named=True)', 'rows()', 'to_dicts()'])
@pytest.mark.parametrize('assignment', ['rows, labels = df.{method}, ["a"]',
                                        '(labels, rows) = (["a"], df.{method})'])
@pytest.mark.parametrize('iteration', [
    LOOP, 'async def f():\n    async for r in rows:\n        pass',
    '[r for r in rows]', '{r for r in rows}', '{i: r for i, r in enumerate(rows)}',
    '(r for r in iter(rows))', '[p for p in zip(rows, labels)]',
    '[p for p in zip(labels, rows, weights, strict=True)]',
])
def test_tuple_saved_rows(method, assignment, iteration):
    setup = HEADER + assignment.format(method=method) + '\n'
    assert analyze(setup) == []
    source = setup + iteration
    finding, = analyze(source)
    assert spans(source, [finding]) == ['rows']
    assert finding.code == 'PLV003' and finding.message == RULES['PLV003']
    assert finding.edits == ()


@pytest.mark.parametrize('setup, expected', [
    ('rows, other = df.rows(), df.to_dicts()', ['rows', 'other']),
    ('saved = df.rows()\nrows, other = saved, saved', ['rows', 'other']),
    ('rows = df.rows()\nother = unknown\nrows, other = other, rows', ['other']),
    ('other = df.rows()\nrows = unknown\nrows, other = other, rows', ['rows']),
    ('rows = df.rows()\nother = df.to_dicts()\nrows, other = other, rows', ['rows', 'other']),
    ('rows, other = df.rows(), df\nrows, other = other, rows', ['other']),
    ('saved = df.rows()\nrows, other = saved, unknown\nsaved = unknown\ndf = unknown', ['rows']),
    ('rows, other = unknown.rows(), df.rows()', ['other']),
    ('rows, other = pl.LazyFrame().rows(), df.rows()', ['other']),
    ('rows, other = helper(df.rows()), df.rows()', ['other']),
    ('rows, other = enumerate(df.rows()), df.rows()', ['other']),
    ('rows, other = iter(df.rows()), df.rows()', ['other']),
    ('rows, other = (saved := df.rows()), df.rows()', ['other']),
    ('from polars import read_csv as read\nframe = read("x.csv")\ncopy = frame\n'
     'rows, other = copy.filter(pl.col("x") > 0).rows(), unknown', ['rows']),
    ('rows, other = pl.scan_csv("x.csv").collect().iter_rows(), unknown', ['rows']),
    # Capture each evaluated value before any target binding, and before a later
    # sibling can rebind its source name with a walrus.
    ('saved = df.rows()\nrows, other = saved, (saved := unknown)', ['rows']),
    ('saved = unknown\nrows, other = saved, (saved := df.rows())', []),
    ('rows, other = df.rows(), (df := unknown)', ['rows']),
])
def test_independent_proofs_and_pre_binding_values(setup, expected):
    source = HEADER + setup + '\n[p for p in zip(rows, other)]'
    assert spans(source, analyze(source)) == expected


@pytest.mark.parametrize('assignment', [
    '[rows, other] = (df.rows(), df.rows())',
    '(rows, other) = [df.rows(), df.rows()]',
    'rows, *other = df.rows(), df.rows()', 'rows, other = *saved, df.rows()',
    'rows, (other, third) = df.rows(), (df.rows(), df.rows())',
    '(rows, other), third = (df.rows(), df.rows()), df.rows()',
    'rows, other = (df.rows(), df.rows()), unknown',
    'rows, other = df.rows(),', 'rows, other = df.rows(), unknown, unknown',
    'rows, = (df.rows(),)', 'rows, other, third = df.rows(), df.rows(), df.rows()',
    'rows, other = alias = df.rows(), df.rows()',
    'alias = rows, other = df.rows(), df.rows()',
    'rows, rows = df.rows(), df.rows()',
    'rows, box.other = df.rows(), df.rows()',
    'box.rows, other = df.rows(), df.rows()',
    'rows, box[0] = df.rows(), df.rows()',
    'box[0], other = df.rows(), df.rows()',
    'rows, other = tuple((df.rows(), df.rows()))',
    'rows, other = helper()', 'rows, other = df.rows()',
    'rows, other = unknown.rows(), pl.scan_csv("x.csv").rows()',
    'rows, other = list(df.rows()), wrapper(df.rows())',
])
def test_unsupported_shapes_do_not_infer_provenance(assignment):
    source = HEADER + 'saved = df.rows()\n' + assignment
    source += '\n[p for p in zip(rows, other)]'
    assert analyze(source) == []


@pytest.mark.parametrize('assignment, expected', [
    ('[rows, other] = (df.rows(), df.rows())', []),
    ('rows, other = [df.rows(), df.rows()]', []),
    ('rows, rows = df.rows(), df.rows()', ['other']),
    ('rows, box.other = df.rows(), df.rows()', ['other']),
    ('box[0], other = df.rows(), df.rows()', ['rows']),
    ('rows, other = alias = df.rows(), df.rows()', []),
])
def test_unsupported_targets_invalidate_only_written_names(assignment, expected):
    source = HEADER + 'rows = other = df.rows()\n' + assignment
    source += '\n[p for p in zip(rows, other)]'
    assert spans(source, analyze(source)) == expected


@pytest.mark.parametrize('mutation', [
    'rows = unknown', 'rows: object', '(rows := unknown)', 'del rows',
    'rows += other', 'import helpers as rows', 'from helpers import rows',
    'from helpers import *', 'if ready:\n    rows = unknown',
    'for rows in things:\n    pass', 'while ready:\n    rows = unknown',
    'try:\n    rows, other = df.rows(), unknown\nexcept Exception:\n    pass',
    'with context() as rows:\n    pass', 'def rows():\n    pass',
    'match obj:\n    case {"x": rows}:\n        pass',
])
def test_existing_invalidation(mutation):
    source = HEADER + 'rows, other = df.rows(), unknown\n' + mutation + '\n' + LOOP
    assert analyze(source) == []


@pytest.mark.parametrize('body', [
    'def f(rows):\n' + textwrap.indent(LOOP, '    '),
    'def f():\n' + textwrap.indent(LOOP + 'rows, other = unknown, unknown\n', '    '),
    'def f():\n    global rows\n' + textwrap.indent(LOOP, '    '),
    '[r for rows in things for r in rows]',
    '[r for x in things for r in rows for rows in things]',
])
def test_existing_scope_shadowing(body):
    assert analyze(HEADER + 'rows, other = df.rows(), unknown\n' + body) == []


@pytest.mark.parametrize('setup', [
    'if ready:\n    rows, other = df.rows(), unknown\nelse:\n    rows, other = df.to_dicts(), unknown',
    'def f():\n    rows, other = df.rows(), unknown\n' + textwrap.indent(LOOP, '    '),
    'rows, other = df.rows(), unknown\n[rows for rows in rows]',
])
def test_merge_and_local_creation(setup):
    source = HEADER + setup + ('\n' + LOOP if setup.startswith('if') else '')
    assert spans(source, analyze(source)) == ['rows']


def test_other_kinds_are_not_propagated_and_targets_invalidate_builtins():
    source = HEADER + 'frame, module = df, pl\n'
    source += 'for r in frame.rows():\n    pass\nfor r in module.DataFrame().rows():\n    pass\n'
    assert analyze(source) == []
    source = HEADER + 'zip, rows = custom, df.rows()\n[p for p in zip(rows, labels)]\n' + LOOP
    assert spans(source, analyze(source)) == ['rows']
    source = HEADER + 'df, rows = unknown, df.rows()\n' + LOOP
    assert spans(source, analyze(source)) == ['rows']


def test_unicode_plugin_selection_and_suppression():
    setup = HEADER + 'rows, other = df.rows(), df.to_dicts()\n'
    source = setup + '文本 = "你好"; result = [p for p in zip(rows, other, rows)]\n'
    findings = analyze(source)
    assert spans(source, findings) == ['rows', 'other', 'rows']
    assert analyze(source, selected={'PLV003'}) == findings
    assert analyze(source, selected={'PLV001'}) == []
    plugin = list(PolarsVetChecker(ast.parse(source), 'rows.py', source.splitlines(keepends=True)).run())
    assert [(f[0], f[1], f[2]) for f in plugin] == [(d.line, d.column - 1, 'PLV003 ' + RULES['PLV003']) for d in findings]
    assert analyze(source.rstrip() + '  # noqa: PLV003\n') == []
    comment = '  # polars-vet: ignore[PLV003] -- intentional'
    source = setup.rstrip() + comment + '\nfor p in zip(' + comment + '\n    rows,\n    other):\n    pass\n'
    result = analyze_with_suppressions(source)
    assert [d.line for d in result.diagnostics] == [5, 6]
    assert [s.status for s in result.suppressions] == ['unused', 'unused']
    result = analyze_with_suppressions(source.replace('rows,\n', 'rows,' + comment + '\n'))
    assert [d.line for d in result.diagnostics] == [6]
    assert [s.status for s in result.suppressions] == ['unused', 'unused', 'used']


def test_baseline_identity_counts_and_read_only(tmp_path):
    from .test_baseline import report, run

    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src/job.py'
    setup = HEADER + 'rows, other = df.rows(), df.to_dicts()\n'
    iteration = '[p for p in zip(rows, other, strict=False)]\n'
    source.write_text(setup + iteration)
    flags = ('--select', 'PLV003', '--show-baseline-path-counts', '--show-baseline-rule-counts')
    created = report(run(tmp_path, *flags, '--update-baseline'), 0)
    assert [d['baselineStatus'] for d in created['diagnostics']] == ['new'] * 2
    baseline = tmp_path / 'baseline.json'
    saved = baseline.read_bytes()
    source.write_text('# shifted\n' + setup + iteration.replace('zip(', 'zip( '))
    before = source.read_bytes()
    existing = report(run(tmp_path, *flags), 0)
    counts = dict(existing=2, new=0, resolved=0, uncertain=0, error=0)
    assert existing['baseline']['counts'] == counts
    assert existing['baseline']['pathCounts'] == [dict(path='src/job.py', **counts)]
    assert existing['baseline']['ruleCounts'] == [dict(code='PLV003', **counts)]
    assert source.read_bytes() == before and baseline.read_bytes() == saved
    source.write_text(setup + iteration.rstrip() + '  # noqa: PLV003\n')
    resolved = report(run(tmp_path, *flags), 0)
    assert [d['baselineStatus'] for d in resolved['diagnostics']] == ['resolved'] * 2
    source.write_text(setup + '[p for p in zip(rows, rows)]\n')
    uncertain = report(run(tmp_path, *flags, '--update-baseline'), 1)
    assert uncertain['baseline']['counts'] == dict(existing=0, new=0, resolved=2, uncertain=2, error=0)
    assert not uncertain['baseline']['updated'] and baseline.read_bytes() == saved
