import hashlib
import json
from pathlib import Path
import shutil

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.exporter import build_manifest, serialize
from datawrench.sqlmesh_manifest.loading import load_project

EXAMPLE = Path(__file__).resolve().parents[2] / 'examples' / 'sqlmesh_manifest' / 'lineage_project'


def test_lineage_cli_fixture_and_relocation(tmp_path, capsys):
    outputs = []
    for name in ('first', 'relocated'):
        project = Path(shutil.copytree(EXAMPLE, tmp_path / name))
        # Deliberate credential and source-literal probes must stay out of the report.
        config = project / 'config.yaml'
        config.write_text(config.read_text().replace("type: duckdb", "type: duckdb\n      token: CONFIG_CREDENTIAL_SENTINEL"))
        model = project / 'models/final.sql'
        model.write_text(model.read_text() + '\n-- SOURCE_LITERAL_SECRET\n')
        out, report = tmp_path / f'{name}.manifest.json', tmp_path / f'{name}.lineage.json'
        assert main(['export', '--project', str(project), '--out', str(out), '--report', str(report), '--deterministic']) == 0
        manifest, data = json.loads(out.read_text()), json.loads(report.read_text())
        assert data['report_version'] == 1
        assert data['project_name'] == 'lineage_example'
        assert data['manifest_sha256'] == hashlib.sha256(out.read_bytes()).hexdigest()
        resources = {r['dbt_node_id']: r for r in data['resources']}
        assert list(resources) == sorted(resources)
        assert set(resources) == manifest['nodes'].keys() | manifest['sources'].keys()
        stage = resources['model.lineage_example.stage']
        final = resources['model.lineage_example.final']
        source = resources['source.lineage_example.raw.input']
        assert final['origin']['file'] == 'models/final.sql'
        assert final['origin']['sqlmesh_name'] == '"memory"."analytics"."final"'
        assert final['origin']['sqlmesh_kind'] == 'INCREMENTAL_BY_TIME_RANGE'
        assert final['dependencies'] == [{
            'dbt_node_id': stage['dbt_node_id'], 'file': 'models/stage.sql',
            'sqlmesh_name': '"memory"."analytics"."stage"',
        }]
        assert stage['dependencies'] == [{
            'dbt_node_id': source['dbt_node_id'], 'file': 'external_models/input.yaml',
            'sqlmesh_name': '"memory"."raw"."input"',
        }]
        assert source['origin']['file'] == 'external_models/input.yaml'
        assert source['dependencies'] == []
        semantics = {s['semantic']: s['reason'] for s in final['unconverted']}
        assert {'kind_execution', 'cron', 'pre_statements', 'partitioned_by'} <= semantics.keys()
        assert all(semantics.values())
        audit = next(r for r in resources.values() if r['resource_type'] == 'test')
        assert audit['origin']['file'] == 'models/final.sql'
        assert audit['origin']['sqlmesh_audit'] == 'not_null'
        assert audit['origin']['attached_node'] == final['dbt_node_id']
        assert audit['dependencies'][0]['dbt_node_id'] == final['dbt_node_id']
        for resource in resources.values():
            semantics = [s['semantic'] for s in resource['unconverted']]
            assert semantics == sorted(semantics)
        text = report.read_text()
        for secret in ('CONFIG_CREDENTIAL_SENTINEL', 'SOURCE_LITERAL_SECRET', str(project), 'CREATE TABLE'):
            assert secret not in text
        assert 'SOURCE_LITERAL_SECRET' in out.read_text()  # Probe is actually in the exported raw code.
        outputs.append(report.read_bytes())
    assert outputs[0] == outputs[1]
    assert 'error:' not in capsys.readouterr().err


@pytest.mark.parametrize('relative', ['models/report.json', 'config.yaml'])
def test_report_protects_inputs_and_preserves_manifest(project, tmp_path, capsys, relative):
    out = tmp_path / 'manifest.json'
    out.write_text('previous manifest')
    report = project / relative
    before = report.read_bytes() if report.exists() else None
    assert main(['export', '--project', str(project), '--out', str(out), '--report', str(report)]) == 2
    assert 'overwrite project inputs' in capsys.readouterr().err
    assert out.read_text() == 'previous manifest'
    assert (report.read_bytes() if report.exists() else None) == before


