"""Latest-version details explain only the effective declared family metadata."""
from copy import deepcopy

import pytest

from .conftest import node
from datawrench.contract_diff.cli import render
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import InputError
from datawrench.contract_diff.policy import ProjectPolicy

V1, V2, OTHER = 'model.shop.orders.v1', 'model.shop.orders.v2', 'model.shop.customers'


def record(key=V1, version=1, latest=1, **kwargs):
    n = node(version=version, latest=latest, **kwargs)
    n['unique_id'] = key
    return n


def family(value, package='shop', name='orders'):
    return dict(package_name=package, name=name, latest_version=value)


def assert_latest(change, before=1, after=2, key=V1, package='shop', name='orders', version='1'):
    assert change['model'] == key and change['kind'] == 'latest_version_changed'
    assert change['column'] is None and not change['breaking']
    assert (change['before'], change['after'], change['version']) == (before, after, version)
    assert change['message'] == f'latest_version changed: {before} -> {after}'
    d, = change['details']
    assert d['path'] == 'model' and d['path_segments'] == []
    assert d['rule'] == d['classification'] == 'latest_version_changed'
    assert d['before'] == family(before, package, name) and d['after'] == family(after, package, name)
    assert not d['breaking'] and d['certainty'] == 'known'
    assert 'declarations only' in d['reason'] and 'non-breaking' in d['reason']
    assert 'not validated' in d['reason'] and 'migration' in d['reason']
    assert 'consumers' in d['remediation'] and 'separately' in d['remediation']
    assert all(word in d['remediation'] for word in ('SQL', 'data', 'warehouse'))
    assert not {'before_type', 'after_type', 'policy'} & d.keys()


def pair(manifest, before=1, after=2):
    return (manifest(nodes={V1: record(latest=v), V2: record(V2, 2, v)}) for v in (before, after))


def test_scoped_latest_contract(manifest):
    report = compare_manifests(*pair(manifest), selected_models=[V1])
    data = report.to_dict()
    assert data['schema_version'] == 1 and data['selected_models'] == [V1]
    assert data['summary'] == dict(models_compared=1, changes=1, breaking=0, non_breaking=1, equivalents=0, warnings=0)
    change, = data['changes']; assert_latest(change)
    assert not report.notices and not report.policy_diagnostics
    assert [report.exit_code(g) for g in ('breaking', 'any', 'none')] == [0, 1, 0]


@pytest.mark.parametrize('before,after', [(None, 2), (1, None), ('01', 1), (1, '1'), (1.5, 2.5)])
def test_exact_consumed_latest_values(manifest, before, after):
    change, = compare_manifests(*pair(manifest, before, after), selected_models=[V1]).to_dict()['changes']
    assert_latest(change, before, after)
    assert type(change['details'][0]['before']['latest_version']) is type(before)
    assert type(change['details'][0]['after']['latest_version']) is type(after)


@pytest.mark.parametrize('selected,representative,count', [([V1], V1, 1), ([V2], V2, 1),
    ([V2, V1, V1], V2, 2), (None, V2, 2)])
def test_repeated_records_keep_last_sorted_representative(manifest, selected, representative, count):
    report = compare_manifests(*pair(manifest), selected_models=selected)
    assert report.models_compared == count and len(report.changes) == 1
    assert_latest(report.to_dict()['changes'][0], key=representative, version='1' if representative == V1 else '2')
    assert ('selected_models' in report.to_dict()) == (selected is not None)


def test_selected_record_without_latest_cannot_inherit_sibling(manifest):
    base, head = pair(manifest)
    for m in (base, head): m.models[V1].pop('latest_version')
    scoped = compare_manifests(base, head, selected_models=[V1])
    assert scoped.models_compared == 1 and not scoped.changes
    assert_latest(compare_manifests(base, head).to_dict()['changes'][0], key=V2, version='2')


