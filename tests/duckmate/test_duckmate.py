from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from datawrench.duckmate.cli import main
from datawrench.duckmate.core import Error, connect, target_path


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    (tmp_path / 'db/migrations').mkdir(parents=True)
    return tmp_path


def migration(project, version, up, down='', options=''):
    p = project / f'db/migrations/{version}_change.sql'
    p.write_text(f'-- migrate:up{options}\n{up}\n-- migrate:down\n{down}\n')
    return p


def cli(*args, db='local.duckdb'):
    return main(['--url', f'duckdb:{db}', *args])


def value(db, sql):
    with connect(db, readonly=True) as c:
        return c.execute(sql).fetchall()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_real_fixture_up_status_rollback(project, capsys):
    fixture = Path(__file__).resolve().parents[2] / 'examples/duckmate/basic/db/migrations/20260101120000_create_users.sql'
    shutil.copy(fixture, project / 'db/migrations')
    assert cli('up') == 0
    assert value('local.duckdb', 'select * from users') == [(1, 'sample@example.invalid')]
    assert value('local.duckdb', 'select version from schema_migrations') == [('20260101120000',)]
    assert cli('migrate') == 0
    assert cli('status') == 0
    output = capsys.readouterr().out
    assert '[X] 20260101120000_create_users.sql' in output
    assert 'Applied: 1, Pending: 0' in output
    assert cli('verify') == 0
    assert cli('rollback') == 0
    assert value('local.duckdb', "select count(*) from duckdb_tables() where table_name='users'") == [(0,)]
    assert value('local.duckdb', 'select * from schema_migrations') == []
    assert cli('verify') == 0
    assert cli('up') == 0


def test_atomic_failure_first_migration(project, capsys):
    migration(project, 1, 'create table kept(i int); insert into kept values (1); select missing_column from kept;')
    assert cli('up') == 1
    error = capsys.readouterr().err
    assert 'statement 3 failed' in error and 'missing_column' in error and 'rolled back' in error
    assert value('local.duckdb', "select table_name from duckdb_tables() where not internal") == []
    assert not Path('db/schema.sql').exists()


def test_failure_preserves_existing_data_and_history(project):
    migration(project, 1, 'create table t(i int); insert into t values(1)', 'drop table t')
    assert cli('up') == 0
    schema = Path('db/schema.sql').read_bytes()
    migration(project, 2, 'insert into t values(2); alter table t add column x integer; select nope from t')
    assert cli('up') == 1
    assert value('local.duckdb', 'select * from t') == [(1,)]
    assert value('local.duckdb', 'select * from schema_migrations') == [('1',)]
    assert Path('db/schema.sql').read_bytes() == schema


def test_transaction_false_and_failed_version_unrecorded(project, capsys):
    migration(project, 1, 'create table partial(i int); insert into partial values (8); select bad from partial', options=' transaction:false')
    assert cli('up') == 1
    assert value('local.duckdb', 'select * from partial') == [(8,)]
    assert value('local.duckdb', 'select * from schema_migrations') == []
    assert 'earlier statements may be committed' in capsys.readouterr().err


def test_down_transaction_false(project):
    p = migration(project, 1, 'create table t(i int)', 'drop table t; select bad from missing;')
    p.write_text(p.read_text().replace('-- migrate:down', '-- migrate:down transaction:false'))
    assert cli('up') == 0
    assert cli('rollback') == 1
    assert value('local.duckdb', 'select * from schema_migrations') == [('1',)]
    assert value('local.duckdb', "select count(*) from duckdb_tables() where table_name='t'") == [(0,)]


COMPLEX = '''
CREATE SCHEMA app;
CREATE SEQUENCE app.ids START 10 INCREMENT 2 MINVALUE 2 MAXVALUE 999;
CREATE TYPE app.mood AS ENUM ('happy', 'sad');
CREATE TYPE app.pair AS STRUCT(x INTEGER, y VARCHAR);
CREATE MACRO app.double_it(x) AS x * 2;
CREATE MACRO app.table_it(x) AS TABLE SELECT x AS value;
-- Names intentionally force the child ahead of the parent in alphabetical order.
CREATE TABLE app.z_parent(id INTEGER PRIMARY KEY, mood app.mood, p app.pair);
CREATE TABLE app.a_child(id INTEGER DEFAULT nextval('app.ids'), parent INTEGER REFERENCES app.z_parent(id));
CREATE VIEW app.z_view AS SELECT id, app.double_it(id) AS doubled FROM app.z_parent;
CREATE VIEW app.a_view AS SELECT * FROM app.z_view;
CREATE UNIQUE INDEX parent_mood ON app.z_parent(mood);
COMMENT ON TABLE app.z_parent IS 'synthetic fixture';
COMMENT ON COLUMN app.z_parent.id IS 'identifier';
INSERT INTO app.z_parent VALUES (1, 'happy', {'x': 2, 'y': 'semi;colon'});
INSERT INTO app.a_child(parent) VALUES (1);
'''


def test_catalog_dump_roundtrip_and_determinism(project):
    migration(project, 1, COMPLEX)
    assert cli('up') == 0
    original = Path('db/schema.sql').read_bytes()
    for token in (b'CREATE SCHEMA', b'CREATE SEQUENCE', b'CREATE TYPE', b'CREATE TABLE', b'CREATE VIEW', b'CREATE MACRO', b'CREATE UNIQUE INDEX', b'COMMENT ON COLUMN'):
        assert token in original
    assert b'semi;colon' not in original  # user data excluded
    assert cli('dump') == 0
    assert Path('db/schema.sql').read_bytes() == original
    assert cli('load', db='restored.duckdb') == 0
    assert value('restored.duckdb', 'select count(*) from app.z_parent') == [(0,)]
    assert value('restored.duckdb', 'select app.double_it(3)') == [(6,)]
    assert value('restored.duckdb', 'select * from app.table_it(7)') == [(7,)]
    assert cli('verify', db='restored.duckdb') == 0
    assert cli('dump', db='restored.duckdb') == 0
    assert Path('db/schema.sql').read_bytes() == original
    assert cli('verify') == 0
    # Changing row/sequence state must not produce schema drift.
    with connect('local.duckdb') as c:
        c.execute("select nextval('app.ids')")
        c.execute("insert into app.z_parent values (2, 'sad', NULL)")
    assert cli('verify') == 0


def test_checksum_schema_and_missing_file_drift(project, capsys):
    p = migration(project, 1, 'create table t(i int)')
    assert cli('up') == 0
    source = p.read_text()
    p.write_text(source + '-- edited\n')
    assert cli('verify') == 1
    assert 'checksum drift' in capsys.readouterr().err
    assert cli('up') == 1  # do not silently accept modified history
    p.write_text(source)
    with connect('local.duckdb') as c:
        c.execute('alter table t add column drift int')
    assert cli('verify') == 1
    assert 'schema drift' in capsys.readouterr().err
    p.unlink()
    assert cli('verify') == 1
    assert 'missing from disk' in capsys.readouterr().err


@pytest.mark.parametrize('command', [('plan',), ('up', '--dry-run'), ('migrate', '--dry-run'), ('rollback', '--dry-run')])
def test_dry_run_byte_identical(project, command):
    migration(project, 1, 'create table t(i int)', 'drop table t')
    assert cli('up') == 0
    migration(project, 2, 'alter table t add column new_column varchar', 'alter table t drop column new_column')
    before = digest('local.duckdb')
    schema = digest('db/schema.sql')
    assert cli(*command) == 0
    assert digest('local.duckdb') == before
    assert digest('db/schema.sql') == schema
    assert not Path('local.duckdb.wal').exists()
    assert value('local.duckdb', 'select * from schema_migrations') == [('1',)]


def test_plan_diff_and_nonexistent_target(project, capsys):
    migration(project, 1, 'create table t(i int)')
    assert cli('plan') == 0
    assert not Path('local.duckdb').exists()
    out = capsys.readouterr().out
    assert '+CREATE TABLE t' in out
    assert not Path('db/schema.sql').exists()
    assert cli('up') == 0
    migration(project, 2, 'alter table t add column v varchar')
    assert cli('plan') == 0
    assert '+CREATE TABLE t(i INTEGER, v VARCHAR)' in capsys.readouterr().out


def test_plan_failure_and_wal_refusal(project, capsys):
    migration(project, 1, 'create table t(i int)')
    assert cli('up') == 0
    before = digest('local.duckdb')
    migration(project, 2, 'drop table t; select bad from t')
    assert cli('plan') == 1
    assert digest('local.duckdb') == before
    Path('local.duckdb.wal').write_bytes(b'synthetic WAL marker')
    assert cli('plan') == 1
    assert 'WAL' in capsys.readouterr().err
    assert digest('local.duckdb') == before


@pytest.mark.parametrize('url', ['md:analytics', 'https://example.invalid/db', 's3://bucket/db', 'duckdb://server/a', 'duckdb:////server/a', 'duckdb://localhost/a', 'duckdb:s3://bucket/db', 'duckdb:../md:db', 'duckdb:', 'duckdb::memory:', 'duckdb:x?token=secret', 'duckdb:x#fragment', 'duckdb:%2F%2Fserver/a', 'duckdb:C:\\x', 'duckdb:%00x', 'duckdb:%68ttps%3A//x'])
def test_reject_urls(url):
    with pytest.raises(Error) as error:
        target_path(url)
    assert error.value.code == 2


@pytest.mark.parametrize('url,expected', [('duckdb:test.duckdb', 'test.duckdb'), ('duckdb:dir%20space/a', 'dir space/a'), ('duckdb:///tmp/a', '/tmp/a'), ('duckdb:/tmp/a', '/tmp/a')])
def test_local_url(url, expected):
    assert target_path(url) == Path(expected).absolute()


@pytest.mark.parametrize('sql', ['ATTACH \'remote.duckdb\' AS remote', "INSTALL httpfs", "LOAD httpfs", "COPY (select 1) TO 'outside.csv'", "SET enable_external_access=true", "PRAGMA enable_external_access=true", 'BEGIN', "CREATE SECRET s (TYPE s3, KEY_ID 'secret')", 'CREATE TEMP TABLE tempdata(i int)', "EXPORT DATABASE 'exported'", "CALL checkpoint()"])
def test_forbidden_sql_preflight(project, sql):
    migration(project, 1, 'create table t(i int); ' + sql)
    assert cli('up', '--no-dump-schema') == 2
    # Preflight validates the whole section before executing any statement.
    assert value('local.duckdb', "select table_name from duckdb_tables() where not internal") == []
    assert not Path('outside.csv').exists()
    assert not Path('remote.duckdb').exists()
    assert not Path('exported').exists()


@pytest.mark.parametrize('sql', ["SELECT * FROM read_text('/etc/passwd')", "CREATE TABLE t AS SELECT * FROM read_csv('https://example.invalid/data.csv')", "SELECT * FROM glob('/etc/*')"])
def test_external_access_disabled(project, sql):
    migration(project, 1, sql)
    assert cli('up', '--no-dump-schema') == 1
    assert value('local.duckdb', "select table_name from duckdb_tables() where not internal") == []


@pytest.mark.parametrize('source', ['create table t(i int);', '-- migrate:down\n-- migrate:up\n', '-- migrate:up transaction:maybe\n-- migrate:down\n', '-- migrate:up\n-- migrate:up\n-- migrate:down\n', 'select 1;\n-- migrate:up\n-- migrate:down\n'])
def test_bad_migration_format(project, source, capsys):
    Path('db/migrations/001_bad.sql').write_text(source)
    assert cli('up') == 2
    assert '001_bad.sql' in capsys.readouterr().err
    assert not Path('local.duckdb').exists()


def test_duplicates_and_bad_filename(project):
    migration(project, '001', 'select 1')
    migration(project, '1', 'select 1')
    assert cli('up') == 2
    Path('db/migrations/1_change.sql').unlink()
    Path('db/migrations/name.sql').write_text('-- migrate:up\n-- migrate:down\n')
    assert cli('up') == 2


def test_parser_semicolons_comments_and_quoted_bodies(project):
    migration(project, 1, """
    /* ; */ CREATE TABLE t(x VARCHAR);
    INSERT INTO t VALUES ('first;second'), ($$third;fourth$$);
    CREATE MACRO msg() AS 'safe;string';
    -- ; an irrelevant delimiter
    """)
    assert cli('up') == 0
    assert value('local.duckdb', 'select * from t') == [('first;second',), ('third;fourth',)]
    assert cli('verify') == 0


def test_out_of_order_strict_and_permissive(project, capsys):
    migration(project, 10, 'create table last(i int)', 'drop table last')
    assert cli('up') == 0
    migration(project, 2, 'create table earlier(i int)', 'drop table earlier')
    assert cli('up', '--strict') == 2
    assert 'out-of-order' in capsys.readouterr().err
    assert value('local.duckdb', 'select * from schema_migrations') == [('10',)]
    assert cli('up') == 0
    assert cli('rollback') == 0  # numeric maximum, not most recently inserted
    assert value('local.duckdb', 'select * from schema_migrations') == [('2',)]
    assert cli('rollback', '--count', '2') == 0
    assert value('local.duckdb', 'select * from schema_migrations') == []


