import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from sqlmesh.core.config.connection import ConnectionConfig

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.exporter import build_manifest, serialize
from datawrench.sqlmesh_manifest.loading import ExportError, load_project
from datawrench.sqlmesh_manifest.lineage import build_lineage_report
from datawrench.sqlmesh_manifest.validation import ManifestError, read_manifest, validate_manifest


def test_real_cli_fixture_subprocess(project, tmp_path):
    out = tmp_path / 'nested' / 'manifest.json'
    command = [sys.executable, '-m', 'datawrench', 'sqlmesh-manifest']
    result = subprocess.run(command + ['export', '--project', str(project), '--out', str(out), '--deterministic'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text())['metadata']['adapter_type'] == 'duckdb'
    valid = subprocess.run(command + ['validate', str(out)], capture_output=True, text=True)
    assert valid.returncode == 0 and 'Valid dbt manifest' in valid.stdout
    summary = subprocess.run(command + ['summary', str(out)], capture_output=True, text=True)
    assert summary.returncode == 0
    assert summary.stdout.strip() == 'models=3 seeds=1 sources=1 tests=2 edges=5'


def test_no_connections_no_state_no_network_no_project_writes(project, monkeypatch):
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()}
    from sqlmesh.core import constants
    personal = project.parent / 'personal-sqlmesh'
    monkeypatch.setattr(constants, 'SQLMESH_PATH', personal)
    attempts = []
    def blocked(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('No connection or network allowed')
    import socket
    import requests
    monkeypatch.setattr(ConnectionConfig, 'create_engine_adapter', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    (project / '.env').write_text('041_TEST_SECRET=should-not-load\n')
    import os
    monkeypatch.delenv('041_TEST_SECRET', raising=False)
    with load_project(project) as c:
        manifest, _ = build_manifest(c, deterministic=True)
        report = build_lineage_report(c, manifest)
        assert len(report['resources']) == len(manifest['nodes']) + len(manifest['sources'])
        assert c._engine_adapter is None and c._state_sync is None
        for attr in ('engine_adapter', 'engine_adapters', 'state_sync'):
            with pytest.raises(ExportError, match='disabled'):
                getattr(c, attr)
    validate_manifest(manifest)
    assert not attempts
    assert not personal.exists()
    assert '041_TEST_SECRET' not in os.environ
    after = {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file() and p.name != '.env'}
    assert before == after


@pytest.mark.parametrize('kind', ['missing', 'empty'])
def test_bad_directory(tmp_path, capsys, kind):
    path = tmp_path / kind
    if kind == 'empty':
        path.mkdir()
    out = tmp_path / 'manifest.json'
    assert main(['export', '--project', str(path), '--out', str(out)]) == 2
    assert 'error:' in capsys.readouterr().err
    assert not out.exists()


def test_bad_schema_rejected_before_load(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(['export', '--project', str(tmp_path), '--out', str(tmp_path / 'manifest.json'), '--schema-version', '13'])
    assert error.value.code == 2
    assert 'invalid choice' in capsys.readouterr().err


@pytest.mark.parametrize('content', ['{', '{}', 'null', '[]', '{"a":1,"a":2}', '{"metadata": {"dbt_schema_version":"unknown"}}'])
def test_invalid_manifests(tmp_path, capsys, content):
    file = tmp_path / 'broken.json'
    file.write_text(content)
    assert main(['validate', str(file)]) == 1
    assert 'validation error:' in capsys.readouterr().err


def test_invalid_node_and_bad_summary(example, tmp_path, capsys):
    m = json.loads(serialize(example[1]))
    del m['nodes']['model.mini_project.fct_orders']['unique_id']
    file = tmp_path / 'invalid.json'
    file.write_text(json.dumps(m))
    assert main(['validate', str(file)]) == 1
    assert '$.nodes.model.mini_project.fct_orders' in capsys.readouterr().err
    assert main(['summary', str(file)]) == 1


def test_dangling_dependency_with_empty_maps_cli(example, tmp_path, capsys):
    m = json.loads(serialize(example[1]))
    m['nodes']['model.mini_project.stg_orders']['depends_on']['nodes'] = ['model.mini_project.missing']
    m['parent_map'] = m['child_map'] = {}
    file = tmp_path / 'dangling.json'
    file.write_text(json.dumps(m))
    for command in ('validate', 'summary'):
        assert main([command, str(file)]) == 1
        assert 'dangling' in capsys.readouterr().err


def test_missing_manifest(tmp_path, capsys):
    assert main(['validate', str(tmp_path / 'absent.json')]) == 1
    assert 'Cannot read manifest' in capsys.readouterr().err


def test_python_config_requires_explicit_trust(project, tmp_path):
    (project / 'config.yaml').unlink()
    sentinel = tmp_path / 'did-run'
    (project / 'config.py').write_text(f'''from pathlib import Path
Path({str(sentinel)!r}).write_text("executed")
from sqlmesh import Config
config = Config(model_defaults={{"dialect": "duckdb"}}, project="python_config", gateways={{"local": {{"connection": {{"type": "duckdb"}}}}}})
''')
    with pytest.raises(ExportError, match='trust-project'):
        with load_project(project):
            pass
    assert not sentinel.exists()
    with load_project(project, trusted=True) as c:
        m, _ = build_manifest(c)
    assert m['metadata']['project_name'] == 'python_config'
    assert sentinel.exists()


@pytest.mark.parametrize('directory', ['models', 'macros', 'signals', 'materializations', 'linter'])
def test_python_files_not_imported(project, tmp_path, directory):
    sentinel = tmp_path / 'executed'
    target = project / directory
    target.mkdir(exist_ok=True)
    (target / 'unsafe.py').write_text(f'from pathlib import Path\nPath({str(sentinel)!r}).touch()\n')
    with pytest.raises(ExportError, match='trust-project'):
        with load_project(project):
            pass
    assert not sentinel.exists()


@pytest.mark.parametrize('relative,content', [
    ('models/unsafe.sql', "MODEL (name a.b); SELECT @SQL('1') AS x;"),
    ('models/unsafe.sql', "MODEL (name a.b); JINJA_QUERY_BEGIN; SELECT {{ 1 }}; JINJA_END;"),
    ('tests/test_unsafe.yaml', "{{ cycler.__init__.__globals__.os.system('false') }}"),
    ('external_models/unsafe.yaml', '- name: a.b\n  python_env: {}\n'),
    ('schema.yaml', '- name: a.b\n  python_env: {}\n'),
])
def test_executable_inputs_rejected(project, relative, content):
    path = project / relative
    path.parent.mkdir(exist_ok=True)
    path.write_text(content)
    with pytest.raises(ExportError):
        with load_project(project):
            pass


def test_seed_path_cannot_escape_before_loading(project, tmp_path):
    seed = project / 'models/raw_orders.sql'
    seed.write_text(seed.read_text().replace('../seeds/raw_orders.csv', '../../private.csv'))
    with pytest.raises(ExportError, match='escapes'):
        with load_project(project):
            pass


@pytest.mark.parametrize('where', ['models', 'seeds/raw_orders.csv', 'external_models.yaml', 'external_models', 'tests'])
def test_input_symlinks_rejected(project, tmp_path, where):
    path = project / where
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExportError, match='Symlink'):
        with load_project(project):
            pass


def test_output_symlink_rejected_without_mutation(project, tmp_path, capsys):
    original = tmp_path / 'original'
    original.write_text('untouched')
    out = tmp_path / 'manifest.json'
    out.symlink_to(original)
    assert main(['export', '--project', str(project), '--out', str(out)]) == 2
    assert 'symlink' in capsys.readouterr().err
    assert original.read_text() == 'untouched'


def test_failed_export_preserves_previous_artifact(project, tmp_path, capsys):
    out = tmp_path / 'manifest.json'
    out.write_text('previous')
    (project / 'models/stg_orders.sql').write_text('MODEL (name broken) SELECT syntax error')
    assert main(['export', '--project', str(project), '--out', str(out)]) == 2
    assert out.read_text() == 'previous'


def test_upstream_error_text_not_leaked(project, tmp_path, capsys):
    (project / 'config.yaml').write_text('password: EXAMPLE_SECRET_DO_NOT_PRINT\nmodel_defaults: broken\n')
    assert main(['export', '--project', str(project), '--out', str(tmp_path / 'out')]) == 2
    capture = capsys.readouterr()
    assert 'EXAMPLE_SECRET_DO_NOT_PRINT' not in capture.out + capture.err


def test_gateway_selection_and_catalog(project):
    (project / 'config.yaml').write_text('''project: mini_project
model_defaults:
  dialect: duckdb
  start: 2024-01-01
gateways:
  local:
    connection:
      type: duckdb
  alternate:
    connection:
      type: duckdb
    model_defaults:
      owner: alternate-team
''')
    with load_project(project, gateway='alternate') as c:
        m, _ = build_manifest(c)
        assert c.selected_gateway == 'alternate'
    assert m['nodes']['model.mini_project.fct_orders']['meta']['owner'] == 'analytics-team'
    assert m['nodes']['model.mini_project.stg_orders']['meta']['owner'] == 'alternate-team'


def test_unknown_gateway_is_cli_error(project, tmp_path, capsys):
    assert main(['export', '--project', str(project), '--out', str(tmp_path / 'out'), '--gateway', 'absent']) == 2
    assert 'error:' in capsys.readouterr().err


def test_manifest_size_limit(tmp_path, monkeypatch):
    from datawrench.sqlmesh_manifest import validation
    monkeypatch.setattr(validation, 'MAX_MANIFEST_BYTES', 8)
    file = tmp_path / 'large.json'
    file.write_text('{"0123456789": true}')
    with pytest.raises(ManifestError, match='limit'):
        read_manifest(file)


@pytest.mark.parametrize('relative', ['config.yaml', 'models/fct_orders.sql', 'seeds/raw_orders.csv', 'external_models.yaml', 'models/new.json'])
def test_output_cannot_overwrite_project_inputs(project, relative, capsys):
    path = project / relative
    before = path.read_bytes() if path.exists() else None
    assert main(['export', '--project', str(project), '--out', str(path)]) == 2
    assert 'overwrite project inputs' in capsys.readouterr().err
    if before is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == before


def test_output_parent_traversal_cannot_bypass_input_protection(project, capsys):
    path = project / 'target/../models/fct_orders.sql'
    before = (project / 'models/fct_orders.sql').read_bytes()
    assert main(['export', '--project', str(project), '--out', str(path)]) == 2
    assert (project / 'models/fct_orders.sql').read_bytes() == before
