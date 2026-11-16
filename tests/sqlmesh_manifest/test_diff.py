"""Offline semantic comparisons against real SQLMesh exports / pinned dbt v12."""
import copy
import json
from pathlib import Path
import shutil

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.exporter import serialize
from datawrench.sqlmesh_manifest.validation import validate_manifest

EXAMPLE = Path(__file__).resolve().parents[2] / 'examples' / 'sqlmesh_manifest' / 'lineage_project'
FINAL = 'model.lineage_example.final'
STAGE = 'model.lineage_example.stage'
SOURCE = 'source.lineage_example.raw.input'


def export(project, out):
    assert main(['export', '--project', str(project), '--out', str(out)]) == 0
    return json.loads(out.read_text())


def inputs(tmp_path, before, after):
    paths = [tmp_path / 'before.json', tmp_path / 'after.json']
    for path, manifest in zip(paths, (before, after)):
        path.write_text(serialize(manifest))
    return paths


def test_repeat_exports_have_no_semantic_changes(tmp_path, capsys, diff_schema):
    project = Path(shutil.copytree(EXAMPLE, tmp_path / 'project'))
    before, after = tmp_path / 'before.json', tmp_path / 'after.json'
    first, second = export(project, before), export(project, after)
    assert first['metadata']['invocation_id'] != second['metadata']['invocation_id']
    # Reordering JSON objects and set-like column tags is also non-semantic.
    for manifest, tags in ((first, ['a', 'b']), (second, ['b', 'a', 'a'])):
        manifest['nodes'][FINAL]['columns']['id']['tags'] = tags
    second['nodes'] = dict(reversed(list(second['nodes'].items())))
    before.write_text(serialize(first))
    after.write_text(json.dumps(second))
    capsys.readouterr()
    assert main(['diff', str(before), str(after), '--format', 'json']) == 0
    report = json.loads(capsys.readouterr().out)
    diff_schema.validate(report)
    assert report['diff_version'] == 1
    assert report['has_changes'] is False
    assert report['changes'] == []
    assert all(count == 0 for count in report['summary'].values())
    assert main(['diff', str(before), str(after)]) == 0
    assert capsys.readouterr().out == 'No semantic changes.\n'


def test_real_deletion_dependency_redirection_and_columns(tmp_path, capsys, diff_schema):
    project = Path(shutil.copytree(EXAMPLE, tmp_path / 'project'))
    before, after = tmp_path / 'before.json', tmp_path / 'after.json'
    export(project, before)
    (project / 'models/stage.sql').unlink()
    final = project / 'models/final.sql'
    final.write_text(final.read_text().replace('FROM analytics.stage', 'FROM raw.input'))
    (project / 'models/added.sql').write_text('MODEL (name analytics.added, kind VIEW); SELECT 1 AS id;')
    current = export(project, after)
    # v12-local metadata edits also exercise additions/removals without relying
    # on type inference or executing SQL. Each edited artifact must validate.
    columns = current['nodes'][FINAL]['columns']
    columns['id']['description'] = 'Business identifier'
    columns['id']['data_type'] = 'BIGINT'
    del columns['ds']
    columns['label'] = {'name': 'label', 'description': 'Label', 'data_type': 'TEXT'}
    validate_manifest(current)
    after.write_text(serialize(current))
    capsys.readouterr()
    original_bytes = [p.read_bytes() for p in (before, after)]
    out = tmp_path / 'nested/diff.json'
    assert main(['diff', str(before), str(after), '--format', 'json', '--out', str(out)]) == 1
    assert capsys.readouterr().out == ''
    report = json.loads(out.read_text())
    diff_schema.validate(report)
    changes = report['changes']
    assert report['has_changes'] is True
    assert report['summary'] == {
        'model_added': 1, 'model_removed': 1, 'dependencies_changed': 1,
        'column_added': 1, 'column_removed': 1, 'column_changed': 1,
        'source_added': 0, 'source_removed': 0,
    }
    removed = next(c for c in changes if c['kind'] == 'model_removed')
    assert removed == {'kind': 'model_removed', 'node_id': STAGE,
                       'before_file': 'models/stage.sql', 'after_file': None}
    added = next(c for c in changes if c['kind'] == 'model_added')
    assert added['node_id'] == 'model.lineage_example.added'
    assert added['after_file'] == 'models/added.sql'
    redirected = next(c for c in changes if c['kind'] == 'dependencies_changed')
    assert redirected['node_id'] == FINAL
    assert redirected['before_file'] == redirected['after_file'] == 'models/final.sql'
    assert redirected['removed'] == [{'node_id': STAGE, 'file': 'models/stage.sql'}]
    assert redirected['added'] == [{'node_id': SOURCE, 'file': 'external_models/input.yaml'}]
    changed = next(c for c in changes if c['kind'] == 'column_changed')
    assert changed['column'] == 'id'
    assert changed['after']['description'] == 'Business identifier'
    assert changed['after']['data_type'] == 'BIGINT'
    assert next(c for c in changes if c['kind'] == 'column_removed')['column'] == 'ds'
    assert next(c for c in changes if c['kind'] == 'column_added')['column'] == 'label'
    from datawrench.sqlmesh_manifest.diff import render_diff
    assert main(['diff', str(before), str(after)]) == 1
    text = capsys.readouterr().out
    assert text == render_diff(report)
    for value in (STAGE, FINAL, SOURCE, 'models/stage.sql', 'models/final.sql', 'external_models/input.yaml'):
        assert value in text
    assert [p.read_bytes() for p in (before, after)] == original_bytes
    assert out.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('content', ['{', '{}', 'null', '{"a":1,"a":2}', '{"a":NaN}', 'dangling', 'schema'])
