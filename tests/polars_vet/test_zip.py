"""One proven builtin zip consumes each direct/saved eager row source."""
import ast
import textwrap

import pytest

from datawrench.polars_vet import analyze
from datawrench.polars_vet.analysis import RULES, analyze_with_suppressions
from datawrench.polars_vet.plugin import PolarsVetChecker

HEADER = 'import polars as pl\ndf = pl.read_csv("x.csv")\nrows = df.iter_rows()\n'
LOOP = 'for row, label in zip(rows, labels):\n    pass\n'


def spans(source, findings):
    return [source.splitlines()[d.line - 1][d.column - 1:d.end_column - 1] for d in findings]


@pytest.mark.parametrize('args, expected', [
    ('rows, labels', ['rows']), ('labels, rows', ['rows']),
    ('df.iter_rows(), labels', ['df.iter_rows()']),
    ('labels, df.rows(named=True)', ['df.rows(named=True)']),
    ('df.to_dicts(), rows', ['df.to_dicts()', 'rows']),
    ('rows, rows', ['rows', 'rows']),
    ('df.rows(), df.to_dicts()', ['df.rows()', 'df.to_dicts()']),
    ('wrapper(rows), df.rows()', ['df.rows()']),
    ('enumerate(rows), rows', ['rows']),
])
@pytest.mark.parametrize('template', [
    'for item in {iterator}:\n    pass',
    '[row for row, label in {iterator}]',
    '{{label: row for row, label in {iterator}}}',
    '{{label for row, label in {iterator}}}',
    '(row for row, label in {iterator})',
])
def test_two_argument_builtin_zip(args, expected, template):
    source = HEADER + template.format(iterator=f'zip({args})')
    findings = analyze(source)
    assert spans(source, findings) == expected
    for d in findings:
        assert d.code == 'PLV003' and d.message == RULES['PLV003']
        assert d.edits == () and d.line == d.end_line


@pytest.mark.parametrize('setup', [
    'from polars import read_csv as read\nframe = read("x.csv")\ncopy = frame\nrows = copy.rows()',
    'p = pl\nframe = p.DataFrame()\noriginal = frame.to_dicts()\nrows: object = original',
    'rows = pl.scan_csv("x.csv").collect().rows()',
    'rows = df.rows()\ndf = unknown',
])
def test_alias_provenance(setup):
    assert [d.code for d in analyze(HEADER + setup + '\n' + LOOP)] == ['PLV003']


@pytest.mark.parametrize('mutation', [
    'zip = custom', 'zip: object', 'zip: object = custom', 'zip += custom',
    '(zip := custom)', 'del zip', 'from helpers import zip',
    'from builtins import zip', 'import helpers as zip',
    'def zip(*args):\n    pass', 'class zip:\n    pass',
    'if ready:\n    zip = custom', 'if ready:\n    zip = custom\nelse:\n    zip = custom',
    'for zip in things:\n    pass', 'for item in things:\n    zip = custom',
    'while ready:\n    zip = custom',
    'try:\n    zip = custom\nexcept Exception:\n    pass',
    'try:\n    pass\nexcept Exception as zip:\n    pass',
    'with context() as zip:\n    pass', 'match obj:\n    case {"value": zip}:\n        pass',
    'from helpers import *\nimport polars as pl\ndf = pl.DataFrame()\nrows = df.rows()',
    'alias = zip\nzip = custom\nzip = alias', 'zip = zip',
])
def test_zip_binding_invalidation(mutation):
    source = HEADER + mutation + '\n' + LOOP + 'for row in rows:\n    pass\n'
    findings = analyze(source)
    assert len(findings) == 1 and spans(source, findings) == ['rows']
    assert findings[0].line == len(source.splitlines()) - 1


@pytest.mark.parametrize('body', [
    'def f(zip):\n' + textwrap.indent(LOOP, '    '),
    'def f(zip, /):\n' + textwrap.indent(LOOP, '    '),
    'def f(*, zip):\n' + textwrap.indent(LOOP, '    '),
    'def f(*zip):\n' + textwrap.indent(LOOP, '    '),
    'def f(**zip):\n' + textwrap.indent(LOOP, '    '),
    'def f():\n' + textwrap.indent(LOOP + 'zip = custom\n', '    '),
    'def f():\n    global zip\n' + textwrap.indent(LOOP, '    '),
    'def f():\n    zip = custom\n    def g():\n        nonlocal zip\n' + textwrap.indent(LOOP, '        '),
    'lambda zip: [r for r in zip(rows, labels)]',
    'lambda: ([r for r in zip(rows, labels)], (zip := custom))',
    '[r for zip in things for r in zip(rows, labels)]',
    '[r for x in things for r in zip(rows, labels) for zip in things]',
    '[r for rows in things for r in zip(rows, labels)]',
])
def test_local_shadowing(body):
    assert analyze(HEADER + body) == []


