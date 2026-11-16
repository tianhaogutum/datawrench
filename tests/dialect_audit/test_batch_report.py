from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.report import exit_code, render


ROOT = Path(__file__).resolve().parents[2]
BATCH = ROOT / 'tests/dialect_audit/fixtures/batch'


def test_batch_binding_outcomes_and_file_rollups():
    report = run_audit([str(BATCH)], 'snowflake', 'duckdb')
    statements = report['statements']
    assert [s['binding_status'] for s in statements] == [
        'bound', 'bound', 'failed', 'bound',
        'not-attempted', 'not-attempted', 'failed', 'bound',
    ]
    assert [(Path(s['file']).name, s['index'], s['line_start'], s['line_end']) for s in statements] == [
        ('01_valid.sql', 1, 2, 2), ('01_valid.sql', 2, 3, 4),
        ('02_unknown.sql', 1, 2, 2), ('02_unknown.sql', 2, 3, 3),
        ('03_writes.sql', 1, 2, 2), ('03_writes.sql', 2, 3, 3),
        ('03_writes.sql', 3, 4, 4), ('03_writes.sql', 4, 5, 5),
    ]
    assert [s['categories'] for s in statements] == [
        ['OK'], ['OK'], ['ANONYMOUS_FUNCTION', 'BIND_ERROR'], ['OK'],
        ['UNSUPPORTED'], ['UNSUPPORTED'], ['UNKNOWN_TABLE', 'BIND_ERROR'], ['OK'],
    ]
    assert all(s['target_sql'] for s in statements)
    summaries = report['file_summaries']
    assert list(summaries) == [str(BATCH / name) for name in ('01_valid.sql', '02_unknown.sql', '03_writes.sql')]
    assert summaries[str(BATCH / '01_valid.sql')] == {
        'status': 'ok', 'statements': 2, 'ok': 2, 'flagged': 0, 'skipped': False,
        'errors': 0, 'warnings': 0, 'categories': {'OK': 2},
        'binding_statuses': {'bound': 2},
    }
    assert summaries[str(BATCH / '02_unknown.sql')] == {
        'status': 'error', 'statements': 2, 'ok': 1, 'flagged': 1, 'skipped': False,
        'errors': 1, 'warnings': 1, 'categories': {'ANONYMOUS_FUNCTION': 1, 'BIND_ERROR': 1, 'OK': 1},
        'binding_statuses': {'bound': 1, 'failed': 1},
    }
    assert summaries[str(BATCH / '03_writes.sql')] == {
        'status': 'error', 'statements': 4, 'ok': 1, 'flagged': 3, 'skipped': False,
        'errors': 4, 'warnings': 0, 'categories': {'BIND_ERROR': 1, 'OK': 1, 'UNKNOWN_TABLE': 1, 'UNSUPPORTED': 2},
        'binding_statuses': {'bound': 1, 'failed': 1, 'not-attempted': 2},
    }
    assert report['summary'] == {
        'files': 3, 'statements': 8, 'ok': 4, 'flagged': 4, 'skipped_files': 0,
        'errors': 5, 'warnings': 1,
        'categories': {'ANONYMOUS_FUNCTION': 1, 'BIND_ERROR': 2, 'OK': 4, 'UNKNOWN_TABLE': 1, 'UNSUPPORTED': 2},
    }
    assert [exit_code(report, policy) for policy in ('error', 'warn', 'never')] == [1, 1, 0]


def test_static_only_outcomes_do_not_claim_binding():
    report = run_audit([str(BATCH)], 'snowflake', 'postgres')
    assert report['binding'] == 'not-available'
    assert all(s['binding_status'] == 'static-only' for s in report['statements'])
    assert [s['status'] for s in report['file_summaries'].values()] == ['ok', 'warn', 'ok']
    assert [s['binding_statuses'] for s in report['file_summaries'].values()] == [
        {'static-only': 2}, {'static-only': 2}, {'static-only': 4},
    ]
    assert [exit_code(report, policy) for policy in ('error', 'warn', 'never')] == [0, 1, 0]