@pytest.mark.parametrize('invalid_side', [0, 1])
def test_diff_invalid_input_preserves_files(example, tmp_path, capsys, content, invalid_side):
    paths = inputs(tmp_path, example[1], example[1])
    if content in ('dangling', 'schema'):
        broken = copy.deepcopy(example[1])
        if content == 'dangling':
            broken['nodes']['model.mini_project.stg_orders']['depends_on']['nodes'] = ['model.mini_project.missing']
            broken['parent_map'] = broken['child_map'] = {}
        else:
            del broken['nodes']['model.mini_project.stg_orders']['unique_id']
        content = serialize(broken)
    paths[invalid_side].write_text(content)
    before = [path.read_bytes() for path in paths]
    out = tmp_path / 'diff.json'
    out.write_text('previous report')
    assert main(['diff', *map(str, paths), '--format', 'json', '--out', str(out)]) == 2
    capture = capsys.readouterr()
    assert 'validation error:' in capture.err
    assert capture.out == ''
    assert out.read_text() == 'previous report'
    assert [path.read_bytes() for path in paths] == before


@pytest.mark.parametrize('destination', ['before', 'after', 'dotdot', 'symlink', 'hardlink', 'ancestor_symlink'])
def test_diff_output_cannot_replace_inputs(example, tmp_path, capsys, destination):
    paths = inputs(tmp_path, example[1], example[1])
    out = {'before': paths[0], 'after': paths[1], 'dotdot': tmp_path / 'nested/../before.json'}.get(destination)
    if destination in ('symlink', 'hardlink'):
        out = tmp_path / 'alias.json'
        if destination == 'symlink':
            out.symlink_to(paths[0])
        else:
            out.hardlink_to(paths[0])
    if destination == 'ancestor_symlink':
        directory = tmp_path / 'linked'
        directory.symlink_to(tmp_path, target_is_directory=True)
        out = directory / 'new.json'
    before = [path.read_bytes() for path in paths]
    assert main(['diff', *map(str, paths), '--out', str(out)]) == 2
    capture = capsys.readouterr()
    assert capture.out == ''
    assert 'symlink' in capture.err or 'input' in capture.err
    assert [path.read_bytes() for path in paths] == before


def test_optional_indexes_defaults_and_order_are_nonsemantic(example):
    from datawrench.sqlmesh_manifest.diff import compare_manifests
    before, after = copy.deepcopy(example[1]), copy.deepcopy(example[1])
    uid = 'model.mini_project.fct_orders'
    before['parent_map'] = before['child_map'] = None
    after['nodes'][uid]['depends_on']['nodes'].reverse()
    after['nodes'][uid]['columns'] = dict(reversed(list(after['nodes'][uid]['columns'].items())))
    column = after['nodes'][uid]['columns']['order_id']
    column.update(quote=None, constraints=[], granularity=None, _extra={},
                  config={'meta': {}, 'tags': [], '_extra': {}})
    # Empty fields omitted by another conforming v12 writer mean the same thing.
    for key in ('description', 'meta', 'tags'):
        assert not column.pop(key)
    snapshot = copy.deepcopy((before, after))
    assert compare_manifests(before, after)['changes'] == []
    assert (before, after) == snapshot


