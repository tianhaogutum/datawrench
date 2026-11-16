from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import sqlglot


def cli(root, command, *args, write='duckdb'):
    return subprocess.run(
        [sys.executable, '-m', 'datawrench', 'dialect-audit', command, 'sql', '--read', 'snowflake',
         '--write', write, *map(str, args)], cwd=root, capture_output=True, text=True, timeout=15)


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'sql').mkdir()
    (tmp_path / 'sql/query.sql').write_text('SELECT 1;\n')
    return tmp_path


def save(root, *args, write='duckdb'):
    result = cli(root, 'baseline', '--output', 'baseline.json', *args, write=write)
    assert result.returncode == 0, result.stderr
    return root / 'baseline.json'


def compare(root, write='duckdb', *args):
    result = cli(root, 'diff', '--baseline', 'baseline.json', '--format', 'json', *args, write=write)
    assert result.returncode in (0, 1), result.stderr
    return result, json.loads(result.stdout)


def test_same_input_portable_and_explicit_updates(project, tmp_path_factory):
    baseline = save(project)
    original = baseline.read_bytes()
    save(project, '--update')
    assert baseline.read_bytes() == original
    assert str(project).encode() not in original
    result, delta = compare(project)
    assert result.returncode == 0
    assert delta['changed'] is False and delta['reasons'] == []
    assert delta['current_binding_statuses'] == {'bound': 1}
    other = tmp_path_factory.mktemp('checkout')
    shutil.copytree(project, other, dirs_exist_ok=True)
    result, delta = compare(other)
    assert result.returncode == 0, delta
    # Absolute vs relative input spellings and command order must be immaterial.
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'dialect-audit', 'diff',
                             str(other / 'sql/query.sql'), '--read', 'snowflake', '--write', 'duckdb',
                             '--root', str(other), '--baseline', str(other / 'baseline.json')],
                            cwd='/', capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    (project / 'sql/query.sql').write_text('SELECT MISSING_AUDIT_FN(1);\n')
    result, delta = compare(project)
    assert result.returncode == 1
    assert baseline.read_bytes() == original
    result = cli(project, 'baseline', '--output', 'baseline.json')
    assert result.returncode == 2 and '--update' in result.stderr
    assert baseline.read_bytes() == original
    save(project, '--update')
    assert baseline.read_bytes() != original
    result, delta = compare(project)
    assert result.returncode == 0  # Existing acknowledged findings remain in the snapshot.
    assert delta['current_binding_statuses'] == {'failed': 1}


def test_localizes_new_and_resolved_findings_sql_and_binding(project):
    save(project)
    (project / 'sql/query.sql').write_text('-- changed\nSELECT MISSING_AUDIT_FN(1);\n')
    result, delta = compare(project)
    assert result.returncode == 1
    assert {'new_findings', 'target_sql_changed', 'binding_changed', 'source_changed'} <= set(delta['reasons'])
    assert {d['category'] for d in delta['new_findings']} == {'ANONYMOUS_FUNCTION', 'BIND_ERROR'}
    for finding in delta['new_findings']:
        assert (finding['file'], finding['index'], finding['line_start']) == ('sql/query.sql', 1, 2)
    assert delta['sql_changes'][0]['before'] == 'SELECT 1'
    assert delta['sql_changes'][0]['after'] == 'SELECT MISSING_AUDIT_FN(1)'
    assert delta['binding_changes'][0]['before'] == 'bound'
    assert delta['binding_changes'][0]['after'] == 'failed'
    save(project, '--update')
    (project / 'sql/query.sql').write_text('SELECT 1;\n')
    result, delta = compare(project)
    assert result.returncode == 1
    assert {d['category'] for d in delta['resolved_findings']} == {'ANONYMOUS_FUNCTION', 'BIND_ERROR'}
    assert delta['new_findings'] == []
    text = cli(project, 'diff', '--baseline', 'baseline.json')
    assert text.returncode == 1
    assert 'RESOLVED' in text.stdout and 'sql/query.sql#1 L2-2' in text.stdout
    assert 'SQL_CHANGED' in text.stdout and 'SELECT MISSING_AUDIT_FN(1)' in text.stdout


def test_tool_version_reason_is_separate_from_sql_changes(project):
    baseline = save(project)
    saved = json.loads(baseline.read_text())
    assert {'dialect-audit', 'sqlglot', 'duckdb', 'PyYAML'} <= saved['tool_versions'].keys()
    saved['tool_versions']['sqlglot'] = 'old-fixture-version'
    baseline.write_text(json.dumps(saved))
    result, delta = compare(project)
    assert result.returncode == 1
    assert delta['reasons'] == ['tool_versions_changed']
    assert delta['version_changes'] == [{'tool': 'sqlglot', 'before': 'old-fixture-version', 'after': sqlglot.__version__}]
    assert delta['sql_changes'] == []
    # Synthetic older-generator output in the real baseline v1 format:
    # source is unchanged, and version attribution must not suppress SQL drift.
    saved['statements'][0]['target_sql'] = 'SELECT CAST(1 AS INT)'
    baseline.write_text(json.dumps(saved))
    result, delta = compare(project)
    assert result.returncode == 1
    assert delta['reasons'] == ['tool_versions_changed', 'target_sql_changed']
    assert delta['sql_changes'][0]['file'] == 'sql/query.sql'
    assert delta['sql_changes'][0]['before'] == 'SELECT CAST(1 AS INT)'
    assert delta['sql_changes'][0]['after'] == 'SELECT 1'


