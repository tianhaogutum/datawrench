"""Opt-in per-path audit counts cover the complete classified comparison."""
import json

import pytest

from . import test_baseline
from .test_baseline import BAD, EXPR, report, run
from .test_baseline_policy_metadata import cli

project = test_baseline.project
SHOW = '--show-baseline-path-counts'
AUDITS = ('--show-baseline-policy', '--show-baseline-rule-counts')
STATUSES = ('existing', 'new', 'resolved', 'uncertain', 'error')


def row(path, *, existing=0, new=0, resolved=0, uncertain=0, error=0):
    return dict(path=path, existing=existing, new=new, resolved=resolved,
                uncertain=uncertain, error=error)


@pytest.mark.parametrize('mapped', [False, True])
def test_mixed_paths_include_prior_rows_errors_and_merge_display_spellings(project, mapped):
    for name, content in [('duplicate', BAD), ('invalid', BAD + EXPR), ('removed', BAD)]:
        (project / f'src/{name}.py').write_text('import polars as pl\n' + content)
    report(run(project, '--update-baseline'), 0)
    baseline = project / 'baseline.json'
    saved = baseline.read_bytes()
    source = project / 'src/job.py'
    # A current relative-path row and prior absolute-path row belong to the same path.
    source.write_text('import polars as pl\n' + BAD +
                      'extra = pl.read_parquet("new.parquet").lazy()\n')
    (project / 'src/duplicate.py').write_text('import polars as pl\n' + BAD + BAD)
    (project / 'src/invalid.py').write_text('def invalid(:\n')
    (project / 'src/removed.py').unlink()
    (project / 'src/clean.py').write_text('x = 1\n')
    flags = AUDITS
    job, invalid = 'job.py', 'invalid.py'
    if mapped:
        job, invalid = 'pipeline 你好.py', 'broken.py'
        source.rename(project / 'src' / job)
        (project / 'src/invalid.py').rename(project / 'src' / invalid)
        flags += ('--baseline-path-map', 'src/job.py=src/' + job,
                  '--baseline-path-map', 'src/invalid.py=src/' + invalid)
    sources = {p: p.read_bytes() for p in (project / 'src').iterdir()}
    expected = sorted([row('src/' + invalid, uncertain=2, error=1),
                       row('src/duplicate.py', uncertain=2),
                       row('src/' + job, existing=1, new=1, resolved=1),
                       row('src/removed.py', resolved=1)], key=lambda r: r['path'])
    plain = report(run(project, *flags), 1)
    data = report(run(project, *flags, SHOW), 1)
    assert data == {**plain, 'baseline': {**plain['baseline'], 'pathCounts': expected}}
    for status in STATUSES:
        assert sum(r[status] for r in expected) == data['baseline']['counts'][status]
        assert sum(r[status] for r in data['baseline']['ruleCounts']) == data['baseline']['counts'][status]
    assert report(run(project, *flags, SHOW, '--exit-zero'), 0) == data
    assert report(run(project, *flags, SHOW, '--exit-zero', '--update-baseline'), 1) == data
    for targets in [('src/../src', './src'), (str(project / 'src'),)]:
        other = report(cli(project, *targets, '--baseline', './baseline.json', '--format', 'json',
                           *flags, SHOW), 1)
        assert other['baseline'] == data['baseline']
    elsewhere = report(cli(project / 'src', '.', '--baseline', '../baseline.json', '--format', 'json',
                           *flags, SHOW), 1)
    assert elsewhere['baseline'] == data['baseline']
    assert baseline.read_bytes() == saved
    assert all(p.read_bytes() == raw for p, raw in sources.items())


def test_creation_mapped_explicit_file_update_and_resolved_only_paths(project):
    other = project / 'src/second.py'
    other.write_text('import polars as pl\n' + BAD)
    options = ('--baseline', 'baseline.json', '--format', 'json', *AUDITS, SHOW)
    created = report(cli(project, 'src/second.py', './src/job.py', *options, '--update-baseline'), 0)
    assert created['baseline']['pathCounts'] == [row('src/job.py', new=2), row('src/second.py', new=1)]
    (project / 'src/job.py').rename(project / 'src/new.py')
    args = ('./src/second.py', 'src/new.py', *options,
            '--baseline-path-map', 'src/job.py=src/new.py')
    checked = report(cli(project, *args), 0)
    expected = [row('src/new.py', existing=2), row('src/second.py', existing=1)]
    assert checked['baseline']['pathCounts'] == expected
    updated = report(cli(project, *args, '--update-baseline'), 0)
    assert updated['baseline'] == {**checked['baseline'], 'updated': True}
    stored = json.loads((project / 'baseline.json').read_bytes())
    assert set(stored) == {'version', 'policy', 'entries'} and stored['version'] == 1
    assert set(stored['policy']) == {'targets', 'selected', 'reportUnusedSuppressions'}
    assert all(set(e) == {'path', 'code', 'scope', 'anchor', 'line', 'column', 'end_line', 'end_column'}
               for e in stored['entries'])
    assert {e['path'] for e in stored['entries']} == {'src/new.py', 'src/second.py'}
    plain_options = ('--baseline', 'baseline.json', '--format', 'json')
    assert set(report(cli(project, 'src/new.py', 'src/second.py', *plain_options), 0)['baseline']) == {
        'path', 'updated', 'counts'}
    for p in (project / 'src').iterdir():
        p.write_text('x = 1\n')
    resolved = report(cli(project, 'src/new.py', 'src/second.py', *options, '--update-baseline'), 0)
    assert resolved['baseline']['pathCounts'] == [row('src/new.py', resolved=2), row('src/second.py', resolved=1)]
    assert report(cli(project, 'src/new.py', 'src/second.py', *options), 0)['baseline']['pathCounts'] == []