def test_skipped_and_parse_failed_files_have_rollups(tmp_path):
    (tmp_path / '01_empty.sql').write_text('-- comment only\n')
    (tmp_path / '02_binary.sql').write_bytes(b'\0')
    (tmp_path / '03_template.sql').write_text('SELECT {{ ref("orders") }}')
    (tmp_path / '04_tokens.sql').write_text("SELECT 'unterminated")
    (tmp_path / '05_parse.sql').write_text('SELECT * FROM; SELECT 1;')
    report = run_audit([str(tmp_path)], 'snowflake', 'duckdb')
    summaries = list(report['file_summaries'].values())
    assert [s['status'] for s in summaries] == ['warn', 'error', 'warn', 'error', 'error']
    assert [s['skipped'] for s in summaries] == [True, True, True, False, False]
    assert [s['binding_statuses'] for s in summaries] == [
        {}, {}, {}, {'not-attempted': 1}, {'bound': 1, 'not-attempted': 1},
    ]
    assert [s['categories'] for s in summaries[:3]] == [
        {'EMPTY_INPUT': 1}, {'INPUT_SKIPPED': 1}, {'JINJA_SKIPPED': 1},
    ]
    assert [exit_code(report, policy) for policy in ('error', 'warn', 'never')] == [1, 1, 0]


@pytest.mark.parametrize('sql, category, target_sql', [
    ('VACUUM orders;', 'COMMAND_FALLBACK', None),
    ('SELECT * FROM catalog.s.t;', 'UNSUPPORTED', 'SELECT * FROM catalog.s.t'),
])
def test_binding_preflight_failure_is_not_an_explain_failure(tmp_path, sql, category, target_sql):
    path = tmp_path / 'query.sql'
    path.write_text(sql + '\nSELECT 1;')
    report = run_audit([str(path)], 'snowflake', 'duckdb')
    first, second = report['statements']
    assert first['categories'] == [category]
    assert first['binding_status'] == 'not-attempted'
    assert first['target_sql'] == target_sql
    assert second['binding_status'] == 'bound'
    assert report['file_summaries'][str(path)]['status'] == 'error'


@pytest.mark.parametrize('sql, category, severity, expected_exit', [
    ("SELECT strlen('x');", 'ANONYMOUS_FUNCTION', 'warn', 0),
    ('SELECT table_name FROM information_schema.tables;', 'UNKNOWN_TABLE', 'error', 1),
])
def test_successful_binding_preserves_other_findings(tmp_path, sql, category, severity, expected_exit):
    path = tmp_path / 'query.sql'
    path.write_text(sql)
    report = run_audit([str(path)], 'duckdb', 'duckdb')
    stmt = report['statements'][0]
    assert stmt['binding_status'] == 'bound'
    assert stmt['categories'] == [category]
    counts = report['file_summaries'][str(path)]
    assert counts['binding_statuses'] == {'bound': 1}
    assert counts['status'] == severity
    assert counts['ok'] == 0
    assert counts['flagged'] == 1
    assert exit_code(report, 'error') == expected_exit
    assert exit_code(report, 'warn') == 1
    assert exit_code(report, 'never') == 0


@pytest.mark.parametrize('target, expected, statuses', [
    ('duckdb', 1, ['bound', 'failed', 'not-attempted']),
    ('postgres', 0, ['static-only']),
])
@pytest.mark.parametrize('format', ['json', 'text', 'md', 'junit'])
def test_cli_batch_report_formats(target, expected, statuses, format):
    proc = subprocess.run([
        sys.executable, '-m', 'datawrench', 'dialect-audit', 'run', str(BATCH),
        '--read', 'snowflake', '--write', target, '--format', format,
    ], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert proc.returncode == expected, proc.stderr
    assert not proc.stderr
    if format == 'json':
        report = json.loads(proc.stdout)
        assert len(report['file_summaries']) == 3
        assert {s['binding_status'] for s in report['statements']} == set(statuses)
    elif format == 'junit':
        suite = ET.fromstring(proc.stdout)
        properties = {p.attrib['name']: p.attrib['value'] for p in suite.findall('properties/property')}
        assert len(json.loads(properties['file_summaries'])) == 3
        assert {json.loads(case.find('system-out').text)['binding_status']
                for case in suite.findall('testcase')} == set(statuses)
        assert suite.attrib['failures'] == ('4' if target == 'duckdb' else '0')
    else:
        for status in statuses:
            assert f'binding: {status}' in proc.stdout
        assert 'Files:' in proc.stdout
        assert '02_unknown.sql' in proc.stdout
        assert ('ERROR' if target == 'duckdb' else 'WARN') in proc.stdout
        if target == 'duckdb':
            assert '8 statements, 4 OK, 4 flagged; 5 errors, 1 warnings' in proc.stdout