def test_lock_conflict_subprocess(project):
    migration(project, 1, 'select 1')
    lock = Path('local.duckdb.duckmate.lock')
    with lock.open('w') as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = subprocess.run([sys.executable, '-m', 'datawrench', 'duckmate', '-u', 'duckdb:local.duckdb', 'up'],
                               capture_output=True, text=True, timeout=15)
        assert child.returncode == 1
        assert 'another datawrench duckmate run holds the lock' in child.stderr
        assert not Path('local.duckdb').exists()
    assert cli('up') == 0
    assert lock.exists()  # retained inode avoids advisory-lock unlink race


def test_load_refuses_overwrite_and_is_atomic(project):
    migration(project, 1, 'create table t(i int)')
    assert cli('up') == 0
    before = digest('local.duckdb')
    assert cli('load') == 2
    assert digest('local.duckdb') == before
    Path('db/schema.sql').write_text('create table x(i int); select bad from x;')
    assert cli('load', db='new.duckdb') == 1
    assert not Path('new.duckdb').exists()
    Path('db/schema.sql').write_text("attach 'outside.duckdb' as outside")
    assert cli('load', db='new.duckdb') == 2
    assert not Path('new.duckdb').exists()
    assert not Path('outside.duckdb').exists()
    assert not list(project.glob('.duckmate-load-*'))


def test_custom_table_and_legacy_missing_checksums(project, capsys):
    migration(project, 1, 'create table t(i int)', 'drop table t')
    assert cli('up', '--migrations-table', 'history') == 0
    assert value('local.duckdb', 'select version from history') == [('1',)]
    assert cli('verify', '--migrations-table', 'history') == 0
    with connect('local.duckdb') as c:
        c.execute('drop table _duckmate_checksums')
    assert cli('verify', '--migrations-table', 'history') == 1
    assert 'no recorded checksum' in capsys.readouterr().err
    assert cli('rollback', '--migrations-table', 'history') == 0


@pytest.mark.parametrize('macro', ['CREATE MACRO f(x, y := 2) AS x+y', 'CREATE MACRO f(x := 2) AS x', 'CREATE MACRO f(x) AS x+1, (x,y) AS x+y'])
def test_unsupported_macro_dump_explicit(project, macro, capsys):
    migration(project, 1, macro)
    assert cli('up', '--no-dump-schema') == 0
    assert cli('dump') == 2
    assert 'macro' in capsys.readouterr().err
    assert not Path('db/schema.sql').exists()


def test_new_and_path_boundaries(project, capsys):
    assert main(['new', 'create_accounts']) == 0
    assert len(list(Path('db/migrations').glob('*_create_accounts.sql'))) == 1
    assert main(['new', '../escape']) == 2
    assert main(['new', 'anything', '--dry-run']) == 2
    assert not Path('../escape.sql').exists()
    assert cli('up', '--schema-file', 'local.duckdb') == 2
    assert not Path('local.duckdb').exists()
    assert cli('up', '--migrations-table', 'x; drop table t') == 2
    Path('linked.duckdb').symlink_to('local.duckdb')
    assert cli('up', db='linked.duckdb') == 2


def test_database_url_and_options_before_after(project, monkeypatch):
    migration(project, 1, 'create table t(i int)')
    monkeypatch.setenv('DATABASE_URL', 'duckdb:env.duckdb')
    assert main(['--strict', 'up', '--no-dump-schema']) == 0
    assert Path('env.duckdb').exists()
    assert not Path('db/schema.sql').exists()
    assert main(['--url', 'duckdb:env.duckdb', 'dump']) == 0
    assert main(['verify', '--url', 'duckdb:env.duckdb']) == 0


def test_readonly_commands_do_not_create_target(project):
    migration(project, 1, 'select 1')
    for command in ['status', 'dump', 'verify']:
        assert cli(command) == 2
        assert not Path('local.duckdb').exists()


@pytest.mark.parametrize('sql', ['DELETE FROM schema_migrations', 'DROP TABLE "_duckmate_checksums"', 'INSERT INTO main."schema_migrations" VALUES (\'99\')', 'CREATE TABLE _duckmate_private(i int)'])
def test_migration_metadata_reserved(project, sql):
    migration(project, 1, 'create table t(i int); ' + sql)
    assert cli('up') == 2
    assert value('local.duckdb', "select table_name from duckdb_tables() where not internal") == []


def test_keyword_literals_and_nested_comments(project):
    migration(project, 1, """
    /* outside /* nested */ still outside */
    create table t(v varchar);
    insert into t values ('SECRET'), ('TEMPORARY'), ($tag$schema_migrations$tag$);
    create table "temp" (i integer);
    """)
    assert cli('up') == 0
    assert value('local.duckdb', 'select * from t') == [('SECRET',), ('TEMPORARY',), ('schema_migrations',)]
    assert cli('verify') == 0


def test_schema_output_must_not_overwrite_migration(project):
    p = migration(project, 1, 'create table t(i int)')
    assert cli('up') == 0
    original = p.read_bytes()
    assert cli('dump', '--schema-file', str(p)) == 2
    assert cli('dump', '--schema-file', 'db/migrations/new.sql') == 2
    assert p.read_bytes() == original


def test_history_identifier_case(project):
    migration(project, 1, 'create table t(i int)')
    assert cli('up', '--migrations-table', 'History') == 0
    assert cli('verify', '--migrations-table', 'HISTORY') == 0


def test_rollback_atomic_failure(project):
    migration(project, 1, 'create table t(i int); insert into t values(6)',
              'drop table t; select bad from missing;')
    assert cli('up') == 0
    original = Path('db/schema.sql').read_bytes()
    assert cli('rollback') == 1
    assert value('local.duckdb', 'select * from t') == [(6,)]
    assert value('local.duckdb', 'select * from schema_migrations') == [('1',)]
    assert Path('db/schema.sql').read_bytes() == original


def test_lock_symlink_and_unsupported_database_reference(project, capsys):
    migration(project, 1, 'create table t(i int)')
    Path('local.duckdb.duckmate.lock').symlink_to('elsewhere')
    assert cli('up') == 1
    assert not Path('elsewhere').exists()
    Path('local.duckdb.duckmate.lock').unlink()
    assert cli('up') == 0
    with connect('local.duckdb') as c:
        c.execute('create view v as select * from local.main.t')
    original = Path('db/schema.sql').read_bytes()
    assert cli('dump') == 2
    assert 'self-contained schema dump' in capsys.readouterr().err
    assert Path('db/schema.sql').read_bytes() == original


def test_orphan_checksum_not_silently_adopted(project, capsys):
    migration(project, 1, 'create table t(i int)')
    assert cli('up') == 0
    with connect('local.duckdb') as c:
        c.execute("insert into _duckmate_checksums values ('schema_migrations', '2', 'forged')")
    migration(project, 2, 'create table new_t(i int)')
    assert cli('up') == 1
    assert 'orphan checksum' in capsys.readouterr().err
    assert value('local.duckdb', "select count(*) from duckdb_tables() where table_name='new_t'") == [(0,)]
    assert cli('verify') == 1


def test_commit_failure_keeps_original_engine_diagnostic(project, capsys):
    migration(project, 1, 'CREATE TABLE totals(k INTEGER, amount DECIMAL(12,2)); INSERT INTO totals VALUES (9, 12.34);')
    assert cli('up') == 0
    before_schema = Path('db/schema.sql').read_bytes()
    path = migration(project, 2, 'UPDATE totals SET amount=99.99; ALTER TABLE totals ADD COLUMN memo VARCHAR;')
    assert cli('up') == 1
    error = capsys.readouterr().err
    assert path.name in error
    assert 'COMMIT failed' in error
    assert 'Failed to commit' in error
    assert 'cannot rollback' not in error
    assert value('local.duckdb', 'SELECT k, CAST(amount AS VARCHAR) FROM totals') == [(9, '12.34')]
    assert value('local.duckdb', 'SELECT version FROM schema_migrations') == [('1',)]
    assert Path('db/schema.sql').read_bytes() == before_schema


def status_json(capsys, *args, expected=0):
    assert cli('status', '--json', *args) == expected
    captured = capsys.readouterr()
    assert captured.err == ''
    return json.loads(captured.out)


def test_json_status_upgrade_edit_delete_fixture(project, capsys):
    first = migration(project, '001', 'create table accounts(id int)', 'drop table accounts')
    assert cli('up') == 0
    second = migration(project, '002', 'insert into accounts values (42)')
    capsys.readouterr()
    before = digest('local.duckdb'), digest('db/schema.sql')
    report = status_json(capsys)
    assert report['schema_version'] == 1
    assert report['database'] == str(project / 'local.duckdb')
    assert report['migrations_directory'] == str(project / 'db/migrations')
    assert report['migrations_table'] == 'schema_migrations'
    assert report['ok'] is True
    assert report['problems'] == []
    assert report['summary'] == {'applied': 1, 'pending': 1, 'changed': 0, 'missing': 0,
                                 'unverified': 0, 'out_of_order': 0}
    assert [(m['version'], m['state'], m['applied']) for m in report['migrations']] == [
        ('001', 'applied', True), ('002', 'pending', False)]
    entry = report['migrations'][0]
    assert entry['path'] == str(first)
    assert entry['checksum'] == entry['recorded_checksum'] == digest(first)
    assert report['migrations'][1]['recorded_checksum'] is None
    assert value('local.duckdb', 'select * from accounts') == []
    assert before == (digest('local.duckdb'), digest('db/schema.sql'))
    assert cli('up') == 0
    capsys.readouterr()
    assert status_json(capsys)['summary'] == {'applied': 2, 'pending': 0, 'changed': 0, 'missing': 0,
                                             'unverified': 0, 'out_of_order': 0}
    assert value('local.duckdb', 'select * from accounts') == [(42,)]

    original = first.read_bytes()
    first.write_bytes(original + b'-- edited down SQL/comment\n')
    before = digest('local.duckdb'), digest('db/schema.sql')
    report = status_json(capsys, expected=1)
    assert report['ok'] is False
    assert report['summary']['changed'] == 1
    assert report['summary']['applied'] == 2
    assert report['migrations'][0]['state'] == 'changed'
    assert report['migrations'][0]['recorded_checksum'] == hashlib.sha256(original).hexdigest()
    assert report['migrations'][0]['checksum'] == digest(first)
    problem = report['problems'][0]
    assert problem['code'] == 'checksum_drift' and problem['version'] == '001'
    assert problem['path'] == str(first)
    assert '001' in problem['message'] and str(first) in problem['message']
    migration(project, '003', 'insert into accounts values (99)')
    assert cli('up') == 1
    error = capsys.readouterr().err
    assert '001' in error and str(first) in error
    assert value('local.duckdb', 'select * from accounts') == [(42,)]
    assert before == (digest('local.duckdb'), digest('db/schema.sql'))

    first.write_bytes(original)
    first.unlink()
    report = status_json(capsys, expected=1)
    assert report['summary']['missing'] == 1 and report['summary']['changed'] == 0
    missing = report['migrations'][0]
    assert missing['state'] == 'missing' and missing['path'] is None
    assert missing['expected_path'] == str(first.parent / '001_*.sql')
    assert missing['checksum'] is None
    problem = report['problems'][0]
    assert problem['code'] == 'missing_file' and problem['version'] == '001'
    assert str(first.parent / '001_*.sql') in problem['message']
    assert cli('up') == 1
    error = capsys.readouterr().err
    assert '001' in error and str(first.parent) in error
    assert before == (digest('local.duckdb'), digest('db/schema.sql'))
    assert second.exists()


@pytest.mark.parametrize('edited', [b'invalid migration markers', b'\xff\xfe'])
def test_json_status_hashes_even_malformed_edited_history(project, capsys, edited):
    path = migration(project, '007', 'select 1')
    assert cli('up') == 0
    path.write_bytes(edited)
    capsys.readouterr()
    report = status_json(capsys, expected=1)
    assert report['migrations'][0]['state'] == 'changed'
    assert report['migrations'][0]['checksum'] == hashlib.sha256(edited).hexdigest()
    assert report['problems'][0]['version'] == '007'