def test_static_only_and_inferred_binding_are_not_promoted(project):
    baseline = save(project, write='postgres')
    saved = json.loads(baseline.read_text())
    assert saved['statements'][0]['binding_status'] == 'static-only'
    result, delta = compare(project, 'postgres')
    assert result.returncode == 0
    assert delta['current_binding_statuses'] == {'static-only': 1}
    assert 'verified' not in saved
    (project / 'sql/query.sql').write_text('SELECT t.amount FROM missing t;')
    save(project, '--update', '--auto-stub')
    snapshot = json.loads(baseline.read_text())
    assert snapshot['statements'][0]['auto_stubs'][0]['type'] == 'DOUBLE'


def test_schema_content_change_even_if_queries_unchanged(project):
    schema = project / 'schema.sql'
    schema.write_text('CREATE TABLE t(id INT);')
    save(project, '--schema', 'schema.sql')
    schema.write_text('CREATE TABLE t(id BIGINT);')
    result, delta = compare(project, 'duckdb', '--schema', 'schema.sql')
    assert result.returncode == 1
    assert delta['reasons'] == ['configuration_changed']
    assert delta['configuration_changes'][0]['option'] == 'schema'
    assert delta['sql_changes'] == []


def test_file_statement_and_skip_coverage(project):
    save(project)
    (project / 'sql/query.sql').unlink()
    (project / 'sql/new.sql').write_text('SELECT {{ x }};')
    result, delta = compare(project)
    assert result.returncode == 1
    assert delta['file_changes'] == [
        {'file': 'sql/new.sql', 'change': 'added'}, {'file': 'sql/query.sql', 'change': 'removed'}]
    assert delta['statement_changes'][0]['change'] == 'removed'
    assert delta['new_findings'][0]['category'] == 'JINJA_SKIPPED'
    assert delta['new_findings'][0]['index'] is None
    save(project, '--update')
    (project / 'sql/new.sql').write_text('SELECT 2; SELECT 3;')
    result, delta = compare(project)
    assert len(delta['statement_changes']) == 2
    assert delta['resolved_findings'][0]['category'] == 'JINJA_SKIPPED'


@pytest.mark.parametrize('payload', ['{', '[]', '{}', '{"baseline_version": 999}',
    '{"baseline_version": true}', '{"baseline_version": 1, "statements": []}'])
def test_bad_baseline_fails_closed_without_rewrite(project, payload):
    baseline = project / 'baseline.json'
    baseline.write_text(payload)
    result = cli(project, 'diff', '--baseline', baseline)
    assert result.returncode == 2
    assert 'baseline' in result.stderr.lower() and 'Traceback' not in result.stderr
    assert not result.stdout
    assert baseline.read_text() == payload


def test_output_and_input_aliases_rejected(project):
    source = project / 'sql/query.sql'
    original = source.read_bytes()
    result = cli(project, 'baseline', '--output', source, '--update')
    assert result.returncode == 2
    assert source.read_bytes() == original
    baseline = save(project)
    original = baseline.read_bytes()
    target = project / 'linked.json'
    target.symlink_to(baseline)
    result = cli(project, 'baseline', '--output', target, '--update')
    assert result.returncode == 2 and 'symlink' in result.stderr
    assert baseline.read_bytes() == original


def test_root_boundary_is_configuration_error(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp('outside')
    result = cli(project, 'baseline', '--output', 'baseline.json', '--root', outside)
    assert result.returncode == 2
    assert 'root' in result.stderr.lower()
    assert not (project / 'baseline.json').exists()


@pytest.mark.parametrize('field,value', [
    ('statements', [{}]), ('notices', [{'file': []}]), ('files', ['../outside.sql']),
    ('tool_versions', {'sqlglot': 28}), ('configuration', None),
])
def test_nested_malformed_baselines_rejected(project, field, value):
    baseline = save(project)
    data = json.loads(baseline.read_text())
    data[field] = value
    baseline.write_text(json.dumps(data))
    result = cli(project, 'diff', '--baseline', baseline)
    assert result.returncode == 2 and 'baseline' in result.stderr
    assert 'Traceback' not in result.stderr and not result.stdout


def test_special_baseline_file_rejected(project):
    pipe = project / 'pipe.json'
    os.mkfifo(pipe)
    result = cli(project, 'diff', '--baseline', pipe)
    assert result.returncode == 2 and 'regular baseline file' in result.stderr


def test_committed_example_matches_pinned_audit():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-m', 'datawrench', 'dialect-audit', 'diff', 'examples/dialect_audit/snowflake',
        '--read', 'snowflake', '--write', 'duckdb', '--schema', 'examples/dialect_audit/schema.sql',
        '--baseline', 'examples/dialect_audit/snowflake.baseline.json', '--format', 'json'],
        cwd=root, capture_output=True, text=True, timeout=15)
    # The committed baseline records the library versions it was produced with (sqlglot 28.0.0,
    # DuckDB 1.4.1, PyYAML 6.0.3, tool 0.1.0); datawrench installs newer ranges, so a version-only
    # difference is expected. Any SQL, binding, finding or file drift still fails this test.
    assert result.returncode in (0, 1), result.stderr or result.stdout
    diff = json.loads(result.stdout)
    assert set(diff['reasons']) <= {'tool_versions_changed'}, diff['reasons']
    assert result.returncode == (1 if diff['reasons'] else 0)
    assert diff['current_binding_statuses'] == {'bound': 3, 'failed': 2}
