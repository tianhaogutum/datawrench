"""Opt-in top-level descriptions: exact v12 strings, additive reports, offline CLI."""
import copy
import json

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from datawrench.sqlmesh_manifest.cli import main
from datawrench.sqlmesh_manifest.diff import compare_manifests, render_diff
from datawrench.sqlmesh_manifest.exporter import serialize

MODEL = 'model.mini_project.stg_orders'
SOURCE = 'source.mini_project.raw.customers'


def pair(example):
    return copy.deepcopy(example[1]), copy.deepcopy(example[1])


def event(uid, before, after, path):
    return {'kind': 'description_changed', 'node_id': uid,
            'before_file': path, 'after_file': path, 'before': before, 'after': after}


def write_pair(tmp_path, manifests):
    paths = [tmp_path / 'before.json', tmp_path / 'after.json']
    for path, data in zip(paths, manifests):
        path.write_text(serialize(data))
    return list(map(str, paths))


def test_description_cli_regression(example, tmp_path, capsys, diff_schema):
    before, after = pair(example)
    before['nodes'][MODEL]['description'] = 'Orders.'
    after['nodes'][MODEL]['description'] = 'Orders with returns.'
    paths = write_pair(tmp_path, (before, after))
    capsys.readouterr()
    assert main(['diff', *paths, '--include-descriptions', '--format', 'json']) == 1
    out, err = capsys.readouterr()
    assert err == ''
    report = json.loads(out)
    assert report['changes'] == [event(MODEL, 'Orders.', 'Orders with returns.', 'models/stg_orders.sql')]
    assert report['summary']['description_changed'] == 1
    assert sum(report['summary'].values()) == 1
    diff_schema.validate(report)
    saved = tmp_path / 'report.json'
    saved.write_text(out)
    assert main(['validate-diff', str(saved)]) == 0
    assert capsys.readouterr() == ('Valid semantic diff v1 (structural validation only)\n', '')


@pytest.mark.parametrize('group,uid', [('nodes', MODEL), ('sources', SOURCE)])
@pytest.mark.parametrize('previous,current', [('', 'x'), ('x', ''), ('Orders.', 'Orders with returns.'),
    ('a\nb', 'a\r\nb'), ('café 🐍', 'cafe\u0301 🐍'), (' a ', 'a'), ('"\\\t', '次\n行')])
def test_exact_strings_and_escaped_text(example, diff_schema, group, uid, previous, current):
    before, after = pair(example)
    before[group][uid]['description'] = previous
    after[group][uid]['description'] = current
    if group == 'sources':
        for data in (before, after):
            data[group][uid]['meta']['implicit'] = True
    snapshot = copy.deepcopy((before, after))
    report = compare_manifests(before, after, include_descriptions=True)
    path = before[group][uid]['original_file_path'] if group == 'nodes' else None
    assert report['changes'] == [event(uid, previous, current, path)]
    diff_schema.validate(report)
    text = render_diff(report)
    assert text.splitlines()[-2:] == ['  before: ' + json.dumps(previous, ensure_ascii=True),
                                    '  after: ' + json.dumps(current, ensure_ascii=True)]
    assert len(text.splitlines()) == 4
    assert (before, after) == snapshot
    reverse = compare_manifests(after, before, include_descriptions=True)
    assert reverse['changes'] == [event(uid, current, previous, path)]


@pytest.mark.parametrize('group,uid', [('nodes', MODEL), ('sources', SOURCE)])
def test_omission_is_empty_and_default_bytes_unchanged(example, group, uid, diff_schema):
    before, after = pair(example)
    before[group][uid].pop('description', None)
    after[group][uid]['description'] = ''
    baseline = compare_manifests(before, before)
    assert serialize(compare_manifests(before, after)) == serialize(baseline)
    selected = compare_manifests(before, after, include_descriptions=True)
    assert selected == {**baseline, 'summary': {**baseline['summary'], 'description_changed': 0}}
    diff_schema.validate(selected)
    assert render_diff(selected) == 'No semantic changes.\n'
    after[group][uid]['description'] = 'Changed'
    assert serialize(compare_manifests(before, after)) == serialize(baseline)
    assert 'description_changed' not in baseline['summary']
    assert compare_manifests(before, after, include_descriptions=True)['changes'][0]['before'] == ''


def test_order_and_no_duplicate_column_description(example, diff_schema):
    before, after = pair(example)
    after['nodes'][MODEL]['depends_on']['nodes'] = []
    after['parent_map'] = after['child_map'] = None
    for group, uid in (('nodes', MODEL), ('sources', SOURCE)):
        after[group][uid]['description'] = 'new'
        columns = after[group][uid]['columns']
        columns[next(iter(columns))]['description'] = 'column'
    # Dictionary insertion order does not influence event order.
    after['nodes'] = dict(reversed(list(after['nodes'].items())))
    report = compare_manifests(before, after, include_descriptions=True)
    assert [(c['node_id'], c['kind']) for c in report['changes']] == [
        (MODEL, 'dependencies_changed'), (MODEL, 'description_changed'), (MODEL, 'column_changed'),
        (SOURCE, 'description_changed'), (SOURCE, 'column_changed')]
    diff_schema.validate(report)
    after['nodes'][MODEL]['description'] = before['nodes'][MODEL]['description']
    after['sources'][SOURCE]['description'] = before['sources'][SOURCE]['description']
    assert compare_manifests(before, after, include_descriptions=True)['summary']['description_changed'] == 0


