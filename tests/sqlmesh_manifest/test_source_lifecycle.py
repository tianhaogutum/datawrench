"""Source lifecycle contract against real external YAML exports, without SQL execution."""
import copy
import json
from pathlib import Path
import shutil

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.diff import compare_manifests, render_diff
from datawrench.sqlmesh_manifest.exporter import serialize

FIXTURES = Path(__file__).parent / 'fixtures' / 'source_lifecycle'
PREFIX = 'source.source_lifecycle.raw.'
MODEL = 'model.source_lifecycle.stage'


def lifecycle(kind, name):
    present = f'external_models/{name}.yaml'
    return {'kind': kind, 'node_id': PREFIX + name,
            'before_file': present if kind == 'source_removed' else None,
            'after_file': present if kind == 'source_added' else None}


@pytest.fixture(scope='module')
def lifecycle_pair(tmp_path_factory):
    directory = tmp_path_factory.mktemp('source_lifecycle')
    manifests = []
    for side in ('before', 'after'):
        out = directory / f'{side}.json'
        assert main(['export', '--project', str(FIXTURES / side),
                     '--out', str(out), '--deterministic']) == 0
        manifests.append(json.loads(out.read_text()))
    return manifests


def test_external_yaml_source_lifecycle_with_identical_models(lifecycle_pair, tmp_path, capsys, diff_schema):
    before, after = lifecycle_pair
    assert before['nodes'] == after['nodes']
    assert set(before['sources']) == {PREFIX + n for n in ('input', 'legacy', 'retired')}
    assert set(after['sources']) == {PREFIX + n for n in ('input', 'renamed', 'added')}
    # These declared sources really are unreferenced in the exported graph.
    for manifest in lifecycle_pair:
        assert manifest['nodes'][MODEL]['depends_on']['nodes'] == [PREFIX + 'input']
    paths = [tmp_path / f'{side}.json' for side in ('before', 'after')]
    for path, manifest in zip(paths, lifecycle_pair):
        path.write_text(serialize(manifest))
    input_bytes = [p.read_bytes() for p in paths]
    snapshot = copy.deepcopy(lifecycle_pair)
    capsys.readouterr()
    out = tmp_path / 'nested/diff.json'
    assert main(['diff', *map(str, paths), '--format', 'json', '--out', str(out)]) == 1
    assert capsys.readouterr().out == ''
    report = json.loads(out.read_text())
    diff_schema.validate(report)
    assert report == {
        'diff_version': 1, 'has_changes': True,
        'summary': {'model_added': 0, 'model_removed': 0, 'dependencies_changed': 0,
                    'column_added': 0, 'column_removed': 0, 'column_changed': 0,
                    'source_added': 2, 'source_removed': 2},
        'changes': [lifecycle('source_added', 'added'), lifecycle('source_removed', 'legacy'),
                    lifecycle('source_added', 'renamed'), lifecycle('source_removed', 'retired')],
    }
    assert out.stat().st_mode & 0o777 == 0o600
    assert main(['diff', *map(str, paths)]) == 1
    assert capsys.readouterr().out == render_diff(report)
    assert [p.read_bytes() for p in paths] == input_bytes
    assert lifecycle_pair == snapshot
    # Input dictionary order, optional graph indexes, and direction do not alter identity.
    reordered = copy.deepcopy(after)
    reordered['sources'] = dict(reversed(list(reordered['sources'].items())))
    reordered['parent_map'] = reordered['child_map'] = None
    assert compare_manifests(before, reordered) == report
    reverse = compare_manifests(after, before)
    assert reverse['summary'] == report['summary']
    for forward, backward in zip(report['changes'], reverse['changes']):
        assert backward['node_id'] == forward['node_id']
        assert backward['kind'] != forward['kind']
        assert backward['before_file'] == forward['after_file']
        assert backward['after_file'] == forward['before_file']