def test_json_status_never_parses_or_executes_migration_sql(project, capsys):
    with connect('local.duckdb') as con:
        con.execute('create table untouched(id int)')
    migration(project, 10, 'this is not valid SQL;')
    migration(project, 2, 'insert into untouched values (99)', options=' transaction:false')
    # Inventory is independent of marker/SQL validity, including pending files.
    Path('db/migrations/003_raw.sql').write_bytes(b'not even a migration\xff')
    before = digest('local.duckdb')
    report = status_json(capsys)
    assert report['summary']['pending'] == 3 and report['summary']['applied'] == 0
    assert [m['version'] for m in report['migrations']] == ['2', '003', '10']
    assert all(m['state'] == 'pending' for m in report['migrations'])
    assert digest('local.duckdb') == before
    assert value('local.duckdb', 'select * from untouched') == []
    assert value('local.duckdb', "select count(*) from duckdb_tables() where table_name='schema_migrations'") == [(0,)]
    assert not Path('db/schema.sql').exists()


@pytest.mark.parametrize('legacy', ['empty_checksums', 'absent_checksums'])
def test_json_status_legacy_and_orphan_custom_history(project, capsys, legacy):
    path = migration(project, '001', 'select 1')
    assert cli('up', '--migrations-table', 'History') == 0
    with connect('local.duckdb') as con:
        con.execute('delete from _duckmate_checksums')
        con.execute("insert into _duckmate_checksums values ('other_history', '004', 'ignored')")
        if legacy == 'absent_checksums':
            con.execute('drop table _duckmate_checksums')
    capsys.readouterr()
    report = status_json(capsys, '--migrations-table', 'HISTORY', expected=1)
    assert report['migrations_table'] == 'history'
    assert report['summary']['unverified'] == 1
    assert report['migrations'][0]['state'] == 'unverified'
    assert report['migrations'][0]['applied'] is True
    assert report['problems'][0]['code'] == 'missing_checksum'
    assert report['problems'][0]['path'] == str(path)
    with connect('local.duckdb') as con:
        if legacy == 'absent_checksums':
            con.execute('create table _duckmate_checksums(migrations_table varchar, version varchar, checksum varchar)')
        con.execute("insert into _duckmate_checksums values ('history', '001', ?)", [digest(path)])
        con.execute("insert into _duckmate_checksums values ('history', '002', 'orphan')")
    report = status_json(capsys, '--migrations-table', 'history', expected=1)
    assert report['summary']['unverified'] == 0
    assert report['problems'][0]['code'] == 'orphan_checksum'
    assert report['problems'][0]['version'] == '002'
    assert report['ok'] is False


def test_json_status_empty_inventory_reports_each_missing_version(project, capsys):
    first = migration(project, '001', 'select 1')
    second = migration(project, '010', 'select 2')
    assert cli('up') == 0
    first.unlink()
    second.unlink()
    capsys.readouterr()
    report = status_json(capsys, expected=1)
    assert report['summary'] == {'applied': 2, 'pending': 0, 'changed': 0, 'missing': 2,
                                 'unverified': 0, 'out_of_order': 0}
    assert [m['version'] for m in report['migrations']] == ['001', '010']
    assert len(report['problems']) == 2
    assert report == status_json(capsys, expected=1)  # no timestamps or unordered sets
    before = digest('local.duckdb'), digest('db/schema.sql')
    assert cli('migrate') == 1
    error = capsys.readouterr().err
    for version in ('001', '010'):
        assert str(first.parent / f'{version}_*.sql') in error
    assert before == (digest('local.duckdb'), digest('db/schema.sql'))


def test_json_option_boundaries(project, capsys):
    migration(project, 1, 'select 1')
    assert cli('status', '--json') == 2
    assert not Path('local.duckdb').exists()
    capsys.readouterr()
    assert cli('up', '--json') == 2
    assert not Path('local.duckdb').exists()
    capsys.readouterr()
    assert cli('up') == 0
    capsys.readouterr()
    assert main(['--json', '--url', 'duckdb:local.duckdb', 'status']) == 0
    assert json.loads(capsys.readouterr().out)['ok'] is True
    migration(project, '01', 'select 1')
    assert cli('status', '--json') == 2
    assert 'duplicate numeric migration version' in capsys.readouterr().err


def test_json_status_out_of_order_strict_and_permissive(project, capsys):
    migration(project, '010', 'create table accounts(id int)')
    assert cli('up') == 0
    low = migration(project, '002', 'insert into accounts values (2)')
    migration(project, '020', 'insert into accounts values (20)')
    capsys.readouterr()
    before = plan_bytes()
    report = status_json(capsys)
    assert report['strict'] is False and report['ok'] is True and report['problems'] == []
    assert report['summary']['out_of_order'] == 1
    assert [(m['version'], m['state'], m['out_of_order']) for m in report['migrations']] == [
        ('002', 'pending', True), ('010', 'applied', False), ('020', 'pending', False)]
    strict = status_json(capsys, '--strict', expected=1)
    assert strict['strict'] is True and strict['ok'] is False
    assert strict['summary'] == report['summary']
    assert strict['migrations'] == report['migrations']
    assert len(strict['problems']) == 1
    problem = strict['problems'][0]
    assert problem['code'] == 'out_of_order' and problem['version'] == '002'
    assert problem['path'] == str(low)
    assert all(s in problem['message'] for s in ('002', '010', str(low)))
    assert strict == status_json(capsys, '--strict', expected=1)
    assert cli('up', '--strict') == 2  # The execution command retains its input refusal code.
    assert 'out-of-order' in capsys.readouterr().err
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from accounts') == []
    assert value('local.duckdb', 'select version from schema_migrations') == [('010',)]
    assert cli('up') == 0  # Reporting must not change permissive execution policy.
    capsys.readouterr()
    assert value('local.duckdb', 'select * from accounts order by id') == [(2,), (20,)]
    assert status_json(capsys, '--strict')['summary']['out_of_order'] == 0


def test_json_status_out_of_order_inventory_never_parses_sql(project, capsys):
    migration(project, '010', 'select 10')
    assert cli('up') == 0
    first = migration(project, '0002', 'this is not SQL')
    second = project / 'db/migrations/003_raw.sql'
    second.write_bytes(b'not UTF-8 or migration markers: \xff')
    capsys.readouterr()
    before = plan_bytes()
    report = status_json(capsys, '--strict', expected=1)
    assert report['summary']['out_of_order'] == report['summary']['pending'] == 2
    assert [(p['code'], p['version'], p['path']) for p in report['problems']] == [
        ('out_of_order', '0002', str(first)), ('out_of_order', '003', str(second))]
    for entry in report['migrations'][:2]:
        assert entry['state'] == 'pending' and entry['out_of_order'] is True
        assert entry['checksum'] == digest(entry['path'])
        assert entry['recorded_checksum'] is None
    assert plan_bytes() == before


@pytest.mark.parametrize('history', ['empty', 'gapped', 'newer_pending', 'empty_inventory'])
def test_json_status_out_of_order_negative_boundaries(project, capsys, history):
    if history in ('empty', 'empty_inventory'):
        with connect('local.duckdb') as con:
            con.execute('select 1')
        if history == 'empty':
            migration(project, '000', 'not SQL')
            migration(project, '002', 'not SQL')
    else:
        migration(project, '002', 'select 2')
        migration(project, '010', 'select 10')
        assert cli('up') == 0
        if history == 'newer_pending':
            migration(project, '020', 'not SQL')
    capsys.readouterr()
    before = plan_bytes()
    report = status_json(capsys, '--strict')
    assert report['strict'] is True and report['ok'] is True
    assert report['summary']['out_of_order'] == 0 and report['problems'] == []
    assert all(m['out_of_order'] is False for m in report['migrations'])
    assert plan_bytes() == before
    if history in ('empty', 'empty_inventory'):
        assert value('local.duckdb', "select count(*) from duckdb_tables() where not internal") == [(0,)]


@pytest.mark.parametrize('damage', ['edit', 'delete'])
def test_json_status_out_of_order_aggregates_drift_custom_history(project, capsys, damage):
    high = migration(project, '010', 'select 10')
    assert cli('up', '--migrations-table', 'History') == 0
    low = migration(project, '002', 'select 2')
    if damage == 'edit':
        high.write_bytes(high.read_bytes() + b'-- drift\n')
    else:
        high.unlink()  # Missing applied files still contribute to the numeric high-water mark.
    capsys.readouterr()
    before = plan_bytes()
    report = status_json(capsys, '--strict', '--migrations-table', 'HISTORY', expected=1)
    assert report['migrations_table'] == 'history'
    assert report['summary']['out_of_order'] == 1
    problems = {p['code']: p for p in report['problems']}
    assert set(problems) == {'out_of_order', 'checksum_drift' if damage == 'edit' else 'missing_file'}
    assert problems['out_of_order']['version'] == '002'
    assert problems['out_of_order']['path'] == str(low)
    assert report['migrations'][1]['out_of_order'] is False
    assert plan_bytes() == before


def plan_bytes():
    paths = [Path('local.duckdb'), Path('local.duckdb.wal'), Path('db/schema.sql'),
             *sorted(Path('db/migrations').glob('*.sql'))]
    return {str(p): p.read_bytes() if p.exists() else None for p in paths}


def test_target_plan_order_bounds_and_no_execution(project, capsys):
    first = migration(project, '001', 'create table accounts(id int)', 'drop table accounts')
    assert cli('up') == 0
    second = migration(project, '002', 'insert into accounts values (42)', 'delete from accounts')
    last = migration(project, '010', 'select 10', 'select 10')
    capsys.readouterr()
    before = plan_bytes()
    assert cli('plan', '--to', '10') == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert output.out == (
        'Migration plan (no SQL executed)\nCurrent version: 001\nTarget version: 010\nDirection: up\n'
        f'1. up 002 {second}\n2. up 010 {last}\nSteps: 2\n'
        'SQL execution: not checked; a generated plan does not guarantee executable SQL.\n')
    assert plan_bytes() == before
    assert cli('plan', '--to', '002') == 0
    assert f'1. up 002 {second}' in capsys.readouterr().out
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from accounts') == []
    assert cli('up') == 0
    capsys.readouterr()
    before = plan_bytes()
    assert cli('plan', '--to', '1') == 0
    output = capsys.readouterr().out
    assert 'Current version: 010\nTarget version: 001\nDirection: down\n' in output
    assert f'1. down 010 {last}\n2. down 002 {second}\nSteps: 2\n' in output
    assert plan_bytes() == before
    assert cli('plan', '--to', '0') == 0
    assert f'3. down 001 {first}' in capsys.readouterr().out
    assert plan_bytes() == before
    assert cli('plan', '--to', '0' * 5000 + '10') == 0
    assert 'Direction: none\nSteps: 0\n' in capsys.readouterr().out
    assert plan_bytes() == before
    for target, reason in [('11', 'outside available range 0..010'),
                           ('9' * 5000, 'outside available range 0..010'), ('3', 'has no migration file'),
                           ('-1', 'nonnegative numeric version'), ('latest', 'nonnegative numeric version')]:
        assert cli('plan', '--to', target) == 2
        output = capsys.readouterr()
        assert output.out == '' and reason in output.err
        assert plan_bytes() == before


def test_target_plan_absent_target_and_invalid_sql(project, capsys):
    file = migration(project, '005', 'not valid SQL', 'also not SQL')
    before = plan_bytes()
    assert cli('plan', '--to', '5') == 0
    output = capsys.readouterr().out
    assert 'Current version: 0\nTarget version: 005\nDirection: up' in output
    assert f'1. up 005 {file}' in output
    assert 'does not guarantee executable SQL' in output
    assert plan_bytes() == before
    assert cli('plan') == 1  # Existing copy-execution plan really parses SQL.
    assert 'SQL parse failed' in capsys.readouterr().err
    assert plan_bytes() == before
    file.write_bytes(b'not even valid markers or UTF-8: \xff')
    before = plan_bytes()
    assert cli('plan', '--to', '5') == 0  # Scheduling inventories bytes only.
    assert plan_bytes() == before


def test_target_plan_gapped_history_strict_and_custom_table(project, capsys):
    migration(project, '010', 'select 10', 'select 10')
    assert cli('up', '--migrations-table', 'History') == 0
    low = migration(project, '002', 'select 2', 'select 2')
    migration(project, '020', 'select 20', 'select 20')
    capsys.readouterr()
    before = plan_bytes()
    assert cli('plan', '--to', '20', '--migrations-table', 'HISTORY', '--strict') == 2
    assert 'out-of-order' in capsys.readouterr().err
    assert cli('plan', '--to', '20', '--migrations-table', 'history') == 0
    assert f'1. up 002 {low}' in capsys.readouterr().out
    assert cli('plan', '--to', '2', '--migrations-table', 'history') == 2
    assert 'not applied; rollback cannot reach it' in capsys.readouterr().err
    assert cli('plan', '--to', '10', '--migrations-table', 'history') == 0
    assert f'1. up 002 {low}' in capsys.readouterr().out
    assert cli('plan', '--to', '0', '--migrations-table', 'history', '--strict') == 0
    output = capsys.readouterr().out
    assert '1. down 010' in output and 'Steps: 1' in output
    assert plan_bytes() == before


