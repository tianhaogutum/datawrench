"""Opt-in current baseline policy is descriptive, with no validation bypass."""
import json
import subprocess
import sys

import pytest

from . import test_baseline
from .test_baseline import BAD, report, run

project = test_baseline.project
SHOW = '--show-baseline-policy'
POLICY = {'targets': [{'path': 'src', 'kind': 'directory'}],
          'selected': ['PLV001', 'PLV002'], 'reportUnusedSuppressions': True}
RULES = ('--select', 'PLV003,PLV002,PLV001', '--ignore', 'PLV003', '--report-unused-suppressions')


def cli(root, *args):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', *args],
                          cwd=root, capture_output=True, text=True)


def test_policy_is_current_normalized_deterministic_and_read_only(project):
    other = project / 'other.py'
    other.write_text('import polars as pl\n' + BAD)
    args = ('./other.py', 'src/../src', './other.py', '--baseline', 'baseline.json',
            '--format', 'json', *RULES)
    expected = {**POLICY, 'targets': [{'path': 'other.py', 'kind': 'file'},
                                     {'path': 'src', 'kind': 'directory'}]}
    created = report(cli(project, *args, SHOW, '--update-baseline'), 0)
    assert created['baseline']['policy'] == expected
    baseline = project / 'baseline.json'
    saved = baseline.read_bytes()
    sources = {p: p.read_bytes() for p in [other, project / 'src/job.py']}
    plain = report(cli(project, *args), 0)
    checked = report(cli(project, *args, SHOW), 0)
    assert checked == {**plain, 'baseline': {**plain['baseline'], 'policy': expected}}
    equivalent = cli(project, 'src', 'other.py', '--baseline', './baseline.json', '--format', 'json',
                     '--select', 'PLV002,PLV001', '--report-unused-suppressions', SHOW)
    assert report(equivalent, 0)['baseline'] == checked['baseline']
    assert baseline.read_bytes() == saved
    assert all(p.read_bytes() == raw for p, raw in sources.items())
    assert set(json.loads(saved)) == {'version', 'policy', 'entries'}
    assert json.loads(saved)['policy'] == expected  # Existing snapshot policy, no new fields.


def test_mapped_explicit_file_policy_is_current_not_saved_and_baseline_relative(project):
    other = project / 'src/second.py'
    other.write_text('import polars as pl\n' + BAD)
    options = ('--baseline', 'baseline.json', '--format', 'json', *RULES)
    report(cli(project, 'src/job.py', 'src/second.py', *options, '--update-baseline'), 0)
    (project / 'src/job.py').rename(project / 'src/new.py')
    other.rename(project / 'src/z.py')
    flags = ('--baseline-path-map', 'src/second.py=src/z.py',
             '--baseline-path-map', 'src/job.py=src/new.py')
    saved = (project / 'baseline.json').read_bytes()
    args = ('src/z.py', './src/new.py', *options, *flags)
    plain = report(cli(project, *args), 0)
    data = report(cli(project, *args, SHOW), 0)
    expected = {**POLICY, 'targets': [{'path': 'src/new.py', 'kind': 'file'},
                                     {'path': 'src/z.py', 'kind': 'file'}]}
    assert data == {**plain, 'baseline': {**plain['baseline'], 'policy': expected}}
    assert data['baseline']['pathMaps'] == [
        {'oldPath': 'src/job.py', 'newPath': 'src/new.py'},
        {'oldPath': 'src/second.py', 'newPath': 'src/z.py'}]
    elsewhere = report(cli(project / 'src', 'new.py', 'z.py', '--baseline', '../baseline.json',
                           '--format', 'json', *RULES, *flags, SHOW), 0)
    assert elsewhere['baseline'] == data['baseline']
    assert (project / 'baseline.json').read_bytes() == saved
    updated = report(cli(project, *args, SHOW, '--update-baseline'), 0)
    assert updated['baseline'] == {**data['baseline'], 'updated': True}
    stored = json.loads((project / 'baseline.json').read_bytes())
    assert set(stored) == {'version', 'policy', 'entries'} and stored['version'] == 1
    assert stored['policy'] == expected
    assert {e['path'] for e in stored['entries']} == {'src/new.py', 'src/z.py'}
    assert set(report(cli(project, 'src/new.py', 'src/z.py', *options), 0)['baseline']) == {
        'path', 'updated', 'counts'}


