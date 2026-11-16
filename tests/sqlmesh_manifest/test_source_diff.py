"""Upstream-only schema changes from paired native external-model fixtures."""
import copy
import json
from pathlib import Path

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.diff import compare_manifests, render_diff
from datawrench.sqlmesh_manifest.exporter import serialize

FIXTURES = Path(__file__).parent / 'fixtures' / 'source_diff'
SOURCE = 'source.source_diff.raw.input'


@pytest.fixture(scope='module')
def source_pair(tmp_path_factory):
    directory = tmp_path_factory.mktemp('source_diff')
    manifests = []
    for side in ('before', 'after'):
        out = directory / f'{side}.json'
        assert main(['export', '--project', str(FIXTURES / side),
                     '--out', str(out), '--deterministic']) == 0
        manifests.append(json.loads(out.read_text()))
    return manifests


def test_external_yaml_changes_with_identical_models(source_pair, tmp_path, capsys, diff_schema):
    before, after = source_pair
    assert before['nodes'] == after['nodes']
    assert before['parent_map'] == after['parent_map']
    assert before['child_map'] == after['child_map']
    paths = [tmp_path / f'{side}.json' for side in ('before', 'after')]
    for path, manifest in zip(paths, source_pair):
        path.write_text(serialize(manifest))
    original_bytes = [path.read_bytes() for path in paths]
    snapshot = copy.deepcopy(source_pair)
    capsys.readouterr()
    assert main(['diff', *map(str, paths), '--format', 'json']) == 1
    report = json.loads(capsys.readouterr().out)
    diff_schema.validate(report)
    assert report['diff_version'] == 1
    assert report['has_changes'] is True
    assert report['summary'] == {
        'model_added': 0, 'model_removed': 0, 'dependencies_changed': 0,
        'column_added': 1, 'column_removed': 1, 'column_changed': 1,
        'source_added': 0, 'source_removed': 0,
    }
    assert [(c['kind'], c['column']) for c in report['changes']] == [
        ('column_changed', 'id'), ('column_added', 'label'), ('column_removed', 'retired'),
    ]
    for change in report['changes']:
        assert change['node_id'] == SOURCE
        assert change['before_file'] == change['after_file'] == 'external_models/input.yaml'
    changed, added, removed = report['changes']
    assert changed['before']['data_type'] == 'INT'
    assert changed['after']['data_type'] == 'BIGINT'
    assert changed['before']['description'] == 'Legacy identifier'
    assert changed['after']['description'] == 'Upstream business identifier'
    assert added['before'] is None and added['after']['data_type'] == 'TEXT'
    assert removed['after'] is None and removed['before']['data_type'] == 'TEXT'
    assert main(['diff', *map(str, paths)]) == 1
    assert capsys.readouterr().out == render_diff(report)
    assert [path.read_bytes() for path in paths] == original_bytes
    assert source_pair == snapshot
    # Reversing inputs swaps additions/removals and preserves respective values.
    reverse = compare_manifests(after, before)['changes']
    assert [c['kind'] for c in reverse] == ['column_changed', 'column_removed', 'column_added']
    for forward, backward in zip(report['changes'], reverse):
        assert forward['before'] == backward['after']
        assert forward['after'] == backward['before']


@pytest.mark.parametrize('field,value', [
    ('meta', {'pii': True}), ('tags', ['pii']), ('quote', True),
    ('constraints', [{'type': 'not_null'}]), ('config', {'tags': ['public']}),
    ('granularity', 'day'), ('_extra', {'custom': 'value'}),
])
def test_source_column_metadata_is_preserved(source_pair, field, value):
    before = source_pair[0]
    after = copy.deepcopy(before)
    after['sources'][SOURCE]['columns']['id'][field] = value
    change, = compare_manifests(before, after)['changes']
    assert (change['kind'], change['node_id'], change['column']) == ('column_changed', SOURCE, 'id')
    assert change['before'][field] != change['after'][field]
    expected = {'meta': {}, '_extra': {}, **value} if field == 'config' else value
    assert change['after'][field] == expected