@pytest.mark.parametrize('damage', ['edit', 'delete', 'wal'])
def test_target_plan_refuses_history_drift_and_wal(project, capsys, damage):
    file = migration(project, '001', 'select 1')
    assert cli('up') == 0
    if damage == 'edit':
        file.write_bytes(file.read_bytes() + b'-- changed\n')
    elif damage == 'delete':
        file.unlink()
    else:
        Path('local.duckdb.wal').write_bytes(b'synthetic WAL marker')
    before = plan_bytes()
    capsys.readouterr()
    assert cli('plan', '--to', '0') == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert {'edit': 'checksum drift', 'delete': 'missing from disk', 'wal': 'WAL'}[damage] in output.err
    assert plan_bytes() == before


@pytest.mark.parametrize('args', [('plan', '--to', '0', '--count', '0'),
                                  ('plan', '--to', '0', '--count', '1'),
                                  ('plan', '--to', '0', '--dry-run'),
                                  ('up', '--to', '0'), ('status', '--to', '0')])
def test_target_plan_option_conflicts(project, capsys, args):
    migration(project, 1, 'select 1')
    before = plan_bytes()
    assert cli(*args) == 2
    output = capsys.readouterr()
    assert '--to' in output.err and output.out == ''
    assert plan_bytes() == before


def test_target_plan_empty_inventory_and_zero_version_boundary(project, capsys):
    assert main(['--to', '0', '-u', 'duckdb:local.duckdb', 'plan']) == 0
    assert 'Current version: 0\nTarget version: 0\nDirection: none\nSteps: 0\n' in capsys.readouterr().out
    assert cli('plan', '--to', '1') == 2
    assert 'outside available range 0..0' in capsys.readouterr().err
    migration(project, '000', 'select 0')
    assert cli('plan', '--to', '0') == 2
    assert 'version 0 is reserved for the empty baseline' in capsys.readouterr().err
    assert not Path('local.duckdb').exists()


def test_target_plan_down_sql_is_not_executed_or_validated(project, capsys):
    migration(project, '001', 'create table kept(id int)', 'not valid SQL')
    assert cli('up') == 0
    before = plan_bytes()
    capsys.readouterr()
    assert cli('plan', '--to', '0') == 0
    assert '1. down 001' in capsys.readouterr().out
    assert plan_bytes() == before
    assert cli('plan', '--count', '1') == 1
    assert 'SQL parse failed' in capsys.readouterr().err
    assert plan_bytes() == before


def target_json(capsys, target, *options, expected=0):
    assert cli('plan', '--to', target, '--json', *options) == expected
    output = capsys.readouterr()
    assert output.err == ''
    report = json.loads(output.out)
    assert report['schema_version'] == 1 and report['command'] == 'plan'
    assert report['sql_execution'] == 'not_checked'
    assert report['requested_target'] == target
    assert report['exit_code'] == expected and report['ok'] is (expected == 0)
    if expected:
        assert report['steps'] == [] and report['direction'] is None and report['problems']
    else:
        assert report['problems'] == []
    return report


def test_json_target_plan_order_strict_and_no_execution(project, capsys):
    high = migration(project, '010', 'create table accounts(id int)', 'not valid down SQL')
    assert cli('up', '--migrations-table', 'History') == 0
    low = migration(project, '002', 'insert into accounts values (2)')
    last = migration(project, '020', 'insert into accounts values (20)')
    low.write_bytes(b'no markers or UTF-8: \xff')
    capsys.readouterr()
    before = plan_bytes()
    report = target_json(capsys, '20', '--migrations-table', 'HISTORY')
    assert report['database'] == str(project / 'local.duckdb')
    assert report['migrations_directory'] == str(project / 'db/migrations')
    assert report['migrations_table'] == 'history' and report['strict'] is False
    assert (report['current_version'], report['target_version'], report['direction']) == ('010', '020', 'up')
    assert report['steps'] == [
        {'position': 1, 'direction': 'up', 'version': '002', 'path': str(low),
         'checksum': digest(low), 'recorded_checksum': None},
        {'position': 2, 'direction': 'up', 'version': '020', 'path': str(last),
         'checksum': digest(last), 'recorded_checksum': None}]
    strict = target_json(capsys, '20', '--migrations-table', 'history', '--strict', expected=2)
    assert strict['strict'] is True and strict['current_version'] == '010' and strict['target_version'] == '020'
    problem, = strict['problems']
    assert (problem['code'], problem['version'], problem['path']) == ('out_of_order', '002', str(low))
    assert '010' in problem['message']
    down = target_json(capsys, '0', '--migrations-table', 'history', '--strict')
    assert down['direction'] == 'down' and down['target_version'] == '0'
    assert down['steps'] == [{'position': 1, 'direction': 'down', 'version': '010', 'path': str(high),
                              'checksum': digest(high), 'recorded_checksum': digest(high)}]
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from accounts') == []
    assert value('local.duckdb', 'select version from history') == [('010',)]


def test_json_target_plan_down_noop_and_absent(project, capsys):
    first = migration(project, '001', 'select 1')
    last = migration(project, '010', 'select 10', 'not SQL')
    before = plan_bytes()
    report = target_json(capsys, '00010')
    assert report['current_version'] == '0' and report['target_version'] == '010'
    assert [s['version'] for s in report['steps']] == ['001', '010']
    assert plan_bytes() == before and not Path('local.duckdb').exists()
    assert cli('up') == 0
    capsys.readouterr()
    before = plan_bytes()
    down = target_json(capsys, '1')
    assert down['steps'] == [{'position': 1, 'direction': 'down', 'version': '010', 'path': str(last),
                              'checksum': digest(last), 'recorded_checksum': digest(last)}]
    baseline = target_json(capsys, '0')
    assert [s['path'] for s in baseline['steps']] == [str(last), str(first)]
    assert main(['--json', '--to', '0' * 5000 + '10', '-u', 'duckdb:local.duckdb', 'plan']) == 0
    noop = json.loads(capsys.readouterr().out)
    assert noop['direction'] == 'none' and noop['steps'] == [] and noop['target_version'] == '010'
    assert plan_bytes() == before


@pytest.mark.parametrize('target,options,code', [
    ('latest', (), 'invalid_target'), ('-1', (), 'invalid_target'),
    ('3', (), 'target_not_found'), ('21', (), 'target_out_of_range'),
    ('2', (), 'target_not_applied'), ('0', ('--count', '0'), 'incompatible_options'),
    ('0', ('--dry-run',), 'incompatible_options')])
def test_json_target_plan_refusal_contract(project, capsys, target, options, code):
    migration(project, '010', 'select 10')
    assert cli('up') == 0
    migration(project, '002', 'not SQL')
    migration(project, '020', 'not SQL')
    capsys.readouterr()
    before = plan_bytes()
    report = target_json(capsys, target, *options, expected=2)
    assert [p['code'] for p in report['problems']] == [code]
    assert all({'code', 'version', 'path', 'message'} <= p.keys() for p in report['problems'])
    assert plan_bytes() == before


@pytest.mark.parametrize('damage,code,exit_code', [
    ('edit', 'checksum_drift', 1), ('delete', 'missing_file', 1),
    ('orphan', 'orphan_checksum', 1), ('wal', 'wal_present', 1),
    ('lock', 'lock_busy', 1), ('corrupt_database', 'database_error', 1),
    ('duplicate', 'duplicate_migration_version', 2), ('zero', 'reserved_version_zero', 2)])
def test_json_target_plan_failure_boundaries(project, capsys, damage, code, exit_code):
    file = migration(project, '010', 'select 10')
    assert cli('up') == 0
    if damage == 'edit':
        file.write_bytes(file.read_bytes() + b'-- changed\n')
    elif damage == 'delete':
        file.unlink()
    elif damage == 'orphan':
        with connect('local.duckdb') as con:
            con.execute("INSERT INTO _duckmate_checksums VALUES ('schema_migrations', '099', 'orphan')")
    elif damage == 'wal':
        Path('local.duckdb.wal').write_bytes(b'synthetic WAL marker')
    elif damage == 'corrupt_database':
        Path('local.duckdb').write_bytes(b'not a database')
    elif damage == 'duplicate':
        migration(project, '10', 'not SQL')
    elif damage == 'zero':
        migration(project, '000', 'not SQL')
    capsys.readouterr()
    before = plan_bytes()
    with Path('local.duckdb.duckmate.lock').open('a') as lock:
        if damage == 'lock':
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = target_json(capsys, '0', expected=exit_code)
    assert [p['code'] for p in report['problems']] == [code]
    if damage in ('edit', 'delete'):
        assert report['problems'][0]['version'] == '010'
    assert plan_bytes() == before


@pytest.mark.parametrize('history,version', [('absent', '000'), ('empty', '0'), ('applied', '000')])
def test_json_target_plan_zero_context(project, capsys, history, version):
    migration(project, '010', 'select 10')
    if history == 'applied':
        assert cli('up') == 0
    elif history == 'empty':
        with connect('local.duckdb'):
            pass
    capsys.readouterr()
    zero = migration(project, version, '')
    zero.write_bytes(b'neither SQL nor UTF-8: \xff\r\n')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel\n')
    before = plan_bytes()
    report = target_json(capsys, '00010', '--strict', expected=2)
    message = 'target planning: version 0 is reserved for the empty baseline; use positive migration versions'
    assert report['problems'] == [
        {'code': 'reserved_version_zero', 'version': version, 'path': str(zero), 'message': message}]
    assert report['current_version'] == ('010' if history == 'applied' else '0')
    assert report['target_version'] is None and report['strict'] is True
    assert cli('plan', '--to', '10') == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert plan_bytes() == before
    assert Path('local.duckdb').exists() is (history != 'absent')
    if history == 'applied':
        assert value('local.duckdb', 'select version from schema_migrations') == [('010',)]
        assert value('local.duckdb', 'select version from _duckmate_checksums') == [('010',)]


@pytest.mark.parametrize('damage,code,exit_code', [
    ('duplicate', 'duplicate_migration_version', 2), ('filename', 'invalid_migration_filename', 2),
    ('read', 'io_error', 1), ('drift', 'checksum_drift', 1), ('missing', 'missing_file', 1),
    ('orphan', 'orphan_checksum', 1), ('wal', 'wal_present', 1), ('lock', 'lock_busy', 1)])
def test_json_target_plan_zero_refusal_precedence(project, capsys, damage, code, exit_code):
    high = migration(project, '010', 'select 10')
    assert cli('up') == 0
    zero = migration(project, '000', '')
    zero.write_bytes(b'invalid UTF-8\xff')
    if damage == 'duplicate':
        duplicate = migration(project, '0', '')
    elif damage == 'filename':
        Path('db/migrations/bad.sql').write_bytes(b'invalid\xff')
    elif damage == 'read':
        Path('db/migrations/020_unreadable.sql').mkdir()
    elif damage == 'drift':
        high.write_bytes(b'edited\xff')
    elif damage == 'missing':
        high.unlink()
    elif damage == 'orphan':
        with connect('local.duckdb') as con:
            con.execute("INSERT INTO _duckmate_checksums VALUES ('schema_migrations', '099', 'orphan')")
    elif damage == 'wal':
        Path('local.duckdb.wal').write_bytes(b'WAL sentinel')
    capsys.readouterr()
    def source_bytes():
        return {p: p.read_bytes() for p in project.rglob('*')
                if p.is_file() and not p.name.endswith('.duckmate.lock')}
    before = source_bytes()
    with Path('local.duckdb.duckmate.lock').open('a') as lock:
        if damage == 'lock':
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = target_json(capsys, '00010', expected=exit_code)
    assert [p['code'] for p in report['problems']] == [code]
    assert report['target_version'] is None
    assert report['current_version'] == ('010' if damage in ('drift', 'missing', 'orphan') else None)
    if damage == 'duplicate':
        assert report['problems'][0]['version'] == '0'
        assert report['problems'][0]['path'] == str(duplicate)
    assert source_bytes() == before


def test_json_target_plan_legacy_empty_and_io_error(project, capsys, monkeypatch):
    before = plan_bytes()
    empty = target_json(capsys, '0')
    assert empty['current_version'] == empty['target_version'] == '0'
    assert empty['direction'] == 'none' and empty['steps'] == []
    assert plan_bytes() == before
    file = migration(project, '010', 'not SQL')
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar primary key)')
        con.execute("insert into schema_migrations values ('010')")
    before = plan_bytes()
    legacy = target_json(capsys, '0')
    assert legacy['steps'][0]['path'] == str(file)  # Retain legacy compatibility, no checksum adoption.
    assert plan_bytes() == before
    def failed_copy(*args, **kwargs):
        raise OSError('synthetic copy failure')
    monkeypatch.setattr('datawrench.duckmate.core.shutil.copyfile', failed_copy)
    report = target_json(capsys, '0', expected=1)
    assert report['problems'] == [
        {'code': 'io_error', 'version': None, 'path': None, 'message': 'synthetic copy failure'}]
    assert plan_bytes() == before


