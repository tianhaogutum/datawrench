"""Synthetic CLI integration fixtures for the baseline CI contract."""
import json
import subprocess
import sys

import pytest

BAD = 'q = pl.read_csv("local.csv").lazy()\n'
EXPR = 'expr = pl.col("x").map_elements(lambda x: x + 1)\n'


def run(root, *args):
    return subprocess.run(
        [sys.executable, '-m', 'datawrench', 'polars-vet', 'check', 'src', '--baseline', 'baseline.json',
         '--format', 'json', *args], cwd=root, capture_output=True, text=True,
    )


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/job.py').write_text('import polars as pl\n' + BAD + EXPR)
    return tmp_path


def report(result, exit_code):
    assert result.returncode == exit_code, result.stdout + result.stderr
    return json.loads(result.stdout)


def statuses(data):
    return [(d['code'], d['baselineStatus']) for d in data['diagnostics']]


def test_baseline_existing_new_resolved_and_read_only(project):
    source = project / 'src/job.py'
    before = source.read_bytes()
    created = report(run(project, '--update-baseline'), 0)
    assert created['baseline']['updated'] is True
    assert source.read_bytes() == before
    baseline = project / 'baseline.json'
    saved = baseline.read_bytes()
    stored = json.loads(saved)
    assert stored['version'] == 1
    assert {e['path'] for e in stored['entries']} == {'src/job.py'}
    assert {e['code'] for e in stored['entries']} == {'PLV001', 'PLV002'}
    assert all(len(e['anchor']) == 64 and len(e['scope']) == 64 for e in stored['entries'])
    assert b'local.csv' not in saved
    assert statuses(report(run(project), 0)) == [('PLV001', 'existing'), ('PLV002', 'existing')]

    source.write_text('# unrelated move\nimport polars as pl\nother = 7\n' + BAD +
                      'expr = pl.col("x") + 1\n' +
                      'second = pl.read_parquet("new.parquet").lazy()\n')
    changed = source.read_bytes()
    data = report(run(project), 1)
    assert sorted(statuses(data)) == [('PLV001', 'existing'), ('PLV001', 'new'), ('PLV002', 'resolved')]
    assert data['baseline']['counts'] == dict(existing=1, new=1, resolved=1, uncertain=0, error=0)
    assert baseline.read_bytes() == saved
    assert source.read_bytes() == changed
    assert report(run(project, '--exit-zero'), 0)['baseline']['counts']['new'] == 1
    assert report(run(project, '--update-baseline'), 0)['baseline']['updated']
    assert baseline.read_bytes() != saved
    assert source.read_bytes() == changed
    assert all(s == 'existing' for _, s in statuses(report(run(project), 0)))


def test_ast_identity_survives_reformat_reorder_and_checkout_move(project, tmp_path):
    import shutil

    source = project / 'src/job.py'
    source.write_text('import polars as pl\ndef first():\n    ' + BAD +
                      'def second():\n    ' + BAD)
    report(run(project, '--update-baseline'), 0)
    source.write_text('import polars as pl\ndef second():\n    # moved\n    ' + BAD +
                      'def first():\n    q = pl.read_csv(\n        \'local.csv\',\n    ).lazy()\n')
    assert statuses(report(run(project), 0)) == [('PLV001', 'existing'), ('PLV001', 'existing')]
    moved = tmp_path / 'relocated'
    shutil.copytree(project, moved)
    assert report(run(moved), 0)['baseline']['counts']['existing'] == 2


def test_duplicate_anchor_is_uncertain_and_cannot_refresh(project):
    report(run(project, '--update-baseline'), 0)
    saved = (project / 'baseline.json').read_bytes()
    source = project / 'src/job.py'
    source.write_text('import polars as pl\n' + BAD + BAD + EXPR)
    data = report(run(project), 1)
    assert data['baseline']['counts']['uncertain'] == 2
    assert data['baseline']['counts']['existing'] == 1
    assert all('Ambiguous' in d['baselineReason'] for d in data['diagnostics'] if d['baselineStatus'] == 'uncertain')
    result = report(run(project, '--update-baseline'), 1)
    assert result['baseline']['updated'] is False
    assert (project / 'baseline.json').read_bytes() == saved


def test_failed_file_keeps_old_findings_uncertain_and_blocks_refresh(project):
    report(run(project, '--update-baseline'), 0)
    saved = (project / 'baseline.json').read_bytes()
    (project / 'src/job.py').write_text('def invalid(:\n')
    data = report(run(project), 1)
    assert data['baseline']['counts'] == dict(existing=0, new=0, resolved=0, uncertain=2, error=1)
    assert all(s != 'resolved' for _, s in statuses(data))
    assert report(run(project, '--update-baseline'), 1)['baseline']['updated'] is False
    assert (project / 'baseline.json').read_bytes() == saved


