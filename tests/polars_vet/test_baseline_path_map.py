"""Exact rename contracts: preserve identity, reject ambiguity, never infer moves."""
import json
import subprocess
import sys

import pytest

from . import test_baseline
from .test_baseline import BAD, EXPR, report, run, statuses

project = test_baseline.project

MAP = ('--baseline-path-map', 'src/job.py=src/pipeline.py')


def rename(project):
    report(run(project, '--update-baseline'), 0)
    source = project / 'src/job.py'
    moved = project / 'src/pipeline.py'
    source.rename(moved)
    return moved, (project / 'baseline.json').read_bytes()


def rejected(project, *args, match):
    baseline = project / 'baseline.json'
    saved = baseline.read_bytes() if baseline.exists() else None
    sources = {p: p.read_bytes() for p in (project / 'src').rglob('*') if p.is_file()}
    result = run(project, *args, '--exit-zero')
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ''  # No partial comparison/metadata for invalid maps.
    assert 'unrecognized arguments' not in result.stderr
    assert match in result.stderr, result.stderr
    assert (baseline.read_bytes() if baseline.exists() else None) == saved
    assert all(p.read_bytes() == data for p, data in sources.items())


def test_exact_rename_preserves_history_and_update_persists(project):
    moved, saved = rename(project)
    source = moved.read_bytes()
    assert sorted(statuses(report(run(project), 1))) == [
        ('PLV001', 'new'), ('PLV001', 'resolved'), ('PLV002', 'new'), ('PLV002', 'resolved')]
    data = report(run(project, *MAP), 0)
    assert statuses(data) == [('PLV001', 'existing'), ('PLV002', 'existing')]
    assert all(d['path'].endswith('src/pipeline.py') for d in data['diagnostics'])
    assert (project / 'baseline.json').read_bytes() == saved
    assert moved.read_bytes() == source
    assert report(run(project, *MAP, '--update-baseline'), 0)['baseline']['updated']
    assert {e['path'] for e in json.loads((project / 'baseline.json').read_bytes())['entries']} == {'src/pipeline.py'}
    assert report(run(project), 0)['baseline']['counts']['existing'] == 2
    rejected(project, *MAP, match='saved findings')


def test_map_does_not_hide_changed_or_additional_findings(project):
    moved, saved = rename(project)
    moved.write_text('# unrelated shift\nimport polars as pl\n' + BAD +
                     'expr = pl.col("x") + 1\nextra = pl.read_parquet("new.parquet").lazy()\n')
    data = report(run(project, *MAP), 1)
    assert sorted(statuses(data)) == [('PLV001', 'existing'), ('PLV001', 'new'), ('PLV002', 'resolved')]
    assert all(d['path'].endswith('src/pipeline.py') for d in data['diagnostics'])
    assert (project / 'baseline.json').read_bytes() == saved


@pytest.mark.parametrize('contents', ['def invalid(:\n', 'import polars as pl\n' + BAD + BAD + EXPR])
def test_mapped_source_errors_and_repeats_block_updates(project, contents):
    moved, saved = rename(project)
    moved.write_text(contents)
    data = report(run(project, *MAP), 1)
    assert data['baseline']['counts']['uncertain'] == 2
    assert not data['baseline']['counts']['resolved']
    assert not report(run(project, *MAP, '--update-baseline'), 1)['baseline']['updated']
    assert (project / 'baseline.json').read_bytes() == saved


@pytest.mark.parametrize('mapping', ['missing', '=src/pipeline.py', 'src/job.py=',
    'src/job.py=src/pipeline.py=extra', '/src/job.py=src/pipeline.py',
    'src/job.py=../escape.py', './src/job.py=src/pipeline.py', 'src//job.py=src/pipeline.py',
    'src/job.py=src/./pipeline.py', '.=src/pipeline.py', 'src/job.py=src/job.py'])
def test_malformed_maps_rejected_even_during_update(project, mapping):
    rename(project)
    rejected(project, '--baseline-path-map', mapping, '--update-baseline', match='path map')