@pytest.mark.parametrize('padded', [False, True])
@pytest.mark.parametrize('target,zero', [('0', False), ('9' * 5000, False), ('10', True)],
                         ids=['baseline', 'huge-target', 'reserved-zero'])
def test_json_target_plan_oversized_history(project, capsys, padded, target, zero):
    limit = sys.get_int_max_str_digits()
    if not limit:
        pytest.skip('Python decimal conversion limit is disabled')
    version = ('0' if padded else '9') * max(5000, limit + 1) + ('10' if padded else '')
    table = 'history' if padded else 'schema_migrations'
    with connect('local.duckdb') as con:
        con.execute(f'create table {table}(version varchar primary key)')
        con.execute(f'insert into {table} values (?)', [version])
        con.execute('CHECKPOINT')
    (project / 'db/migrations/010_raw.sql').write_bytes(b'not UTF-8 or SQL: \xff\r\n')
    if zero:
        (project / 'db/migrations/000_zero.sql').write_bytes(b'invalid\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel\n')
    before = plan_bytes()
    options = ('--migrations-table', table, '--strict')
    report = target_json(capsys, target, *options, expected=2)
    message = f'{table}: recorded migration versions exceed the supported numeric conversion limit'
    assert report == {
        'schema_version': 1, 'command': 'plan', 'database': str(project / 'local.duckdb'),
        'migrations_table': table, 'migrations_directory': str(project / 'db/migrations'),
        'strict': True, 'requested_target': target, 'current_version': None, 'target_version': None,
        'sql_execution': 'not_checked', 'direction': None, 'steps': [], 'ok': False, 'exit_code': 2,
        'problems': [{'code': 'invalid_input', 'version': None, 'path': None, 'message': message}]}
    assert cli('plan', '--to', target, *options) == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert sys.get_int_max_str_digits() == limit
    assert plan_bytes() == before
    assert value('local.duckdb', f'select version from {table}') == [(version,)]
    assert value('local.duckdb', "select count(*) from duckdb_tables() where table_name='_duckmate_checksums'") == [(0,)]


@pytest.mark.parametrize('history,message', [
    (['010', '10'], 'duplicate numeric migration versions'),
    (['010', 'not numeric'], 'migration versions must be numeric')])
def test_json_target_plan_invalid_history_controls(project, capsys, history, message):
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar primary key)')
        con.executemany('insert into schema_migrations values (?)', [(v,) for v in history])
    (project / 'db/migrations/010_raw.sql').write_bytes(b'invalid\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    before = plan_bytes()
    report = target_json(capsys, '9' * 5000, '--strict', expected=2)
    assert report['current_version'] is report['target_version'] is None
    assert report['problems'] == [{'code': 'invalid_input', 'version': None, 'path': None,
                                   'message': f'schema_migrations: {message}'}]
    assert plan_bytes() == before


@pytest.mark.parametrize('table', ['schema_migrations', 'history'])
@pytest.mark.parametrize('target,zero', [('0', False), ('9' * 5000, False), ('10', True)],
                         ids=['baseline', 'huge-target', 'reserved-zero'])
def test_json_target_plan_exact_history_duplicates(project, capsys, table, target, zero):
    with connect('local.duckdb') as con:
        # Externally supplied legacy metadata need not have duckmate's primary key.
        con.execute(f'create table {table}(version varchar)')
        con.execute(f"insert into {table} values ('010'), ('010')")
        con.execute('CHECKPOINT')
    (project / 'db/migrations/010_raw.sql').write_bytes(b'not UTF-8 or SQL: \xff\r\n')
    (project / 'db/migrations/002_pending.sql').write_bytes(b'invalid\xff')
    if zero:
        (project / 'db/migrations/000_zero.sql').write_bytes(b'invalid\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel\n')
    before = plan_bytes()
    limit = sys.get_int_max_str_digits()
    options = ('--migrations-table', table, '--strict')
    report = target_json(capsys, target, *options, expected=2)
    message = f'{table}: duplicate numeric migration versions'
    assert report == {
        'schema_version': 1, 'command': 'plan', 'database': str(project / 'local.duckdb'),
        'migrations_table': table, 'migrations_directory': str(project / 'db/migrations'),
        'strict': True, 'requested_target': target, 'current_version': None, 'target_version': None,
        'sql_execution': 'not_checked', 'direction': None, 'steps': [], 'ok': False, 'exit_code': 2,
        'problems': [{'code': 'invalid_input', 'version': None, 'path': None, 'message': message}]}
    assert cli('plan', '--to', target, *options) == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert sys.get_int_max_str_digits() == limit
    assert plan_bytes() == before
    assert value('local.duckdb', f'select version from {table}') == [('010',), ('010',)]
    assert value('local.duckdb', 'select count(*) from duckdb_constraints()') == [(0,)]
    assert value('local.duckdb', "select count(*) from duckdb_tables() where table_name='_duckmate_checksums'") == [(0,)]


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('damage', ['syntax', 'size', 'syntax-and-size'])
def test_json_target_plan_history_duplicate_precedence(project, capsys, damage, reverse):
    limit = sys.get_int_max_str_digits()
    if 'size' in damage and not limit:
        pytest.skip('Python decimal conversion limit is disabled')
    versions = ['010', '010']
    if 'size' in damage:
        versions.append('9' * max(5000, limit + 1))
    if 'syntax' in damage:
        versions.append('not numeric')
    if reverse:
        versions.reverse()
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar)')
        con.executemany('insert into schema_migrations values (?)', [(v,) for v in versions])
        con.execute('CHECKPOINT')
    (project / 'db/migrations/010_raw.sql').write_bytes(b'invalid\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    before = plan_bytes()
    report = target_json(capsys, '0', '--strict', expected=2)
    message = ('migration versions must be numeric' if 'syntax' in damage else
               'recorded migration versions exceed the supported numeric conversion limit')
    assert report['current_version'] is report['target_version'] is report['direction'] is None
    assert report['steps'] == [] and report['ok'] is False
    assert report['problems'] == [{'code': 'invalid_input', 'version': None, 'path': None,
                                   'message': f'schema_migrations: {message}'}]
    assert sys.get_int_max_str_digits() == limit
    assert plan_bytes() == before
    assert value('local.duckdb', 'select version from schema_migrations') == [(v,) for v in versions]


@pytest.mark.parametrize('primary_key', [False, True])
@pytest.mark.parametrize('versions', [[], ['010', '002', '020']])
def test_applied_unique_history_rows(project, primary_key, versions):
    from datawrench.duckmate.core import applied

    with connect() as con:
        con.execute('create table history(version varchar' + (' primary key' if primary_key else '') + ')')
        for version in versions:
            con.execute('insert into history values (?)', [version])
        assert applied(con, 'history') == set(versions)


@pytest.mark.parametrize('table', ['schema_migrations', 'history'])
@pytest.mark.parametrize('order', ['bad-then-good', 'good-then-bad', 'identical'])
def test_json_target_plan_checksum_duplicates(project, capsys, table, order):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    checks = [digest(file), digest(file)] if order == 'identical' else ['wrong', digest(file)]
    if order == 'good-then-bad':
        checks.reverse()
    checksum_rows = [(table, '010', checksum) for checksum in checks]
    with connect('local.duckdb') as con:
        con.execute(f'create table {table}(version varchar primary key)')
        con.execute(f"insert into {table} values ('010')")
        con.execute('create table _duckmate_checksums '
                    '(migrations_table varchar, version varchar, checksum varchar not null)')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', checksum_rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    constraints = value('local.duckdb', 'select * from duckdb_constraints()')
    report = target_json(capsys, '0', '--migrations-table', table, expected=2)
    message = f'{table}: duplicate checksum rows in _duckmate_checksums'
    assert report == {
        'schema_version': 1, 'command': 'plan', 'database': str(project / 'local.duckdb'),
        'migrations_table': table, 'migrations_directory': str(project / 'db/migrations'),
        'strict': False, 'requested_target': '0', 'current_version': None, 'target_version': None,
        'sql_execution': 'not_checked', 'direction': None, 'steps': [], 'ok': False, 'exit_code': 2,
        'problems': [{'code': 'invalid_input', 'version': None, 'path': None, 'message': message}]}
    assert cli('plan', '--to', '0', '--migrations-table', table) == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert plan_bytes() == before
    assert value('local.duckdb', f'select * from {table}') == [('010',)]
    assert value('local.duckdb', 'select * from _duckmate_checksums') == checksum_rows
    assert value('local.duckdb', 'select * from duckdb_constraints()') == constraints


@pytest.mark.parametrize('damage', ['syntax', 'size', 'history-duplicate', 'drift', 'missing',
                                    'orphan', 'zero', 'target', 'strict'])
def test_json_target_plan_checksum_duplicate_precedence(project, capsys, damage):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    versions = ['010']
    message = 'duplicate checksum rows in _duckmate_checksums'
    if damage == 'syntax':
        versions += ['bad']
        message = 'migration versions must be numeric'
    elif damage == 'size':
        limit = sys.get_int_max_str_digits()
        if not limit:
            pytest.skip('Python decimal conversion limit is disabled')
        versions += ['9' * (limit + 1)]
        message = 'recorded migration versions exceed the supported numeric conversion limit'
    elif damage == 'history-duplicate':
        versions += ['010']
        message = 'duplicate numeric migration versions'
    elif damage in ('drift', 'missing'):
        versions += ['020']
        if damage == 'drift':
            (project / 'db/migrations/020_raw.sql').write_bytes(b'other\xfe')
    elif damage == 'zero':
        (project / 'db/migrations/000_zero.sql').write_bytes(b'zero\xff')
    elif damage == 'strict':
        (project / 'db/migrations/002_raw.sql').write_bytes(b'pending\xff')
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar)')
        con.executemany('insert into schema_migrations values (?)', [(v,) for v in versions])
        con.execute('create table _duckmate_checksums '
                    '(migrations_table varchar, version varchar, checksum varchar not null)')
        key = '099' if damage == 'orphan' else '010'
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)',
                        [('schema_migrations', key, digest(file))] * 2)
        if damage == 'drift':
            con.execute("insert into _duckmate_checksums values ('schema_migrations', '020', 'wrong')")
        con.execute('CHECKPOINT')
    before = plan_bytes()
    report = target_json(capsys, '999' if damage == 'target' else '10' if damage == 'strict' else '0',
                         '--strict', expected=2)
    assert report['current_version'] is report['target_version'] is report['direction'] is None
    assert report['problems'] == [{'code': 'invalid_input', 'version': None, 'path': None,
                                   'message': f'schema_migrations: {message}'}]
    assert plan_bytes() == before


@pytest.mark.parametrize('primary_key', [False, True])
def test_json_target_plan_checksum_unique_and_scoped(project, capsys, primary_key):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    checksum_rows = [('history', '010', digest(file)), ('other_history', '010', 'wrong')]
    if not primary_key:
        checksum_rows += [('other_history', '010', 'another wrong checksum')]
        checksum_rows += [('other_history', None, 'null'), ('other_history', None, 'another null')]
    with connect('local.duckdb') as con:
        con.execute('create table history(version varchar primary key)')
        con.execute("insert into history values ('010')")
        con.execute('create table _duckmate_checksums '
                    '(migrations_table varchar, version varchar, checksum varchar not null' +
                    (', primary key(migrations_table, version)' if primary_key else '') + ')')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', checksum_rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    report = target_json(capsys, '0', '--migrations-table', 'HISTORY')
    assert report['steps'] == [{'position': 1, 'direction': 'down', 'version': '010', 'path': str(file),
                                'checksum': digest(file), 'recorded_checksum': digest(file)}]
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from _duckmate_checksums') == checksum_rows


@pytest.mark.parametrize('table', ['schema_migrations', 'history'])
@pytest.mark.parametrize('keys', [[None], [None, '099'], ['099', None], [None, None],
                                 [None, '010'], ['010', None]])
def test_json_target_plan_checksum_null_versions(project, capsys, table, keys):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    checksum_rows = [(table, '010', digest(file))] + [(table, key, 'wrong') for key in keys]
    with connect('local.duckdb') as con:
        con.execute(f'create table {table}(version varchar primary key)')
        con.execute(f"insert into {table} values ('010')")
        con.execute('create table _duckmate_checksums '
                    '(migrations_table varchar, version varchar, checksum varchar not null)')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', checksum_rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    constraints = value('local.duckdb', 'select * from duckdb_constraints()')
    report = target_json(capsys, '0', '--migrations-table', table, expected=2)
    message = f'{table}: checksum versions must not be NULL in _duckmate_checksums'
    assert report == {
        'schema_version': 1, 'command': 'plan', 'database': str(project / 'local.duckdb'),
        'migrations_table': table, 'migrations_directory': str(project / 'db/migrations'),
        'strict': False, 'requested_target': '0', 'current_version': None, 'target_version': None,
        'sql_execution': 'not_checked', 'direction': None, 'steps': [], 'ok': False, 'exit_code': 2,
        'problems': [{'code': 'invalid_input', 'version': None, 'path': None, 'message': message}]}
    assert cli('plan', '--to', '0', '--migrations-table', table) == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert plan_bytes() == before
    assert value('local.duckdb', f'select * from {table}') == [('010',)]
    assert value('local.duckdb', 'select * from _duckmate_checksums') == checksum_rows
    assert value('local.duckdb', 'select * from duckdb_constraints()') == constraints


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('damage', ['syntax', 'size', 'history-duplicate', 'drift', 'missing',
                                    'zero', 'target', 'strict'])