def test_removed_file_resolved_but_discovery_error_is_uncertain(project):
    report(run(project, '--update-baseline'), 0)
    (project / 'src/job.py').unlink()
    assert statuses(report(run(project), 0)) == [('PLV001', 'resolved'), ('PLV002', 'resolved')]
    # A symlink input error must not turn stored findings into resolved findings.
    (project / 'src/job.py').symlink_to(project / 'elsewhere.py')
    data = report(run(project), 1)
    assert data['baseline']['counts']['uncertain'] == 2
    assert data['baseline']['counts']['error'] == 1


@pytest.mark.parametrize('args', [('--select', 'PLV001'), ('--ignore', 'PLV002'),
                                 ('--report-unused-suppressions',)])
def test_changed_policy_rejected_without_modifying_baseline(project, args):
    report(run(project, '--update-baseline'), 0)
    saved = (project / 'baseline.json').read_bytes()
    result = run(project, *args)
    assert result.returncode == 2
    assert 'policy' in result.stderr
    assert (project / 'baseline.json').read_bytes() == saved


def test_partial_scope_and_unsafe_flag_combinations_rejected(project):
    report(run(project, '--update-baseline'), 0)
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', 'src/job.py',
                             '--baseline', 'baseline.json'], cwd=project, capture_output=True, text=True)
    assert result.returncode == 2
    assert 'scope' in result.stderr
    assert run(project, '--fix').returncode == 2
    no_baseline = subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', 'src', '--update-baseline'],
                                 cwd=project, capture_output=True, text=True)
    assert no_baseline.returncode == 2


@pytest.mark.parametrize('contents', ['{}', '{', '{"version": 999}',
    '{"version": 1, "version": 1}', '[]'])
def test_invalid_baseline_is_usage_error_not_a_silent_reset(project, contents):
    (project / 'baseline.json').write_text(contents)
    result = run(project)
    assert result.returncode == 2
    assert 'baseline' in result.stderr.lower()
    assert (project / 'baseline.json').read_text() == contents


def test_baseline_entry_validation_and_missing_file(project):
    assert run(project).returncode == 2
    report(run(project, '--update-baseline'), 0)
    baseline = project / 'baseline.json'
    saved = json.loads(baseline.read_text())
    for field, value in [('path', '../escape.py'), ('path', '/absolute.py'), ('path', 'src/\ud800.py'), ('code', 'PLV000'),
                         ('line', 0), ('column', True), ('anchor', 'invalid')]:
        changed = json.loads(json.dumps(saved))
        changed['entries'][0][field] = value
        baseline.write_text(json.dumps(changed))
        assert run(project).returncode == 2, (field, value)


@pytest.mark.parametrize('format', ['text', 'sarif', 'github'])
def test_baseline_output_formats_show_classification(project, format):
    report(run(project, '--update-baseline'), 0)
    result = run(project, '--format', format)
    assert result.returncode == 0, result.stdout + result.stderr
    if format == 'sarif':
        results = json.loads(result.stdout)['runs'][0]['results']
        assert {r['baselineState'] for r in results} == {'unchanged'}
    else:
        assert '[existing]' in result.stdout


def test_sources_outside_baseline_root_and_symlink_baseline_rejected(project):
    nested = project / 'nested'
    nested.mkdir()
    assert run(project, '--baseline', 'nested/baseline.json', '--update-baseline').returncode == 2
    (project / 'baseline.json').symlink_to(nested / 'saved.json')
    assert run(project, '--update-baseline').returncode == 2
    assert not (nested / 'saved.json').exists()


def test_all_rule_identities_and_unrelated_loop_body_change(tmp_path):
    from datawrench.polars_vet import analyze
    from datawrench.polars_vet.baseline import compare, identities, load, policy, snapshot
    from .test_rules import CASES, HEADER

    root = tmp_path
    path = str(root / 'all.py')
    current_policy = policy([path], root, {'PLV%03d' % i for i in range(1, 12)}, False)
    for code, (positive, _) in CASES.items():
        for snippet in positive:
            text = HEADER + snippet
            diagnostics = analyze(text, path)
            entries = identities(text, diagnostics, root)
            assert all(entries), (code, snippet)
            previous = load(snapshot(entries, current_policy), current_policy)
            moved = '# unrelated line\n' + text
            changed = analyze(moved, path)
            rows = compare(changed, identities(moved, changed, root), previous, root)
            assert all(row.status == 'existing' for row in rows), (code, snippet)

    text = 'import polars as pl\ndf = pl.DataFrame({"x": [1]})\nfor row in df.iter_rows():\n    pass\n'
    diagnostics = analyze(text, path)
    entries = identities(text, diagnostics, root)
    moved = text.replace('    pass', '    unrelated = 42\n    pass')
    changed = analyze(moved, path)
    assert [r.status for r in compare(changed, identities(moved, changed, root), entries, root)] == ['existing']


