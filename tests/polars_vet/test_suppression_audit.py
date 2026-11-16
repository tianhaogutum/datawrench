"""Completeness contracts distinguish verified empty audits from failed inspection."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from datawrench.polars_vet import analyze_with_suppressions
from datawrench.polars_vet.analysis import Analyzer
from datawrench.polars_vet import cli as app

VALID = '# polars-vet: ignore[PLV001, PLV002] -- audit private café'
BAD = 'import polars as pl\nq = pl.read_csv("local.csv").lazy()'


def run(*args):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', *map(str, args)],
                          capture_output=True, text=True)


def audit(*rows):
    return {'complete': all(status == 'complete' for _, status in rows),
            'paths': sorted([{'path': str(path), 'status': status} for path, status in rows],
                            key=lambda row: row['path'])}


@pytest.mark.parametrize('source,status', [
    ('', 'complete'),
    ('x = 1\n', 'complete'),
    ('x = "# polars-vet: ignore[PLV999] -- just a string"\n', 'complete'),
    ('# noqa\n', 'complete'),
    (VALID + '\n', 'complete'),
    ('# polars-vet: ignore[PLV999] -- private-invalid\n', 'invalid-directive'),
    ('# polars-vet: ignore[PLV001, PLV001] -- private-invalid\n', 'invalid-directive'),
    ('# polars-vet: ignore[PLV001] -- \n', 'invalid-directive'),
    ('def broken(:\n', 'syntax-error'),
    ('return\n', 'syntax-error'),
    (VALID + '\nvalue = (\n', 'syntax-error'),
    (VALID + '\nvalue = """unterminated\n', 'syntax-error'),
    ('# polars-vet: ignore[PLV999] -- private-invalid\nreturn\n', 'syntax-error'),
])
def test_api_reports_inspection_status(source, status):
    result = analyze_with_suppressions(source, 'job.py', selected={'PLV001'})
    assert result.suppression_status == status
    if status != 'complete':
        assert any(d.code == 'PLV000' for d in result.diagnostics)
    if status == 'syntax-error' and source.startswith(VALID):
        assert [s.status for s in result.suppressions] == ['unknown', 'disabled']
    assert 'private-invalid' not in str(result.diagnostics) + str(result.suppressions)


def test_mixed_cli_retains_entries_and_deduplicates_paths(tmp_path):
    folder = tmp_path / 'src'
    folder.mkdir()
    sources = {
        'empty.py': b'',
        'good.py': (BAD + ' ' + VALID + '\n').encode(),
        'invalid.py': (BAD + ' # polars-vet: ignore[PLV999] -- private-invalid\n'
                       + VALID + '\n').encode(),
        'syntax.py': (VALID + '\nreturn\n').encode(),
        'tokens.py': (VALID + '\nx = (\n').encode(),
        'encoding.py': b'\xff\xff',
    }
    for name, raw in sources.items():
        (folder / name).write_bytes(raw)
    missing = tmp_path / 'missing.py'
    args = [folder, folder / 'good.py', missing, missing, '--format', 'json', '--select', 'PLV001']
    ordinary = run(*args)
    result = run(*args, '--show-suppressions')
    assert ordinary.returncode == result.returncode == 1
    data = json.loads(result.stdout)
    assert data['suppressionAudit'] == audit(
        (folder / 'empty.py', 'complete'), (folder / 'good.py', 'complete'),
        (folder / 'invalid.py', 'invalid-directive'), (folder / 'syntax.py', 'syntax-error'),
        (folder / 'tokens.py', 'syntax-error'), (folder / 'encoding.py', 'read-error'),
        (missing, 'discovery-error'))
    assert {k: v for k, v in data.items() if k not in {'suppressions', 'suppressionAudit'}} == json.loads(ordinary.stdout)
    assert [(Path(s['path']).name, s['code'], s['status']) for s in data['suppressions']] == [
        ('good.py', 'PLV001', 'used'), ('good.py', 'PLV002', 'disabled'),
        ('invalid.py', 'PLV001', 'unused'), ('invalid.py', 'PLV002', 'disabled'),
        ('syntax.py', 'PLV001', 'unknown'), ('syntax.py', 'PLV002', 'disabled'),
        ('tokens.py', 'PLV001', 'unknown'), ('tokens.py', 'PLV002', 'disabled')]
    assert any(d['code'] == 'PLV001' and d['path'].endswith('invalid.py') for d in data['diagnostics'])
    assert 'private-invalid' not in result.stdout
    assert all((folder / name).read_bytes() == raw for name, raw in sources.items())
    masked = run(*args, '--show-suppressions', '--exit-zero')
    assert masked.returncode == 0
    assert json.loads(masked.stdout) == data