def test_json_target_plan_checksum_null_precedence(project, capsys, damage, reverse):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    versions = ['010']
    message = 'checksum versions must not be NULL in _duckmate_checksums'
    if damage == 'syntax':
        versions += ['bad']
        message = 'migration versions must be numeric'
    elif damage == 'size':
        limit = sys.get_int_max_str_digits()
        if not limit:
            pytest.skip('Python decimal conversion limit is disabled')
        versions += ['9' * (limit + 1)]
        message = 'recorded migration versions exceed the supported numeric conversion limit'
    elif damage == 'history-duplicate':
        versions += ['010']
        message = 'duplicate numeric migration versions'
    elif damage == 'missing':
        versions += ['020']
    elif damage == 'zero':
        (project / 'db/migrations/000_zero.sql').write_bytes(b'zero\xff')
    elif damage == 'strict':
        (project / 'db/migrations/002_raw.sql').write_bytes(b'pending\xff')
    checksum_rows = [('schema_migrations', '010', 'wrong' if damage == 'drift' else digest(file)),
                     ('schema_migrations', None, 'null-key')]
    if reverse:
        versions.reverse()
        checksum_rows.reverse()
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar)')
        con.executemany('insert into schema_migrations values (?)', [(v,) for v in versions])
        con.execute('create table _duckmate_checksums '
                    '(migrations_table varchar, version varchar, checksum varchar not null)')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', checksum_rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    report = target_json(capsys, '999' if damage == 'target' else '10' if damage == 'strict' else '0',
                         '--strict', expected=2)
    assert report['current_version'] is report['target_version'] is report['direction'] is None
    assert report['steps'] == []
    assert report['problems'] == [{'code': 'invalid_input', 'version': None, 'path': None,
                                   'message': f'schema_migrations: {message}'}]
    assert plan_bytes() == before
    assert value('local.duckdb', 'select version from schema_migrations') == [(v,) for v in versions]
    assert value('local.duckdb', 'select * from _duckmate_checksums') == checksum_rows


def test_json_target_plan_checksum_ordinary_orphan(project, capsys):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar primary key)')
        con.execute("insert into schema_migrations values ('010')")
        con.execute('create table _duckmate_checksums '
                    '(migrations_table varchar, version varchar, checksum varchar not null)')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)',
                        [('schema_migrations', '010', digest(file)),
                         ('schema_migrations', '099', 'orphan'), ('other_history', None, 'foreign')])
        con.execute('CHECKPOINT')
    before = plan_bytes()
    report = target_json(capsys, '0', expected=1)
    assert report['current_version'] == '010'
    assert report['target_version'] is report['direction'] is None
    message = 'orphan checksum without an applied version: 099'
    assert report['steps'] == [] and report['problems'] == [
        {'code': 'orphan_checksum', 'version': '099', 'path': None, 'message': message}]
    assert cli('plan', '--to', '0') == 1
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert plan_bytes() == before


@pytest.mark.parametrize('table', ['schema_migrations', 'history'])
@pytest.mark.parametrize('key_type,key', [('blob', b'099'), ('blob', b'010'), ('integer', 99),
                                        ('integer[]', [99]), ('struct(v integer)', {'v': 99})])
def test_json_target_plan_checksum_nontext_versions(project, capsys, table, key_type, key):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    checksum_rows = [(table, key, digest(file))]
    with connect('local.duckdb') as con:
        con.execute(f'create table {table}(version varchar primary key)')
        con.execute(f"insert into {table} values ('010')")
        con.execute('create table _duckmate_checksums '
                    f'(migrations_table varchar, version {key_type}, checksum varchar not null)')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', checksum_rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    constraints = value('local.duckdb', 'select * from duckdb_constraints()')
    report = target_json(capsys, '0', '--migrations-table', table, expected=2)
    message = f'{table}: checksum versions must be text in _duckmate_checksums'
    assert report == {
        'schema_version': 1, 'command': 'plan', 'database': str(project / 'local.duckdb'),
        'migrations_table': table, 'migrations_directory': str(project / 'db/migrations'),
        'strict': False, 'requested_target': '0', 'current_version': None, 'target_version': None,
        'sql_execution': 'not_checked', 'direction': None, 'steps': [], 'ok': False, 'exit_code': 2,
        'problems': [{'code': 'invalid_input', 'version': None, 'path': None, 'message': message}]}
    assert cli('plan', '--to', '0', '--migrations-table', table) == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert plan_bytes() == before
    assert value('local.duckdb', f'select * from {table}') == [('010',)]
    assert value('local.duckdb', 'select * from _duckmate_checksums') == checksum_rows
    assert value('local.duckdb', 'select * from duckdb_constraints()') == constraints


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('damage', ['syntax', 'size', 'history-duplicate', 'null', 'duplicate-key',
                                    'drift', 'missing', 'zero', 'target', 'strict'])
def test_json_target_plan_checksum_nontext_precedence(project, capsys, damage, reverse):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    versions = ['010']
    message = 'checksum versions must be text in _duckmate_checksums'
    if damage == 'syntax':
        versions += ['bad']
        message = 'migration versions must be numeric'
    elif damage == 'size':
        limit = sys.get_int_max_str_digits()
        if not limit:
            pytest.skip('Python decimal conversion limit is disabled')
        versions += ['9' * (limit + 1)]
        message = 'recorded migration versions exceed the supported numeric conversion limit'
    elif damage == 'history-duplicate':
        versions += ['010']
        message = 'duplicate numeric migration versions'
    elif damage in ('missing', 'drift'):
        versions += ['020']
        if damage == 'drift':
            (project / 'db/migrations/020_raw.sql').write_bytes(b'other\xfe')
    elif damage == 'zero':
        (project / 'db/migrations/000_zero.sql').write_bytes(b'zero\xff')
    elif damage == 'strict':
        (project / 'db/migrations/002_raw.sql').write_bytes(b'pending\xff')
    checksum_rows = [('schema_migrations', b'010', digest(file))]
    if damage == 'null':
        checksum_rows += [('schema_migrations', None, 'null')]
        message = 'checksum versions must not be NULL in _duckmate_checksums'
    elif damage == 'duplicate-key':
        checksum_rows += checksum_rows[:]
    elif damage == 'drift':
        checksum_rows += [('schema_migrations', b'020', 'wrong')]
    if reverse:
        versions.reverse()
        checksum_rows.reverse()
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar)')
        con.executemany('insert into schema_migrations values (?)', [(v,) for v in versions])
        con.execute('create table _duckmate_checksums '
                    '(migrations_table varchar, version blob, checksum varchar not null)')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', checksum_rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    report = target_json(capsys, '999' if damage == 'target' else '10' if damage == 'strict' else '0',
                         '--strict', expected=2)
    assert report['current_version'] is report['target_version'] is report['direction'] is None
    assert report['steps'] == []
    assert report['problems'] == [{'code': 'invalid_input', 'version': None, 'path': None,
                                   'message': f'schema_migrations: {message}'}]
    assert plan_bytes() == before
    assert value('local.duckdb', 'select version from schema_migrations') == [(v,) for v in versions]
    assert value('local.duckdb', 'select * from _duckmate_checksums') == checksum_rows


@pytest.mark.parametrize('key_type,key', [('blob', b'099'), ('integer[]', [99])])
def test_json_target_plan_checksum_foreign_nontext_scoped(project, capsys, key_type, key):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar primary key)')
        con.execute("insert into schema_migrations values ('010')")
        con.execute('create table _duckmate_checksums '
                    f'(migrations_table varchar, version {key_type}, checksum varchar not null)')
        con.execute('insert into _duckmate_checksums values (?, ?, ?)', ['other_history', key, 'foreign'])
        con.execute('CHECKPOINT')
    before = plan_bytes()
    report = target_json(capsys, '0')
    assert report['steps'] == [{'position': 1, 'direction': 'down', 'version': '010', 'path': str(file),
                                'checksum': digest(file), 'recorded_checksum': None}]
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from _duckmate_checksums') == [('other_history', key, 'foreign')]


@pytest.mark.parametrize('padded', [False, True])
def test_applied_numeric_conversion_boundary(project, padded):
    from datawrench.duckmate.core import applied

    limit = sys.get_int_max_str_digits() or 4300
    version = '0' * (limit - 2) + '10' if padded else '9' * limit
    with connect() as con:
        con.execute('create table history(version varchar primary key)')
        con.execute('insert into history values (?)', [version])
        assert applied(con, 'history') == {version}
        # Supported padded aliases still use numeric equality, not string equality.
        if padded:
            con.execute("insert into history values ('10')")
            with pytest.raises(Error, match='duplicate numeric migration versions'):
                applied(con, 'history')


def test_applied_unrelated_value_error_propagates(monkeypatch):
    from datawrench.duckmate.core import applied

    original = ValueError('unrelated history read failure')

    class FailedRead:
        def execute(self, sql):
            raise original

    monkeypatch.setattr('datawrench.duckmate.core.exists', lambda *args: True)
    with pytest.raises(ValueError) as caught:
        applied(FailedRead(), 'history')
    assert caught.value is original


def test_json_target_plan_absent_huge_targets(project, capsys):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'invalid\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    before = plan_bytes()
    report = target_json(capsys, '9' * 5000, expected=2)
    assert report['problems'][0]['code'] == 'target_out_of_range'
    assert report['current_version'] == '0' and report['target_version'] is None
    report = target_json(capsys, '0' * 5000 + '10')
    assert report['current_version'] == '0' and report['target_version'] == '010'
    assert report['steps'] == [{'position': 1, 'direction': 'up', 'version': '010', 'path': str(file),
                                'checksum': digest(file), 'recorded_checksum': None}]
    assert not Path('local.duckdb').exists() and plan_bytes() == before


@pytest.mark.parametrize('damage', [
    'missing_directory', 'not_directory', 'bad_filename', 'malformed_prefix',
    'alias_duplicate', 'duplicate'])
def test_json_target_plan_inventory_refusal_context(project, capsys, damage):
    directory = Path('db/migrations')  # Context must be absolute even with relative CLI input.
    Path('db/schema.sql').write_bytes(b'snapshot sentinel\n')
    version = None
    if damage in ('missing_directory', 'not_directory'):
        directory.rmdir()
        if damage == 'not_directory':
            directory.write_bytes(b'not a directory\xff')
        path = project / directory
        code = 'migration_directory_not_found'
        message = f'migrations directory does not exist: {directory}'
    elif damage in ('bad_filename', 'malformed_prefix'):
        name = 'bad.sql' if damage == 'bad_filename' else '010_bad.name.sql'
        path = project / directory / name
        path.write_bytes(b'not SQL or UTF-8\xff')
        code = 'invalid_migration_filename'
        message = f'{directory / name}: expected NUMERIC_VERSION_name.sql'
    else:
        version = '10' if damage == 'alias_duplicate' else '010'
        name = f'{version}_two.sql'
        path = project / directory / name
        # Create in reverse order to prove the reported duplicate is lexical, not creation order.
        path.write_bytes(b'invalid SQL/UTF-8\xff')
        (directory / '010_one.sql').write_bytes(b'also invalid\xfe')
        code = 'duplicate_migration_version'
        message = f'{directory / name}: duplicate numeric migration version {version}'
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    report = target_json(capsys, '00010', expected=2)
    assert report['problems'] == [
        {'code': code, 'version': version, 'path': str(path), 'message': message}]
    assert report['current_version'] is None and report['target_version'] is None
    assert report['database'] == str(project / 'local.duckdb')
    assert report['migrations_directory'] == str(project / directory)
    # The new machine codes must not change human diagnostics, including ordinary up.
    for args in [('plan', '--to', '10'), ('up',)]:
        assert cli(*args) == 2
        output = capsys.readouterr()
        assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert {p: p.read_bytes() for p in project.rglob('*') if p.is_file()} == before
    assert not Path('local.duckdb').exists()
    assert not Path('local.duckdb.duckmate.lock').exists()


