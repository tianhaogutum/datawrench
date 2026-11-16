"""Whole-column additions describe raw metadata without claiming validation."""
from copy import deepcopy
import json

import pytest

from .conftest import artifact, node
from datawrench.contract_diff.cli import render
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import InputError, Manifest
from datawrench.contract_diff.policy import ProjectPolicy
from .test_explanations import compare_fixture

ADDED_TYPE = 'STRUCT(amount DECIMAL(10,2))'


def test_column_added_fixture_exact_location_type_and_gates():
    result = compare_fixture('column-added')
    assert result.returncode == 0 and not result.stderr
    data = json.loads(result.stdout)
    assert data['schema_version'] == 1 and not data['warnings']
    assert data['summary'] == {'models_compared': 1, 'changes': 1, 'breaking': 0,
                               'non_breaking': 1, 'equivalents': 0, 'warnings': 0}
    change, = data['changes']
    assert (change['model'], change['column']) == ('model.shop.orders', 'payload.items')
    assert change['kind'] == 'column_added' and not change['breaking']
    assert change['before'] is None and change['after'] == ADDED_TYPE
    assert change['version'] == '1' and 'version bump' not in change['message']
    detail, = change['details']
    assert detail['path'] == 'column' and detail['path_segments'] == []
    assert detail['before'] is detail['before_type'] is None
    assert detail['after'] == detail['after_type'] == ADDED_TYPE
    assert detail['classification'] == detail['rule'] == 'column_added'
    assert not detail['breaking'] and detail['certainty'] == 'known'
    assert 'non-breaking' in detail['reason'] and 'not validated' in detail['reason']
    assert 'SQL' in detail['reason'] and 'data' in detail['reason']
    assert 'Validate' in detail['remediation'] and 'warehouse' in detail['remediation']
    assert 'policy' not in detail
    for gate, expected in [('breaking', 0), ('any', 1), ('none', 0)]:
        gated = compare_fixture('column-added', 'json', '--fail-on', gate)
        assert gated.returncode == expected and gated.stdout == result.stdout and not gated.stderr


@pytest.mark.parametrize('fmt', ['text', 'markdown', 'github'])
def test_column_added_human_formats_explain_addition(fmt):
    result = compare_fixture('column-added', fmt)
    assert result.returncode == 0 and not result.stderr
    assert 'payload.items' in result.stdout and 'column_added' in result.stdout
    assert 'Action:' in result.stdout and 'not validated' in result.stdout
    assert ADDED_TYPE in result.stdout


@pytest.mark.parametrize('raw,warn', [('  integer  ', False), (ADDED_TYPE, False),
                                    ('STRUCT(items ARRAY(custom_type))', True),
                                    ('STRUCT(broken', True), ('', True), (None, True)])
def test_raw_added_types_and_existing_parse_warnings_are_preserved(manifest, raw, warn):
    before, after = node(), node()
    after['columns']['new'] = {'data_type': raw, 'constraints': [{'type': 'not_null'}]}
    report = compare_manifests(manifest(before), manifest(after))
    assert len(report.warnings) == int(warn)
    if warn:
        assert report.warnings[0].startswith('model.shop.orders column new: added data_type unparseable:')
        assert report.warnings[0].endswith('; no previous type to compare')
    change, = report.changes
    detail, = change.details
    assert change.after == detail['after'] == detail['after_type'] == raw
    assert detail['before'] is detail['before_type'] is None
    assert detail['certainty'] == 'known' and detail['rule'] == 'column_added'
    assert not change.breaking and not detail['breaking']
    assert report.exit_code('breaking') == 0 and report.exit_code('any') == 1
    # Missing data_type retains the same null and warning as explicit null.
    if raw is None:
        del after['columns']['new']['data_type']
        assert compare_manifests(manifest(before), manifest(after)).to_dict() == report.to_dict()


@pytest.mark.parametrize('name', ['payload.items', '"payload.items"', 'Payload.Items', 'element'])
def test_addition_location_uses_literal_model_and_column_keys(manifest, name):
    before = node()
    before['unique_id'] = 'model.Shop."Orders.v1"'
    before['columns']['payload'] = {'data_type': 'STRUCT(items INT)'}
    after = deepcopy(before)
    after['columns'][name] = {'name': name, 'data_type': '  integer  '}
    report = compare_manifests(manifest(before), manifest(after))
    change, = report.changes
    assert (change.model, change.column) == (before['unique_id'], name)
    detail, = change.details
    assert detail['path_segments'] == [] and detail['path'] == 'column'
    assert detail['after_type'] == '  integer  '


def test_unchanged_columns_new_models_and_probable_renames_keep_counts(manifest):
    before = node()
    assert not compare_manifests(manifest(before), manifest(before)).changes
    after = deepcopy(before)
    after['columns'] = {'new_id': {'data_type': 'INTEGER'}}
    report = compare_manifests(manifest(before), manifest(after))
    removed, added = report.changes
    assert removed.kind == 'column_removed' and removed.breaking
    assert added.kind == 'column_added' and not added.breaking
    assert added.details[0]['rule'] == 'column_added' and report.breaking_count == 1
    assert len(report.notices) == 1 and 'probable rename' in report.notices[0]
    assert report.exit_code('breaking') == 1
    new_model = compare_manifests(manifest(nodes={}), manifest(after))
    change, = new_model.changes
    from .test_model_addition import assert_added
    assert_added(new_model.to_dict()['changes'][0])


def test_known_type_policy_cannot_override_whole_column_addition(manifest):
    before, after = node(), node()
    after['columns']['new'] = {'data_type': 'INT'}
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': [{
        'model': before['unique_id'], 'column': 'new', 'path_segments': [],
        'rule': 'integer_range', 'severity': 'breaking',
    }]})
    report = compare_manifests(manifest(before), manifest(after), policy=policy)
    change, = report.changes
    detail, = change.details
    assert not change.breaking and 'policy' not in detail
    assert report.policy_diagnostics[0]['pointer'] == '/rules/0'
    for severity in ['breaking', 'non_breaking']:
        with pytest.raises(InputError, match='unknown rule'):
            ProjectPolicy.from_dict({'version': 1, 'rules': [{
                'model': before['unique_id'], 'column': 'new', 'path_segments': [],
                'rule': 'column_added', 'severity': severity,
            }]})


def test_addition_explanation_escapes_raw_column_and_type_content():
    name = 'payload.\n::error::injected|<script>`\x1b[31m'
    dtype = 'custom\n::error::injected|<script>`\x1b[31m'
    before = node()
    after = deepcopy(before)
    after['columns'][name] = {'data_type': dtype}
    report = compare_manifests(Manifest.from_dict(artifact(before)), Manifest.from_dict(artifact(after)))
    data = json.loads(render(report, 'json'))
    assert data['changes'][0]['column'] == name
    assert data['changes'][0]['details'][0]['after_type'] == dtype
    github = render(report, 'github')
    assert len(github.splitlines()) == 3 and '%0A::error::injected' in github
    markdown = render(report, 'markdown')
    assert '<script>' not in markdown and '&#124;' in markdown and '&#96;' in markdown
    assert '\x1b' not in github + markdown + render(report, 'text')
