"""Model additions explain exact declarations, without implying warehouse creation."""
from copy import deepcopy

import pytest

from .conftest import node
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import InputError
from datawrench.contract_diff.policy import ProjectPolicy

KEY = 'model.shop.orders'
OTHER = 'model.shop.customers'


def assert_added(change, key=KEY, version=None):
    assert change['model'] == key and change['column'] is None
    assert change['kind'] == 'model_added' and not change['breaking']
    # Legacy fields describe version metadata; the detail locates the declaration.
    assert change['before'] is None and change['after'] == version and change['version'] is None
    assert change['message'] == 'model/version added'
    detail, = change['details']
    assert detail['path'] == 'model' and detail['path_segments'] == []
    assert detail['classification'] == detail['rule'] == 'model_added'
    assert detail['before'] is None and detail['after'] == key
    assert not detail['breaking'] and detail['certainty'] == 'known'
    assert 'declared model contract' in detail['reason'] and 'active manifest' in detail['reason']
    assert 'non-breaking' in detail['reason'] and 'declarations only' in detail['reason']
    assert 'SQL' in detail['reason'] and 'data' in detail['reason'] and 'not validated' in detail['reason']
    assert 'warehouse creation' in detail['reason']
    assert 'Validate' in detail['remediation'] and 'SQL' in detail['remediation']
    assert 'data' in detail['remediation'] and 'warehouse' in detail['remediation']
    assert not {'before_type', 'after_type', 'policy'} & detail.keys()


def test_scoped_model_added_contract(manifest):
    report = compare_manifests(manifest(nodes={}), manifest(), selected_models=[KEY])
    data = report.to_dict()
    assert data['selected_models'] == [KEY] and data['schema_version'] == 1
    assert data['summary'] == {'models_compared': 1, 'changes': 1, 'breaking': 0,
        'non_breaking': 1, 'equivalents': 0, 'warnings': 0}
    change, = data['changes']
    assert_added(change)
    assert not report.warnings and not report.notices and not report.policy_diagnostics
    assert [report.exit_code(g) for g in ('breaking', 'any', 'none')] == [0, 1, 0]


@pytest.mark.parametrize('enforcement', ['node', 'config', 'both', 'false', 'absent'])
@pytest.mark.parametrize('included', [False, True])
def test_enforcement_filter_and_literal_versioned_key(manifest, enforcement, included):
    after = node(version='02', enforced=enforcement != 'false')
    key = after['unique_id'] = 'model.Shop."Orders.a.b".v02'
    if enforcement in ('config', 'both'):
        after['config']['contract'] = {'enforced': True}
    if enforcement in ('config', 'absent'):
        del after['contract']
    report = compare_manifests(manifest(nodes={}), manifest(after), selected_models=[key], include_unenforced=included)
    active = included or enforcement in ('node', 'config', 'both')
    assert report.models_compared == int(active)
    if active:
        change, = report.to_dict()['changes']
        assert_added(change, key, '02')
    else:
        assert not report.changes and 'selected scope' in report.notices[0]


@pytest.mark.parametrize('form', ['missing', 'disabled_list', 'config_disabled'])
def test_disabled_to_active_is_one_addition(manifest, form):
    n = node()
    if form == 'missing':
        base = manifest(nodes={})
    elif form == 'disabled_list':
        base = manifest(nodes={}, disabled={KEY: [deepcopy(n)]})
    else:
        n['config']['enabled'] = False
        base = manifest(n)
    report = compare_manifests(base, manifest(), selected_models=[KEY])
    change, = report.to_dict()['changes']
    assert_added(change)
    assert report.models_compared == 1 and not report.warnings


@pytest.mark.parametrize('raw', ['INT', ' custom_type ', 'STRUCT(', None, '', 'missing'])
def test_new_model_types_warn_without_column_or_constraint_findings(manifest, raw):
    n = node(raw)
    if raw == 'missing':
        del n['columns']['id']['data_type']
    n['columns']['id']['constraints'] = [{'type': 'not_null'}]
    n['columns']['payload.items'] = {'data_type': 'STRUCT(amount INT)', 'constraints': []}
    n['constraints'] = [{'type': 'unique', 'columns': ['id']}]
    report = compare_manifests(manifest(nodes={}), manifest(n), selected_models=[KEY])
    change, = report.to_dict()['changes']
    assert_added(change)
    assert len(report.warnings) == int(raw != 'INT')
    if report.warnings:
        assert KEY + ' column id: added data_type unparseable:' in report.warnings[0]
        assert 'no previous type to compare' in report.warnings[0]
    assert not report.equivalents


