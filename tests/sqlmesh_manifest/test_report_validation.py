"""Contract for validating exact-byte provenance report / manifest pairs."""
import copy
import hashlib
import json

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.exporter import build_manifest, serialize
from datawrench.sqlmesh_manifest.lineage import build_lineage_report

STAGE = 'model.mini_project.stg_orders'
SEED = 'seed.mini_project.raw_orders'
FINAL = 'model.mini_project.fct_orders'


@pytest.fixture
def pair(example, tmp_path):
    context, manifest, _ = example
    report = build_lineage_report(context, manifest)
    manifest_path, report_path = tmp_path / 'manifest.json', tmp_path / 'report.json'
    manifest_path.write_text(serialize(manifest))
    report_path.write_text(serialize(report))
    return manifest_path, report_path


def validate(pair):
    manifest, report = pair
    return main(['validate-report', str(report), '--manifest', str(manifest)])


def rewrite_report(pair, change):
    data = json.loads(pair[1].read_text())
    change(data)
    pair[1].write_text(serialize(data))


def entry(data, uid=FINAL):
    return next(r for r in data['resources'] if r['dbt_node_id'] == uid)


def test_validate_report_valid_pair_is_read_only_and_offline(pair, monkeypatch, capsys):
    import socket
    import requests
    import datawrench.sqlmesh_manifest.cli as cli

    def blocked(*args, **kwargs):
        pytest.fail('Report validation must not load projects or use network')

    monkeypatch.setattr(cli, 'load_project', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    before = [p.read_bytes() for p in pair]
    assert validate(pair) == 0
    capture = capsys.readouterr()
    assert capture.out == 'Valid provenance report v1 for supplied manifest\n'
    assert capture.err == ''
    assert [p.read_bytes() for p in pair] == before


@pytest.mark.parametrize('version', [None, True, 1.0, '1', 0, 2])
def test_report_version_is_exact_integer_one(pair, capsys, version):
    rewrite_report(pair, lambda d: d.update(report_version=version))
    assert validate(pair) == 1
    capture = capsys.readouterr()
    assert capture.out == ''
    assert 'report_version' in capture.err


# Known existing IDs are deliberately used for several edits: existence alone
# cannot prove complete provenance, the right attachment, or the right edge.
@pytest.mark.parametrize('change,rule', [
    (lambda d: d.pop('report_version'), 'report_version'),
    (lambda d: d.update(manifest_sha256='0' * 64), 'manifest_sha256'),
    (lambda d: d.update(manifest_sha256='NOT_A_DIGEST'), 'schema'),
    (lambda d: d.update(project_name='OTHER_PROJECT_SECRET'), 'project_name'),
    (lambda d: d['resources'].pop(), 'resources'),
    (lambda d: d['resources'].append(copy.deepcopy(d['resources'][0])), 'duplicate'),
    (lambda d: entry(d).update(dbt_node_id='model.missing.SECRET'), 'resource'),
    (lambda d: entry(d).update(resource_type='seed'), 'resource_type'),
    (lambda d: entry(d)['origin'].update(file='SECRET_PATH'), 'origin'),
    (lambda d: entry(d)['origin'].update(sqlmesh_name='SECRET_NAME'), 'origin'),
    (lambda d: entry(d)['origin'].update(sqlmesh_kind='VIEW'), 'origin'),
    (lambda d: entry(d)['origin'].update(sqlmesh_audit='SECRET_AUDIT'), 'origin'),
    (lambda d: next(r for r in d['resources'] if r['resource_type'] == 'test')['origin'].update(attached_node=SEED), 'origin'),
    (lambda d: entry(d)['dependencies'].clear(), 'dependencies'),
    (lambda d: entry(d)['dependencies'].append(copy.deepcopy(entry(d)['dependencies'][0])), 'duplicate'),
    (lambda d: entry(d)['dependencies'][0].update(dbt_node_id='model.missing.SECRET'), 'dependencies'),
    (lambda d: entry(d)['dependencies'][0].update(dbt_node_id=SEED), 'dependencies'),
    (lambda d: entry(d)['dependencies'][0].update(file='SECRET_PATH'), 'provenance'),
    (lambda d: entry(d)['dependencies'][0].update(sqlmesh_name='SECRET_NAME'), 'provenance'),
    (lambda d: entry(d).pop('origin'), 'schema'),
    (lambda d: entry(d).update(dependencies=[None]), 'schema'),
    (lambda d: entry(d)['origin'].update(attached_node=123), 'schema'),
    (lambda d: entry(d).update(unconverted=[{'semantic': 'x'}]), 'schema'),
    (lambda d: d.update(project_unconverted='SECRET_GAPS'), 'schema'),
    (lambda d: d.update(unknown='SECRET_FIELD'), 'schema'),
])
def test_tampered_reports_fail_without_echoing_values(pair, capsys, change, rule):
    rewrite_report(pair, change)
    before = [p.read_bytes() for p in pair]
    assert validate(pair) == 1
    capture = capsys.readouterr()
    assert capture.out == ''
    assert 'validation error:' in capture.err
    assert rule in capture.err
    assert 'SECRET' not in capture.err
    assert [p.read_bytes() for p in pair] == before


@pytest.mark.parametrize('mutation', ['formatting', 'new_export'])
def test_mismatched_artifact_pair_rejected(pair, example, capsys, mutation):
    if mutation == 'formatting':
        pair[0].write_text(json.dumps(json.loads(pair[0].read_text())))
    else:
        # A valid second export has a new invocation ID and timestamp.
        manifest, _ = build_manifest(example[0], deterministic=False)
        pair[0].write_text(serialize(manifest))
    assert validate(pair) == 1
    capture = capsys.readouterr()
    assert capture.out == ''
    assert 'manifest_sha256' in capture.err


def test_resource_and_dependency_order_is_not_significant(pair, capsys):
    def reorder(d):
        d['resources'].reverse()
        for resource in d['resources']:
            resource['dependencies'].reverse()
    rewrite_report(pair, reorder)
    assert validate(pair) == 0
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize('maps', ['null', 'parents_only', 'children_only'])
def test_optional_graph_indexes(pair, capsys, maps):
    manifest = json.loads(pair[0].read_text())
    for key in ('parent_map', 'child_map'):
        if maps == 'null' or (maps == 'parents_only' and key == 'child_map') or (maps == 'children_only' and key == 'parent_map'):
            manifest[key] = None
    pair[0].write_text(serialize(manifest))
    rewrite_report(pair, lambda d: d.update(manifest_sha256=hashlib.sha256(pair[0].read_bytes()).hexdigest()))
    assert validate(pair) == 0
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize('content,rule', [
    ('{', 'JSON'), ('null', 'object'), ('{}', 'report_version'),
    ('{"a":1,"a":2}', 'Duplicate'), ('{"a":NaN}', 'Non-finite'),
    ('{"diff_version":1,"changes":[]}', 'report_version'),
])
def test_invalid_report_json(pair, capsys, content, rule):
    pair[1].write_text(content)
    assert validate(pair) == 1
    capture = capsys.readouterr()
    assert capture.out == ''
    assert rule in capture.err


@pytest.mark.parametrize('side', [0, 1])
def test_missing_and_oversized_inputs(pair, monkeypatch, capsys, side):
    import datawrench.sqlmesh_manifest.validation as validation
    before = pair[side].read_bytes()
    pair[side].unlink()
    assert validate(pair) == 1
    assert 'Cannot read' in capsys.readouterr().err
    pair[side].write_bytes(before)
    # Keep the other side under the limit; each side must enforce its own cap.
    limit = max(p.stat().st_size for p in pair) + 1
    monkeypatch.setattr(validation, 'MAX_MANIFEST_BYTES', limit)
    pair[side].write_bytes(before + b' ' * limit)
    assert validate(pair) == 1
    capture = capsys.readouterr()
    assert capture.out == ''
    assert '64 MiB' in capture.err


@pytest.mark.parametrize('corruption', ['json', 'schema', 'graph', 'version', 'absent_maps', 'metadata_type'])
def test_manifest_must_validate_even_when_hash_matches(pair, capsys, corruption):
    manifest = json.loads(pair[0].read_text())
    if corruption == 'schema':
        del manifest['nodes'][STAGE]['unique_id']
    elif corruption == 'graph':
        manifest['nodes'][STAGE]['depends_on']['nodes'] = ['model.missing.SECRET']
        manifest['parent_map'] = manifest['child_map'] = {}
    elif corruption == 'absent_maps':
        manifest.pop('parent_map')
        manifest.pop('child_map')
    elif corruption == 'metadata_type':
        manifest['metadata'] = []
    elif corruption == 'version':
        manifest['metadata']['dbt_schema_version'] = 'https://schemas.getdbt.com/dbt/manifest/v11.json'
    pair[0].write_text('{' if corruption == 'json' else serialize(manifest))
    rewrite_report(pair, lambda d: d.update(manifest_sha256=hashlib.sha256(pair[0].read_bytes()).hexdigest()))
    assert validate(pair) == 1
    capture = capsys.readouterr()
    assert capture.out == ''
    assert 'validation error:' in capture.err


def test_seed_implicit_source_and_standalone_audit(project, tmp_path, capsys):
    (project / 'models/implicit.sql').write_text('MODEL (name analytics.implicit, kind VIEW); SELECT id FROM unknown.upstream;')
    (project / 'audits').mkdir()
    (project / 'audits/check.sql').write_text('AUDIT (name check_rows, standalone true); SELECT * FROM analytics.stg_orders WHERE order_id IS NULL;')
    paths = tmp_path / 'manifest.json', tmp_path / 'report.json'
    assert main(['export', '--project', str(project), '--out', str(paths[0]), '--report', str(paths[1]), '--deterministic']) == 0
    capsys.readouterr()
    assert validate(paths) == 0
    assert capsys.readouterr().err == ''
    implicit = 'source.mini_project.unknown.upstream'
    rewrite_report(paths, lambda d: entry(d, implicit)['origin'].update(file='external_models.yaml'))
    assert validate(paths) == 1
    assert 'origin' in capsys.readouterr().err


@pytest.mark.parametrize('maps', ['parents', 'children', 'neither'])
def test_seed_edges_use_available_graph_index(pair, capsys, maps):
    # Seeds have no depends_on.nodes field in v12; their map edges still count.
    manifest = json.loads(pair[0].read_text())
    source = next(iter(manifest['sources']))
    manifest['parent_map'][SEED] = [source]
    manifest['child_map'][source].append(SEED)
    report = json.loads(pair[1].read_text())
    entry(report, SEED)['dependencies'] = [{
        'dbt_node_id': source,
        'file': manifest['sources'][source]['original_file_path'],
        'sqlmesh_name': manifest['sources'][source]['meta']['sqlmesh_name'],
    }]
    if maps != 'parents':
        manifest['parent_map'] = None
    if maps != 'children':
        manifest['child_map'] = None
    pair[0].write_text(serialize(manifest))
    report['manifest_sha256'] = hashlib.sha256(pair[0].read_bytes()).hexdigest()
    pair[1].write_text(serialize(report))
    assert validate(pair) == (1 if maps == 'neither' else 0)
    if maps == 'neither':
        assert 'dependencies' in capsys.readouterr().err
    else:
        assert capsys.readouterr().err == ''
        rewrite_report(pair, lambda d: entry(d, SEED)['dependencies'].clear())
        assert validate(pair) == 1
        assert 'dependencies' in capsys.readouterr().err


@pytest.mark.parametrize('attachment', ['model.missing.SECRET', 'inconsistent'])
def test_attachment_in_manifest_cannot_bypass_id_checks(pair, capsys, attachment):
    manifest = json.loads(pair[0].read_text())
    report = json.loads(pair[1].read_text())
    uid = next(uid for uid, node in manifest['nodes'].items() if node['resource_type'] == 'test')
    value = SEED if attachment == 'inconsistent' else attachment
    manifest['nodes'][uid]['meta']['attached_node'] = value
    entry(report, uid)['origin']['attached_node'] = value
    pair[0].write_text(serialize(manifest))
    report['manifest_sha256'] = hashlib.sha256(pair[0].read_bytes()).hexdigest()
    pair[1].write_text(serialize(report))
    assert validate(pair) == 1
    capture = capsys.readouterr()
    assert capture.out == ''
    assert 'origin.attached_node' in capture.err
    assert 'SECRET' not in capture.err


def test_noncanonical_manifest_bytes_with_matching_digest_are_valid(pair, capsys):
    # Checksum validation must hash the supplied bytes, not serialize the dict.
    pair[0].write_text(json.dumps(json.loads(pair[0].read_text()), indent=4) + '\n\n')
    rewrite_report(pair, lambda d: d.update(manifest_sha256=hashlib.sha256(pair[0].read_bytes()).hexdigest()))
    assert validate(pair) == 0
    assert capsys.readouterr().err == ''
