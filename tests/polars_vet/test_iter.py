"""One proven builtin iter wrapper consumes a direct/saved eager row source."""
import ast
import textwrap

import pytest

from datawrench.polars_vet import analyze
from datawrench.polars_vet.analysis import RULES, analyze_with_suppressions
from datawrench.polars_vet.plugin import PolarsVetChecker

HEADER = 'import polars as pl\ndf = pl.read_csv("x.csv")\nsaved = df.iter_rows()\n'
LOOP = 'for row in iter(saved):\n    pass\n'


def spans(source, findings):
    return [source.splitlines()[d.line - 1][d.column - 1:d.end_column - 1] for d in findings]


@pytest.mark.parametrize('inner', ['df.iter_rows()', 'df.rows(named=True)', 'df.to_dicts()', 'saved'])
@pytest.mark.parametrize('template', [
    'for row in {iterator}:\n    pass',
    '[row for row in {iterator}]', '{{row for row in {iterator}}}',
    '{{i: row for row in {iterator}}}', '(row for row in {iterator})',
])
def test_single_argument_builtin_iter(inner, template):
    source = HEADER + template.format(iterator=f'iter({inner})')
    diagnostic, = analyze(source)
    assert spans(source, [diagnostic]) == [inner]
    assert diagnostic.code == 'PLV003' and diagnostic.message == RULES['PLV003']
    assert diagnostic.edits == ()


@pytest.mark.parametrize('setup', [
    'from polars import read_csv as read\nframe = read("x.csv")\ncopy = frame\nsaved = copy.rows()',
    'p = pl\nframe = p.DataFrame()\noriginal = frame.to_dicts()\nsaved: object = original',
    'saved = pl.scan_csv("x.csv").collect().rows()',
    'saved = df.rows()\ndf = unknown',
    'a = saved = df.to_dicts()\na = unknown',
])
def test_alias_provenance(setup):
    assert [d.code for d in analyze(HEADER + setup + '\n' + LOOP)] == ['PLV003']


@pytest.mark.parametrize('mutation', [
    'iter = custom', 'iter: object', 'iter: object = custom', 'iter += custom',
    '(iter := custom)', 'del iter', 'from helpers import iter',
    'from builtins import iter', 'import helpers as iter',
    'def iter(*args):\n    pass', 'class iter:\n    pass',
    'if ready:\n    iter = custom',
    'for iter in things:\n    pass', 'for item in things:\n    iter = custom',
    'while ready:\n    iter = custom',
    'try:\n    iter = custom\nexcept Exception:\n    pass',
    'try:\n    pass\nexcept Exception as iter:\n    pass',
    'with context() as iter:\n    pass', 'match obj:\n    case {"value": iter}:\n        pass',
    'from helpers import *\nimport polars as pl\ndf = pl.DataFrame()\nsaved = df.rows()',
    'alias = iter\niter = custom\niter = alias', 'iter = iter',
])
def test_iter_binding_invalidation(mutation):
    source = HEADER + mutation + '\n' + LOOP + 'for row in saved:\n    pass\n'
    diagnostic, = analyze(source)
    assert spans(source, [diagnostic]) == ['saved']
    assert diagnostic.line == len(source.splitlines()) - 1


@pytest.mark.parametrize('body', [
    'def f(iter):\n' + textwrap.indent(LOOP, '    '),
    'def f(iter, /):\n' + textwrap.indent(LOOP, '    '),
    'def f(*, iter):\n' + textwrap.indent(LOOP, '    '),
    'def f(*iter):\n' + textwrap.indent(LOOP, '    '),
    'def f(**iter):\n' + textwrap.indent(LOOP, '    '),
    'def f():\n' + textwrap.indent(LOOP + 'iter = custom\n', '    '),
    'def f():\n    global iter\n' + textwrap.indent(LOOP, '    '),
    'def f():\n    iter = custom\n    def g():\n        nonlocal iter\n' + textwrap.indent(LOOP, '        '),
    'lambda iter: [r for r in iter(saved)]',
    'lambda: ([r for r in iter(saved)], (iter := custom))',
    '[r for iter in things for r in iter(saved)]',
    '[r for x in things for r in iter(saved) for iter in things]',
    '[r for saved in things for r in iter(saved)]',
])
def test_local_shadowing(body):
    assert analyze(HEADER + body) == []


@pytest.mark.parametrize('body', [
    '[iter for iter in iter(saved)]', '[saved for saved in iter(saved)]',
    '[0 for iter in things]\n' + LOOP, 'def f(iter):\n    pass\n' + LOOP,
    '[r for x in things for r in iter(saved)]',
    '(lambda iter=[r for r in iter(saved)]: None)',
    'async def f():\n    async for row in iter(saved):\n        pass',
])
def test_scope_restore_and_first_iterable(body):
    assert [d.code for d in analyze(HEADER + body)] == ['PLV003']


