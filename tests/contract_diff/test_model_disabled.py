"""Disabled model declarations explain contract loss within the requested scope."""
from copy import deepcopy

import pytest

from .conftest import node
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.policy import ProjectPolicy

KEY = 'model.shop.orders'
OTHER = 'model.shop.customers'


def assert_disabled(change, key=KEY):
    assert change['model'] == key and change['column'] is None
    assert change['kind'] == 'model_disabled' and change['breaking']
    assert change['before'] is change['after'] is None
    detail, = change['details']
    assert detail['path'] == 'model' and detail['path_segments'] == []
    assert detail['classification'] == detail['rule'] == 'model_disabled'
    assert detail['before'] == key and detail['after'] is None
    assert detail['breaking'] and detail['certainty'] == 'known'
    assert 'declared model contract' in detail['reason'] and 'active manifest' in detail['reason']
    assert 'declarations only' in detail['reason'] and 'not validated' in detail['reason']
    assert 'SQL' in detail['reason'] and 'warehouse' in detail['reason']
    assert 'Re-enable' in detail['remediation'] and 'original' in detail['remediation']
    assert 'version' in detail['remediation'] and 'migrating consumers' in detail['remediation']
    assert not {'before_type', 'after_type', 'policy'} & detail.keys()


def disabled_head(manifest, before, form):
    after = deepcopy(before)
    if form == 'disabled_list':
        return manifest(nodes={}, disabled={before['unique_id']: [after]})
    after['config']['enabled'] = False
    return manifest(after)


@pytest.mark.parametrize('form', ['disabled_list', 'config_enabled'])
def test_scoped_model_disabled_contract(manifest, form):
    report = compare_manifests(manifest(), disabled_head(manifest, node(), form), selected_models=[KEY])
    data = report.to_dict()
    assert data['selected_models'] == [KEY] and data['schema_version'] == 1
    assert data['summary'] == {'models_compared': 1, 'changes': 1, 'breaking': 1,
        'non_breaking': 0, 'equivalents': 0, 'warnings': 0}
    change, = data['changes']
    assert_disabled(change)
    assert change['message'] == 'model disabled' and change['version'] is None
    assert not report.warnings and not report.notices and not report.policy_diagnostics
    assert [report.exit_code(g) for g in ('breaking', 'any', 'none')] == [1, 1, 0]


@pytest.mark.parametrize('form', ['disabled_list', 'config_enabled'])
@pytest.mark.parametrize('enforcement', ['node', 'config', 'both', 'false', 'absent'])
@pytest.mark.parametrize('included', [False, True])
def test_enforcement_scope_and_exact_versioned_identity(manifest, form, enforcement, included):
    before = node('STRUCT(', version=1, enforced=enforcement != 'false')
    before['unique_id'] = 'model.Shop."Orders.v1"'
    before['columns']['payload.items'] = {'data_type': ' custom_type ', 'constraints': [{'type': 'not_null'}]}
    before['constraints'] = [{'type': 'unique', 'columns': ['id']}]
    if enforcement in ('config', 'both'):
        before['config']['contract'] = {'enforced': True}
    if enforcement in ('config', 'absent'):
        del before['contract']
    report = compare_manifests(manifest(before), disabled_head(manifest, before, form),
                               selected_models=[before['unique_id']], include_unenforced=included)
    active = included or enforcement in ('node', 'config', 'both')
    assert report.models_compared == int(active)
    assert not report.warnings and not report.equivalents
    if active:
        change, = report.to_dict()['changes']
        assert_disabled(change, before['unique_id'])
        assert change['version'] == '1'
        assert change['message'] == 'model disabled; breaking change without version bump (version 1)'
    else:
        assert not report.changes and 'selected scope' in report.notices[0]


