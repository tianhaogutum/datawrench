from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from datawrench.dialect_audit.audit import run_audit, split_statements
from datawrench.dialect_audit.report import emit, exit_code, render
from datawrench.dialect_audit.schema import Binder, InputError

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'tests/dialect_audit/fixtures/corpus'
SCHEMA = ROOT / 'examples/dialect_audit/schema.sql'
CASES = json.loads((CORPUS / 'manifest.json').read_text())


def sql_file(tmp_path, sql, name='query.sql'):
    path = tmp_path / name
    path.write_text(sql)
    return str(path)


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'dialect-audit', 'run', *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['file'])
def test_real_category_fixtures(case):
    report = run_audit([str(CORPUS / case['file'])], case['read'], 'duckdb', str(SCHEMA), schema_read='snowflake')
    assert report['statements'][0]['categories'] == case['categories']
    assert report['summary']['statements'] == 1


def test_e2e_snowflake_report():
    proc = cli('examples/dialect_audit/snowflake', '--read', 'snowflake', '--write', 'duckdb',
               '--schema', SCHEMA, '--format', 'json')
    assert proc.returncode == 1, proc.stderr
    assert not proc.stderr
    report = json.loads(proc.stdout)
    assert report['summary'] == {
        'files': 1, 'statements': 5, 'ok': 3, 'flagged': 2, 'skipped_files': 0,
        'errors': 2, 'warnings': 1,
        'categories': {'OK': 3, 'ANONYMOUS_FUNCTION': 1, 'BIND_ERROR': 2},
    }
    assert report['statements'][4]['line_start'] == 10
    assert 'missing_column' in report['statements'][4]['diagnostics'][0]['message']
    assert report['function_inventory'] == {'my_udf': 1, 'sum': 1, 'timestamp_trunc': 1}


def test_token_aware_line_mapping_and_recovery(tmp_path):
    path = sql_file(tmp_path, "-- leading comment\nSELECT ';' AS x;\n/* ; */\nSELECT\n  'two;three';\nSELECT * FROM;\nSELECT 4; -- tail\n")
    report = run_audit([path], 'snowflake', 'duckdb')
    assert [(s['line_start'], s['line_end']) for s in report['statements']] == [(2, 2), (4, 5), (6, 6), (7, 7)]
    assert [s['categories'] for s in report['statements']] == [['OK'], ['OK'], ['PARSE_ERROR'], ['OK']]
    assert '\x1b' not in report['statements'][2]['diagnostics'][0]['message']


def test_dollar_quoted_string():
    parts = split_statements("SELECT $$a;b$$;\nSELECT 2;", 'postgres')
    assert parts == [('SELECT $$a;b$$', 1, 1, 1, 14), ('SELECT 2', 2, 2, 1, 8)]


def test_token_error_is_parse_error(tmp_path):
    report = run_audit([sql_file(tmp_path, "SELECT 'unterminated")], 'snowflake', 'duckdb')
    assert report['statements'][0]['categories'] == ['PARSE_ERROR']


@pytest.mark.parametrize('schema_name', ['schema.sql', 'schema.yml'])
def test_ddl_yaml_match(schema_name):
    report = run_audit([str(CORPUS / 'ok.sql')], 'snowflake', 'duckdb', str(ROOT / 'examples/dialect_audit' / schema_name))
    assert report['statements'][0]['categories'] == ['OK']


def test_schema_different_dialect_and_qualified_table(tmp_path):
    schema = tmp_path / 'schema.sql'
    schema.write_text('CREATE TABLE `sales`.`orders` (`id` INT64, `tags` ARRAY<STRING>);')
    path = sql_file(tmp_path, 'SELECT id, tags FROM sales.orders;')
    report = run_audit([path], 'snowflake', 'duckdb', str(schema), schema_read='bigquery')
    assert report['statements'][0]['categories'] == ['OK']


@pytest.mark.parametrize('schema', [
    'DROP TABLE orders;',
    "CREATE TABLE stolen AS SELECT * FROM read_csv('/etc/passwd');",
    'CREATE TABLE orders (id INT DEFAULT random());',
    'CREATE TABLE orders (id INT PRIMARY KEY);',
    'CREATE TABLE orders (id INT, ID INT);',
    'CREATE TABLE orders (id STRANGE_TYPE);',
    'CREATE TABLE orders (id INT); CREATE TABLE orders (id INT);',
    '',
])
def test_unsafe_or_unsupported_ddl_rejected(tmp_path, schema):
    fixture = tmp_path / 'schema.sql'
    fixture.write_text(schema)
    with pytest.raises(InputError, match='schema'):
        run_audit([str(CORPUS / 'ok.sql')], 'snowflake', 'duckdb', str(fixture))