@pytest.mark.parametrize('flags', [(), ('--format', 'text'), ('--format', 'sarif'),
    ('--format', 'github')])
def test_non_json_policy_rejected_before_update(project, flags):
    report(run(project, '--update-baseline'), 0)
    baseline = project / 'baseline.json'
    saved = baseline.read_bytes()
    source = (project / 'src/job.py').read_bytes()
    result = cli(project, 'src', '--baseline', 'baseline.json', SHOW,
                 '--update-baseline', '--exit-zero', *flags)
    assert result.returncode == 2 and result.stdout == ''
    assert '--show-baseline-policy requires --format json' in result.stderr
    assert baseline.read_bytes() == saved
    assert (project / 'src/job.py').read_bytes() == source


def test_policy_requires_baseline_before_fix(project):
    source = project / 'src/job.py'
    saved = source.read_bytes()
    result = cli(project, 'src', '--format', 'json', SHOW, '--fix', '--exit-zero')
    assert result.returncode == 2 and result.stdout == ''
    assert '--show-baseline-policy requires --baseline FILE' in result.stderr
    assert source.read_bytes() == saved
    assert not (project / 'baseline.json').exists()


@pytest.mark.parametrize('change', ['rule', 'scope', 'schema', 'collision', 'missing-new'])
def test_policy_does_not_bypass_map_validation_or_emit_partial_json(project, change):
    report(run(project, '--update-baseline'), 0)
    source = project / 'src/job.py'
    moved = project / 'src/new.py'
    source.rename(moved)
    flags = ['--baseline-path-map', 'src/job.py=src/new.py']
    if change == 'rule':
        flags += ['--select', 'PLV001']
    elif change == 'scope':
        pass  # Use the explicit NEW file target below instead of the saved directory.
    elif change == 'schema':
        (project / 'baseline.json').write_text('{}')
    elif change == 'collision':
        flags += ['--baseline-path-map', 'src/other.py=src/new.py']
    else:
        moved.unlink()
    baseline = project / 'baseline.json'
    saved = baseline.read_bytes()
    sources = {p: p.read_bytes() for p in (project / 'src').iterdir()}
    target = 'src/new.py' if change == 'scope' else 'src'
    result = cli(project, target, '--baseline', 'baseline.json', '--format', 'json',
                 *flags, SHOW, '--update-baseline', '--exit-zero')
    assert result.returncode == 2 and result.stdout == ''
    assert 'unrecognized arguments' not in result.stderr
    assert baseline.read_bytes() == saved
    assert all(p.read_bytes() == raw for p, raw in sources.items())


@pytest.mark.parametrize('contents', ['def invalid(:\n', 'import polars as pl\n' + BAD + BAD])
def test_blocked_update_keeps_policy_descriptive_and_preserves_bytes(project, contents):
    report(run(project, *RULES, '--update-baseline'), 0)
    (project / 'src/job.py').write_text(contents)
    baseline = project / 'baseline.json'
    saved = baseline.read_bytes()
    plain = report(run(project, *RULES, '--update-baseline', '--exit-zero'), 1)
    data = report(run(project, *RULES, SHOW, '--update-baseline', '--exit-zero'), 1)
    assert data == {**plain, 'baseline': {**plain['baseline'], 'policy': POLICY}}
    assert not data['baseline']['updated']
    assert data['baseline']['counts']['uncertain']
    assert baseline.read_bytes() == saved
    assert (project / 'src/job.py').read_text() == contents


def test_explicit_refresh_reports_new_policy_and_flag_is_absent_by_default(project):
    report(run(project, '--update-baseline'), 0)
    updated = report(run(project, SHOW, *RULES, '--update-baseline'), 0)
    assert updated['baseline']['policy'] == POLICY
    assert updated['baseline']['updated']
    assert report(run(project, *RULES, SHOW), 0)['baseline']['policy'] == POLICY
    assert set(report(run(project, *RULES), 0)['baseline']) == {'path', 'updated', 'counts'}
    ordinary = report(cli(project, 'src', '--format', 'json'), 1)
    assert set(ordinary) == {'version', 'diagnostics', 'fixedFiles'}