def test_empty_directory_and_optional_unused_diagnostic_are_complete(tmp_path):
    result = run(tmp_path, '--format', 'json', '--show-suppressions')
    assert result.returncode == 0
    assert json.loads(result.stdout)['suppressionAudit'] == audit()
    path = tmp_path / 'empty.py'
    path.write_text(VALID + '\n')
    result = run(tmp_path, path, '--format', 'json', '--show-suppressions', '--report-unused-suppressions')
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert data['suppressionAudit'] == audit((path, 'complete'))
    assert len(data['diagnostics']) == 2


@pytest.mark.parametrize('failure', ['read', 'discovery', 'analysis', 'rewrite'])
def test_injected_failures_are_explicit_and_keep_successes(tmp_path, monkeypatch, capsys, failure):
    good, broken = tmp_path / 'good.py', tmp_path / 'broken.py'
    good.write_text('x = 1\n')
    broken.write_text(BAD + ' ' + VALID + '\nother = pl.read_csv("other.csv").lazy()\n')
    before = broken.read_bytes()
    args = [str(tmp_path), str(broken), '--format', 'json', '--show-suppressions', '--select', 'PLV001']
    if failure == 'read':
        original = app.read_source

        def read(path):
            if path == broken:
                raise PermissionError('private-exception')
            return original(path)

        monkeypatch.setattr(app, 'read_source', read)
    elif failure == 'discovery':
        original = app.os.walk

        def walk(root, **kwargs):
            kwargs['onerror'](PermissionError(13, 'private-exception', str(broken)))
            yield from original(root, **kwargs)

        monkeypatch.setattr(app.os, 'walk', walk)
    elif failure == 'analysis':
        original = Analyzer.visit

        def visit(self, node):
            if self.path == str(broken):
                self.used_suppressions.add((2, 'PLV001'))
                raise RecursionError('private-exception')
            return original(self, node)

        monkeypatch.setattr(Analyzer, 'visit', visit)
    else:
        def write(*args):
            raise OSError('private-exception')

        monkeypatch.setattr(app, 'atomic_write', write)
        args.append('--fix')
    assert app.main(['check', *args]) == 1
    output = capsys.readouterr().out
    data = json.loads(output)
    status = {'read': 'read-error', 'discovery': 'discovery-error',
              'analysis': 'analysis-error', 'rewrite': 'analysis-error'}[failure]
    assert data['suppressionAudit'] == audit((good, 'complete'), (broken, status))
    assert any(d['code'] == 'PLV000' for d in data['diagnostics'])
    assert 'private-exception' not in output
    if failure in {'analysis', 'rewrite'}:
        assert [s['status'] for s in data['suppressions']] == ['unknown', 'disabled']
    assert broken.read_bytes() == before


def test_supplied_ast_cannot_hide_tokenization_failure():
    result = analyze_with_suppressions(VALID + '\nx = (\n', tree=ast.parse('x = 1'), selected={'PLV001'})
    assert result.suppression_status == 'syntax-error'
    assert [s.status for s in result.suppressions] == ['unknown', 'disabled']
    assert any(d.code == 'PLV000' for d in result.diagnostics)


def test_baseline_and_fix_report_current_audit_without_snapshot_metadata(tmp_path):
    path, saved = tmp_path / 'job.py', tmp_path / 'baseline.json'
    path.write_text(BAD + '\n')
    assert run(path, '--baseline', saved, '--update-baseline').returncode == 0
    original_baseline = saved.read_bytes()
    path.write_text(BAD + ' ' + VALID + '\n')
    original_source = path.read_bytes()
    result = run(path, '--baseline', saved, '--format', 'json', '--show-suppressions')
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data['suppressionAudit'] == audit((path, 'complete'))
    assert path.read_bytes() == original_source and saved.read_bytes() == original_baseline
    path.write_text(VALID + '\nx = (\n')
    result = run(path, '--baseline', saved, '--update-baseline', '--exit-zero', '--format', 'json', '--show-suppressions')
    assert result.returncode == 1
    assert json.loads(result.stdout)['suppressionAudit'] == audit((path, 'syntax-error'))
    assert saved.read_bytes() == original_baseline
    path.write_text(BAD + '\n')
    result = run(path, '--fix', '--format', 'json', '--show-suppressions')
    assert result.returncode == 0
    assert json.loads(result.stdout)['suppressionAudit'] == audit((path, 'complete'))
    assert 'pl.scan_csv' in path.read_text()
    assert 'suppressionAudit' not in json.loads(saved.read_text())


def test_duplicate_missing_spellings_and_symlink_read_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = Path('src')
    source.mkdir()
    target = source / 'target.py'
    target.write_text('x = 1\n')
    link = source / 'link.py'
    link.symlink_to('target.py')
    missing = Path('missing.py')
    result = run(source, missing, os.path.abspath(missing), '--format', 'json', '--show-suppressions')
    assert result.returncode == 1
    assert json.loads(result.stdout)['suppressionAudit'] == audit(
        (link, 'read-error'), (target, 'complete'), (os.path.abspath(missing), 'discovery-error'))
