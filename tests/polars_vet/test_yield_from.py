"""Direct generator delegation consumes proven rows without unwrapping calls."""
import ast
import json
import textwrap

import pytest

from datawrench.polars_vet import analyze
from datawrench.polars_vet.analysis import RULES, analyze_with_suppressions
from datawrench.polars_vet.plugin import PolarsVetChecker
from .test_zip import spans

HEADER = 'import polars as pl\ndf = pl.read_csv("x.csv")\nrows = df.iter_rows()\n'
DELEGATE = 'def generate():\n    yield from rows\n'


@pytest.mark.parametrize('value', ['rows', 'df.iter_rows(named=True)', 'df.rows()', 'df.to_dicts()'])
@pytest.mark.parametrize('context', [
    'def generate():\n    yield from {value}',
    'def generate():\n    return (yield from {value})',
    'def generate():\n    for item in things:\n        yield from {value}',
    'def generate():\n    def inner():\n        yield from {value}',
    'generate = lambda: (yield from {value})',
])
def test_direct_delegation(value, context):
    source = HEADER + context.format(value=value)
    finding, = analyze(source)
    assert spans(source, [finding]) == [value]
    assert finding.code == 'PLV003' and finding.message == RULES['PLV003']
    assert finding.edits == ()


@pytest.mark.parametrize('setup', [
    'from polars import read_csv as read\nframe = read("x.csv")\nrows = frame.rows()',
    'p = pl\nframe = p.DataFrame()\ncopy = frame\nrows = copy.to_dicts()',
    'original = df.rows()\nrows: object = original',
    'copy = rows = df.rows()\ncopy = unknown\ndf = unknown',
    'rows, labels = df.iter_rows(), unknown',
    'other = rows\nrows, labels = other, unknown',
    'other = df.rows()\nrows = unknown\nrows, other = other, rows',
    'rows = pl.scan_csv("x.csv").collect().rows()',
    'rows = df.filter(pl.col("x") > 0).rows()',
    'if ready:\n    rows = df.rows()\nelse:\n    rows = df.to_dicts()',
])
def test_existing_provenance(setup):
    source = HEADER + setup + '\n' + DELEGATE
    assert spans(source, analyze(source)) == ['rows']


@pytest.mark.parametrize('mutation', [
    'rows = unknown', 'rows: object', 'rows += other', '(rows := unknown)',
    'del rows', 'import helpers as rows', 'from helpers import rows',
    'if ready:\n    rows = unknown',
    'for rows in things:\n    pass', 'for item in things:\n    rows = unknown',
    'while ready:\n    rows = unknown',
    'try:\n    rows = unknown\nexcept Exception:\n    pass',
    'try:\n    pass\nexcept Exception as rows:\n    pass',
    'with context() as rows:\n    pass', 'def rows():\n    pass',
    'match obj:\n    case {"x": rows}:\n        pass',
])
def test_existing_invalidation(mutation):
    # Local creation is required before mutation so lexical pre-invalidation
    # cannot by itself make these control-flow assertions pass.
    source = HEADER + 'def generate():\n' + textwrap.indent(
        'rows = df.rows()\n' + mutation + '\nyield from rows\n', '    ')
    assert analyze(source) == []


@pytest.mark.parametrize('body', [
    'def generate(rows):\n    yield from rows',
    'def generate(rows, /):\n    yield from rows',
    'def generate(*, rows):\n    yield from rows',
    'def generate(*rows):\n    yield from rows',
    'def generate(**rows):\n    yield from rows',
    'def generate():\n    yield from rows\n    rows = unknown',
    'def generate():\n    global rows\n    yield from rows',
    'def outer():\n    rows = df.rows()\n    def generate():\n        nonlocal rows\n        yield from rows',
    'generate = lambda rows: (yield from rows)',
    'generate = lambda: ((yield from rows), (rows := unknown))',
    'def generate(df):\n    yield from df.rows()',
    'def generate():\n    yield from df.rows()\n    df = unknown',
])
def test_scope_shadowing(body):
    assert analyze(HEADER + body) == []


@pytest.mark.parametrize('value', [
    'unknown.rows()', 'pl.LazyFrame().rows()', 'pl.scan_csv("x.csv").iter_rows()',
    'df', 'df.rows', 'helper()', 'helper(df.rows())', 'getattr(df, "rows")()',
    'enumerate(rows)', 'iter(rows)', 'zip(rows, labels)', 'list(rows)',
    'wrapper(rows)', 'enumerate(iter(rows))', '(copy := rows)',
    'rows if ready else rows', 'box.rows', 'box[0]',
])
def test_unproven_or_wrapped_values(value):
    assert analyze(HEADER + f'def generate():\n    yield from {value}\n') == []


@pytest.mark.parametrize('setup', [
    'from helpers import *',
    'rows = helper(df.rows())', 'rows = pl.LazyFrame().rows()',
    'rows = unknown.rows()', 'rows = iter(df.rows())',
    'rows = enumerate(df.rows())', 'rows = zip(df.rows(), labels)',
    'df = unknown\nrows = df.rows()',
    'pl.read_csv = custom\nrows = pl.read_csv("x.csv").rows()',
])
def test_unproven_saved_sources(setup):
    assert analyze(HEADER + setup + '\n' + DELEGATE) == []