def test_lifecycle_and_other_metadata_excluded(example):
    before, after = pair(example)
    # No models/consumers needed for source lifecycle or descriptions.
    for data in (before, after):
        data['nodes'] = {MODEL: data['nodes'][MODEL]}
        data['nodes'][MODEL]['depends_on']['nodes'] = []
        data['parent_map'] = data['child_map'] = None
    removed = after['nodes'].pop(MODEL)
    added_id = 'model.mini_project.added'
    after['nodes'][added_id] = {**removed, 'unique_id': added_id, 'description': 'Added'}
    removed_source = after['sources'].pop(SOURCE)
    added_source = 'source.mini_project.raw.added'
    after['sources'][added_source] = {**removed_source, 'unique_id': added_source, 'description': 'Added'}
    report = compare_manifests(before, after, include_descriptions=True)
    assert [c['kind'] for c in report['changes']] == ['model_added', 'model_removed', 'source_added', 'source_removed']
    assert report['summary']['description_changed'] == 0
    before, after = pair(example)
    after['nodes'][MODEL].update(raw_code='SELECT 999', tags=['x'], meta={'description': 'ignored'})
    after['nodes'][MODEL]['config']['meta'] = {'description': 'ignored'}
    after['sources'][SOURCE]['source_description'] = 'Source group description excluded'
    after['sources'][SOURCE]['tags'] = ['x']
    after['disabled'] = {MODEL: [copy.deepcopy(after['nodes'][MODEL])]}
    after['disabled'][MODEL][0]['description'] = 'Disabled map excluded'
    assert not compare_manifests(before, after, include_descriptions=True)['has_changes']


@pytest.mark.parametrize('group,uid', [('nodes', MODEL), ('sources', SOURCE)])
@pytest.mark.parametrize('side', [0, 1])
def test_disabled_on_either_side_excluded(example, group, uid, side):
    manifests = pair(example)
    manifests[side][group][uid]['config']['enabled'] = False
    manifests[1][group][uid]['description'] = 'Different'
    assert compare_manifests(*manifests, include_descriptions=True)['summary']['description_changed'] == 0


@pytest.mark.parametrize('group,uid', [('nodes', MODEL), ('sources', SOURCE)])
@pytest.mark.parametrize('value', [None, 1, {}, []])
@pytest.mark.parametrize('side', [0, 1])
def test_invalid_description_rejected_before_output(example, tmp_path, capsys, group, uid, value, side):
    manifests = pair(example)
    manifests[side][group][uid]['description'] = value
    paths = write_pair(tmp_path, manifests)
    contents = [open(p, 'rb').read() for p in paths]
    saved = tmp_path / 'report.json'
    saved.write_text('keep')
    capsys.readouterr()
    assert main(['diff', *paths, '--include-descriptions', '--out', str(saved)]) == 2
    out, err = capsys.readouterr()
    assert out == '' and 'validation error:' in err
    assert saved.read_text() == 'keep'
    assert [open(p, 'rb').read() for p in paths] == contents


def test_offline_output_and_input_safety(example, tmp_path, capsys, monkeypatch):
    import socket
    import requests
    from datawrench.sqlmesh_manifest import cli

    before, after = pair(example)
    after['nodes'][MODEL]['description'] = 'changed'
    paths = write_pair(tmp_path, (before, after))
    original = [open(p, 'rb').read() for p in paths]
    def blocked(*args, **kwargs):
        pytest.fail('Description diff must remain offline and not load projects')
    monkeypatch.setattr(cli, 'load_project', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    out = tmp_path / 'nested/report.json'
    capsys.readouterr()
    assert main(['diff', *paths, '--include-descriptions', '--format', 'json', '--out', str(out)]) == 1
    assert capsys.readouterr() == ('', '')
    assert out.stat().st_mode & 0o777 == 0o600
    assert main(['diff', *paths, '--include-descriptions', '--out', paths[0]]) == 2
    assert capsys.readouterr().out == ''
    assert [open(p, 'rb').read() for p in paths] == original


def description_report():
    return {'diff_version': 1, 'has_changes': True,
            'summary': {k: 0 for k in ('model_added', 'model_removed', 'dependencies_changed',
                'column_added', 'column_removed', 'column_changed', 'source_added', 'source_removed')},
            'changes': [event(MODEL, 'Orders.', 'Orders with returns.', None)]}


@pytest.mark.parametrize('field,value', [('before', None), ('after', None), ('before', {}), ('after', 1),
    ('before', ['x']), ('after', False), ('column', 'id'), ('added', []), ('removed', [])])
def test_malformed_known_description_rejected(diff_schema, tmp_path, capsys, field, value):
    report = description_report()
    report['changes'][0][field] = value
    assert not diff_schema.is_valid(report)
    path = tmp_path / 'report.json'
    path.write_text(serialize(report))
    assert main(['validate-diff', str(path)]) == 1
    assert capsys.readouterr() == ('', 'validation error: Invalid or unsupported semantic diff report\n')


@pytest.mark.parametrize('field', ['before', 'after', 'before_file', 'after_file'])
def test_known_description_fields_required(diff_schema, field):
    report = description_report()
    del report['changes'][0][field]
    assert not diff_schema.is_valid(report)


def test_schema_description_extension_stays_additive(diff_schema):
    report = description_report()
    # Counter optional for schema compatibility, including old eight-counter reports.
    diff_schema.validate(report)
    report['summary']['description_changed'] = 1
    report['changes'][0]['future_field'] = [True, None]
    diff_schema.validate(report)
    report['changes'][0]['kind'] = 'future_kind'
    report['changes'][0]['before'] = {'unrestricted': True}
    diff_schema.validate(report)
    for invalid in (None, -1, '1', True, 1.5):
        report['summary']['description_changed'] = invalid
        assert not diff_schema.is_valid(report)
