import ast
import json
import os
import subprocess
import sys

import pytest

from datawrench.polars_vet.cli import MAX_BYTES, atomic_write, read_source
from datawrench.polars_vet.plugin import PolarsVetChecker

BAD = 'import polars as pl\nq = pl.read_csv("local.csv").lazy()\n'


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', *map(str, args)], capture_output=True, text=True)


def test_cli_fixture_and_formats(tmp_path):
    path = tmp_path / 'sample.py'
    path.write_text(BAD)
    result = cli(path, '--format', 'json')
    assert result.returncode == 1
    data = json.loads(result.stdout)
    diagnostic, = data['diagnostics']
    assert diagnostic['code'] == 'PLV001'
    assert diagnostic['fixable'] is True
    result = cli(path, '--format', 'sarif')
    report = json.loads(result.stdout)
    assert report['version'] == '2.1.0'
    item, = report['runs'][0]['results']
    assert item['ruleId'] == 'PLV001'
    assert item['locations'][0]['physicalLocation']['region']['startLine'] == 2
    assert item['locations'][0]['physicalLocation']['artifactLocation']['uri'].startswith('file:///')
    result = cli(path, '--format', 'github')
    assert result.stdout.startswith('::warning file=')
    assert 'title=PLV001::' in result.stdout
    assert cli(path, '--exit-zero').returncode == 0
    assert cli(path, '--select', 'PLV999').returncode == 2
    assert cli(path, '--ignore', 'PLV001').returncode == 0


def test_cli_fix_and_permissions(tmp_path):
    path = tmp_path / 'sample.py'
    path.write_bytes(BAD.replace('\n', '\r\n').encode('utf-8'))
    path.chmod(0o640)
    assert cli(path, '--fix').returncode == 0
    assert b'pl.scan_csv("local.csv")\r\n' in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o640
    assert cli(path).returncode == 0
    original = path.read_bytes()
    assert cli(path, '--fix').returncode == 0
    assert path.read_bytes() == original


def test_syntax_other_file_and_no_execution(tmp_path):
    (tmp_path / 'syntax.py').write_text('def broken(:\n')
    (tmp_path / 'other.py').write_text(BAD)
    sentinel = tmp_path / 'should-not-exist'
    (tmp_path / 'danger.py').write_text(f'open({str(sentinel)!r}, "w").write("bad")\n')
    result = cli(tmp_path, '--format', 'json', '--select', 'PLV001')
    assert result.returncode == 1
    assert [d['code'] for d in json.loads(result.stdout)['diagnostics']] == ['PLV001', 'PLV000']
    assert not sentinel.exists()


def test_missing_and_invalid_encoding_and_limit(tmp_path):
    assert cli(tmp_path / 'missing.py').returncode == 1
    bad = tmp_path / 'bad.py'
    bad.write_bytes(b'\xff\xff')
    assert cli(bad).returncode == 1
    bad.write_bytes(b'#' * (MAX_BYTES + 1))
    result = cli(bad)
    assert result.returncode == 1
    assert '2 MiB' in result.stdout


def test_symlink_escape_refused(tmp_path):
    target = tmp_path / 'outside.py'
    target.write_text(BAD)
    folder = tmp_path / 'input'
    folder.mkdir()
    (folder / 'link.py').symlink_to(target)
    result = cli(folder, '--fix')
    assert result.returncode == 1
    assert 'symbolic link' in result.stdout
    assert target.read_text() == BAD
    alias = tmp_path / 'alias'
    alias.symlink_to(folder, target_is_directory=True)
    assert cli(alias / 'link.py', '--fix').returncode == 1


def test_hardlink_fix_replaces_only_requested_name(tmp_path):
    target = tmp_path / 'target.py'
    target.write_text(BAD)
    linked = tmp_path / 'linked.py'
    os.link(target, linked)
    assert cli(linked, '--fix').returncode == 0
    assert target.read_text() == BAD


def test_skip_venv_dedupe_and_selected_fix(tmp_path):
    (tmp_path / '.venv').mkdir()
    (tmp_path / '.venv/bad.py').write_text(BAD)
    path = tmp_path / 'sample.py'
    path.write_text(BAD)
    result = cli(tmp_path, path, '--format', 'json')
    assert len(json.loads(result.stdout)['diagnostics']) == 1
    assert cli(path, '--fix', '--select', 'PLV002').returncode == 0
    assert path.read_text() == BAD


