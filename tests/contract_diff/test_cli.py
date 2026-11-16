import json
from pathlib import Path
import subprocess
import sys

import pytest

from datawrench import __version__
from datawrench.contract_diff.cli import main, render
from datawrench.contract_diff.diff import Change, Report
from .conftest import artifact, node

ROOT = Path(__file__).resolve().parents[2]  # repository root
BASE = ROOT / 'tests/contract_diff/fixtures/base/manifest.json'
HEAD = ROOT / 'tests/contract_diff/fixtures/head/manifest.json'


def run(*args):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'contract-diff', *map(str, args)], text=True, capture_output=True, cwd=ROOT)


@pytest.mark.parametrize('fmt,marker', [('text', 'BREAKING model.shop.orders'), ('json', '"breaking": 6'),
                                       ('markdown', '| BREAKING | model.shop.orders'), ('github', '::error title=')])
def test_formats_real_cli(fmt, marker):
    result = run('--base', BASE, '--head', HEAD, '--format', fmt)
    assert result.returncode == 1 and not result.stderr and marker in result.stdout
    assert 'type.amount' in result.stdout
    if fmt == 'json':
        data = json.loads(result.stdout)
        assert data['summary']['changes'] == 10 and data['summary']['breaking'] == 6


@pytest.mark.parametrize('fail_on,code', [('breaking', 1), ('any', 1), ('none', 0)])
def test_fail_modes(fail_on, code):
    result = run('--base', BASE, '--head', HEAD, '--fail-on', fail_on)
    assert result.returncode == code


def test_identical_cli_and_installed_entrypoint():
    result = run('--base', BASE, '--head', BASE, '--format', 'json')
    assert result.returncode == 0 and json.loads(result.stdout)['summary']['changes'] == 0
    result = run('--version')
    assert result.returncode == 0 and result.stdout.strip() == __version__


@pytest.mark.parametrize('content', ['{broken', '{}', '{"nodes": []}', '{"nodes": {}, "nodes": {}}'])
def test_invalid_cli_input(tmp_path, content):
    path = tmp_path / 'bad.json'
    path.write_text(content)
    result = run('--base', path, '--head', HEAD)
    assert result.returncode == 2 and not result.stdout and 'input error:' in result.stderr
    assert 'Traceback' not in result.stderr


def test_missing_file_and_bad_option(tmp_path):
    assert run('--base', tmp_path / 'missing', '--head', HEAD).returncode == 2
    assert run('--base', BASE, '--head', HEAD, '--dialect', 'oracle').returncode == 2
    assert run('--base', BASE).returncode == 2


def test_no_enforced_cli(tmp_path):
    path = tmp_path / 'none.json'
    path.write_text(json.dumps(artifact(node(enforced=False))))
    result = run('--base', path, '--head', path)
    assert result.returncode == 0 and 'No enforced contracts found' in result.stdout
    included = run('--base', path, '--head', path, '--include-unenforced')
    assert included.returncode == 0 and '1 models compared' in included.stdout


def test_read_only_no_subprocess_or_network(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('CLI must not execute external commands or access network')
    import socket
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    before = {p: p.read_bytes() for p in (BASE, HEAD)}
    assert main(['--base', str(BASE), '--head', str(HEAD), '--fail-on', 'none']) == 0
    assert before == {p: p.read_bytes() for p in (BASE, HEAD)}
    assert not list(tmp_path.iterdir())
    assert 'BREAKING' in capsys.readouterr().out


def test_unparseable_and_sql_input_not_executed(tmp_path):
    marker = tmp_path / 'executed'
    data = artifact(node(f"INT; COPY (SELECT 1) TO '{marker}'"))
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(data))
    result = run('--base', path, '--head', path, '--format', 'json')
    assert result.returncode == 0 and not marker.exists()
    report = json.loads(result.stdout)
    assert report['warnings'] and report['changes'][0]['kind'] == 'type_unparseable'
    assert run('--base', path, '--head', path, '--fail-on', 'any').returncode == 1


def test_output_escaping():
    malicious = 'x\n::error::injected\x1b[31m|<script>`\u202e'
    report = Report('duckdb', changes=[Change(malicious, 'type_changed', True, malicious)])
    github = render(report, 'github')
    assert len(github.splitlines()) == 2 and '%0A::error::injected' in github
    assert '\x1b' not in github and '\u202e' not in github
    markdown = render(report, 'markdown')
    assert '<script>' not in markdown and '&#124;' in markdown and '&#96;' in markdown
    assert len(markdown.splitlines()) == 7
    assert '\x1b' not in render(report, 'text')
    assert json.loads(render(report, 'json'))['changes'][0]['model'] == malicious


def test_surrogate_text_is_safe_json(tmp_path):
    data = artifact(node('custom\ud800'))
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(data))
    result = run('--base', path, '--head', path, '--format', 'json')
    assert result.returncode == 0 and json.loads(result.stdout)['warnings']


def test_real_dbt_generated_node_slice():
    base = ROOT / 'tests/contract_diff/fixtures/real/base.json'
    head = ROOT / 'tests/contract_diff/fixtures/real/head.json'
    artifact = json.loads(base.read_text())
    assert artifact['metadata']['dbt_version'] == '1.12.6'
    assert artifact['nodes']['model.contract_fixture.orders']['config']['contract']['enforced']
    result = run('--base', base, '--head', head, '--format', 'json')
    assert result.returncode == 1 and not result.stderr
    report = json.loads(result.stdout)
    assert report['summary']['breaking'] == 1 and report['summary']['changes'] == 1 and not report['warnings']
    assert report['changes'][0]['details'][0]['path'] == 'type.amount'


def test_baseline_fixture_counts_and_script_output():
    result = subprocess.run([sys.executable, 'tests/contract_diff/compare_baseline.py', '--json'], text=True, capture_output=True, cwd=ROOT)
    assert result.returncode == 0 and not result.stderr
    data = json.loads(result.stdout)
    assert data['summary'] == {'type_pairs': 7, 'baseline_unchanged': 5, 'baseline_missed_breaking': 4, 'baseline_false_mismatches': 1}
    assert {r['column'] for r in data['pairs'] if r['baseline_unchanged'] and r['breaking']} == {'amount', 'payload', 'lookup', 'tags'}
    result = subprocess.run([sys.executable, 'tests/contract_diff/compare_baseline.py'], text=True, capture_output=True, cwd=ROOT)
    assert result.returncode == 0 and 'baseline=UNCHANGED; AST=nested_change BREAKING' in result.stdout