def test_unicode_columns_and_suppression_resolution(project):
    path = project / 'src/job.py'
    path.write_bytes(('import polars as pl\né = "你好"; ' + BAD).replace('\n', '\r\n').encode())
    report(run(project, '--update-baseline'), 0)
    assert statuses(report(run(project), 0)) == [('PLV001', 'existing')]
    path.write_text('import polars as pl\né = "你好"; ' + BAD.rstrip() +
                    '  # polars-vet: ignore[PLV001] -- intentional eager IO\n')
    assert statuses(report(run(project), 0)) == [('PLV001', 'resolved')]


def test_unmatched_identity_is_uncertain_instead_of_resolved(tmp_path):
    from datawrench.polars_vet import analyze
    from datawrench.polars_vet import baseline

    path = str(tmp_path / 'source.py')
    diagnostics = analyze('import polars as pl\n' + BAD, path)
    old = baseline.identities('import polars as pl\n' + BAD, diagnostics, tmp_path)
    rows = baseline.compare(diagnostics, [None], old, tmp_path)
    assert [r.status for r in rows] == ['uncertain', 'uncertain']


def test_atomic_baseline_replace_refuses_changes(tmp_path):
    from datawrench.polars_vet.cli import atomic_replace, read_source

    path = tmp_path / 'baseline.json'
    path.write_text('{}\n')
    _, _, raw, info = read_source(path)
    path.write_text('{"changed": true}\n')
    with pytest.raises(ValueError, match='changed'):
        atomic_replace(path, b'{}\n', raw, info)
    with pytest.raises(ValueError, match='changed'):
        atomic_replace(path, b'{}\n', None, None)
    assert path.read_text() == '{"changed": true}\n'


def test_discovery_failure_does_not_resolve_unseen_findings(project):
    from datawrench.polars_vet import analyze
    from datawrench.polars_vet import baseline
    from datawrench.polars_vet.cli import error

    path = str(project / 'src/job.py')
    text = (project / 'src/job.py').read_text()
    diagnostics = analyze(text, path)
    entries = baseline.identities(text, diagnostics, project)
    rows = baseline.compare([error(str(project / 'src'), 'Cannot traverse directory')], [None],
                            entries, project, discovery_failed=True)
    assert sorted(r.status for r in rows) == ['error', 'uncertain', 'uncertain']


def test_skipped_symlink_directory_is_uncertain_instead_of_resolved(project):
    sub = project / 'src/sub'
    sub.mkdir()
    (project / 'src/job.py').rename(sub / 'job.py')
    report(run(project, '--update-baseline'), 0)
    sub.rename(project / 'outside')
    sub.symlink_to(project / 'outside', target_is_directory=True)
    data = report(run(project), 1)
    assert statuses(data) == [('PLV001', 'uncertain'), ('PLV002', 'uncertain')]
    saved = (project / 'baseline.json').read_bytes()
    assert report(run(project, '--update-baseline'), 1)['baseline']['updated'] is False
    assert (project / 'baseline.json').read_bytes() == saved


def test_linux_unusual_filename_roundtrip(project):
    path = project / 'src/job.py'
    path.rename(project / 'src/back\\slash,你好\n.py')
    report(run(project, '--update-baseline'), 0)
    assert sorted(statuses(report(run(project), 0))) == [('PLV001', 'existing'), ('PLV002', 'existing')]


def test_empty_baseline_and_explicit_schema_policy_reset(project):
    source = project / 'src/job.py'
    source.write_text('import polars as pl\n')
    assert report(run(project, '--update-baseline'), 0)['baseline']['counts']['new'] == 0
    source.write_text('import polars as pl\n' + BAD + EXPR)
    assert report(run(project), 1)['baseline']['counts']['new'] == 2
    (project / 'baseline.json').write_text('{broken')
    report(run(project, '--update-baseline'), 0)
    assert report(run(project), 0)['baseline']['counts']['existing'] == 2
    report(run(project, '--select', 'PLV001', '--update-baseline'), 0)
    assert statuses(report(run(project, '--select', 'PLV001'), 0)) == [('PLV001', 'existing')]
    assert run(project).returncode == 2


def test_resolved_entries_display_cwd_relative_paths(project):
    report(run(project, '--update-baseline'), 0)
    (project / 'src/job.py').unlink()
    data = report(run(project), 0)
    # Resolved entries use the same cwd-relative spelling as scanned diagnostics,
    # never an absolute path that leaks the checkout location.
    assert {d['path'] for d in data['diagnostics']} == {'src/job.py'}
    github = run(project, '--format', 'github')
    assert github.returncode == 0, github.stdout + github.stderr
    assert str(project) not in github.stdout
    assert 'file=src/job.py' in github.stdout
