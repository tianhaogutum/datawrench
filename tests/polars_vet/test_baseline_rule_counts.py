"""Per-rule audit summaries count classified output without changing CI policy."""
import json

import pytest

from . import test_baseline
from .test_baseline import BAD, EXPR, report, run
from .test_baseline_policy_metadata import cli

project = test_baseline.project
SHOW = '--show-baseline-rule-counts'
STATUSES = ('existing', 'new', 'resolved', 'uncertain', 'error')


def row(code, *, existing=0, new=0, resolved=0, uncertain=0, error=0):
    return dict(code=code, existing=existing, new=new, resolved=resolved,
                uncertain=uncertain, error=error)


@pytest.mark.parametrize('mapped', [False, True])
def test_mixed_classifications_include_prior_rows_and_errors_read_only(project, mapped):
    for name, content in [('duplicate', BAD), ('invalid', BAD + EXPR), ('removed', BAD)]:
        (project / f'src/{name}.py').write_text('import polars as pl\n' + content)
    report(run(project, '--update-baseline'), 0)
    baseline = project / 'baseline.json'
    saved = baseline.read_bytes()
    source = project / 'src/job.py'
    source.write_text('import polars as pl\n' + BAD +
                      'extra = pl.read_parquet("new.parquet").lazy()\n')
    (project / 'src/duplicate.py').write_text('import polars as pl\n' + BAD + BAD)
    (project / 'src/invalid.py').write_text('def invalid(:\n')
    (project / 'src/removed.py').unlink()
    flags = ('--show-baseline-policy',)
    if mapped:
        source.rename(project / 'src/pipeline.py')
        (project / 'src/invalid.py').rename(project / 'src/broken.py')
        flags += ('--baseline-path-map', 'src/job.py=src/pipeline.py',
                  '--baseline-path-map', 'src/invalid.py=src/broken.py')
    sources = {p: p.read_bytes() for p in (project / 'src').iterdir()}
    expected = [row('PLV000', error=1),
                row('PLV001', existing=1, new=1, resolved=1, uncertain=3),
                row('PLV002', resolved=1, uncertain=1)]
    plain = report(run(project, *flags), 1)
    data = report(run(project, *flags, SHOW), 1)
    assert data == {**plain, 'baseline': {**plain['baseline'], 'ruleCounts': expected}}
    for status in STATUSES:
        assert sum(r[status] for r in expected) == data['baseline']['counts'][status]
    masked = report(run(project, *flags, SHOW, '--exit-zero'), 0)
    assert masked == data
    blocked = report(run(project, *flags, SHOW, '--exit-zero', '--update-baseline'), 1)
    assert blocked == data
    # Summary is independent of diagnostic display spelling and input/select order.
    other = report(cli(project / 'src', '.', '--baseline', '../baseline.json', '--format', 'json',
                       *flags, SHOW, '--select', 'plv011,PLV010,PLV009,PLV008,PLV007,PLV006,'
                       'PLV005,PLV004,PLV003,PLV002,PLV001'), 1)
    assert other['baseline'] == data['baseline']
    assert baseline.read_bytes() == saved
    assert all(p.read_bytes() == raw for p, raw in sources.items())


def test_creation_and_mapped_update_count_pre_update_rows_only(project):
    created = report(run(project, SHOW, '--show-baseline-policy', '--update-baseline'), 0)
    assert created['baseline']['ruleCounts'] == [row('PLV001', new=1), row('PLV002', new=1)]
    source = project / 'src/job.py'
    moved = project / 'src/new.py'
    source.rename(moved)
    flags = ('--baseline-path-map', 'src/job.py=src/new.py', '--show-baseline-policy')
    plain = report(run(project, *flags), 0)
    updated = report(run(project, *flags, SHOW, '--update-baseline'), 0)
    expected = [row('PLV001', existing=1), row('PLV002', existing=1)]
    assert updated == {**plain, 'baseline': {**plain['baseline'], 'updated': True,
                                           'ruleCounts': expected}}
    stored = json.loads((project / 'baseline.json').read_bytes())
    assert set(stored) == {'version', 'policy', 'entries'} and stored['version'] == 1
    assert set(stored['policy']) == {'targets', 'selected', 'reportUnusedSuppressions'}
    assert {e['path'] for e in stored['entries']} == {'src/new.py'}
    assert report(run(project, SHOW), 0)['baseline']['ruleCounts'] == expected
    assert set(report(run(project), 0)['baseline']) == {'path', 'updated', 'counts'}