def test_source_defaults_order_and_out_of_scope_edits(source_pair):
    before, after = copy.deepcopy(source_pair[0]), copy.deepcopy(source_pair[0])
    before['parent_map'] = before['child_map'] = None
    source = after['sources'][SOURCE]
    source.update(description='Source-level description', tags=['new'],
                  original_file_path='external_models/relocated.yaml')
    source['config']['enabled'] = False
    source['columns'] = dict(reversed(list(source['columns'].items())))
    for manifest, tags in ((before, ['a', 'b']), (after, ['b', 'a', 'a'])):
        column = manifest['sources'][SOURCE]['columns']['id']
        column.update(tags=tags, config={'tags': tags})
    column = source['columns']['retired']
    column.update(quote=None, constraints=[], granularity=None, _extra={},
                  config={'meta': {}, 'tags': [], '_extra': {}})
    for key in ('description', 'meta', 'tags'):
        assert not column.pop(key)
    snapshot = copy.deepcopy((before, after))
    assert compare_manifests(before, after)['changes'] == []
    assert (before, after) == snapshot
    source['columns']['id']['description'] = 'Changed meaning'
    change, = compare_manifests(before, after)['changes']
    assert change['before_file'] == 'external_models/input.yaml'
    assert change['after_file'] == 'external_models/relocated.yaml'


def test_source_columns_without_models_and_lifecycle(source_pair):
    before, after = copy.deepcopy(source_pair)
    # Compare sources even if they have no surviving consumers.
    for manifest in (before, after):
        manifest['nodes'] = {}
        manifest['parent_map'] = manifest['child_map'] = None
    assert len(compare_manifests(before, after)['changes']) == 3
    # A whole-source lifecycle emits one event, without a column event flood.
    empty = copy.deepcopy(before)
    empty['sources'] = {}
    assert compare_manifests(empty, after)['changes'] == [
        {'kind': 'source_added', 'node_id': SOURCE, 'before_file': None,
         'after_file': 'external_models/input.yaml'},
    ]
    assert compare_manifests(before, empty)['changes'] == [
        {'kind': 'source_removed', 'node_id': SOURCE,
         'before_file': 'external_models/input.yaml', 'after_file': None},
    ]
    renamed = after['sources'].pop(SOURCE)
    renamed['unique_id'] = SOURCE + '_renamed'
    renamed['name'] = renamed['identifier'] = 'input_renamed'
    renamed['fqn'][-1] = 'input_renamed'
    after['sources'][renamed['unique_id']] = renamed
    # The same YAML path does not merge different source IDs.
    assert compare_manifests(before, after)['changes'] == [
        {'kind': 'source_removed', 'node_id': SOURCE,
         'before_file': 'external_models/input.yaml', 'after_file': None},
        {'kind': 'source_added', 'node_id': SOURCE + '_renamed',
         'before_file': None, 'after_file': 'external_models/input.yaml'},
    ]


def test_source_exact_column_keys_implicit_paths_and_boolean_metadata(source_pair):
    before, after = copy.deepcopy(source_pair[0]), copy.deepcopy(source_pair[0])
    for manifest, flag in ((before, True), (after, 1)):
        source = manifest['sources'][SOURCE]
        source['meta']['implicit'] = True
        source['columns']['id']['meta'] = {'flag': flag}
    change, = compare_manifests(before, after)['changes']
    assert change['before_file'] is change['after_file'] is None
    assert change['before']['meta'] == {'flag': True}
    assert change['after']['meta'] == {'flag': 1}
    after = copy.deepcopy(before)
    column = after['sources'][SOURCE]['columns'].pop('id')
    column['name'] = 'ID'
    after['sources'][SOURCE]['columns']['ID'] = column
    assert [(c['kind'], c['column']) for c in compare_manifests(before, after)['changes']] == [
        ('column_added', 'ID'), ('column_removed', 'id'),
    ]


@pytest.mark.parametrize('invalid_side', [0, 1])
def test_invalid_source_columns_preserve_artifacts(source_pair, tmp_path, capsys, invalid_side):
    manifests = copy.deepcopy(source_pair)
    manifests[invalid_side]['sources'][SOURCE]['columns']['id']['tags'] = 'not-an-array'
    paths = [tmp_path / f'{side}.json' for side in ('before', 'after')]
    for path, manifest in zip(paths, manifests):
        path.write_text(serialize(manifest))
    original_bytes = [path.read_bytes() for path in paths]
    out = tmp_path / 'diff.json'
    out.write_text('previous report')
    capsys.readouterr()
    assert main(['diff', *map(str, paths), '--format', 'json', '--out', str(out)]) == 2
    capture = capsys.readouterr()
    assert capture.out == ''
    assert 'validation error:' in capture.err
    assert out.read_text() == 'previous report'
    assert [path.read_bytes() for path in paths] == original_bytes