@pytest.mark.parametrize('field,value', [
    ('meta', {'pii': True}), ('tags', ['pii']), ('quote', True),
    ('constraints', [{'type': 'not_null'}]), ('config', {'tags': ['public']}),
    ('granularity', 'day'), ('_extra', {'custom': 'value'}),
])
def test_column_metadata_changes_are_reported(example, field, value):
    from datawrench.sqlmesh_manifest.diff import compare_manifests
    before, after = example[1], copy.deepcopy(example[1])
    after['nodes']['model.mini_project.stg_orders']['columns']['order_id'][field] = value
    report = compare_manifests(before, after)
    assert len(report['changes']) == 1
    change = report['changes'][0]
    assert change['kind'] == 'column_changed'
    assert change['node_id'] == 'model.mini_project.stg_orders'
    assert change['column'] == 'order_id'
    assert change['before'][field] != change['after'][field]


def test_metadata_boolean_is_not_number_and_config_tag_order_is_ignored(example):
    from datawrench.sqlmesh_manifest.diff import compare_manifests
    before, after = copy.deepcopy(example[1]), copy.deepcopy(example[1])
    uid = 'model.mini_project.stg_orders'
    for manifest, flag, tags in ((before, True, ['a', 'b']), (after, 1, ['b', 'a'])):
        manifest['nodes'][uid]['columns']['order_id']['meta'] = {'flag': flag}
        manifest['nodes'][uid]['columns']['order_id']['config'] = {'tags': tags}
    change, = compare_manifests(before, after)['changes']
    assert change['before']['meta'] == {'flag': True}
    assert change['after']['meta'] == {'flag': 1}
    assert change['before']['config'] == change['after']['config']


def test_diff_scope_and_provenance_not_identity(example):
    from datawrench.sqlmesh_manifest.diff import compare_manifests
    after = copy.deepcopy(example[1])
    uid = 'model.mini_project.stg_orders'
    after['nodes'][uid]['original_file_path'] = 'models/relocated.sql'
    after['nodes'][uid]['description'] = 'Node descriptions are outside the scoped comparison'
    after['nodes'][uid]['raw_code'] += '\n-- formatting\n'
    after['nodes'][uid]['checksum']['checksum'] = 'different'
    after['sources']['source.mini_project.raw.customers']['description'] = 'Source-only edit'
    assert not compare_manifests(example[1], after)['has_changes']
    after['nodes'][uid]['columns']['order_id']['description'] = 'New meaning'
    change, = compare_manifests(example[1], after)['changes']
    assert change['before_file'] == 'models/stg_orders.sql'
    assert change['after_file'] == 'models/relocated.sql'


def test_diff_is_offline_and_does_not_load_project(example, tmp_path, monkeypatch, capsys):
    import socket
    import requests
    from datawrench.sqlmesh_manifest import cli
    def blocked(*args, **kwargs):
        pytest.fail('Offline diff must not load a project or use the network')
    monkeypatch.setattr(cli, 'load_project', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    paths = inputs(tmp_path, example[1], example[1])
    assert main(['diff', *map(str, paths)]) == 0
    assert capsys.readouterr().out == 'No semantic changes.\n'


def test_diff_missing_and_oversize_inputs(example, tmp_path, monkeypatch, capsys):
    from datawrench.sqlmesh_manifest import validation
    paths = inputs(tmp_path, example[1], example[1])
    paths[1].unlink()
    assert main(['diff', *map(str, paths)]) == 2
    assert 'Cannot read manifest' in capsys.readouterr().err
    paths[1].write_text(serialize(example[1]))
    monkeypatch.setattr(validation, 'MAX_MANIFEST_BYTES', 8)
    assert main(['diff', *map(str, paths)]) == 2
    assert 'input limit' in capsys.readouterr().err


def test_diff_unsupported_version_has_explicit_error(example, tmp_path, capsys):
    before = copy.deepcopy(example[1])
    # A parser-valid v11 artifact must not silently be treated as the pinned v12
    # comparison contract, even though validate supports that older format.
    before['metadata']['dbt_schema_version'] = 'https://schemas.getdbt.com/dbt/manifest/v11.json'
    del before['unit_tests']  # v12 introduced this top-level resource group.
    validate_manifest(before)
    paths = inputs(tmp_path, before, example[1])
    assert main(['diff', *map(str, paths)]) == 2
    assert 'before manifest: Semantic diff supports dbt manifest v12 inputs only' in capsys.readouterr().err


def test_text_escapes_untrusted_provenance(example, tmp_path, capsys):
    before, after = copy.deepcopy(example[1]), copy.deepcopy(example[1])
    uid = 'model.mini_project.stg_orders'
    after['nodes'][uid]['original_file_path'] = 'models/untrusted\n\x1b[31m.sql'
    after['nodes'][uid]['columns']['order_id']['description'] = 'first\nsecond'
    paths = inputs(tmp_path, before, after)
    assert main(['diff', *map(str, paths)]) == 1
    text = capsys.readouterr().out
    assert '\x1b' not in text
    assert 'untrusted\\n\\u001b' in text
    assert 'first\\nsecond' in text