@pytest.mark.parametrize('ignored', [False, True])
def test_no_classified_rows_emit_empty_array_without_selected_zero_rows(project, ignored):
    (project / 'src/job.py').write_text('import polars as pl\n' + (BAD if ignored else ''))
    flags = ('--ignore', 'PLV001') if ignored else ()
    report(run(project, *flags, '--update-baseline'), 0)
    saved = (project / 'baseline.json').read_bytes()
    plain = report(run(project, *flags), 0)
    data = report(run(project, *flags, SHOW), 0)
    assert data == {**plain, 'baseline': {**plain['baseline'], 'ruleCounts': []}}
    assert not any(data['baseline']['counts'].values())
    assert (project / 'baseline.json').read_bytes() == saved


def test_unused_suppression_error_counts_compose_with_inventory(project):
    report(run(project, '--report-unused-suppressions', '--update-baseline'), 0)
    (project / 'src/job.py').write_text('import polars as pl\n' + BAD + EXPR +
        'x = 1  # polars-vet: ignore[PLV001] -- deliberate\n')
    flags = ('--report-unused-suppressions', '--show-suppressions')
    plain = report(run(project, *flags), 1)
    data = report(run(project, *flags, SHOW), 1)
    assert data == {**plain, 'baseline': {**plain['baseline'], 'ruleCounts': [
        row('PLV000', error=1), row('PLV001', existing=1), row('PLV002', existing=1)]}}
    assert data['suppressionAudit']['complete']


@pytest.mark.parametrize('format', [None, 'text', 'sarif', 'github'])
def test_requires_json_before_writes_even_with_update_and_exit_zero(project, format):
    report(run(project, '--update-baseline'), 0)
    saved = (project / 'baseline.json').read_bytes()
    source = (project / 'src/job.py').read_bytes()
    flags = ('--format', format) if format else ()
    result = cli(project, 'src', '--baseline', 'baseline.json', SHOW,
                 '--update-baseline', '--exit-zero', *flags)
    assert result.returncode == 2 and result.stdout == ''
    assert '--show-baseline-rule-counts requires --format json' in result.stderr
    assert (project / 'baseline.json').read_bytes() == saved
    assert (project / 'src/job.py').read_bytes() == source


def test_requires_baseline_before_fix(project):
    source = project / 'src/job.py'
    saved = source.read_bytes()
    result = cli(project, 'src', '--format', 'json', SHOW, '--fix', '--exit-zero')
    assert result.returncode == 2 and result.stdout == ''
    assert '--show-baseline-rule-counts requires --baseline FILE' in result.stderr
    assert source.read_bytes() == saved
    assert not (project / 'baseline.json').exists()


@pytest.mark.parametrize('change', ['rule', 'scope', 'schema', 'collision', 'missing-new'])
def test_summary_never_bypasses_mapping_guards_or_emits_partial_json(project, change):
    report(run(project, '--update-baseline'), 0)
    moved = project / 'src/new.py'
    (project / 'src/job.py').rename(moved)
    flags = ['--baseline-path-map', 'src/job.py=src/new.py']
    if change == 'rule':
        flags += ['--select', 'PLV001']
    elif change == 'schema':
        (project / 'baseline.json').write_text('{}')
    elif change == 'collision':
        flags += ['--baseline-path-map', 'src/other.py=src/new.py']
    elif change == 'missing-new':
        moved.unlink()
    target = 'src/new.py' if change == 'scope' else 'src'
    saved = (project / 'baseline.json').read_bytes()
    sources = {p: p.read_bytes() for p in (project / 'src').iterdir()}
    result = cli(project, target, '--baseline', 'baseline.json', '--format', 'json',
                 *flags, SHOW, '--show-baseline-policy', '--update-baseline', '--exit-zero')
    assert result.returncode == 2 and result.stdout == ''
    assert 'unrecognized arguments' not in result.stderr
    assert (project / 'baseline.json').read_bytes() == saved
    assert all(p.read_bytes() == raw for p, raw in sources.items())