@pytest.mark.parametrize('schema', [
    'orders: {id: INT}\norders: {x: INT}',
    'orders: {id: INT, id: TEXT}',
    'orders: {id: INT, ID: TEXT}',
    'orders: {id: 5}',
    'orders: [id]',
    'orders: {}',
    'orders: &a {id: INT}\nother: *a',
    '!!python/object/apply:os.system [echo unsafe]',
    'orders: {id: "INT); DROP TABLE t;--"}',
    'orders: {id: "INT garbage"}',
    '"t; DROP TABLE x": {id: INT}',
    '"t AS alias": {id: INT}',
    '42: {id: INT}',
])
def test_invalid_yaml_rejected(tmp_path, schema):
    fixture = tmp_path / 'schema.yml'
    fixture.write_text(schema)
    with pytest.raises(InputError):
        run_audit([str(CORPUS / 'ok.sql')], 'snowflake', 'duckdb', str(fixture))


def test_quoted_identifiers_not_interpolated_as_sql(tmp_path):
    fixture = tmp_path / 'schema.yml'
    # YAML table names use SQL identifier syntax, not raw strings.
    fixture.write_text("'\"weird table\"':\n  'x\"; DROP TABLE other;--': INTEGER\n")
    path = sql_file(tmp_path, 'SELECT "x""; DROP TABLE other;--" FROM "weird table";')
    report = run_audit([path], 'snowflake', 'duckdb', str(fixture))
    assert report['statements'][0]['categories'] == ['OK']


def test_auto_stub_alias_scope_and_statement_isolation(tmp_path):
    path = sql_file(tmp_path, 'SELECT o.amount FROM sales.orders o; SELECT o.price FROM sales.orders o;')
    report = run_audit([path], 'snowflake', 'duckdb', auto_stub=True)
    assert report['summary']['ok'] == 2
    assert report['statements'][0]['auto_stubs'] == [{'table': 'sales.orders', 'columns': ['amount'], 'type': 'DOUBLE'}]
    assert report['statements'][1]['auto_stubs'][0]['columns'] == ['price']


def test_auto_stub_does_not_hide_known_schema_missing_column(tmp_path):
    path = sql_file(tmp_path, 'SELECT o.price FROM orders o;')
    report = run_audit([path], 'snowflake', 'duckdb', str(SCHEMA), auto_stub=True)
    assert report['statements'][0]['categories'] == ['BIND_ERROR']
    assert not report['statements'][0]['auto_stubs']


@pytest.mark.parametrize('query', ['SELECT x FROM absent', 'SELECT t.* FROM absent t'])
def test_auto_stub_requires_qualified_columns(tmp_path, query):
    report = run_audit([sql_file(tmp_path, query)], 'snowflake', 'duckdb', auto_stub=True)
    assert report['statements'][0]['categories'] == ['UNKNOWN_TABLE', 'BIND_ERROR']


def test_ctes_are_not_unknown_tables(tmp_path):
    query = 'WITH c AS (SELECT o.id FROM orders o) SELECT c.id FROM c'
    report = run_audit([sql_file(tmp_path, query)], 'snowflake', 'duckdb', str(SCHEMA))
    assert report['statements'][0]['categories'] == ['OK']


def test_join_and_cte_auto_stub(tmp_path):
    query = 'WITH c AS (SELECT o.id FROM sales.orders o) SELECT c.id FROM c JOIN customers x ON c.id = x.id'
    report = run_audit([sql_file(tmp_path, query)], 'snowflake', 'duckdb', auto_stub=True)
    assert report['statements'][0]['categories'] == ['OK']
    assert {s['table'] for s in report['statements'][0]['auto_stubs']} == {'sales.orders', 'main.customers'}