def test_plain_yield_creation_and_scope_restore():
    assert analyze(HEADER + 'def generate():\n    yield rows\n    yield df.rows()\n') == []
    source = HEADER + 'def other(rows):\n    yield from rows\n' + DELEGATE + DELEGATE.replace('generate', 'again')
    assert spans(source, analyze(source)) == ['rows', 'rows']


def test_value_traversal_without_loop_context_or_duplicate():
    source = HEADER + 'def generate():\n    yield from (r for r in rows)\n'
    assert spans(source, analyze(source)) == ['rows']  # inner comprehension only
    source = HEADER + 'def generate():\n    yield from pl.read_csv("x.csv").lazy()\n'
    assert [d.code for d in analyze(source)] == ['PLV001']
    source = HEADER + 'def generate():\n    yield from df.with_columns(pl.lit(1)).rows()\n'
    assert [d.code for d in analyze(source)] == ['PLV003']  # no PLV004 loop context


@pytest.mark.parametrize('value', ['rows', 'df.rows()'])
def test_unicode_selection_plugin_and_multiline_suppression(value):
    source = HEADER + f'def generate():\n    文本 = "你好"; yield from {value}\n'
    finding, = analyze(source)
    column = source.splitlines()[4].index(value) + 1
    assert (finding.line, finding.column, finding.end_line, finding.end_column) == (5, column, 5, column + len(value))
    assert analyze(source, selected={'PLV003'}) == [finding]
    assert analyze(source, selected={'PLV001'}) == []
    plugin = list(PolarsVetChecker(ast.parse(source), 'rows.py', source.splitlines(keepends=True)).run())
    assert [(d[0], d[1], d[2]) for d in plugin] == [(5, column - 1, 'PLV003 ' + RULES['PLV003'])]
    comment = '  # polars-vet: ignore[PLV003] -- intentional'
    multiline = HEADER.rstrip() + comment + '\ndef generate():\n    yield from (' + comment + f'\n        {value}\n    )\n'
    result = analyze_with_suppressions(multiline)
    assert spans(multiline, result.diagnostics) == [value]
    assert [s.status for s in result.suppressions] == ['unused', 'unused']
    result = analyze_with_suppressions(multiline.replace(value + '\n', value + comment + '\n'))
    assert result.diagnostics == []
    assert [s.status for s in result.suppressions] == ['unused', 'unused', 'used']
    for noqa in ['# noqa', '# noqa: PLV003']:
        assert analyze(source.rstrip() + '  ' + noqa + '\n') == []


def test_multiline_call_span():
    source = HEADER + 'def generate():\n    yield from df.iter_rows(\n        named=True\n    )\n'
    finding, = analyze(source)
    assert (finding.line, finding.column, finding.end_line, finding.end_column) == (5, 16, 7, 6)


@pytest.mark.parametrize('value', ['rows', 'df.rows()'])
def test_baseline_identity_classification_counts_and_read_only(tmp_path, value):
    from .test_baseline import report, run

    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src/job.py'
    body = f'def generate():\n    yield from {value}\n'
    source.write_text(HEADER + body)
    flags = ('--select', 'PLV003', '--show-baseline-path-counts', '--show-baseline-rule-counts')
    created = report(run(tmp_path, *flags, '--update-baseline'), 0)
    assert [d['baselineStatus'] for d in created['diagnostics']] == ['new']
    baseline = tmp_path / 'baseline.json'
    saved = baseline.read_bytes()
    assert set(json.loads(saved)) == {'version', 'policy', 'entries'}
    assert json.loads(saved)['version'] == 1
    source.write_text('# moved\n' + HEADER + body.replace('yield from ', 'yield from (').replace(value + '\n', value + ')\n'))
    before = source.read_bytes()
    existing = report(run(tmp_path, *flags), 0)
    counts = dict(existing=1, new=0, resolved=0, uncertain=0, error=0)
    assert existing['baseline']['counts'] == counts
    assert existing['baseline']['pathCounts'] == [dict(path='src/job.py', **counts)]
    assert existing['baseline']['ruleCounts'] == [dict(code='PLV003', **counts)]
    assert source.read_bytes() == before and baseline.read_bytes() == saved
    source.write_text(HEADER + body.replace('yield from', 'yield'))
    assert report(run(tmp_path, *flags), 0)['baseline']['counts']['resolved'] == 1
    source.write_text(HEADER + body.replace(value, 'df.to_dicts()'))
    changed = report(run(tmp_path, *flags), 1)
    assert changed['baseline']['counts'] == dict(existing=0, new=1, resolved=1, uncertain=0, error=0)
    source.write_text(HEADER + body + f'    yield from {value}\n')
    uncertain = report(run(tmp_path, *flags, '--update-baseline'), 1)
    assert uncertain['baseline']['counts'] == dict(existing=0, new=0, resolved=0, uncertain=2, error=0)
    assert not uncertain['baseline']['updated'] and baseline.read_bytes() == saved