@pytest.mark.parametrize('iterator', [
    'iter()', 'iter(saved, sentinel)', 'iter(df.rows, sentinel)',
    'iter(lambda: df.rows(), sentinel)', 'iter(saved, other, sentinel)',
    'iter(*[saved])', 'iter(saved, *extra)', 'iter(**options)',
    'iter(saved, **options)', 'iter(object=saved)', 'iter(iterable=saved)',
    'iter(saved, sentinel=sentinel)', 'builtins.iter(saved)', 'alias(saved)',
    'wrapper(iter(saved))', 'iter(iter(saved))', 'enumerate(iter(saved))',
    'iter(enumerate(saved))', 'iter(zip(saved, labels))', 'zip(iter(saved), labels)',
    'iter(wrapper(saved))', 'iter(list(saved))', 'iter((copy := saved))',
    'iter(unknown.rows())', 'iter(pl.LazyFrame().rows())', 'iter(df)',
])
def test_unsupported_consumption(iterator):
    assert analyze(HEADER + 'alias = iter\n' + f'for item in {iterator}:\n    pass') == []


@pytest.mark.parametrize('setup', [
    'saved = unknown', 'del saved', 'if ready:\n    saved = unknown',
    'saved = helper(df.rows())', 'saved = pl.LazyFrame().rows()',
    'saved, other = df.rows()', 'saved = unknown.rows()',
    'pl.read_csv = custom\nsaved = pl.read_csv("x.csv").rows()',
    'df = unknown\nsaved = df.rows()',
])
def test_source_proof_invalidated(setup):
    assert analyze(HEADER + setup + '\n' + LOOP) == []


def test_creation_and_saved_iter_result_stay_silent():
    assert analyze(HEADER + 'wrapped = iter(saved)\nfor row in wrapped:\n    pass\n') == []


def test_unicode_spans_suppression_selection_and_plugin():
    source = HEADER + '文本 = "你好"; result = [r for r in iter(saved)]\n'
    diagnostic, = analyze(source)
    column = source.splitlines()[3].index('saved') + 1
    assert (diagnostic.line, diagnostic.column, diagnostic.end_line, diagnostic.end_column) == (4, column, 4, column + 5)
    assert analyze(source, selected={'PLV001'}) == []
    assert analyze(source, selected={'PLV003'}) == [diagnostic]
    plugin = list(PolarsVetChecker(ast.parse(source), 'rows.py', source.splitlines(keepends=True)).run())
    assert [(f[0], f[1], f[2]) for f in plugin] == [(4, column - 1, 'PLV003 ' + RULES['PLV003'])]
    comment = '  # polars-vet: ignore[PLV003] -- intentional'
    multiline = HEADER.rstrip() + comment + '\nfor row in iter(' + comment + '\n    saved):\n    pass\n'
    result = analyze_with_suppressions(multiline)
    assert [(d.line, d.column, d.end_column) for d in result.diagnostics] == [(5, 5, 10)]
    assert [s.status for s in result.suppressions] == ['unused', 'unused']
    result = analyze_with_suppressions(multiline.replace('saved):', 'saved' + comment + '\n):'))
    assert result.diagnostics == []
    assert [s.status for s in result.suppressions] == ['unused', 'unused', 'used']


@pytest.mark.parametrize('comment', ['# noqa', '# noqa: PLV003', '# polars-vet: ignore[PLV003] -- intentional'])
def test_start_line_suppressions(comment):
    assert analyze(HEADER + LOOP.replace(':\n', ':  ' + comment + '\n'), report_unused_suppressions=True) == []


def test_multiline_call_span_and_no_duplicates():
    source = HEADER + '[r for r in iter(\n    df.rows(\n        named=True))]\n' + LOOP
    assert [(d.line, d.column, d.end_line, d.end_column) for d in analyze(source)] == [(5, 5, 6, 20), (7, 17, 7, 22)]


@pytest.mark.parametrize('inner', ['saved', 'df.rows()'])
def test_baseline_identity_statuses_counts_and_read_only_comparison(tmp_path, inner):
    from .test_baseline import report, run

    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src/job.py'
    iteration = f'for row in iter({inner}):\n    pass\n'
    source.write_text(HEADER + iteration)
    flags = ('--select', 'PLV003', '--show-baseline-path-counts', '--show-baseline-rule-counts')
    created = report(run(tmp_path, *flags, '--update-baseline'), 0)
    assert [d['baselineStatus'] for d in created['diagnostics']] == ['new']
    baseline = tmp_path / 'baseline.json'
    saved = baseline.read_bytes()
    source.write_text('# shifted\n' + HEADER + iteration.replace('iter(', 'iter( '))
    before = source.read_bytes()
    existing = report(run(tmp_path, *flags), 0)
    counts = dict(existing=1, new=0, resolved=0, uncertain=0, error=0)
    assert [d['baselineStatus'] for d in existing['diagnostics']] == ['existing']
    assert existing['baseline']['counts'] == counts
    assert existing['baseline']['pathCounts'] == [dict(path='src/job.py', **counts)]
    assert existing['baseline']['ruleCounts'] == [dict(code='PLV003', **counts)]
    assert source.read_bytes() == before and baseline.read_bytes() == saved
    source.write_text(HEADER + iteration.replace(':\n', ':  # noqa: PLV003\n'))
    resolved = report(run(tmp_path, *flags), 0)
    assert [d['baselineStatus'] for d in resolved['diagnostics']] == ['resolved']
    assert baseline.read_bytes() == saved
    source.write_text(HEADER + iteration + iteration)
    uncertain = report(run(tmp_path, *flags, '--update-baseline'), 1)
    assert uncertain['baseline']['counts'] == dict(existing=0, new=0, resolved=0, uncertain=2, error=0)
    assert not uncertain['baseline']['updated'] and baseline.read_bytes() == saved