@pytest.mark.parametrize('query', [
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_parquet('https://example.invalid/secret.parquet')",
    "SELECT * FROM glob('/etc/*')",
])
def test_external_io_disabled(tmp_path, query):
    report = run_audit([sql_file(tmp_path, query)], 'duckdb', 'duckdb')
    assert 'BIND_ERROR' in report['statements'][0]['categories']
    assert 'disabled' in report['statements'][0]['diagnostics'][-1]['message'].lower() or 'does not exist' in report['statements'][0]['diagnostics'][-1]['message']


@pytest.mark.parametrize('query', [
    'CREATE TABLE changed (id INT)', 'DROP TABLE orders', 'INSERT INTO orders VALUES (1, 2, 3, CURRENT_TIMESTAMP)',
    "COPY orders TO '/tmp/should-not-be-created.csv'", 'SELECT * INTO changed FROM orders',
    "ATTACH '/tmp/nope.db' AS nope", 'PRAGMA version',
])
def test_non_queries_never_executed(query):
    from datawrench.dialect_audit.audit import audit_statement
    binder = Binder()
    try:
        binder.load(SCHEMA, 'snowflake')
        report = audit_statement(query, 'input.sql', 1, 1, 1, 'duckdb', 'duckdb', binder)
        assert any(c in report['categories'] for c in ['UNSUPPORTED', 'COMMAND_FALLBACK', 'PARSE_ERROR'])
        assert binder.conn.execute('SELECT COUNT(*) FROM orders').fetchone() == (0,)
        assert binder.conn.execute("SELECT table_name FROM information_schema.tables").fetchall() == [('orders',)]
    finally:
        binder.close()


def test_inventory_per_file_and_total(tmp_path):
    first = sql_file(tmp_path, 'SELECT ABS(-1), ABS(-2), MY_UDF(1)', 'a.sql')
    second = sql_file(tmp_path, 'SELECT ABS(-3)', 'b.sql')
    report = run_audit([first, second], 'snowflake', 'postgres')
    assert report['function_inventory'] == {'abs': 3, 'my_udf': 1}
    assert report['file_function_inventory'][first] == {'abs': 2, 'my_udf': 1}
    assert report['binding'] == 'not-available'


def test_skips_jinja_and_binary_and_continues(tmp_path):
    sql_file(tmp_path, 'SELECT {{ ref("orders") }}', 'jinja.sql')
    (tmp_path / 'binary.sql').write_bytes(b'\xff\x00')
    (tmp_path / 'encoding.sql').write_bytes(b'\xff\xfe')
    sql_file(tmp_path, 'SELECT 1', 'good.sql')
    report = run_audit([str(tmp_path)], 'snowflake', 'duckdb')
    assert report['summary']['ok'] == 1
    assert report['summary']['skipped_files'] == 3
    assert {n['category'] for n in report['notices']} == {'JINJA_SKIPPED', 'INPUT_SKIPPED'}
    assert exit_code(report, 'error') == 1


def test_input_size_limit(tmp_path):
    (tmp_path / 'big.sql').write_bytes(b' ' * (2 * 1024 * 1024 + 1))
    report = run_audit([str(tmp_path)], 'snowflake', 'duckdb')
    assert report['notices'][0]['category'] == 'INPUT_SKIPPED'


@pytest.mark.parametrize('policy, expected', [('error', 0), ('warn', 1), ('never', 0)])
def test_warning_exit_policy(tmp_path, policy, expected):
    path = sql_file(tmp_path, 'SELECT MY_UDF(1)')
    proc = cli(path, '--read', 'snowflake', '--write', 'postgres', '--fail-on', policy)
    assert proc.returncode == expected, proc.stderr


@pytest.mark.parametrize('format', ['text', 'md', 'json', 'junit'])
def test_formats_and_failure_policy(format):
    proc = cli(CORPUS / 'bind.sql', '--read', 'snowflake', '--write', 'duckdb', '--schema', SCHEMA, '--format', format)
    assert proc.returncode == 1
    assert not proc.stderr
    if format == 'json':
        assert json.loads(proc.stdout)['version'] == 1
    elif format == 'junit':
        root = ET.fromstring(proc.stdout)
        assert root.attrib['tests'] == '1'
        assert root.attrib['failures'] == '1'
        assert root.find('testcase/failure') is not None
        properties = {p.attrib['name']: p.attrib['value'] for p in root.findall('properties/property')}
        assert properties['binding'] == 'duckdb-explain'
        assert json.loads(properties['function_inventory']) == {}
        assert len(json.loads(properties['file_function_inventory'])) == 1
    else:
        assert 'BIND_ERROR' in proc.stdout


