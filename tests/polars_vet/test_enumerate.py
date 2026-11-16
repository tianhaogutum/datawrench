"""PLV003 unwraps exactly one proven builtin enumerate, retaining inner spans."""
import textwrap

import pytest

from datawrench.polars_vet import analyze
from datawrench.polars_vet.analysis import RULES, analyze_with_suppressions
from datawrench.polars_vet.plugin import PolarsVetChecker

HEADER = 'import polars as pl\ndf = pl.read_csv("x.csv")\n'
LOOP = 'for i, row in enumerate(df.iter_rows(), start=1):\n    pass\n'


@pytest.mark.parametrize('method', ['iter_rows()', 'rows(named=True)', 'to_dicts()'])
@pytest.mark.parametrize('start', ['', ', 1', ', start=1', ', -2', ', start=offset'])
@pytest.mark.parametrize('template', [
    'for i, row in {iterator}:\n    pass',
    '[row for i, row in {iterator}]',
    '{{i: row for i, row in {iterator}}}',
    '{{i for i, row in {iterator}}}',
    '(row for i, row in {iterator})',
])
def test_builtin_enumerate_forms(method, start, template):
    inner = 'df.' + method
    source = HEADER + template.format(iterator=f'enumerate({inner}{start})')
    diagnostic, = analyze(source)
    line = source.splitlines()[diagnostic.line - 1]
    assert diagnostic.code == 'PLV003'
    assert diagnostic.message == RULES['PLV003']
    assert line[diagnostic.column - 1:diagnostic.end_column - 1] == inner
    assert diagnostic.end_line == diagnostic.line
    assert diagnostic.edits == ()


def test_import_and_frame_assignment_aliases():
    source = ('from polars import read_csv as read\n'
              'frame = read("x.csv")\ncopy: object = frame\nother = copy\n'
              'for i, row in enumerate(other.iter_rows()):\n    pass\n')
    assert [d.code for d in analyze(source)] == ['PLV003']


@pytest.mark.parametrize('iterator', [
    'wrapper(df.rows())', 'zip(df.rows())', 'enumerate(wrapper(df.rows()))',
    'wrapper(enumerate(df.rows()))', 'enumerate(enumerate(df.rows()))',
    'enumerate(zip(df.rows()))', 'enumerate(other.iter_rows())',
    'enumerate([1, 2])', 'enumerate(range(4))', 'enumerate(df)',
    'enumerate(*[df.rows()])', 'enumerate(df.rows(), **options)',
    'enumerate(iterable=df.rows())', 'enumerate(df.rows(), stop=1)',
    'enumerate(df.rows(), 1, start=2)', 'enumerate(df.rows(), 1, 2)',
    'builtins.enumerate(df.rows())', 'enum(df.rows())',
])
def test_unknown_or_unsupported_iterables(iterator):
    assert analyze(HEADER + f'for item in {iterator}:\n    pass\n') == []


@pytest.mark.parametrize('prefix', [
    'enum = enumerate\nenumerate = other\nenumerate = enum',
    'enumerate = other', 'def enumerate(items, start=0):\n    return items',
    'from helpers import enumerate', 'import helpers as enumerate',
    'from builtins import enumerate', 'enumerate: object = other',
    'enumerate += other', 'del enumerate', '(enumerate := other)',
    'if ready:\n    enumerate = other',
    'for enumerate in things:\n    pass',
    'while ready:\n    enumerate = other',
    'try:\n    enumerate = other\nexcept Exception:\n    pass',
    'with context() as enumerate:\n    pass',
    'match obj:\n    case {"wrapper": enumerate}:\n        pass',
    'from helpers import *\nimport polars as pl\ndf = pl.read_csv("x.csv")',
])
def test_enumerate_binding_invalidation(prefix):
    assert analyze(HEADER + prefix + '\n' + LOOP) == []


@pytest.mark.parametrize('source', [
    'def f(enumerate):\n' + textwrap.indent(LOOP, '    '),
    'def f(enumerate, /):\n' + textwrap.indent(LOOP, '    '),
    'def f(*, enumerate):\n' + textwrap.indent(LOOP, '    '),
    'def f(*enumerate):\n' + textwrap.indent(LOOP, '    '),
    'def f(**enumerate):\n' + textwrap.indent(LOOP, '    '),
    'def f():\n' + textwrap.indent(LOOP + 'enumerate = other\n', '    '),
    'def f():\n    global enumerate\n' + textwrap.indent(LOOP, '    '),
    'def f():\n    enumerate = other\n    def g():\n        nonlocal enumerate\n' + textwrap.indent(LOOP, '        '),
    'lambda enumerate: [row for i, row in enumerate(df.rows())]',
    'lambda *enumerate: [row for i, row in enumerate(df.rows())]',
    'lambda **enumerate: [row for i, row in enumerate(df.rows())]',
    'lambda: ([row for i, row in enumerate(df.rows())], (enumerate := other))',
    '[row for enumerate in things for i, row in enumerate(df.rows())]',
    '[row for x in things for i, row in enumerate(df.rows()) for enumerate in things]',
    '[row for x in things if [0 for enumerate in things for i, row in enumerate(df.rows())]]',
])
def test_scope_shadows(source):
    assert analyze(HEADER + source) == []


