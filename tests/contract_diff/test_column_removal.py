"""Whole-column removal explanations retain literal paths and raw metadata."""
from copy import deepcopy
import json

import pytest

from .conftest import artifact, node
from datawrench.contract_diff.cli import render
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import InputError, Manifest
from datawrench.contract_diff.policy import ProjectPolicy
from .test_explanations import compare_fixture

REMOVED_TYPE = 'STRUCT("a.b" INTEGER, amounts ARRAY(DECIMAL(10,2)))'


def test_column_removed_fixture_exact_location_type_and_gate():
    result = compare_fixture('column-removed')
    assert result.returncode == 1 and not result.stderr
    data = json.loads(result.stdout)
    assert data['schema_version'] == 1 and not data['warnings']
    assert data['summary']['changes'] == data['summary']['breaking'] == 1
    change, = data['changes']
    assert (change['model'], change['column']) == ('model.shop.orders', 'payload.items')
    assert change['kind'] == 'column_removed' and change['breaking']
    assert change['before'] == REMOVED_TYPE and change['after'] is None
    assert change['version'] == '1' and 'without version bump' in change['message']
    detail, = change['details']
    assert detail['path'] == 'column' and detail['path_segments'] == []
    assert detail['before'] == detail['before_type'] == REMOVED_TYPE
    assert detail['after'] is detail['after_type'] is None
    assert detail['classification'] == detail['rule'] == 'column_removed'
    assert detail['breaking'] and detail['certainty'] == 'known'
    assert 'consumer' in detail['reason'] and 'column' in detail['reason']
    assert 'Restore' in detail['remediation'] and 'version' in detail['remediation']
    assert 'policy' not in detail
    # A disabled gate changes only the exit status, not the removal evidence.
    no_gate = compare_fixture('column-removed', 'json', '--fail-on', 'none')
    assert no_gate.returncode == 0 and no_gate.stdout == result.stdout
    assert compare_fixture('column-removed', 'json', '--fail-on', 'any').returncode == 1


@pytest.mark.parametrize('fmt', ['text', 'markdown', 'github'])
def test_column_removed_human_formats_explain_lost_column(fmt):
    result = compare_fixture('column-removed', fmt)
    assert result.returncode == 1 and not result.stderr
    assert 'payload.items' in result.stdout and 'column_removed' in result.stdout
    assert 'Action:' in result.stdout and 'version' in result.stdout
    assert 'DECIMAL(10,2)' in result.stdout and 'INTEGER' in result.stdout


@pytest.mark.parametrize('raw', ['  integer  ', 'STRUCT(items ARRAY(custom_type))',
                                 'STRUCT(broken', '', None])
def test_removal_is_known_without_parsing_old_type(manifest, raw):
    before, after = node(raw), node()
    after['columns'] = {}
    # Lost column guarantees are included in the column loss, not double counted.
    before['columns']['id']['constraints'] = [{'type': 'not_null'}]
    report = compare_manifests(manifest(before), manifest(after))
    assert not report.warnings and report.breaking_count == 1
    change, = report.changes
    detail, = change.details
    assert detail['rule'] == 'column_removed' and detail['certainty'] == 'known'
    assert detail['before_type'] == change.before == raw
    assert detail['after_type'] is None and detail['breaking']
    # Absent data_type has the same null representation as explicit null.
    if raw is None:
        del before['columns']['id']['data_type']
        assert compare_manifests(manifest(before), manifest(after)).to_dict() == report.to_dict()


@pytest.mark.parametrize('name', ['payload.items', '"payload.items"', 'Payload.Items', 'element'])
def test_column_location_is_literal_and_not_a_nested_selector(manifest, name):
    before, after = node(), node()
    before['columns'][name] = {'name': name, 'data_type': 'INTEGER'}
    report = compare_manifests(manifest(before), manifest(after))
    change, = report.changes
    assert change.column == name
    detail, = change.details
    assert detail['path_segments'] == [] and detail['path'] == 'column'


def test_addition_rename_and_model_loss_keep_existing_classification(manifest):
    before, after = node(), node()
    after['columns'] = {'new_id': {'data_type': 'INTEGER'}}
    report = compare_manifests(manifest(before), manifest(after))
    removed, added = report.changes
    assert removed.kind == 'column_removed' and removed.details[0]['rule'] == 'column_removed'
    assert added.kind == 'column_added' and added.details[0]['rule'] == 'column_added' and not added.breaking
    assert report.breaking_count == 1 and 'probable rename' in report.notices[0]
    lost_model = compare_manifests(manifest(before), manifest(nodes={}))
    change, = lost_model.changes
    assert change.kind == 'model_removed'
    detail, = change.details
    assert detail['rule'] == 'model_removed' and detail['path'] == 'model'


def test_type_policy_cannot_downgrade_whole_column_removal(manifest):
    before, after = node(), node()
    after['columns'] = {}
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': [{
        'model': before['unique_id'], 'column': 'id', 'path_segments': [],
        'rule': 'integer_range', 'severity': 'non_breaking',
    }]})
    report = compare_manifests(manifest(before), manifest(after), policy=policy)
    change, = report.changes
    detail, = change.details
    assert report.exit_code('breaking') == 1 and detail['breaking'] and 'policy' not in detail
    policy_data = {'version': 1, 'rules': [{
        'model': before['unique_id'], 'column': 'id', 'path_segments': [],
        'rule': 'column_removed', 'severity': 'non_breaking',
    }]}
    with pytest.raises(InputError, match='unknown rule'):
        ProjectPolicy.from_dict(policy_data)


def test_column_removed_rule_cannot_be_configured_through_cli(tmp_path):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'version': 1, 'rules': [{
        'model': 'model.shop.orders', 'column': 'payload.items', 'path_segments': [],
        'rule': 'column_removed', 'severity': 'non_breaking',
    }]}))
    result = compare_fixture('column-removed', 'json', '--policy', path, '--fail-on', 'none')
    assert result.returncode == 2 and not result.stdout and 'unknown rule' in result.stderr


def test_removal_explanation_escapes_raw_column_and_type_content():
    name = 'payload.\n::error::injected|<script>`\x1b[31m'
    dtype = 'custom\n::error::injected|<script>`\x1b[31m'
    before = node()
    before['columns'] = {name: {'data_type': dtype}}
    after = deepcopy(before)
    after['columns'] = {}
    report = compare_manifests(Manifest.from_dict(artifact(before)), Manifest.from_dict(artifact(after)))
    data = json.loads(render(report, 'json'))
    assert data['changes'][0]['column'] == name
    assert data['changes'][0]['details'][0]['before_type'] == dtype
    github = render(report, 'github')
    assert len(github.splitlines()) == 2 and '%0A::error::injected' in github
    markdown = render(report, 'markdown')
    assert '<script>' not in markdown and '&#124;' in markdown and '&#96;' in markdown
    assert '\x1b' not in github + markdown + render(report, 'text')