def test_junit_warning_policy_and_invalid_xml_chars(tmp_path):
    path = sql_file(tmp_path, 'SELECT MY_UDF(1)')
    report = run_audit([path], 'snowflake', 'postgres')
    report['statements'][0]['diagnostics'][0]['message'] += '\x01\x1b'
    assert ET.fromstring(render(report, 'junit', 'warn')).attrib['failures'] == '1'
    assert ET.fromstring(render(report, 'junit', 'never')).attrib['failures'] == '0'


def test_junit_skip_notice(tmp_path):
    path = sql_file(tmp_path, 'SELECT {{ x }}')
    report = run_audit([path], 'snowflake', 'duckdb')
    root = ET.fromstring(render(report, 'junit'))
    assert root.attrib['skipped'] == '1'
    assert root.attrib['tests'] == '1'


def test_invalid_dialect_lists_options():
    proc = cli(CORPUS / 'ok.sql', '--read', 'bogus', '--write', 'duckdb')
    assert proc.returncode == 2
    assert 'available:' in proc.stderr and 'snowflake' in proc.stderr
    assert not proc.stdout


def test_bad_schema_exit_two_and_context(tmp_path):
    schema = tmp_path / 'bad.sql'
    schema.write_text('DROP TABLE orders;')
    proc = cli(CORPUS / 'ok.sql', '--read', 'snowflake', '--write', 'duckdb', '--schema', schema, '--fail-on', 'never')
    assert proc.returncode == 2
    assert 'statement 1' in proc.stderr and 'DROP TABLE orders' in proc.stderr


@pytest.mark.parametrize('option', ['--schema', '--auto-stub'])
def test_binding_options_rejected_for_other_targets(option):
    options = [option, str(SCHEMA)] if option == '--schema' else [option]
    proc = cli(CORPUS / 'ok.sql', '--read', 'snowflake', '--write', 'postgres', *options)
    assert proc.returncode == 2


def test_missing_path_exit_two(tmp_path):
    proc = cli(tmp_path / 'missing.sql', '--read', 'snowflake', '--write', 'duckdb')
    assert proc.returncode == 2


def test_empty_directory_exit_two(tmp_path):
    with pytest.raises(InputError, match='no .sql files'):
        run_audit([str(tmp_path)], 'snowflake', 'duckdb')


def test_input_deduplication_and_symlink_rejection(tmp_path):
    path = Path(sql_file(tmp_path, 'SELECT 1'))
    assert run_audit([str(path), str(path)], 'snowflake', 'duckdb')['summary']['statements'] == 1
    link = tmp_path / 'link.sql'
    link.symlink_to(path)
    with pytest.raises(InputError, match='symlink'):
        run_audit([str(link)], 'snowflake', 'duckdb')


def test_emit_stays_in_target_and_never_overwrites(tmp_path):
    path = sql_file(tmp_path, 'SELECT 1; SELECT 2')
    report = run_audit([path], 'snowflake', 'duckdb')
    report['statements'][0]['file'] = '../../outside/../../evil.sql'
    output = tmp_path / 'emitted'
    emit(report, str(output))
    files = sorted(output.iterdir())
    assert len(files) == 3
    assert all(p.parent == output for p in files)
    assert json.loads((output / 'report.json').read_text()) == report
    assert len(report['emitted_files']) == 2
    assert {entry['output'] for entry in report['emitted_files']} == {p.name for p in output.glob('*.sql')}
    assert sorted(p.read_text() for p in output.glob('*.sql')) == ['SELECT 1;\n', 'SELECT 2;\n']
    with pytest.raises(InputError, match='fresh directory'):
        emit(report, str(output))


def test_emit_symlinks_rejected_without_writing(tmp_path):
    report = run_audit([str(CORPUS / 'ok.sql')], 'snowflake', 'postgres')
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(InputError):
        emit(report, str(link))
    outside = tmp_path / 'outside'
    outside.write_text('unchanged')
    (real / 'report.json').symlink_to(outside)
    with pytest.raises(InputError):
        emit(report, str(real))
    assert outside.read_text() == 'unchanged'