@pytest.fixture
def failed_inventory_listing():
    @contextmanager
    def install(directory, error, *, partial=False):
        scandir = os.scandir
        @contextmanager
        def failed(path):
            if not isinstance(path, int) and Path(path).absolute() == directory.absolute():
                if not partial:
                    raise error
                with scandir(path) as entries:
                    def interrupted():
                        yield next(entries)
                        raise error
                    yield interrupted()
            else:
                with scandir(path) as entries:
                    yield entries
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, 'scandir', failed)
            yield
    return install


@pytest.mark.parametrize('existing_database', [False, True])
@pytest.mark.parametrize('failure', ['permission', 'vanished', 'partial'])
def test_json_target_plan_inventory_listing_error(project, capsys, failed_inventory_listing,
                                                   existing_database, failure):
    if existing_database:
        migration(project, '001', 'create table accounts(id int); insert into accounts values (7)')
        assert cli('up') == 0
        capsys.readouterr()
    else:
        Path('db/schema.sql').write_bytes(b'snapshot sentinel\xff')
    migration(project, '010', '').write_bytes(b'no markers, SQL or UTF-8\xff')
    directory = Path('db/migrations')
    error = (FileNotFoundError(2, 'synthetic enumeration failure', str(directory)) if failure == 'vanished'
             else PermissionError(13, 'synthetic enumeration failure', str(directory)))
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    with failed_inventory_listing(directory, error, partial=failure == 'partial'):
        report = target_json(capsys, '0', expected=1)
        assert report['problems'] == [
            {'code': 'io_error', 'version': None, 'path': None, 'message': str(error)}]
        assert report['current_version'] is None and report['target_version'] is None
        assert report['database'] == str(project / 'local.duckdb')
        assert report['migrations_directory'] == str(project / directory)
        for args in [('plan', '--to', '0'), ('up',), ('status', '--json')]:
            assert cli(*args) == 1
            output = capsys.readouterr()
            assert output.out == '' and output.err == f'datawrench duckmate: {error}\n'
    assert {p: p.read_bytes() for p in project.rglob('*') if p.is_file()} == before
    assert Path('local.duckdb').exists() is existing_database
    if existing_database:
        assert value('local.duckdb', 'select * from accounts') == [(7,)]
        assert value('local.duckdb', 'select version from schema_migrations') == [('001',)]
        assert value('local.duckdb', 'select version from _duckmate_checksums') == [('001',)]
    else:
        assert not Path('local.duckdb.duckmate.lock').exists()


@pytest.mark.parametrize('failure', ['permission', 'vanished', 'partial'])
def test_migration_inventory_listing_error_preserves_exception(project, monkeypatch,
                                                               failed_inventory_listing, failure):
    from datawrench.duckmate.core import migration_sources

    directory = Path('db/migrations')
    migration(project, '010', '').write_bytes(b'raw\xff')
    error = (FileNotFoundError(2, 'synthetic enumeration failure', str(directory)) if failure == 'vanished'
             else PermissionError(13, 'synthetic enumeration failure', str(directory)))
    def no_read(self):
        pytest.fail('inventory must finish listing before validating/reading any entries')
    monkeypatch.setattr(Path, 'read_bytes', no_read)
    with failed_inventory_listing(directory, error, partial=failure == 'partial'):
        with pytest.raises(type(error)) as failure:
            migration_sources(directory)
    assert failure.value is error
    assert failure.value.errno == error.errno and failure.value.filename == str(directory)
    assert failure.value.__cause__ is None


def test_migration_inventory_listing_selection(project):
    from datawrench.duckmate.core import migration_sources

    directory = Path('db/migrations')
    # Keep the glob's basename/suffix matching, hidden entries and numeric sort.
    for name in ('020_last.sql', '010_first.sql'):
        (directory / name).write_bytes(b'raw\xff')
    (directory / 'ignored.SQL').write_bytes(b'ignore')
    (directory / 'nested').mkdir()
    (directory / 'nested/invalid.sql').write_bytes(b'ignore')
    (directory / 'README').write_bytes(b'ignore')
    entries = migration_sources(directory)
    assert [file.version for file, _ in entries] == ['010', '020']
    assert all(raw == b'raw\xff' and file.checksum == hashlib.sha256(raw).hexdigest()
               for file, raw in entries)
    hidden = directory / '.hidden.sql'
    hidden.write_bytes(b'raw\xff')
    with pytest.raises(Error) as failure:
        migration_sources(directory)
    assert failure.value.reason == 'invalid_migration_filename'
    assert failure.value.path == str(project / hidden) and failure.value.version is None


@pytest.mark.parametrize('existing_database', [False, True])
def test_json_target_plan_inventory_read_error_context(project, capsys, existing_database):
    if existing_database:
        migration(project, '001', 'create table accounts(id int)', 'drop table accounts')
        assert cli('up') == 0
        capsys.readouterr()
    else:
        # Successfully read an earlier entry without decoding it or emitting partial steps.
        migration(project, '001', '').write_bytes(b'not SQL or UTF-8: \xff')
        Path('db/schema.sql').write_bytes(b'snapshot sentinel\n')
    path = Path('db/migrations/010_unreadable.sql')
    path.mkdir()
    (path / 'sentinel').write_bytes(b'keep directory contents\xff')
    with pytest.raises(IsADirectoryError) as original:
        path.read_bytes()
    message = str(original.value)
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    report = target_json(capsys, '00010', expected=1)
    assert report['problems'] == [
        {'code': 'io_error', 'version': '010', 'path': str(project / path), 'message': message}]
    assert report['current_version'] is None and report['target_version'] is None
    assert report['database'] == str(project / 'local.duckdb')
    for args in [('plan', '--to', '10'), ('up',), ('status', '--json')]:
        assert cli(*args) == 1
        output = capsys.readouterr()
        assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert {p: p.read_bytes() for p in project.rglob('*') if p.is_file()} == before
    assert Path('local.duckdb').exists() is existing_database
    if existing_database:
        assert value('local.duckdb', 'select * from accounts') == []
        assert value('local.duckdb', 'select version from schema_migrations') == [('001',)]
    else:
        assert not Path('local.duckdb.duckmate.lock').exists()


@pytest.mark.parametrize('error_type,errno', [(FileNotFoundError, 2), (PermissionError, 13)])
def test_migration_inventory_read_error_preserves_cause(project, monkeypatch, error_type, errno):
    from datawrench.duckmate.core import migration_sources

    path = Path('db/migrations/010_change.sql')
    path.write_bytes(b'not decoded\xff')
    original = error_type(errno, 'synthetic read failure', str(path))
    def failed_read(self):
        assert self == path
        raise original
    monkeypatch.setattr(Path, 'read_bytes', failed_read)
    with pytest.raises(Error) as failure:
        migration_sources(Path('db/migrations'))
    error = failure.value
    assert error.code == 1 and error.reason == 'io_error'
    assert error.version == '010' and error.path == str(project / path)
    assert str(error) == str(original) and error.__cause__ is original
    assert error.__cause__.errno == errno and error.__cause__.filename == str(path)


def test_json_target_plan_checksum_raw_bytes_and_refusals(project, capsys):
    high = migration(project, '010', 'create table accounts(id int)', 'not down SQL')
    assert cli('up', '--migrations-table', 'History') == 0
    low = migration(project, '002', 'insert into accounts values (2)')
    last = migration(project, '020', 'insert into accounts values (20)')
    capsys.readouterr()
    previous = None
    for raw in (b'no markers, SQL or UTF-8: \xff\r\n', b'no markers, SQL or UTF-8: \xff\r\n-- edit\n'):
        low.write_bytes(raw)
        before = plan_bytes()
        report = target_json(capsys, '00020', '--migrations-table', 'HISTORY')
        assert report['current_version'] == '010' and report['target_version'] == '020'
        assert report['steps'] == [
            {'position': n, 'direction': 'up', 'version': version, 'path': str(path),
             'checksum': digest(path), 'recorded_checksum': None}
            for n, version, path in ((1, '002', low), (2, '020', last))]
        if previous is not None:
            assert report['steps'][0]['checksum'] != previous['steps'][0]['checksum']
            assert report['steps'][1] == previous['steps'][1]
        previous = report
        assert plan_bytes() == before
    down = target_json(capsys, '0', '--migrations-table', 'history')
    assert down['steps'] == [
        {'position': 1, 'direction': 'down', 'version': '010', 'path': str(high),
         'checksum': digest(high), 'recorded_checksum': digest(high)}]
    strict = target_json(capsys, '20', '--migrations-table', 'history', '--strict', expected=2)
    assert strict['problems'][0]['code'] == 'out_of_order'
    assert strict['problems'][0]['version'] == '002' and strict['problems'][0]['path'] == str(low)
    unknown = target_json(capsys, '3', '--migrations-table', 'history', expected=2)
    assert unknown['problems'][0]['code'] == 'target_not_found'
    assert plan_bytes() == before
    high.write_bytes(high.read_bytes() + b'-- changed applied bytes\n')
    before = plan_bytes()
    drift = target_json(capsys, '20', '--migrations-table', 'history', expected=1)
    assert drift['problems'][0]['code'] == 'checksum_drift'
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from accounts') == []
    assert value('local.duckdb', 'select version from history') == [('010',)]


def test_json_target_plan_checksum_absent_down_and_noop(project, capsys):
    first = migration(project, '001', 'select 1')
    last = migration(project, '010', 'select 10', 'not down SQL')
    before = plan_bytes()
    up = target_json(capsys, '00010')
    assert [(s['checksum'], s['recorded_checksum']) for s in up['steps']] == [
        (digest(first), None), (digest(last), None)]
    assert plan_bytes() == before and not Path('local.duckdb').exists()
    assert cli('up') == 0
    capsys.readouterr()
    before = plan_bytes()
    down = target_json(capsys, '0')
    assert [(s['version'], s['checksum'], s['recorded_checksum']) for s in down['steps']] == [
        ('010', digest(last), digest(last)), ('001', digest(first), digest(first))]
    noop = target_json(capsys, '00010', '--strict')
    assert noop['direction'] == 'none' and noop['steps'] == []
    assert cli('plan', '--to', '1') == 0
    text = capsys.readouterr()
    assert text.err == ''
    assert text.out == ('Migration plan (no SQL executed)\nCurrent version: 010\nTarget version: 001\n'
                        f'Direction: down\n1. down 010 {last}\nSteps: 1\n'
                        'SQL execution: not checked; a generated plan does not guarantee executable SQL.\n')
    assert plan_bytes() == before


@pytest.mark.parametrize('checksum_table', [False, True])
def test_json_target_plan_checksum_legacy_null_without_adoption(project, capsys, checksum_table):
    file = project / 'db/migrations/010_legacy.sql'
    file.write_bytes(b'legacy invalid UTF-8 and markers: \xff\r\n')
    with connect('local.duckdb') as con:
        con.execute('create table history(version varchar primary key)')
        con.execute("insert into history values ('010')")
        if checksum_table:
            con.execute('create table _duckmate_checksums '
                        '(migrations_table varchar, version varchar, checksum varchar)')
            # Same version under another history table must never count as evidence.
            con.execute("insert into _duckmate_checksums values ('other_history', '010', 'foreign')")
    before = plan_bytes()
    down = target_json(capsys, '0', '--migrations-table', 'HISTORY')
    assert down['steps'] == [
        {'position': 1, 'direction': 'down', 'version': '010', 'path': str(file),
         'checksum': digest(file), 'recorded_checksum': None}]
    assert plan_bytes() == before
    with connect('local.duckdb', readonly=True) as con:
        if checksum_table:
            assert con.execute('select * from _duckmate_checksums').fetchall() == [('other_history', '010', 'foreign')]
        else:
            assert con.execute("select count(*) from duckdb_tables() where table_name='_duckmate_checksums'").fetchone() == (0,)


@pytest.mark.parametrize('table', ['schema_migrations', 'history'])
@pytest.mark.parametrize('checksum_type,checksum', [('blob', 'digest-bytes'), ('blob', b'raw\xff'),
                                                   ('integer', 10), ('integer[]', [10]),
                                                   ('struct(v integer)', {'v': 10}), ('varchar', None)])