@pytest.mark.parametrize('key,package', [(V1, 'shop'), ('model.Shop."Orders.a.b".v1', 'Shop'), ('orders', '')])
@pytest.mark.parametrize('explicit', [False, True])
def test_effective_group_fallback_and_literal_keys(manifest, key, package, explicit):
    a, b = record(key, latest=1), record(key, latest=2)
    for n in (a, b):
        n.pop('unique_id'); n.pop('package_name'); n['name'] = 'Orders."a.b"'
    if explicit and package: b['package_name'] = package
    c, = compare_manifests(manifest(nodes={key: a}), manifest(nodes={key: b}), selected_models=[key]).to_dict()['changes']
    assert_latest(c, key=key, package=package, name='Orders."a.b"')


@pytest.mark.parametrize('mode', ['false', 'absent', 'config', 'removed', 'added'])
@pytest.mark.parametrize('include', [False, True])
def test_enforcement_eligibility(manifest, mode, include):
    a, b = record(latest=1, enforced=False), record(latest=2, enforced=False)
    if mode == 'absent':
        for n in (a, b): n.pop('contract')
    elif mode == 'config':
        for n in (a, b): n.pop('contract'); n['config']['contract'] = {'enforced': True}
    elif mode == 'removed': a['contract']['enforced'] = True
    elif mode == 'added': b['contract']['enforced'] = True
    report = compare_manifests(manifest(a), manifest(b), selected_models=[V1], include_unenforced=include)
    eligible = include or mode in ('config', 'removed', 'added')
    assert report.models_compared == int(eligible)
    if eligible: assert_latest(report.to_dict()['changes'][-1])
    else: assert not report.changes and 'selected scope' in report.notices[0]


def test_unselected_family_and_unenforced_sibling_not_expanded(manifest):
    base, head = pair(manifest)
    for m in (base, head):
        m.models[V2]['contract']['enforced'] = False
        n = record(OTHER, latest=None); n['name'] = 'customers'; m.models[OTHER] = n
    assert not compare_manifests(base, head, selected_models=[OTHER]).changes
    assert_latest(compare_manifests(base, head, selected_models=[V1, V2]).to_dict()['changes'][0])
    # Full comparison retains its preexisting artifact-wide latest representative.
    full = compare_manifests(base, head)
    assert full.models_compared == 2
    assert_latest(full.to_dict()['changes'][0], key=V2, version='2')


@pytest.mark.parametrize('selected', [[V1], [OTHER]])
def test_family_validation_remains_artifact_wide_even_empty_scope(manifest, selected):
    base, head = pair(manifest)
    for m in (base, head):
        n = record(OTHER, enforced=False); n['name'] = 'customers'; m.models[OTHER] = n
    head.models[V2]['latest_version'] = 3
    with pytest.raises(InputError, match='inconsistent latest_version'):
        compare_manifests(base, head, selected_models=selected)


def test_family_change_keeps_existing_old_active_family_behavior(manifest):
    a, b = record(), record(latest=2); b['name'] = 'new_orders'
    report = compare_manifests(manifest(a), manifest(b), selected_models=[V1])
    assert [c.kind for c in report.changes] == ['model_identity_changed', 'latest_version_changed']
    assert report.breaking_count == 1
    assert_latest(report.to_dict()['changes'][-1], after=None)


@pytest.mark.parametrize('mode', ['added', 'removed', 'disabled'])
def test_lifecycle_findings_stay_independent(manifest, mode):
    n = record()
    empty = manifest(nodes={})
    base, head = (empty, manifest(n)) if mode == 'added' else (manifest(n), empty)
    if mode == 'disabled': head.disabled.add(V1)
    report = compare_manifests(base, head, selected_models=[V1])
    assert [c.kind for c in report.changes] == ['model_' + mode, 'latest_version_changed']
    assert report.breaking_count == int(mode != 'added')
    assert len(report.changes[0].details) == 1
    assert_latest(report.to_dict()['changes'][-1], before=None if mode == 'added' else 1,
        after=1 if mode == 'added' else None, version=None if mode == 'added' else '1')