def test_emit_cli(tmp_path):
    proc = cli(CORPUS / 'ok.sql', '--read', 'snowflake', '--write', 'duckdb', '--schema', SCHEMA, '--emit-dir', tmp_path / 'out')
    assert proc.returncode == 0, proc.stderr
    assert len(list((tmp_path / 'out').glob('*.sql'))) == 1


def test_reproducible_comparison():
    proc = subprocess.run([sys.executable, 'tests/dialect_audit/compare_plain_transpile.py', '--json'],
                          cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result['statements'] == 8
    assert result['plain_flagged'] == 3
    assert result['audit_flagged'] == 7
    assert result['plain_categories'] == {'COMMAND_FALLBACK': 1, 'PARSE_ERROR': 1, 'UNSUPPORTED': 1}
    assert result['audit_categories'] == {
        'ANONYMOUS_FUNCTION': 1, 'BIND_ERROR': 3, 'COMMAND_FALLBACK': 1, 'PARSE_ERROR': 1,
        'ROUNDTRIP_UNSTABLE': 1, 'UNKNOWN_TABLE': 1, 'UNSUPPORTED': 1,
    }


def test_schema_unicode_identifiers_preserved(tmp_path):
    schema = tmp_path / 'schema.sql'
    schema.write_text('CREATE TABLE "Ä" ("Ö" INTEGER, "ö" INTEGER)')
    report = run_audit([sql_file(tmp_path, 'SELECT "Ö", "ö" FROM "Ä"')], 'duckdb', 'duckdb', str(schema))
    assert report['statements'][0]['categories'] == ['OK']


def test_empty_sql_has_notice(tmp_path):
    report = run_audit([sql_file(tmp_path, '-- comment only\n;')], 'snowflake', 'duckdb')
    assert report['summary']['statements'] == 0
    assert report['notices'][0]['category'] == 'EMPTY_INPUT'
    assert exit_code(report, 'warn') == 1


def test_special_file_rejected(tmp_path):
    import os
    path = tmp_path / 'pipe.sql'
    os.mkfifo(path)
    with pytest.raises(InputError, match='regular'):
        run_audit([str(path)], 'snowflake', 'duckdb')


def test_dynamic_sql_cannot_write_and_audit_continues(tmp_path):
    query = "SELECT * FROM query('CREATE TABLE nope(x INT)'); SELECT 1;"
    report = run_audit([sql_file(tmp_path, query)], 'duckdb', 'duckdb')
    assert 'BIND_ERROR' in report['statements'][0]['categories']
    assert report['statements'][1]['categories'] == ['OK']


def test_session_configuration_locks_and_no_persistent_secrets():
    binder = Binder()
    try:
        settings = dict(binder.conn.execute("SELECT name, value FROM duckdb_settings()").fetchall())
        assert settings['enable_external_access'] == 'false'
        assert settings['allow_persistent_secrets'] == 'false'
        assert settings['python_enable_replacements'] == 'false'
        assert settings['lock_configuration'] == 'true'
        assert settings['temp_directory'] == ''
    finally:
        binder.close()


def test_schema_missing_file_exit_two(tmp_path):
    proc = cli(CORPUS / 'ok.sql', '--read', 'snowflake', '--write', 'duckdb', '--schema', tmp_path / 'missing.sql')
    assert proc.returncode == 2
    assert 'schema' in proc.stderr


def test_junit_parse_failure_is_valid_xml():
    report = run_audit([str(CORPUS / 'parse.sql')], 'snowflake', 'duckdb')
    root = ET.fromstring(render(report, 'junit'))
    assert root.attrib['failures'] == '1'


def test_auto_stub_folds_ascii_column_case(tmp_path):
    report = run_audit([sql_file(tmp_path, 'SELECT o.ID, o.id FROM orders o')], 'duckdb', 'duckdb', auto_stub=True)
    assert report['statements'][0]['categories'] == ['OK']
    assert report['statements'][0]['auto_stubs'][0]['columns'] == ['id']


def test_correlated_auto_stub_columns(tmp_path):
    report = run_audit([sql_file(tmp_path, 'SELECT 1 FROM orders o WHERE EXISTS (SELECT 1 WHERE o.id > 0)')],
                       'duckdb', 'duckdb', auto_stub=True)
    assert report['statements'][0]['categories'] == ['OK']
    assert report['statements'][0]['auto_stubs'][0]['columns'] == ['id']