def test_unselected_addition_cannot_leak_findings_warnings_or_count(manifest):
    other = node(); other.update(unique_id=OTHER, name='customers')
    after = node('STRUCT(')
    base = manifest(nodes={OTHER: other})
    head = manifest(nodes={KEY: after, OTHER: other})
    selected = compare_manifests(base, head, selected_models=[OTHER])
    assert selected.models_compared == 1 and not selected.changes and not selected.warnings
    scoped = compare_manifests(base, head, selected_models=[KEY])
    full = compare_manifests(base, head)
    assert full.changes == scoped.changes and full.warnings == scoped.warnings
    assert full.models_compared == 2 and scoped.models_compared == 1
    assert 'selected_models' not in full.to_dict()
    assert_added(scoped.to_dict()['changes'][0])
    with pytest.raises(InputError, match='unknown selected'):
        compare_manifests(base, head, selected_models=[KEY.lower().replace('orders', 'Orders')])


@pytest.mark.parametrize('mode', ['unchanged', 'removed', 'disabled', 'disabled_only', 'enforcement_added'])
def test_other_lifecycle_details_remain_distinct(manifest, mode):
    base, head = manifest(), manifest()
    if mode == 'removed': head = manifest(nodes={})
    elif mode == 'disabled': head = manifest(nodes={}, disabled={KEY: [node()]})
    elif mode == 'disabled_only': base = head = manifest(nodes={}, disabled={KEY: [node()]})
    elif mode == 'enforcement_added': base = manifest(node(enforced=False))
    report = compare_manifests(base, head, selected_models=[KEY])
    assert not any(d['rule'] == 'model_added' for c in report.changes for d in c.details)
    if mode in ('unchanged', 'disabled_only'): assert not report.changes
    else:
        change, = report.changes
        assert change.details[0]['rule'] == {'removed': 'model_removed', 'disabled': 'model_disabled',
                                            'enforcement_added': 'enforcement_added'}[mode]


def test_replacement_does_not_excuse_original_removal_or_expand_selected_family(manifest):
    old, new = node(version=1, latest=1), node(version=2, latest=2)
    old['unique_id'], new['unique_id'] = KEY + '.v1', KEY + '.v2'
    base, head = manifest(old), manifest(new)
    full = compare_manifests(base, head)
    assert [c.kind for c in full.changes] == ['model_removed', 'model_added', 'latest_version_changed']
    assert full.breaking_count == 1 and 'without version bump (version 1)' in full.changes[0].message
    assert_added(full.to_dict()['changes'][1], new['unique_id'], 2)
    scoped = compare_manifests(base, head, selected_models=[new['unique_id']])
    assert [c.kind for c in scoped.changes] == ['model_added', 'latest_version_changed']
    assert_added(scoped.to_dict()['changes'][0], new['unique_id'], 2)
    latest = scoped.changes[1]
    assert latest.before is None and latest.after == 2
    detail, = latest.details
    assert detail['rule'] == 'latest_version_changed' and detail['path_segments'] == []
    assert detail['before'] == {'package_name': 'shop', 'name': 'orders', 'latest_version': None}
    assert detail['after'] == {'package_name': 'shop', 'name': 'orders', 'latest_version': 2}
    removed = compare_manifests(base, head, selected_models=[old['unique_id']])
    assert removed.breaking_count == 1 and removed.changes[0].details[0]['rule'] == 'model_removed'


@pytest.mark.parametrize('severity', ['breaking', 'non_breaking'])
def test_model_added_policy_rejected(severity):
    with pytest.raises(InputError, match='unknown rule'):
        ProjectPolicy.from_dict({'version': 1, 'rules': [{'model': KEY, 'column': None,
            'path_segments': [], 'rule': 'model_added', 'severity': severity}]})


def test_policy_accounting_is_local_and_new_model_constraints_do_not_consume_entries(manifest):
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': [
        {'model': KEY, 'column': 'id', 'path_segments': [], 'rule': 'integer_range', 'severity': 'non_breaking'},
        {'model': KEY, 'column': None, 'path_segments': [{'kind': 'constraint', 'signature': '{"columns": ["id"], "type": "unique"}'}],
         'rule': 'constraint_guarantee_added', 'severity': 'breaking'},
        {'model': OTHER, 'column': 'id', 'path_segments': [], 'rule': 'integer_range', 'severity': 'non_breaking'}]})
    n = node(); n['constraints'] = [{'type': 'unique', 'columns': ['id']}]
    a, b = node('BIGINT'), node('INT')
    for other in (a, b): other.update(unique_id=OTHER, name='customers')
    base, head = manifest(a), manifest(nodes={KEY: n, OTHER: b})
    full = compare_manifests(base, head, policy=policy)
    assert [d['pointer'] for d in full.policy_diagnostics] == ['/rules/0', '/rules/1']
    scoped = compare_manifests(base, head, policy=policy, selected_models=[KEY])
    change, = scoped.to_dict()['changes']; assert_added(change)
    assert [d['pointer'] for d in scoped.policy_diagnostics] == ['/rules/0', '/rules/1', '/rules/2']
    assert compare_manifests(base, head, policy=policy).to_dict() == full.to_dict()