@pytest.mark.parametrize('form', ['disabled_list', 'config_enabled'])
def test_selected_unselected_and_default_scope(manifest, form):
    other = node()
    other.update(unique_id=OTHER, name='customers')
    base = manifest(nodes={KEY: node(), OTHER: other})
    head = disabled_head(manifest, node(), form)
    # An unselected disabled key contributes no finding or detail.
    head.models[OTHER] = deepcopy(other)
    selected = compare_manifests(base, head, selected_models=[OTHER])
    assert selected.models_compared == 1 and not selected.changes
    assert not selected.warnings
    full = compare_manifests(base, head)
    scoped = compare_manifests(base, head, selected_models=[KEY])
    assert_disabled(scoped.to_dict()['changes'][0])
    assert full.changes == scoped.changes
    assert full.models_compared == 2 and scoped.models_compared == 1
    assert 'selected_models' not in full.to_dict()
    assert not scoped.warnings


@pytest.mark.parametrize('form', ['disabled_list', 'config_enabled'])
def test_disabled_only_key_is_known_without_active_comparison(manifest, form):
    head = disabled_head(manifest, node(), form)
    for included in (False, True):
        report = compare_manifests(head, head, selected_models=[KEY], include_unenforced=included)
        assert report.models_compared == 0 and not report.changes


@pytest.mark.parametrize('mode', ['unchanged', 'added', 'removed', 're_enabled'])
def test_other_lifecycle_details_are_preserved(manifest, mode):
    base, head = manifest(), manifest()
    if mode == 'added':
        base = manifest(nodes={})
    elif mode == 'removed':
        head = manifest(nodes={})
    elif mode == 're_enabled':
        base = disabled_head(manifest, node(), 'disabled_list')
    report = compare_manifests(base, head, selected_models=[KEY])
    assert not any(d['rule'] == 'model_disabled' for c in report.changes for d in c.details)
    if mode == 'removed':
        from .test_model_removal import assert_removal
        assert_removal(report.to_dict()['changes'][0])
    elif mode in ('added', 're_enabled'):
        change, = report.changes
        from .test_model_addition import assert_added
        assert_added(report.to_dict()['changes'][0])
    else:
        assert not report.changes


def test_policy_accounting_remains_comparison_local(manifest):
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': [
        {'model': KEY, 'column': 'id', 'path_segments': [], 'rule': 'integer_range', 'severity': 'non_breaking'},
        {'model': OTHER, 'column': 'id', 'path_segments': [], 'rule': 'integer_range', 'severity': 'non_breaking'},
    ]})
    a, b = node('BIGINT'), node('INT')
    other_a, other_b = deepcopy(a), deepcopy(b)
    for n in (other_a, other_b):
        n.update(unique_id=OTHER, name='customers')
    base = manifest(nodes={KEY: a, OTHER: other_a})
    head = manifest(nodes={OTHER: other_b}, disabled={KEY: [a]})
    full = compare_manifests(base, head, policy=policy)
    assert [d['pointer'] for d in full.policy_diagnostics] == ['/rules/0']
    scoped = compare_manifests(base, head, policy=policy, selected_models=[KEY])
    assert_disabled(scoped.to_dict()['changes'][0])
    assert [d['pointer'] for d in scoped.policy_diagnostics] == ['/rules/0', '/rules/1']
    again = compare_manifests(base, head, policy=policy)
    assert again.to_dict() == full.to_dict()


def test_selected_old_version_disabling_retains_family_notice(manifest):
    before = node(version=1, latest=1)
    key = before['unique_id'] = KEY + '.v1'
    replacement = node(version=2, latest=2)
    replacement['unique_id'] = KEY + '.v2'
    report = compare_manifests(manifest(before),
        manifest(replacement, disabled={key: [deepcopy(before)]}), selected_models=[key])
    disabled, latest = report.to_dict()['changes']
    assert_disabled(disabled, key)
    assert report.models_compared == 1 and report.breaking_count == 1
    assert latest['kind'] == 'latest_version_changed' and latest['model'] == key
    assert latest['before'] == 1 and latest['after'] is None
    detail, = latest['details']
    assert detail['rule'] == 'latest_version_changed' and detail['path_segments'] == []
    assert detail['before'] == {'package_name': 'shop', 'name': 'orders', 'latest_version': 1}
    assert detail['after'] == {'package_name': 'shop', 'name': 'orders', 'latest_version': None}