def test_encoding_cookie(tmp_path):
    path = tmp_path / 'latin.py'
    path.write_bytes(('# coding: latin-1\n# café\n' + BAD).encode('latin-1'))
    assert cli(path, '--fix').returncode == 0
    assert b'caf\xe9' in path.read_bytes()


def test_refuse_concurrent_change(tmp_path):
    path = tmp_path / 'source.py'
    path.write_text(BAD)
    text, encoding, raw, info = read_source(path)
    path.write_text('# changed\n')
    with pytest.raises(ValueError, match='changed'):
        atomic_write(path, text, encoding, raw, info)
    assert path.read_text() == '# changed\n'


def test_github_annotation_path_escape(tmp_path):
    path = tmp_path / 'x,inject\nother.py'
    path.write_text(BAD)
    result = cli(path, '--format', 'github')
    assert 'x%2Cinject%0Aother.py' in result.stdout
    assert result.stdout.count('\n') == 1


def test_plugin_class_and_installed_entrypoint(tmp_path):
    path = tmp_path / 'plugin.py'
    path.write_text(BAD)
    tree = ast.parse(BAD)
    result, = list(PolarsVetChecker(tree, str(path)).run())
    assert result[:2] == (2, 4)
    assert result[2].startswith('PLV001 ')
    run = subprocess.run([sys.executable, '-m', 'flake8', '--isolated', '--select', 'PLV', str(path)], capture_output=True, text=True)
    assert run.returncode == 1
    assert 'PLV001' in run.stdout
    path.write_text(BAD.replace('.lazy()', '.lazy()  # noqa: PLV001'))
    run = subprocess.run([sys.executable, '-m', 'flake8', '--isolated', '--select', 'PLV', str(path)], capture_output=True, text=True)
    assert run.returncode == 0


def test_csv_pipeline_end_to_end(tmp_path):
    from pathlib import Path
    import shutil

    fixtures = Path(__file__).parent / 'fixtures'
    program = tmp_path / 'people_pipeline.py'
    shutil.copyfile(fixtures / program.name, program)
    shutil.copyfile(fixtures / 'people.csv', tmp_path / 'people.csv')
    before = subprocess.run([sys.executable, str(program)], cwd=tmp_path, capture_output=True, text=True)
    assert before.returncode == 0, before.stderr
    assert json.loads(before.stdout) == [
        {'id': 2, 'name': 'Lin', 'score': 20}, {'id': 4, 'name': 'Zoe', 'score': 40},
    ]
    assert cli(program).returncode == 1
    assert cli(program, '--fix').returncode == 0
    assert 'pl.scan_csv' in program.read_text()
    after = subprocess.run([sys.executable, str(program)], cwd=tmp_path, capture_output=True, text=True)
    assert after.returncode == 0, after.stderr
    assert before.stdout == after.stdout


def test_plugin_disable_noqa(tmp_path):
    path = tmp_path / 'plugin.py'
    path.write_text(BAD.replace('.lazy()', '.lazy()  # noqa: PLV001'))
    run = subprocess.run([sys.executable, '-m', 'flake8', '--isolated', '--select', 'PLV', '--disable-noqa', str(path)], capture_output=True, text=True)
    assert run.returncode == 1
    assert 'PLV001' in run.stdout


def test_cli_deleted_scanner_fix_regression(tmp_path):
    (tmp_path / 'data.csv').write_text('x\n7\n')
    path = tmp_path / 'deleted.py'
    source = (
        'from polars import read_csv as read, scan_csv as scan\n'
        'del scan\nq = read("data.csv").lazy()\n'
        'print(q.collect().to_dict(as_series=False))\n'
    )
    path.write_text(source)
    before = subprocess.run([sys.executable, str(path)], cwd=tmp_path, capture_output=True, text=True)
    assert before.returncode == 0, before.stderr
    result = cli(path, '--fix', '--format', 'json')
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report['fixedFiles'] == 0
    diagnostic, = report['diagnostics']
    assert diagnostic['code'] == 'PLV001'
    assert not diagnostic['fixable']
    assert path.read_text() == source
    after = subprocess.run([sys.executable, str(path)], cwd=tmp_path, capture_output=True, text=True)
    assert after.returncode == 0, after.stderr
    assert before.stdout == after.stdout == "{'x': [7]}\n"