def test_checksum_nontext_values(project, capsys, table, checksum_type, checksum):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    if checksum == 'digest-bytes':
        checksum = digest(file).encode('ascii')
    rows = [(table, '010', checksum)]
    with connect('local.duckdb') as con:
        con.execute(f'create table {table}(version varchar primary key)')
        con.execute(f"insert into {table} values ('010')")
        con.execute('create table _duckmate_checksums '
                    f'(migrations_table varchar, version varchar, checksum {checksum_type}'
                    + ('' if checksum is None else ' not null') + ')')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    constraints = value('local.duckdb', 'select * from duckdb_constraints()')
    message = f'{table}: checksum values must be text in _duckmate_checksums'
    assert cli('status', '--json', '--migrations-table', table) == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    report = target_json(capsys, '0', '--migrations-table', table, expected=2)
    assert report == {
        'schema_version': 1, 'command': 'plan', 'database': str(project / 'local.duckdb'),
        'migrations_table': table, 'migrations_directory': str(project / 'db/migrations'),
        'strict': False, 'requested_target': '0', 'current_version': None, 'target_version': None,
        'sql_execution': 'not_checked', 'direction': None, 'steps': [], 'ok': False, 'exit_code': 2,
        'problems': [{'code': 'invalid_input', 'version': None, 'path': None, 'message': message}]}
    assert cli('plan', '--to', '0', '--migrations-table', table) == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert plan_bytes() == before
    assert value('local.duckdb', f'select * from {table}') == [('010',)]
    assert value('local.duckdb', 'select * from _duckmate_checksums') == rows
    assert value('local.duckdb', 'select * from duckdb_constraints()') == constraints


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('damage', ['syntax', 'size', 'history-duplicate', 'null-key', 'nontext-key',
                                    'duplicate-key', 'drift', 'missing', 'orphan', 'zero', 'target', 'strict'])
def test_checksum_value_precedence(project, capsys, damage, reverse):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    versions = ['010']
    message = 'checksum values must be text in _duckmate_checksums'
    if damage == 'syntax':
        versions += ['bad']
        message = 'migration versions must be numeric'
    elif damage == 'size':
        limit = sys.get_int_max_str_digits()
        if not limit:
            pytest.skip('Python decimal conversion limit is disabled')
        versions += ['9' * (limit + 1)]
        message = 'recorded migration versions exceed the supported numeric conversion limit'
    elif damage == 'history-duplicate':
        versions += ['010']
        message = 'duplicate numeric migration versions'
    elif damage == 'missing':
        versions += ['020']
    elif damage == 'zero':
        (project / 'db/migrations/000_zero.sql').write_bytes(b'zero\xff')
    elif damage == 'strict':
        (project / 'db/migrations/002_raw.sql').write_bytes(b'pending\xff')
    key_type = 'blob' if damage == 'nontext-key' else 'varchar'
    rows = [('schema_migrations', b'010' if damage == 'nontext-key' else '010', digest(file).encode('ascii'))]
    if damage == 'null-key':
        rows += [('schema_migrations', None, b'bad')]
        message = 'checksum versions must not be NULL in _duckmate_checksums'
    elif damage == 'nontext-key':
        message = 'checksum versions must be text in _duckmate_checksums'
    elif damage == 'duplicate-key':
        rows += [('schema_migrations', '010', b'different')]
        message = 'duplicate checksum rows in _duckmate_checksums'
    elif damage == 'drift':
        rows += [('schema_migrations', '020', b'wrong')]
        versions += ['020']
        (project / 'db/migrations/020_raw.sql').write_bytes(b'other\xfe')
    elif damage == 'orphan':
        rows += [('schema_migrations', '099', b'orphan')]
    if reverse:
        versions.reverse()
        rows.reverse()
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar)')
        con.executemany('insert into schema_migrations values (?)', [(v,) for v in versions])
        con.execute('create table _duckmate_checksums '
                    f'(migrations_table varchar, version {key_type}, checksum blob not null)')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    message = f'schema_migrations: {message}'
    assert cli('status', '--json', '--strict') == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    report = target_json(capsys, '999' if damage == 'target' else '10' if damage == 'strict' else '0',
                         '--strict', expected=2)
    assert report['current_version'] is report['target_version'] is report['direction'] is None
    assert report['steps'] == []
    assert report['problems'] == [{'code': 'invalid_input', 'version': None, 'path': None, 'message': message}]
    assert plan_bytes() == before
    assert value('local.duckdb', 'select version from schema_migrations') == [(v,) for v in versions]
    assert value('local.duckdb', 'select * from _duckmate_checksums') == rows


def test_checksum_foreign_blob_value_scoped(project, capsys):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'snapshot sentinel')
    rows = [('other_history', '010', digest(file).encode('ascii'))]
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar primary key)')
        con.execute("insert into schema_migrations values ('010')")
        con.execute('create table _duckmate_checksums '
                    '(migrations_table varchar, version varchar, checksum blob not null)')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    assert cli('status', '--json') == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert output.err == '' and report['problems'][0]['code'] == 'missing_checksum'
    assert report['migrations'][0]['recorded_checksum'] is None
    report = target_json(capsys, '0')
    assert report['steps'] == [{'position': 1, 'direction': 'down', 'version': '010', 'path': str(file),
                                'checksum': digest(file), 'recorded_checksum': None}]
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from _duckmate_checksums') == rows


@pytest.mark.parametrize('owner', ['schema_migrations', 'other_history'])
@pytest.mark.parametrize('existing_output', [False, True])
@pytest.mark.parametrize('checksum_type,checksum', [('blob', 'digest-bytes'), ('blob', b'raw\xff'),
                                                   ('integer', 10), ('integer[]', [10]),
                                                   ('struct(v integer)', {'v': 10}), ('varchar', None)])
def test_dump_nontext_checksum_values(project, capsys, owner, existing_output, checksum_type, checksum):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')  # Dump must neither decode nor execute migration files.
    Path('db/schema.sql').write_bytes(b'input snapshot sentinel')
    export = Path('export.sql')
    if existing_output:
        export.write_bytes(b'export sentinel\xff')
    original_export = export.read_bytes() if existing_output else None
    if checksum == 'digest-bytes':
        checksum = digest(file).encode('ascii')
    rows = [(owner, '010', checksum)]
    if checksum is None:
        # A valid row before the invalid one must not hide the later refusal.
        rows.insert(0, ('aaa_history', '001', 'Exact Stored Text'))
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar primary key)')
        con.execute("insert into schema_migrations values ('010')")
        con.execute('create table _duckmate_checksums '
                    f'(migrations_table varchar, version varchar, checksum {checksum_type}'
                    + ('' if checksum is None else ' not null') + ')')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    constraints = value('local.duckdb', 'select * from duckdb_constraints()')
    assert cli('dump', '--schema-file', 'export.sql') == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'datawrench duckmate: _duckmate_checksums: checksum values must be text in schema dumps\n'
    assert (export.read_bytes() if export.exists() else None) == original_export
    assert not list(project.glob('.duckmate-*'))
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from schema_migrations') == [('010',)]
    assert value('local.duckdb', 'select * from _duckmate_checksums') == rows
    assert value('local.duckdb', 'select * from duckdb_constraints()') == constraints


@pytest.mark.parametrize('damage,message', [
    ('syntax', 'schema_migrations: migration versions must be numeric'),
    ('duplicate', 'schema_migrations: duplicate numeric migration versions'),
    ('size', 'schema_migrations: recorded migration versions exceed the supported numeric conversion limit'),
    ('catalog', 'macro default arguments cannot be dumped: "main"."f"')])
@pytest.mark.parametrize('key_type,key', [('varchar', '010'), ('blob', b'010')])
def test_dump_checksum_export_precedence(project, capsys, damage, message, key_type, key):
    versions = ['010']
    if damage == 'syntax':
        versions.append('bad')
    elif damage == 'duplicate':
        versions.append('010')
    elif damage == 'size':
        limit = sys.get_int_max_str_digits()
        if not limit:
            pytest.skip('Python decimal conversion limit is disabled')
        versions.append('9' * (limit + 1))
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar)')
        con.executemany('insert into schema_migrations values (?)', [(v,) for v in versions])
        con.execute('create table _duckmate_checksums '
                    f'(migrations_table varchar, version {key_type}, checksum blob not null)')
        con.execute("insert into _duckmate_checksums values ('other_history', ?, ?)", [key, b'bad'])
        if damage == 'catalog':
            con.execute('create macro f(x := 2) as x')
        con.execute('CHECKPOINT')
    before = plan_bytes()
    assert cli('dump', '--schema-file', 'export.sql') == 2
    output = capsys.readouterr()
    assert output.out == '' and output.err == f'datawrench duckmate: {message}\n'
    assert not Path('export.sql').exists()
    assert plan_bytes() == before


@pytest.mark.parametrize('primary_key', [False, True])
def test_dump_text_checksum_values_roundtrip(project, capsys, primary_key):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'input snapshot sentinel')
    rows = [('schema_migrations', '010', digest(file).upper()),
            ('other_history', '00010', "Exact 'Spelling' \u96ea"),
            ('other_history', "key 'quoted'\n\u96ea", 'Exact Text')]
    if not primary_key:
        rows.append(rows[1])  # Dump retains all exported rows without repairing constraints.
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar primary key)')
        con.execute("insert into schema_migrations values ('010')")
        con.execute('create table _duckmate_checksums '
                    '(migrations_table varchar, version varchar, checksum varchar not null'
                    + (', primary key(migrations_table, version)' if primary_key else '') + ')')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    constraints = value('local.duckdb', 'select * from duckdb_constraints()')
    for output in ['export.sql', 'again.sql']:
        assert cli('dump', '--schema-file', output) == 0
        captured = capsys.readouterr()
        assert captured.err == '' and captured.out == f'Writing: {output}\n'
    original = Path('export.sql').read_bytes()
    assert Path('again.sql').read_bytes() == original
    assert digest(file).upper().encode() in original and b"Exact ''Spelling''" in original
    assert cli('load', '--schema-file', 'export.sql', db='restored.duckdb') == 0
    assert sorted(value('restored.duckdb', 'select * from _duckmate_checksums')) == sorted(rows)
    assert cli('dump', '--schema-file', 'restored.sql', db='restored.duckdb') == 0
    assert Path('restored.sql').read_bytes() == original
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from _duckmate_checksums') == rows
    assert value('local.duckdb', 'select * from duckdb_constraints()') == constraints


@pytest.mark.parametrize('owner', ['schema_migrations', 'other_history'])
@pytest.mark.parametrize('existing_output', [False, True])
@pytest.mark.parametrize('key_type,key,primary_key', [
    ('blob', b'010', False), ('blob', b'010', True), ('blob', b'raw\xff', False),
    ('integer', 10, False), ('integer[]', [10], False),
    ('struct(v integer)', {'v': 10}, False), ('varchar', None, False)])
def test_dump_unsupported_checksum_versions(project, capsys, owner, existing_output, key_type, key, primary_key):
    file = project / 'db/migrations/010_raw.sql'
    file.write_bytes(b'raw\xff')
    Path('db/schema.sql').write_bytes(b'input snapshot sentinel')
    export = Path('export.sql')
    if existing_output:
        export.write_bytes(b'export sentinel\xff')
    old = export.read_bytes() if export.exists() else None
    rows = [(owner, key, digest(file))]
    if key is None:
        rows.insert(0, ('aaa_history', '001', 'Exact Text'))
    with connect('local.duckdb') as con:
        con.execute('create table schema_migrations(version varchar primary key)')
        con.execute("insert into schema_migrations values ('010')")
        con.execute('create table _duckmate_checksums '
                    f'(migrations_table varchar, version {key_type}, checksum varchar not null'
                    + (', primary key(migrations_table, version)' if primary_key else '') + ')')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    constraints = value('local.duckdb', 'select * from duckdb_constraints()')
    assert cli('dump', '--schema-file', 'export.sql') == 2
    output = capsys.readouterr()
    message = 'must not be NULL' if key is None else 'must be text'
    assert output.out == ''
    assert output.err == f'datawrench duckmate: _duckmate_checksums: checksum versions {message} in schema dumps\n'
    assert (export.read_bytes() if export.exists() else None) == old
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from schema_migrations') == [('010',)]
    assert value('local.duckdb', 'select * from _duckmate_checksums') == rows
    assert value('local.duckdb', 'select * from duckdb_constraints()') == constraints
    assert not list(project.glob('.duckmate-*'))


@pytest.mark.parametrize('null_key', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_dump_checksum_version_before_value(project, capsys, null_key, reverse):
    # Across all exported rows, NULL keys precede non-text keys and both precede
    # bad values, independent of owner/insertion order. Duplicate keys survive.
    rows = [('aaa_history', b'010', b'bad'), ('aaa_history', b'010', b'bad'),
            ('schema_migrations', None if null_key else b'020', b'bad')]
    if reverse:
        rows.reverse()
    with connect('local.duckdb') as con:
        con.execute('create table _duckmate_checksums '
                    '(migrations_table varchar, version blob, checksum blob)')
        con.executemany('insert into _duckmate_checksums values (?, ?, ?)', rows)
        con.execute('CHECKPOINT')
    before = plan_bytes()
    assert cli('dump', '--schema-file', 'export.sql') == 2
    output = capsys.readouterr()
    message = 'must not be NULL' if null_key else 'must be text'
    assert output.out == ''
    assert output.err == f'datawrench duckmate: _duckmate_checksums: checksum versions {message} in schema dumps\n'
    assert not Path('export.sql').exists()
    assert plan_bytes() == before
    assert value('local.duckdb', 'select * from _duckmate_checksums') == rows