@pytest.mark.parametrize('source', [
    '[enumerate for enumerate in enumerate(df.rows())]',
    '[0 for enumerate in things]\n' + LOOP,
    'def f():\n    [0 for enumerate in things]\n' + textwrap.indent(LOOP, '    '),
    'def f(enumerate):\n    pass\n' + LOOP,
    '[row for x in things for i, row in enumerate(df.rows())]',
    '(lambda enumerate=[row for i, row in enumerate(df.rows())]: None)',
])
def test_comprehension_and_function_scope_restore(source):
    assert [d.code for d in analyze(HEADER + source)] == ['PLV003']


@pytest.mark.parametrize('prefix', [
    'df = other', 'if ready:\n    df = other',
    'from polars import read_csv as read\nread = other\ndf = read("x.csv")',
    'p = pl\np.read_csv = other\ndf = pl.read_csv("x.csv")',
])
def test_frame_origin_invalidation(prefix):
    assert analyze(HEADER + prefix + '\n' + LOOP) == []


def test_unicode_multiline_inner_span_and_suppression_start_line():
    source = HEADER + '文本 = "你好"; rows = [row for i, row in enumerate(\n    df.iter_rows(\n        named=True), start=1)]\n'
    diagnostic, = analyze(source)
    assert (diagnostic.line, diagnostic.column, diagnostic.end_line, diagnostic.end_column) == (4, 5, 5, 20)
    single = HEADER + '文本 = "你好"; rows = [row for i, row in enumerate(df.rows())]\n'
    diagnostic, = analyze(single)
    assert diagnostic.column == single.splitlines()[2].index('df.rows()') + 1
    assert diagnostic.end_column == diagnostic.column + len('df.rows()')
    comment = '  # polars-vet: ignore[PLV003] -- row boundary'
    outer = HEADER + 'for i, row in enumerate(' + comment + '\n    df.rows()):\n    pass\n'
    assert [d.code for d in analyze(outer)] == ['PLV003']
    inner = HEADER + 'for i, row in enumerate(\n    df.rows()' + comment + '\n):\n    pass\n'
    result = analyze_with_suppressions(inner, report_unused_suppressions=True)
    assert result.diagnostics == []
    assert [s.status for s in result.suppressions] == ['used']


@pytest.mark.parametrize('comment', ['# noqa', '# noqa: PLV003', '# polars-vet: ignore[PLV003] -- deliberate'])
def test_existing_suppressions_selection_and_plugin(comment):
    source = HEADER + LOOP.replace(':\n', ':  ' + comment + '\n')
    assert analyze(source, report_unused_suppressions=True) == []
    assert analyze(HEADER + LOOP, selected={'PLV001'}) == []
    assert [d.code for d in analyze(HEADER + LOOP, selected={'PLV003'})] == ['PLV003']
    import ast
    source = HEADER + LOOP
    findings = list(PolarsVetChecker(ast.parse(source), 'rows.py', source.splitlines(keepends=True)).run())
    assert len(findings) == 1 and findings[0][2] == 'PLV003 ' + RULES['PLV003']


def test_duplicate_start_keeps_existing_syntax_error():
    source = HEADER + 'for item in enumerate(df.rows(), start=1, start=2):\n    pass\n'
    diagnostic, = analyze(source)
    assert diagnostic.code == 'PLV000'


@pytest.mark.parametrize('iteration', [LOOP, 'rows = [row for i, row in enumerate(df.rows(), 1)]\n'])
def test_cli_baseline_fingerprints_classification_and_path_counts(tmp_path, iteration):
    from .test_baseline import report, run

    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src/job.py'
    source.write_text(HEADER + iteration)
    flags = ('--select', 'PLV003', '--show-baseline-path-counts', '--show-baseline-rule-counts')
    created = report(run(tmp_path, *flags, '--update-baseline'), 0)
    assert [(d['code'], d['baselineStatus']) for d in created['diagnostics']] == [('PLV003', 'new')]
    baseline = tmp_path / 'baseline.json'
    saved = baseline.read_bytes()
    # AST-stable reformat and line shift use the same existing fingerprint pipeline.
    source.write_text('# moved\n' + HEADER + iteration.replace('enumerate(', 'enumerate( '))
    before = source.read_bytes()
    existing = report(run(tmp_path, *flags), 0)
    assert [(d['code'], d['baselineStatus']) for d in existing['diagnostics']] == [('PLV003', 'existing')]
    assert existing['baseline']['pathCounts'] == [dict(path='src/job.py', existing=1, new=0,
                                                     resolved=0, uncertain=0, error=0)]
    assert existing['baseline']['ruleCounts'] == [dict(code='PLV003', existing=1, new=0,
                                                     resolved=0, uncertain=0, error=0)]
    assert source.read_bytes() == before
    # The same inner-line suppression marks the prior finding resolved.
    source.write_text(HEADER + iteration.replace('\n', '  # noqa: PLV003\n', 1))
    resolved = report(run(tmp_path, *flags), 0)
    assert [d['baselineStatus'] for d in resolved['diagnostics']] == ['resolved']
    assert resolved['baseline']['pathCounts'][0]['resolved'] == 1
    assert baseline.read_bytes() == saved