def test_real_source_dependency_redirection(tmp_path, capsys):
    project = Path(shutil.copytree(FIXTURES / 'before', tmp_path / 'project'))
    before, after = tmp_path / 'before.json', tmp_path / 'after.json'
    assert main(['export', '--project', str(project), '--out', str(before), '--deterministic']) == 0
    model = project / 'models/stage.sql'
    model.write_text(model.read_text().replace('raw.input', 'raw.redirected'))
    source = project / 'external_models/input.yaml'
    target = project / 'external_models/redirected.yaml'
    target.write_text(source.read_text().replace('raw.input', 'raw.redirected'))
    source.unlink()
    assert main(['export', '--project', str(project), '--out', str(after), '--deterministic']) == 0
    capsys.readouterr()
    assert main(['diff', str(before), str(after), '--format', 'json']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['changes'] == [
        {'kind': 'dependencies_changed', 'node_id': MODEL,
         'before_file': 'models/stage.sql', 'after_file': 'models/stage.sql',
         'removed': [{'node_id': PREFIX + 'input', 'file': 'external_models/input.yaml'}],
         'added': [{'node_id': PREFIX + 'redirected', 'file': 'external_models/redirected.yaml'}]},
        lifecycle('source_removed', 'input'), lifecycle('source_added', 'redirected'),
    ]
    assert report['summary']['dependencies_changed'] == 1
    assert report['summary']['source_added'] == report['summary']['source_removed'] == 1
    assert sum(report['summary'].values()) == 3


def test_source_lifecycle_without_models_and_implicit_provenance(lifecycle_pair):
    before, after = copy.deepcopy(lifecycle_pair)
    for manifest in (before, after):
        manifest['nodes'] = {}
        manifest['parent_map'] = manifest['child_map'] = None
        for source in manifest['sources'].values():
            source['meta']['implicit'] = True
    report = compare_manifests(before, after)
    assert len(report['changes']) == 4
    assert all(c['before_file'] is c['after_file'] is None for c in report['changes'])
    assert report['summary']['source_added'] == report['summary']['source_removed'] == 2


def test_same_source_id_declared_implicit_transition_is_not_lifecycle(lifecycle_pair):
    before = lifecycle_pair[0]
    after = copy.deepcopy(before)
    source = after['sources'][PREFIX + 'input']
    source['meta']['implicit'] = True
    source['original_file_path'] = 'external_models.yaml'
    report = compare_manifests(before, after)
    assert report['changes'] == []
    assert report['has_changes'] is False
    assert report['summary']['source_added'] == report['summary']['source_removed'] == 0


def test_source_lifecycle_is_offline(lifecycle_pair, monkeypatch):
    import socket
    import requests
    from datawrench.sqlmesh_manifest import cli
    def blocked(*args, **kwargs):
        pytest.fail('Offline source lifecycle must not load projects or use network')
    monkeypatch.setattr(cli, 'load_project', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    assert len(compare_manifests(*lifecycle_pair)['changes']) == 4


@pytest.mark.parametrize('invalid_side', [0, 1])
@pytest.mark.parametrize('corruption', ['column_schema', 'dangling_dependency'])
def test_invalid_source_lifecycle_preserves_artifacts(lifecycle_pair, tmp_path, capsys,
                                                     invalid_side, corruption):
    manifests = copy.deepcopy(lifecycle_pair)
    broken = manifests[invalid_side]
    if corruption == 'column_schema':
        # Invalid columns on a wholly removed/added source still must validate.
        name = 'retired' if invalid_side == 0 else 'added'
        broken['sources'][PREFIX + name]['columns']['id']['tags'] = 'not-an-array'
    else:
        del broken['sources'][PREFIX + 'input']
        broken['parent_map'] = broken['child_map'] = None
    paths = [tmp_path / f'{side}.json' for side in ('before', 'after')]
    for path, manifest in zip(paths, manifests):
        path.write_text(serialize(manifest))
    original_bytes = [p.read_bytes() for p in paths]
    out = tmp_path / 'diff.json'
    out.write_text('previous report')
    capsys.readouterr()
    assert main(['diff', *map(str, paths), '--format', 'json', '--out', str(out)]) == 2
    capture = capsys.readouterr()
    assert capture.out == ''
    assert 'validation error:' in capture.err
    assert out.read_text() == 'previous report'
    assert [p.read_bytes() for p in paths] == original_bytes