@pytest.mark.parametrize('ignored', [False, True])
def test_empty_result_omits_scanned_paths_and_keeps_opt_out_schema(project, ignored):
    (project / 'src/job.py').write_text('import polars as pl\n' + (BAD if ignored else ''))
    flags = ('--ignore', 'PLV001') if ignored else ()
    report(run(project, *flags, '--update-baseline'), 0)
    saved = (project / 'baseline.json').read_bytes()
    plain = report(run(project, *flags), 0)
    data = report(run(project, *flags, SHOW), 0)
    assert data == {**plain, 'baseline': {**plain['baseline'], 'pathCounts': []}}
    assert not any(data['baseline']['counts'].values())
    assert (project / 'baseline.json').read_bytes() == saved
    ordinary = report(cli(project, 'src', '--format', 'json', *flags), 0)
    assert set(ordinary) == {'version', 'diagnostics', 'fixedFiles'}


def test_discovery_errors_share_normalized_path_and_preserve_unverified_prior(project):
    # Saved file target may disappear; a discovery error makes its prior rows uncertain.
    options = ('--baseline', 'baseline.json', '--format', 'json')
    report(cli(project, 'src/job.py', *options, '--update-baseline'), 0)
    (project / 'src/job.py').unlink()
    saved = (project / 'baseline.json').read_bytes()
    result = report(cli(project, './src/job.py', 'src/../src/job.py', *options, SHOW,
                        '--update-baseline', '--exit-zero'), 1)
    assert result['baseline']['pathCounts'] == [row('src/job.py', uncertain=2, error=2)]
    assert result['baseline']['counts'] == {k: result['baseline']['pathCounts'][0][k] for k in STATUSES}
    assert not result['baseline']['updated']
    assert (project / 'baseline.json').read_bytes() == saved


def test_unused_suppression_error_counts_compose_with_other_audits(project):
    report(run(project, '--report-unused-suppressions', '--update-baseline'), 0)
    (project / 'src/job.py').write_text('import polars as pl\n' + BAD + EXPR +
        'x = 1  # polars-vet: ignore[PLV001] -- deliberate\n')
    flags = ('--report-unused-suppressions', '--show-suppressions', *AUDITS)
    plain = report(run(project, *flags), 1)
    data = report(run(project, *flags, SHOW), 1)
    assert data == {**plain, 'baseline': {**plain['baseline'], 'pathCounts': [
        row('src/job.py', existing=2, error=1)]}}
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
    assert SHOW + ' requires --format json' in result.stderr
    assert (project / 'baseline.json').read_bytes() == saved
    assert (project / 'src/job.py').read_bytes() == source


def test_requires_baseline_before_fix(project):
    source = project / 'src/job.py'
    saved = source.read_bytes()
    result = cli(project, 'src', '--format', 'json', SHOW, '--fix', '--update-baseline', '--exit-zero')
    assert result.returncode == 2 and result.stdout == ''
    assert SHOW + ' requires --baseline FILE' in result.stderr
    assert source.read_bytes() == saved
    assert not (project / 'baseline.json').exists()


def test_valid_flag_still_rejects_fix_before_source_or_baseline_writes(project):
    report(run(project, '--update-baseline'), 0)
    saved = (project / 'baseline.json').read_bytes()
    source = (project / 'src/job.py').read_bytes()
    result = run(project, SHOW, '--fix', '--update-baseline', '--exit-zero')
    assert result.returncode == 2 and result.stdout == ''
    assert '--baseline and --fix cannot be combined' in result.stderr
    assert (project / 'baseline.json').read_bytes() == saved
    assert (project / 'src/job.py').read_bytes() == source


@pytest.mark.parametrize('change', ['rule', 'scope', 'schema', 'collision', 'missing-new'])
def test_summary_cannot_bypass_mapping_guards_or_emit_partial_json(project, change):
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
                 *flags, SHOW, *AUDITS, '--update-baseline', '--exit-zero')
    assert result.returncode == 2 and result.stdout == ''
    assert 'unrecognized arguments' not in result.stderr
    assert (project / 'baseline.json').read_bytes() == saved
    assert all(p.read_bytes() == raw for p, raw in sources.items())
