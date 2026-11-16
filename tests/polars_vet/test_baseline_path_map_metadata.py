"""JSON audit metadata describes validated mappings, never snapshot history."""
import json
import subprocess
import sys

import pytest

from . import test_baseline
from .test_baseline import BAD, EXPR, report, run
from .test_baseline_path_map import MAP, rename

project = test_baseline.project
EXPECTED = [{'oldPath': 'src/job.py', 'newPath': 'src/pipeline.py'}]


def test_multiple_maps_are_sorted_read_only_and_persist_only_on_update(project):
    other = project / 'src/second.py'
    other.write_text('import polars as pl\n' + BAD)
    created = report(run(project, '--update-baseline'), 0)
    assert set(created['baseline']) == {'path', 'updated', 'counts'}
    baseline = project / 'baseline.json'
    saved = baseline.read_bytes()
    (project / 'src/job.py').rename(project / 'src/z 你好.py')
    other.rename(project / 'src/a\\name.py')
    sources = {p: p.read_bytes() for p in (project / 'src').iterdir()}
    pairs = [('src/job.py', 'src/z 你好.py'), ('src/second.py', 'src/a\\name.py')]

    def flags(pairs):
        return [flag for old, new in pairs for flag in ('--baseline-path-map', f'{old}={new}')]

    first = run(project, *flags(pairs))
    data = report(first, 0)
    assert data['baseline']['pathMaps'] == [dict(oldPath=old, newPath=new) for old, new in pairs]
    assert data['baseline']['counts'] == dict(existing=3, new=0, resolved=0, uncertain=0, error=0)
    assert run(project, *flags(reversed(pairs))).stdout == first.stdout
    assert baseline.read_bytes() == saved
    assert all(p.read_bytes() == raw for p, raw in sources.items())

    updated = report(run(project, *flags(reversed(pairs)), '--update-baseline'), 0)
    assert updated['baseline'] == {**data['baseline'], 'updated': True}
    stored = json.loads(baseline.read_bytes())
    assert set(stored) == {'version', 'policy', 'entries'}
    assert stored['version'] == 1
    assert {entry['path'] for entry in stored['entries']} == {new for _, new in pairs}
    assert all(set(entry) == {'path', 'code', 'scope', 'anchor', 'line', 'column', 'end_line', 'end_column'}
               for entry in stored['entries'])
    assert set(report(run(project), 0)['baseline']) == {'path', 'updated', 'counts'}
    assert all(p.read_bytes() == raw for p, raw in sources.items())
    stale = run(project, *flags(pairs), '--exit-zero', '--update-baseline')
    assert stale.returncode == 2 and stale.stdout == ''


def test_metadata_uses_baseline_relative_paths_from_another_cwd(project):
    rename(project)
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', '.',
        '--baseline', '../baseline.json', '--format', 'json', *MAP],
        cwd=project / 'src', capture_output=True, text=True)
    assert report(result, 0)['baseline']['pathMaps'] == EXPECTED


@pytest.mark.parametrize(('contents', 'counts'), [
    ('import polars as pl\n' + BAD + 'expr = pl.col("x") + 1\n' +
     'extra = pl.read_parquet("new.parquet").lazy()\n',
     dict(existing=1, new=1, resolved=1, uncertain=0, error=0)),
    ('def invalid(:\n', dict(existing=0, new=0, resolved=0, uncertain=2, error=1)),
    ('import polars as pl\n' + BAD + BAD + EXPR,
     dict(existing=1, new=0, resolved=0, uncertain=2, error=0)),
    ('import polars as pl\n', dict(existing=0, new=0, resolved=2, uncertain=0, error=0)),
])
def test_mapping_metadata_is_independent_of_classification(project, contents, counts):
    moved, saved = rename(project)
    moved.write_text(contents)
    data = report(run(project, *MAP), int(bool(counts['new'] or counts['uncertain'] or counts['error'])))
    assert data['baseline']['pathMaps'] == EXPECTED
    assert data['baseline']['counts'] == counts
    assert not data['baseline']['updated']
    assert report(run(project, *MAP, '--exit-zero'), 0)['baseline']['pathMaps'] == EXPECTED
    if counts['uncertain'] or counts['error']:
        blocked = report(run(project, *MAP, '--update-baseline', '--exit-zero'), 1)
        assert blocked['baseline'] == data['baseline']
    assert (project / 'baseline.json').read_bytes() == saved
    assert moved.read_text() == contents


def test_invalid_map_batch_emits_no_partial_metadata_or_writes(project):
    moved, saved = rename(project)
    source = moved.read_bytes()
    # One valid map must not expose partial metadata when another is invalid.
    result = run(project, *MAP, '--baseline-path-map', 'src/unknown.py=src/absent.py',
                 '--exit-zero', '--update-baseline')
    assert result.returncode == 2
    assert result.stdout == ''
    assert 'saved findings' in result.stderr
    assert (project / 'baseline.json').read_bytes() == saved
    assert moved.read_bytes() == source


def test_no_map_preserves_exact_json_schema(project):
    plain = subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', 'src', '--format', 'json'],
                           cwd=project, capture_output=True, text=True)
    assert set(report(plain, 1)) == {'version', 'diagnostics', 'fixedFiles'}
    report(run(project, '--update-baseline'), 0)
    for flags in [(), ('--update-baseline',)]:
        data = report(run(project, *flags), 0)
        assert set(data) == {'version', 'diagnostics', 'fixedFiles', 'baseline'}
        assert set(data['baseline']) == {'path', 'updated', 'counts'}