def test_report_paths_and_failed_loading_preserve_artifacts(project, tmp_path, capsys):
    out, report = tmp_path / 'manifest.json', tmp_path / 'lineage.json'
    out.write_text('previous manifest')
    report.write_text('previous report')
    assert main(['export', '--project', str(project), '--out', str(out), '--report', str(out)]) == 2
    assert 'distinct' in capsys.readouterr().err
    link = tmp_path / 'linked.json'
    link.symlink_to(report)
    assert main(['export', '--project', str(project), '--out', str(out), '--report', str(link)]) == 2
    assert 'symlink' in capsys.readouterr().err
    sentinel = tmp_path / 'executed'
    (project / 'models/unsafe.sql').write_text(f"""MODEL (name analytics.unsafe);
JINJA_QUERY_BEGIN;
{{{{ cycler.__init__.__globals__.os.system('touch {sentinel}') }}}}
JINJA_END;
""")
    assert main(['export', '--project', str(project), '--out', str(out), '--report', str(report)]) == 2
    assert 'trust-project' in capsys.readouterr().err
    assert not sentinel.exists()
    assert out.read_text() == 'previous manifest'
    assert report.read_text() == 'previous report'


def test_report_covers_seed_implicit_source_and_standalone_audit(project):
    from datawrench.sqlmesh_manifest.lineage import build_lineage_report
    (project / 'models/implicit.sql').write_text('MODEL (name analytics.implicit, kind VIEW); SELECT id FROM unknown.upstream;')
    (project / 'audits').mkdir()
    (project / 'audits/check.sql').write_text('AUDIT (name check_rows, standalone true); SELECT * FROM analytics.stg_orders WHERE order_id IS NULL;')
    with load_project(project) as c:
        manifest, _ = build_manifest(c, deterministic=True)
        report = build_lineage_report(c, manifest)
    resources = {r['dbt_node_id']: r for r in report['resources']}
    assert resources['seed.mini_project.raw_orders']['origin']['file'] == 'models/raw_orders.sql'
    implicit = resources['source.mini_project.unknown.upstream']
    assert implicit['origin']['file'] is None
    assert implicit['unconverted'][0]['semantic'] == 'source_declaration'
    audit = resources['test.mini_project.check_rows']
    assert audit['origin']['file'] == 'audits/check.sql'
    assert audit['origin']['sqlmesh_audit'] == 'check_rows'
    assert audit['dependencies'][0]['dbt_node_id'] == 'model.mini_project.stg_orders'


def test_report_python_and_runtime_only_semantics(project, tmp_path):
    from datawrench.sqlmesh_manifest.lineage import build_lineage_report
    sentinel = tmp_path / 'python-entrypoint-executed'
    (project / 'models/python_model.py').write_text(f'''
from sqlmesh import model
from pathlib import Path
@model("analytics.python_model", columns={{"value": "INT"}}, depends_on=["analytics.stg_orders"])
def execute(context, **kwargs):
    Path({str(sentinel)!r}).touch()
''')
    (project / 'models/runtime.sql').write_text("""
MODEL (name analytics.runtime, kind FULL, columns (value INT), depends_on (analytics.stg_orders));
JINJA_QUERY_BEGIN;
{% if runtime_stage == 'evaluating' %}SELECT 1 AS value{% endif %}
JINJA_END;
""")
    with load_project(project, trusted=True) as c:
        manifest, _ = build_manifest(c, deterministic=True)
        report = build_lineage_report(c, manifest)
    resources = {r['dbt_node_id']: r for r in report['resources']}
    assert 'python_execution' in {s['semantic'] for s in resources['model.mini_project.python_model']['unconverted']}
    assert 'compiled_sql' in {s['semantic'] for s in resources['model.mini_project.runtime']['unconverted']}
    assert not sentinel.exists()
    assert str(sentinel) not in serialize(report)


def test_report_does_not_retry_rendering_or_expose_exceptions(example, monkeypatch):
    from sqlmesh.core.model.definition import SqlModel
    from datawrench.sqlmesh_manifest.lineage import build_lineage_report
    c, _, _ = example
    attempts = []
    def fail(self, **kwargs):
        attempts.append(self.fqn)
        raise RuntimeError('EXCEPTION_CREDENTIAL_SENTINEL')
    monkeypatch.setattr(SqlModel, 'render_query', fail)
    manifest, _ = build_manifest(c, deterministic=True)
    before = len(attempts)
    assert before > 0
    report = build_lineage_report(c, manifest)
    assert len(attempts) == before
    assert 'EXCEPTION_CREDENTIAL_SENTINEL' not in serialize(report)
    stage = next(r for r in report['resources'] if r['dbt_node_id'] == 'model.mini_project.stg_orders')
    assert 'compiled_sql' in {s['semantic'] for s in stage['unconverted']}
