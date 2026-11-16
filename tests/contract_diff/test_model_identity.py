"""Identity explanations retain the exact model root and effective family semantics."""
import pytest

from .conftest import node
from datawrench.contract_diff.cli import render
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import InputError
from datawrench.contract_diff.policy import ProjectPolicy

KEY = 'model.shop.orders'
OTHER = 'model.shop.customers'


def assert_identity(change, before, after):
    assert change['model'] == before['unique_id'] == after['unique_id']
    assert change['column'] is None and change['kind'] == 'model_identity_changed'
    assert change['breaking'] and change['before'] == before['version'] and change['after'] == after['version']
    assert change['version'] == (str(before['version']) if before['version'] is not None else None)
    message = 'model identity/version changed under the same unique_id'
    if before['version'] is not None:
        message += f"; breaking change without version bump (version {before['version']})"
    assert change['message'] == message
    detail, = change['details']
    assert detail['path'] == 'model' and detail['path_segments'] == []
    assert detail['rule'] == detail['classification'] == 'model_identity_changed'
    assert detail['before'] == before and detail['after'] == after
    assert detail['breaking'] and detail['certainty'] == 'known'
    assert 'declarations only' in detail['reason'] and 'original identity' in detail['reason']
    assert 'SQL' in detail['reason'] and 'data' in detail['reason'] and 'not validated' in detail['reason']
    assert 'original contract' in detail['remediation'] and 'original identity' in detail['remediation']
    assert not {'before_type', 'after_type', 'policy'} & detail.keys()


def identity(key=KEY, package='shop', name='orders', version=1):
    return dict(unique_id=key, package_name=package, name=name, version=version)


def test_scoped_version_identity_contract(manifest):
    report = compare_manifests(manifest(node(version=1)), manifest(node(version=2)), selected_models=[KEY])
    data = report.to_dict()
    assert data['schema_version'] == 1 and data['selected_models'] == [KEY]
    assert data['summary'] == dict(models_compared=1, changes=1, breaking=1, non_breaking=0, equivalents=0, warnings=0)
    change, = data['changes']
    assert_identity(change, identity(), identity(version=2))
    assert not report.notices and not report.policy_diagnostics
    assert [report.exit_code(g) for g in ('breaking', 'any', 'none')] == [1, 1, 0]


@pytest.mark.parametrize('field,value', [('package_name', 'Shop."new.pkg"'), ('name', 'Orders."a.b"'),
    ('version', '02'), ('version', None), ('version', 2.5)])
def test_consumed_values_kept_exact(manifest, field, value):
    a, b = node(version=1), node(version=1)
    b[field] = value
    report = compare_manifests(manifest(a), manifest(b), selected_models=[KEY])
    change, = report.to_dict()['changes']
    after = identity(); after[field] = value
    assert_identity(change, identity(), after)


@pytest.mark.parametrize('key,package', [(KEY, 'shop'), ('model.Shop."Orders.a.b".v1', 'Shop'), ('orders', '')])
@pytest.mark.parametrize('explicit', [False, True])
def test_effective_package_fallback_and_optional_unique_id(manifest, key, package, explicit):
    a, b = node(version=1), node(version=2)
    for n in (a, b):
        del n['unique_id']; del n['package_name']
    if explicit:
        # Explicit package equal to the old fallback is not itself a family change.
        b['package_name'] = package or 'explicit'
    report = compare_manifests(manifest(nodes={key: a}), manifest(nodes={key: b}), selected_models=[key])
    change, = report.to_dict()['changes']
    assert_identity(change, identity(key, package), identity(key, b.get('package_name', package), version=2))
    b['version'] = 1
    unchanged = compare_manifests(manifest(nodes={key: a}), manifest(nodes={key: b}), selected_models=[key])
    assert bool(unchanged.changes) == (explicit and not package)


@pytest.mark.parametrize('before,after', [(None, 'v2'), ('01', 1), (1, '1')])
def test_version_transition_preserves_raw_types(manifest, before, after):
    change, = compare_manifests(manifest(node(version=before)), manifest(node(version=after))).to_dict()['changes']
    assert_identity(change, identity(version=before), identity(version=after))


@pytest.mark.parametrize('key', [KEY, 'model.Shop."Orders.a.b".v1', 'model.shop.<Orders>|`\n::error::x%\x1b'])
def test_exact_key_and_selected_unselected_scope(manifest, key):
    a, b = node(version=1), node(version=2)
    for n in (a, b): n['unique_id'] = key
    other = node(); other.update(unique_id=OTHER, name='customers')
    base, head = manifest(nodes={key: a, OTHER: other}), manifest(nodes={key: b, OTHER: other})
    scoped = compare_manifests(base, head, selected_models=[key])
    full = compare_manifests(base, head)
    assert scoped.changes == full.changes and scoped.models_compared == 1 and full.models_compared == 2
    assert_identity(scoped.to_dict()['changes'][0], identity(key), identity(key, version=2))
    excluded = compare_manifests(base, head, selected_models=[OTHER])
    assert excluded.models_compared == 1 and not excluded.changes and not excluded.warnings
    if '\n' in key:
        for fmt in ('text', 'markdown', 'github'):
            out = render(scoped, fmt)
            assert '\x1b' not in out and '\n::error::x' not in out and 'Action:' in out
            if fmt == 'markdown': assert '&lt;Orders&gt;&#124;&#96;' in out
            if fmt == 'github': assert len(out.splitlines()) == 3