@pytest.mark.parametrize('body', [
    '[zip for zip in zip(rows, labels)]', '[rows for rows in zip(rows, labels)]',
    '[0 for zip in things]\n' + LOOP, 'def f(zip):\n    pass\n' + LOOP,
    '[r for x in things for r in zip(rows, labels)]',
    '(lambda zip=[r for r in zip(rows, labels)]: None)',
    'async def f():\n    async for row, label in zip(rows, labels):\n        pass',
])
def test_scope_restore_and_first_iterable(body):
    assert [d.code for d in analyze(HEADER + body)] == ['PLV003']


@pytest.mark.parametrize('iterator', [
    'zip()', 'zip(rows)', 'zip(rows, labels, other, fourth)', 'zip(*[rows, labels])',
    'zip(rows, *labels)', 'zip(rows, labels, strict=flag)',
    'zip(rows, labels, **options)', 'zip(rows, other=labels)',
    'zip(first=rows, second=labels)', 'builtins.zip(rows, labels)',
    'alias(rows, labels)', 'wrapper(zip(rows, labels))',
    'enumerate(zip(rows, labels))', 'zip(enumerate(rows), labels)',
    'zip(zip(rows, labels), labels)', 'zip(wrapper(rows), iter(df.rows()))',
    'zip(unknown.rows(), pl.LazyFrame().rows())', 'zip(df, labels)',
    'zip((copy := rows), labels)',
])
def test_unsupported_consumption(iterator):
    assert analyze(HEADER + 'alias = zip\n' + f'for item in {iterator}:\n    pass') == []


@pytest.mark.parametrize('setup', [
    'rows = unknown', 'del rows', 'if ready:\n    rows = unknown',
    'rows = helper(df.rows())', 'rows = pl.LazyFrame().rows()',
    'pl.read_csv = custom\nrows = pl.read_csv("x.csv").rows()',
    'df = unknown\nrows = df.rows()',
])
def test_source_proof_invalidated(setup):
    assert analyze(HEADER + setup + '\n' + LOOP) == []


def test_creation_and_saved_zip_stay_silent():
    source = HEADER + 'pairs = zip(rows, df.rows())\nfor pair in pairs:\n    pass\n'
    assert analyze(source) == []


def test_unicode_suppression_selection_plugin_and_no_outer_duplicate():
    source = HEADER + '文本 = "你好"; result = [p for p in zip(rows, df.rows())]\n'
    findings = analyze(source)
    assert spans(source, findings) == ['rows', 'df.rows()']
    assert analyze(source, selected={'PLV001'}) == []
    assert analyze(source, selected={'PLV003'}) == findings
    plugin = list(PolarsVetChecker(ast.parse(source), 'rows.py', source.splitlines(keepends=True)).run())
    assert [(f[0], f[1], f[2]) for f in plugin] == [(d.line, d.column - 1, 'PLV003 ' + RULES['PLV003']) for d in findings]
    comment = '  # polars-vet: ignore[PLV003] -- intentional'
    multiline = HEADER.rstrip() + comment + '\nfor pair in zip(' + comment + '\n    rows,\n    df.rows()):\n    pass\n'
    result = analyze_with_suppressions(multiline)
    assert [(d.line, d.column, d.end_column) for d in result.diagnostics] == [(5, 5, 9), (6, 5, 14)]
    assert [s.status for s in result.suppressions] == ['unused', 'unused']
    suppressed = multiline.replace('rows,\n', 'rows,' + comment + '\n')
    result = analyze_with_suppressions(suppressed)
    assert [d.line for d in result.diagnostics] == [6]
    assert [s.status for s in result.suppressions] == ['unused', 'unused', 'used']


@pytest.mark.parametrize('comment', ['# noqa', '# noqa: PLV003', '# polars-vet: ignore[PLV003] -- intentional'])
def test_start_line_suppressions(comment):
    assert analyze(HEADER + LOOP.replace(':\n', ':  ' + comment + '\n'), report_unused_suppressions=True) == []


def test_multiline_call_span():
    source = HEADER + '[p for p in zip(\n    df.rows(\n        named=True), rows)]\n'
    assert [(d.line, d.column, d.end_line, d.end_column) for d in analyze(source)] == [(5, 5, 6, 20), (6, 22, 6, 26)]


def test_baseline_statuses_counts_and_read_only_comparison(tmp_path):
    from .test_baseline import report, run

    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src/job.py'
    iteration = 'for pair in zip(rows, df.rows()):\n    pass\n'
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
    assert baseline.read_bytes() == saved
    source.write_text(HEADER + 'for pair in zip(rows, rows):\n    pass\n')
    uncertain = report(run(tmp_path, *flags, '--update-baseline'), 1)
    assert uncertain['baseline']['counts'] == dict(existing=0, new=0, resolved=2, uncertain=2, error=0)
    assert not uncertain['baseline']['updated'] and baseline.read_bytes() == saved