def test_independent_identity_types_constraints_and_policy_locality(manifest):
    a, b = record(dtype='BIGINT'), record(dtype='INT', latest=2)
    b['version'] = 2
    a['constraints'] = [{'type': 'unique', 'columns': ['id']}]
    a['columns']['alias'] = {'data_type': 'INT'}; b['columns']['alias'] = {'data_type': 'INTEGER'}
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': [dict(model=V1, column='id', path_segments=[],
        rule='integer_range', severity='non_breaking')]})
    full = compare_manifests(manifest(a), manifest(b), policy=policy)
    assert [c.kind for c in full.changes] == ['model_identity_changed', 'type_narrowing', 'constraint_removed', 'latest_version_changed']
    assert full.breaking_count == 2 and len(full.equivalents) == 1 and not full.policy_diagnostics
    assert_latest(full.to_dict()['changes'][-1])
    b['columns'] = deepcopy(a['columns']); b['constraints'] = deepcopy(a['constraints']); b['version'] = 1
    latest_only = compare_manifests(manifest(a), manifest(b), policy=policy)
    assert [d['pointer'] for d in latest_only.policy_diagnostics] == ['/rules/0']
    assert_latest(latest_only.to_dict()['changes'][0])
    assert not compare_manifests(manifest(record(dtype='BIGINT')), manifest(record(dtype='INT')), policy=policy).policy_diagnostics


@pytest.mark.parametrize('before,after', [(None, None), (1, 1), (1, 1.0)])
def test_no_new_latest_findings_or_equality_rules(manifest, before, after):
    assert not compare_manifests(*pair(manifest, before, after), selected_models=[V1]).changes


def test_equal_numeric_repeats_keep_exact_last_consumed_value(manifest):
    base, head = pair(manifest)
    base.models[V2]['latest_version'] = 1.0; head.models[V2]['latest_version'] = 2.0
    c, = compare_manifests(base, head).to_dict()['changes']
    assert_latest(c, before=1.0, after=2.0, key=V2, version='2')
    assert type(c['details'][0]['before']['latest_version']) is float


@pytest.mark.parametrize('severity', ['breaking', 'non_breaking'])
@pytest.mark.parametrize('rule', ['latest_version_changed', 'model_identity_changed', 'model_added', 'model_removed', 'model_disabled'])
def test_latest_lifecycle_policies_remain_rejected(severity, rule):
    with pytest.raises(InputError, match='unknown rule'):
        ProjectPolicy.from_dict({'version': 1, 'rules': [dict(model=V1, column=None,
            path_segments=[], rule=rule, severity=severity)]})


def test_literal_keys_render_safely(manifest):
    key = 'model.Shop.<Orders>|`\n::error::x%\x1b'
    a, b = record(key), record(key, latest=2)
    for n in (a, b): n['name'] = 'Orders.<a>|`\n::error::x%\x1b'
    report = compare_manifests(manifest(a), manifest(b), selected_models=[key])
    for fmt in ('text', 'markdown', 'github'):
        out = render(report, fmt)
        assert '\x1b' not in out and '\n::error::x' not in out and 'Action:' in out
        if fmt == 'markdown': assert '&lt;Orders&gt;&#124;&#96;' in out
        if fmt == 'github': assert len(out.splitlines()) == 3


@pytest.mark.parametrize('head_latest', [2, None])
def test_representative_uses_head_metadata_then_base_fallback(manifest, head_latest):
    base, head = pair(manifest)
    base.models[V1]['latest_version'] = None
    head.models[V1]['latest_version'] = head_latest
    head.models[V2]['latest_version'] = None
    c, = compare_manifests(base, head, selected_models=[V1, V2]).to_dict()['changes']
    key = V1 if head_latest is not None else V2
    assert_latest(c, after=head_latest, key=key, version='1' if key == V1 else '2')
