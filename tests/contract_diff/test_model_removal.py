"""Model removal identifies declared contract loss, not warehouse deletion."""
from copy import deepcopy
import json

import pytest

from .conftest import node
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import InputError
from datawrench.contract_diff.policy import ProjectPolicy
from .test_explanations import compare_fixture


def assert_removal(change, key='model.shop.orders'):
    assert change['model'] == key and change['column'] is None
    assert change['kind'] == 'model_removed' and change['breaking']
    assert change['before'] is change['after'] is None
    detail, = change['details']
    assert detail['path'] == 'model' and detail['path_segments'] == []
    assert detail['classification'] == detail['rule'] == 'model_removed'
    assert detail['before'] == key and detail['after'] is None
    assert detail['breaking'] and detail['certainty'] == 'known'
    assert 'declared model contract' in detail['reason'] and 'manifest' in detail['reason']
    assert 'SQL' in detail['reason'] and 'data' in detail['reason'] and 'not validated' in detail['reason']
    assert 'Restore' in detail['remediation'] and 'original' in detail['remediation']
    assert 'version' in detail['remediation'] and 'migrating consumers' in detail['remediation']
    assert not {'before_type', 'after_type', 'policy'} & detail.keys()


def test_model_removed_fixture_contract_and_gates():
    result = compare_fixture('model-removed')
    assert result.returncode == 1 and not result.stderr
    report = json.loads(result.stdout)
    assert report['schema_version'] == 1 and report['dialect'] == 'duckdb'
    assert report['summary'] == {'models_compared': 1, 'changes': 1, 'breaking': 1,
        'non_breaking': 0, 'equivalents': 0, 'warnings': 0}
    assert not report['warnings'] and not report['notices'] and not report['policy_diagnostics']
    change, = report['changes']
    assert_removal(change)
    assert change['message'] == 'model removed' and change['version'] is None
    for gate, code in [('breaking', 1), ('any', 1), ('none', 0)]:
        gated = compare_fixture('model-removed', 'json', '--fail-on', gate)
        assert gated.returncode == code and gated.stdout == result.stdout and not gated.stderr


@pytest.mark.parametrize('fmt', ['text', 'markdown', 'github'])
def test_model_removed_human_formats(fmt):
    result = compare_fixture('model-removed', fmt)
    assert result.returncode == 1 and not result.stderr
    assert 'model.shop.orders' in result.stdout and 'model_removed' in result.stdout
    assert 'model:' in result.stdout and 'Action:' in result.stdout and 'not validated' in result.stdout


@pytest.mark.parametrize('enforcement', ['node', 'config', 'both', 'unenforced', 'absent'])
@pytest.mark.parametrize('included', [False, True])
def test_active_selection_and_exact_versioned_identity(manifest, enforcement, included):
    before = node(version=1, enforced=enforcement != 'unenforced')
    before['unique_id'] = 'model.Shop."Orders.v1"'
    if enforcement in ('config', 'both'):
        before['config']['contract'] = {'enforced': True}
    if enforcement in ('config', 'absent'):
        del before['contract']
    report = compare_manifests(manifest(before), manifest(nodes={}), include_unenforced=included)
    active = included or enforcement in ('node', 'config', 'both')
    assert report.models_compared == int(active)
    if active:
        change, = report.to_dict()['changes']
        assert_removal(change, before['unique_id'])
        assert change['version'] == '1'
        assert change['message'] == 'model removed; breaking change without version bump (version 1)'
    else:
        assert not report.changes


@pytest.mark.parametrize('mode', ['unchanged', 'added', 'disabled_entry', 'config_disabled'])
def test_negative_lifecycle_cases_have_no_model_removed_detail(manifest, mode):
    before, after = node(), node()
    extra = {}
    if mode == 'config_disabled':
        after['config']['enabled'] = False
    if mode == 'disabled_entry':
        extra['disabled'] = {before['unique_id']: [deepcopy(before)]}
    a = manifest(nodes={}) if mode == 'added' else manifest(before)
    b = manifest(nodes={}, **extra) if mode == 'disabled_entry' else manifest(after)
    report = compare_manifests(a, b)
    if mode == 'unchanged':
        assert not report.changes
    else:
        change, = report.changes
        assert change.kind == ('model_added' if mode == 'added' else 'model_disabled')
        if mode == 'added':
            from .test_model_addition import assert_added
            assert_added(report.to_dict()['changes'][0])
        else:
            from .test_model_disabled import assert_disabled
            assert_disabled(report.to_dict()['changes'][0])