def test_cli_reasoned_suppression_fixture_and_fix(tmp_path):
    from pathlib import Path

    path = tmp_path / 'suppressions.py'
    source = (Path(__file__).parent / 'fixtures/reasoned_suppressions.py').read_text()
    path.write_text(source)
    (tmp_path / 'data.csv').write_text('x\n7\n')
    before = subprocess.run([sys.executable, str(path)], cwd=tmp_path, capture_output=True, text=True)
    assert before.returncode == 0, before.stderr
    result = cli(path, '--format', 'json', '--report-unused-suppressions')
    assert result.returncode == 1
    assert [(d['line'], d['code']) for d in json.loads(result.stdout)['diagnostics']] == [
        (3, 'PLV002'), (4, 'PLV001'),
    ]
    result = cli(path, '--fix', '--format', 'json', '--report-unused-suppressions')
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report['fixedFiles'] == 1
    assert [(d['line'], d['code']) for d in report['diagnostics']] == [(3, 'PLV002')]
    assert path.read_text() == source.replace('another = p.read_csv("data.csv").lazy()', 'another = p.scan_csv("data.csv")')
    after = subprocess.run([sys.executable, str(path)], cwd=tmp_path, capture_output=True, text=True)
    assert after.returncode == 0, after.stderr
    assert before.stdout == after.stdout == "{'x': [7]}\n"
    assert json.loads(cli(path, '--fix', '--format', 'json').stdout)['fixedFiles'] == 0


def test_cli_fully_suppressed_fix_is_byte_preserving(tmp_path):
    path = tmp_path / 'suppressed.py'
    raw = (
        'import polars as p\r\n'
        'q = p.read_csv("data.csv").lazy()  # polars-vet: ignore[PLV001] -- intentional eager IO café\r\n'
    ).encode('utf-8')
    path.write_bytes(raw)
    result = cli(path, '--fix', '--format', 'json', '--report-unused-suppressions')
    assert result.returncode == 0
    assert json.loads(result.stdout)['fixedFiles'] == 0
    assert path.read_bytes() == raw


def test_cli_unused_suppressions_follow_select_ignore(tmp_path):
    path = tmp_path / 'unused.py'
    path.write_text(BAD.rstrip() + ' # polars-vet: ignore[PLV001, PLV002] -- expected both\n')
    assert cli(path).returncode == 0
    result = cli(path, '--report-unused-suppressions', '--format', 'json')
    assert result.returncode == 1
    diagnostic, = json.loads(result.stdout)['diagnostics']
    assert diagnostic['code'] == 'PLV000'
    assert diagnostic['message'] == 'Unused suppression for PLV002'
    assert cli(path, '--report-unused-suppressions', '--select', 'PLV001').returncode == 0
    assert cli(path, '--report-unused-suppressions', '--ignore', 'PLV002').returncode == 0


@pytest.mark.parametrize('format', ['text', 'json', 'sarif', 'github'])
def test_cli_invalid_suppression_always_diagnosed(tmp_path, format):
    path = tmp_path / 'invalid.py'
    path.write_text(BAD.rstrip() + ' # polars-vet: ignore[PLV001, PLV999] -- typo\n')
    result = cli(path, '--format', format, '--select', 'PLV002')
    assert result.returncode == 1
    assert 'PLV000' in result.stdout
    assert 'Unknown suppression rule code' in result.stdout


def test_plugin_reasoned_suppression_and_validation(tmp_path):
    path = tmp_path / 'plugin.py'
    path.write_text(BAD.rstrip() + ' # polars-vet: ignore[PLV001] -- intentional\n')
    run = subprocess.run([sys.executable, '-m', 'flake8', '--isolated', '--select', 'PLV', str(path)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout
    path.write_text(BAD.rstrip() + ' # polars-vet: ignore[PLV999] -- typo\n')
    run = subprocess.run([sys.executable, '-m', 'flake8', '--isolated', '--select', 'PLV', str(path)], capture_output=True, text=True)
    assert run.returncode == 1
    assert 'PLV000' in run.stdout
    assert 'PLV001' in run.stdout