@pytest.mark.parametrize('mappings', [
    ['src/job.py=src/pipeline.py', 'src/job.py=src/other.py'],
    ['src/job.py=src/pipeline.py', 'src/other.py=src/pipeline.py'],
    ['src/job.py=src/other.py', 'src/other.py=src/pipeline.py'],
    ['src/job.py=src/other.py', 'src/other.py=src/job.py'],
    ['src/job.py=src/pipeline.py', 'src/job.py=src/pipeline.py'],
])
def test_colliding_and_chained_maps_are_rejected(project, mappings):
    rename(project)
    rejected(project, *(arg for m in mappings for arg in ('--baseline-path-map', m)), match='path map')


def test_saved_destination_collision_is_rejected(project):
    (project / 'src/pipeline.py').write_text('import polars as pl\n' + EXPR)
    report(run(project, '--update-baseline'), 0)
    (project / 'src/job.py').unlink()
    rejected(project, *MAP, '--update-baseline', match='saved findings')


@pytest.mark.parametrize('case', ['old-exists', 'old-symlink', 'old-ancestor-symlink',
    'new-missing', 'new-directory', 'new-symlink', 'new-ancestor-symlink', 'not-discovered', 'unknown-old'])
def test_non_rename_or_unsafe_files_rejected(project, case):
    moved, _ = rename(project)
    mapping = MAP
    if case == 'old-exists':
        (project / 'src/job.py').write_bytes(moved.read_bytes())
    elif case == 'old-symlink':
        (project / 'src/job.py').symlink_to(project / 'absent')
    elif case == 'old-ancestor-symlink':
        # Rewrite the saved path/scan to isolate the ancestor check.
        data = json.loads((project / 'baseline.json').read_text())
        for e in data['entries']:
            e['path'] = 'src/old/job.py'
        (project / 'baseline.json').write_text(json.dumps(data))
        (project / 'src/old').symlink_to(project / 'absent', target_is_directory=True)
        mapping = ('--baseline-path-map', 'src/old/job.py=src/pipeline.py')
    elif case in {'new-missing', 'new-directory', 'new-symlink', 'new-ancestor-symlink'}:
        moved.unlink()
        if case == 'new-directory':
            moved.mkdir()
        elif case == 'new-symlink':
            moved.symlink_to(project / 'absent')
        elif case == 'new-ancestor-symlink':
            (project / 'src/new').symlink_to(project / 'absent', target_is_directory=True)
            mapping = ('--baseline-path-map', 'src/job.py=src/new/pipeline.py')
    elif case == 'not-discovered':
        moved.rename(project / 'src/pipeline.txt')
        mapping = ('--baseline-path-map', 'src/job.py=src/pipeline.txt')
    else:
        mapping = ('--baseline-path-map', 'src/unknown.py=src/pipeline.py')
    rejected(project, *mapping, '--update-baseline', match='path map')


def test_map_requires_valid_existing_baseline_and_unchanged_policy(project):
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', 'src', *MAP],
                            cwd=project, capture_output=True, text=True)
    assert result.returncode == 2
    assert '--baseline-path-map requires --baseline' in result.stderr
    rejected(project, *MAP, '--update-baseline', match='existing valid baseline')
    rename(project)
    rejected(project, *MAP, '--select', 'PLV001', '--update-baseline', match='policy')
    (project / 'baseline.json').write_text('{broken')
    rejected(project, *MAP, '--update-baseline', match='Invalid baseline')


def test_explicit_file_target_remapped_and_paths_relative_to_baseline(project):
    def check(target, *extra, cwd=project):
        return subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', target,
            '--baseline', str(project / 'baseline.json'), '--format', 'json', *extra],
            cwd=cwd, capture_output=True, text=True)
    report(check('src/job.py', '--update-baseline'), 0)
    (project / 'src/job.py').rename(project / 'src/pipeline.py')
    data = report(check('pipeline.py', *MAP, cwd=project / 'src'), 0)
    assert data['baseline']['counts']['existing'] == 2
    rejected(project, *MAP, '--update-baseline', match='scope')  # directory is a different scope
    report(check('src/pipeline.py', *MAP, '--update-baseline'), 0)
    assert report(check('src/pipeline.py'), 0)['baseline']['counts']['existing'] == 2