@pytest.mark.parametrize('raw', ['INT', ' custom_type ', 'STRUCT(', None, ''])
def test_removal_does_not_expand_columns_constraints_or_parse_types(manifest, raw):
    before = node(raw)
    before['columns']['id']['constraints'] = [{'type': 'not_null'}]
    before['columns']['payload.items'] = {'data_type': 'STRUCT(amount INT)', 'constraints': []}
    before['constraints'] = [{'type': 'unique', 'columns': ['id']}]
    report = compare_manifests(manifest(before), manifest(nodes={}))
    change, = report.to_dict()['changes']
    assert_removal(change)
    assert not report.warnings and not report.equivalents and not report.notices


def test_replacement_version_does_not_excuse_original_removal(manifest):
    before, after = node(version=1), node(version=2)
    before['unique_id'], after['unique_id'] = 'model.shop.orders.v1', 'model.shop.orders.v2'
    removed, added = compare_manifests(manifest(before), manifest(after)).to_dict()['changes']
    assert_removal(removed, before['unique_id'])
    assert 'breaking change without version bump (version 1)' in removed['message']
    from .test_model_addition import assert_added
    assert_added(added, after['unique_id'], 2)


@pytest.mark.parametrize('rule', ['model_removed', 'model_disabled', 'model_added', 'model_identity_changed'])
@pytest.mark.parametrize('severity', ['breaking', 'non_breaking'])
def test_lifecycle_policies_still_rejected(rule, severity):
    with pytest.raises(InputError, match='unknown rule'):
        ProjectPolicy.from_dict({'version': 1, 'rules': [{'model': 'model.shop.orders', 'column': None,
            'path_segments': [], 'rule': rule, 'severity': severity}]})


def test_removal_cannot_consume_type_or_constraint_policy(manifest):
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': [
        {'model': 'model.shop.orders', 'column': 'id', 'path_segments': [],
         'rule': 'integer_range', 'severity': 'non_breaking'},
        {'model': 'model.shop.orders', 'column': None,
         'path_segments': [{'kind': 'constraint', 'signature': '{"type": "not_null"}'}],
         'rule': 'constraint_guarantee_removed', 'severity': 'non_breaking'},
    ]})
    report = compare_manifests(manifest(), manifest(nodes={}), policy=policy)
    change, = report.to_dict()['changes']
    assert_removal(change)
    assert len(report.policy_diagnostics) == 2
    assert [d['pointer'] for d in report.policy_diagnostics] == ['/rules/0', '/rules/1']


def test_other_models_keep_existing_explanations_and_latest_version_notice(manifest):
    removed = node(version=1, latest=1)
    removed['unique_id'] = 'model.shop.orders.v1'
    old, new = node('DECIMAL(10,2)'), node('DECIMAL(8,2)', enforced=False)
    old['unique_id'] = new['unique_id'] = 'model.shop.other'
    old['name'] = new['name'] = 'other'
    base = manifest(nodes={removed['unique_id']: removed, old['unique_id']: old})
    head = manifest(nodes={new['unique_id']: new})
    report = compare_manifests(base, head)
    standalone = compare_manifests(manifest(old), manifest(new))
    assert [c for c in report.changes if c.model == old['unique_id']] == standalone.changes
    removal, = [c for c in report.to_dict()['changes'] if c['kind'] == 'model_removed']
    assert_removal(removal, removed['unique_id'])
    latest, = [c for c in report.changes if c.kind == 'latest_version_changed']
    assert latest.before == 1 and latest.after is None and not latest.breaking
    assert latest.message == 'latest_version changed: 1 -> None'
    detail, = latest.details
    assert detail['rule'] == 'latest_version_changed' and detail['path_segments'] == []
    assert detail['before'] == {'package_name': 'shop', 'name': 'orders', 'latest_version': 1}
    assert detail['after'] == {'package_name': 'shop', 'name': 'orders', 'latest_version': None}