@pytest.mark.parametrize('included', [False, True])
@pytest.mark.parametrize('enforcement', ['false', 'absent', 'config'])
def test_enforcement_scope(manifest, included, enforcement):
    a, b = node(version=1, enforced=False), node(version=2, enforced=False)
    for n in (a, b):
        if enforcement != 'false': del n['contract']
        if enforcement == 'config': n['config']['contract'] = {'enforced': True}
    report = compare_manifests(manifest(a), manifest(b), selected_models=[KEY], include_unenforced=included)
    active = included or enforcement == 'config'
    assert report.models_compared == int(active)
    if active: assert_identity(report.to_dict()['changes'][0], identity(), identity(version=2))
    else: assert not report.changes and 'selected scope' in report.notices[0]


@pytest.mark.parametrize('family_changed', [False, True])
def test_latest_type_constraints_and_equivalents_retained(manifest, family_changed):
    a, b = node('BIGINT', version=1, latest=1), node('INT', version=2, latest=2)
    if family_changed: b['name'] = 'new_name'
    a['constraints'] = [{'type': 'unique', 'columns': ['id']}]
    a['columns']['alias'] = {'data_type': 'INT'}
    b['columns']['alias'] = {'data_type': 'INTEGER'}
    report = compare_manifests(manifest(a), manifest(b), selected_models=[KEY])
    assert [c.kind for c in report.changes] == ['model_identity_changed', 'type_narrowing', 'constraint_removed', 'latest_version_changed']
    assert report.models_compared == 1 and report.breaking_count == 3 and len(report.equivalents) == 1
    after = identity(version=2); after['name'] = b['name']
    assert_identity(report.to_dict()['changes'][0], identity(), after)
    latest = report.changes[-1]
    assert latest.before == 1 and latest.after == (None if family_changed else 2)
    detail, = latest.details
    assert detail['rule'] == 'latest_version_changed'
    assert detail['before'] == dict(package_name='shop', name='orders', latest_version=1)
    assert detail['after'] == dict(package_name='shop', name='orders', latest_version=None if family_changed else 2)
    assert report.changes[1].details[0]['rule'] == 'integer_range'
    assert report.changes[2].details[0]['rule'] == 'constraint_guarantee_removed'


def test_new_version_does_not_excuse_changed_original_identity(manifest):
    a, b, sibling = node(version=1), node(version=2), node(version=3)
    sibling['unique_id'] = KEY + '.v3'
    base, head = manifest(a), manifest(nodes={KEY: b, sibling['unique_id']: sibling})
    scoped = compare_manifests(base, head, selected_models=[KEY])
    assert_identity(scoped.to_dict()['changes'][0], identity(), identity(version=2))
    full = compare_manifests(base, head)
    assert [c.kind for c in full.changes] == ['model_identity_changed', 'model_added'] and full.breaking_count == 1


@pytest.mark.parametrize('severity', ['breaking', 'non_breaking'])
def test_identity_policy_rejected(severity):
    with pytest.raises(InputError, match='unknown rule'):
        ProjectPolicy.from_dict({'version': 1, 'rules': [{'model': KEY, 'column': None,
            'path_segments': [], 'rule': 'model_identity_changed', 'severity': severity}]})


def test_unused_policy_accounting_stays_comparison_local(manifest):
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': [dict(model=k, column='id',
        path_segments=[], rule='integer_range', severity='non_breaking') for k in (KEY, OTHER)]})
    a, b, x, y = node(version=1), node(version=2), node('BIGINT'), node('INT')
    for n in (x, y): n.update(unique_id=OTHER, name='customers')
    base, head = manifest(nodes={KEY: a, OTHER: x}), manifest(nodes={KEY: b, OTHER: y})
    full = compare_manifests(base, head, policy=policy)
    assert [d['pointer'] for d in full.policy_diagnostics] == ['/rules/0']
    scoped = compare_manifests(base, head, policy=policy, selected_models=[KEY])
    assert_identity(scoped.to_dict()['changes'][0], identity(), identity(version=2))
    assert [d['pointer'] for d in scoped.policy_diagnostics] == ['/rules/0', '/rules/1']
    assert compare_manifests(base, head, policy=policy).to_dict() == full.to_dict()


@pytest.mark.parametrize('mode', ['same', 'latest_only', 'numerically_equal', 'non_identity_metadata'])
def test_no_new_identity_validation_or_findings(manifest, mode):
    a, b = node(version=1), node(version=1)
    if mode == 'latest_only': b['latest_version'] = 2
    elif mode == 'numerically_equal': b['version'] = 1.0
    elif mode == 'non_identity_metadata': b.update(alias='renamed', schema='elsewhere', database='other')
    report = compare_manifests(manifest(a), manifest(b), selected_models=[KEY])
    assert not any(c.kind == 'model_identity_changed' for c in report.changes)