def test_multiple_independent_renames_and_unusual_posix_paths(project):
    other = project / 'src/second.py'
    other.write_text('import polars as pl\n' + BAD)
    report(run(project, '--update-baseline'), 0)
    (project / 'src/job.py').rename(project / 'src/new 你好.py')
    other.rename(project / 'src/back\\slash.py')
    data = report(run(project, '--baseline-path-map', 'src/job.py=src/new 你好.py',
                      '--baseline-path-map', 'src/second.py=src/back\\slash.py'), 0)
    assert data['baseline']['counts']['existing'] == 3


@pytest.mark.parametrize('format', ['text', 'sarif', 'github'])
def test_map_uses_existing_renderers(project, format):
    rename(project)
    result = run(project, *MAP, '--format', format)
    assert result.returncode == 0, result.stdout + result.stderr
    if format == 'sarif':
        rows = json.loads(result.stdout)['runs'][0]['results']
        assert {r['baselineState'] for r in rows} == {'unchanged'}
        assert all(r['locations'][0]['physicalLocation']['artifactLocation']['uri'].endswith('/src/pipeline.py') for r in rows)
    else:
        assert '[existing]' in result.stdout
        assert 'src/job.py' not in result.stdout


def test_mapped_read_failure_keeps_saved_findings_uncertain(project):
    moved, saved = rename(project)
    # The destination is discovered but cannot be decoded as Python source.
    moved.write_bytes(b'\xff\xfe\x00\n')
    data = report(run(project, *MAP), 1)
    assert data['baseline']['counts'] == dict(existing=0, new=0, resolved=0, uncertain=2, error=1)
    assert all(d['path'].endswith('src/pipeline.py') for d in data['diagnostics'])
    assert report(run(project, *MAP, '--update-baseline'), 1)['baseline']['updated'] is False
    assert (project / 'baseline.json').read_bytes() == saved


def test_map_does_not_bypass_directory_scope_or_old_ambiguity(project):
    rename(project)
    (project / 'src/pipeline.py').rename(project / 'pipeline.py')
    saved = (project / 'baseline.json').read_bytes()
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', '.',
        '--baseline', 'baseline.json', '--baseline-path-map', 'src/job.py=pipeline.py',
        '--update-baseline', '--exit-zero'], cwd=project, capture_output=True, text=True)
    assert result.returncode == 2 and 'scope' in result.stderr, result.stdout + result.stderr
    assert (project / 'baseline.json').read_bytes() == saved
    (project / 'pipeline.py').rename(project / 'src/pipeline.py')
    baseline = project / 'baseline.json'
    data = json.loads(baseline.read_text())
    duplicate = {**data['entries'][0], 'line': 99, 'end_line': 99}
    data['entries'].append(duplicate)
    baseline.write_text(json.dumps(data))
    saved = baseline.read_bytes()
    result = report(run(project, *MAP), 1)
    assert sorted(statuses(result)) == [('PLV001', 'uncertain'), ('PLV002', 'existing')]
    assert report(run(project, *MAP, '--update-baseline'), 1)['baseline']['updated'] is False
    assert baseline.read_bytes() == saved


def test_mapped_suppression_inventory_resolves_at_current_path(project):
    moved, saved = rename(project)
    moved.write_text('import polars as pl\n' + BAD.rstrip() +
                     '  # polars-vet: ignore[PLV001] -- accepted eager read\n' + EXPR)
    data = report(run(project, *MAP, '--show-suppressions'), 0)
    assert sorted(statuses(data)) == [('PLV001', 'resolved'), ('PLV002', 'existing')]
    assert data['suppressions'] == [dict(path='src/pipeline.py', line=2, code='PLV001',
                                       reason='accepted eager read', status='used')]
    assert all(d['path'].endswith('src/pipeline.py') for d in data['diagnostics'])
    assert (project / 'baseline.json').read_bytes() == saved
