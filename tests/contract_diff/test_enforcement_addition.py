"""Enforcement addition explains declarations without claiming runtime validation."""
from copy import deepcopy
import json

import pytest

from .conftest import node
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import InputError
from datawrench.contract_diff.policy import ProjectPolicy
from .test_enforcement_removal import declaration
from .test_explanations import compare_fixture


def assert_addition(change, key='model.shop.orders'):
    assert change['model'] == key and change['column'] is None
    assert change['kind'] == 'enforcement_added' and not change['breaking']
    # Existing top-level fields stay unchanged; booleans belong to the new detail.
    assert change['before'] is change['after'] is None
    detail, = change['details']
    assert detail['path'] == 'contract' and detail['path_segments'] == []
    assert detail['before'] is False and detail['after'] is True
    assert detail['classification'] == detail['rule'] == 'enforcement_added'
    assert detail['certainty'] == 'known' and not detail['breaking']
    assert 'declaration' in detail['reason'] and 'not validated' in detail['reason']
    assert 'SQL' in detail['reason'] and 'data' in detail['reason']
    assert 'Validate' in detail['remediation'] and 'relying' in detail['remediation']
    assert 'before_type' not in detail and 'after_type' not in detail and 'policy' not in detail


def test_enforcement_added_fixture_exact_declaration_and_gates():
    result = compare_fixture('enforcement-added')
    assert result.returncode == 0 and not result.stderr
    report = json.loads(result.stdout)
    assert report['schema_version'] == 1
    assert report['summary'] == {'models_compared': 1, 'changes': 1, 'breaking': 0,
                                  'non_breaking': 1, 'equivalents': 0, 'warnings': 0}
    assert not report['notices'] and not report['policy_diagnostics']
    change, = report['changes']
    assert_addition(change)
    assert change['message'] == 'contract enforcement turned on'
    for gate, code in [('breaking', 0), ('any', 1), ('none', 0)]:
        gated = compare_fixture('enforcement-added', 'json', '--fail-on', gate)
        assert gated.returncode == code and gated.stdout == result.stdout and not gated.stderr


@pytest.mark.parametrize('fmt', ['text', 'markdown', 'github'])
def test_enforcement_added_human_formats(fmt):
    result = compare_fixture('enforcement-added', fmt)
    assert result.returncode == 0 and not result.stderr
    assert 'model.shop.orders' in result.stdout and 'enforcement_added' in result.stdout
    assert 'contract:' in result.stdout and 'True' in result.stdout and 'False' in result.stdout
    assert 'Action:' in result.stdout and 'not validated' in result.stdout


@pytest.mark.parametrize('base_scope,head_scope', [
    ('node', 'node'), ('config', 'config'), ('node', 'config'), ('config', 'node'),
    ('both', 'both'), ('node_empty', 'node_empty'), ('config_empty', 'config_empty'),
])
def test_effective_node_and_config_additions(manifest, base_scope, head_scope):
    report = compare_manifests(manifest(declaration(False, base_scope)),
                               manifest(declaration(True, head_scope)))
    change, = report.to_dict()['changes']
    assert_addition(change)


@pytest.mark.parametrize('scope', ['absent', 'empty', 'config_empty'])
def test_missing_base_enforcement_is_effective_false(manifest, scope):
    base = node()
    del base['contract']
    if scope == 'empty':
        base['contract'] = {}
    if scope == 'config_empty':
        base['config']['contract'] = {}
    change, = compare_manifests(manifest(base), manifest()).to_dict()['changes']
    assert_addition(change)


@pytest.mark.parametrize('value', [True, False])
@pytest.mark.parametrize('included', [True, False])
def test_unchanged_effective_enforcement_has_no_addition(manifest, value, included):
    report = compare_manifests(manifest(declaration(value, 'node')), manifest(declaration(value, 'config')),
                               include_unenforced=included)
    assert not report.changes and report.models_compared == int(value or included)


@pytest.mark.parametrize('side', ['base', 'head'])
def test_conflicting_enforcement_still_rejected(manifest, side):
    n = node(enforced=side == 'base')
    n['config']['contract'] = {'enforced': side != 'base'}
    with pytest.raises(InputError, match='conflicting node/config contract enforcement'):
        manifest(n)


def test_exact_key_version_message_and_unrelated_types_preserved(manifest):
    before = node('  INTEGER  ', enforced=False, version=1)
    before['unique_id'] = 'model.Shop."Orders.v1"'
    after = deepcopy(before)
    after['contract']['enforced'] = True
    after['columns']['id']['data_type'] = 'VARCHAR'
    report = compare_manifests(manifest(before), manifest(after))
    addition, changed_type = report.to_dict()['changes']
    assert_addition(addition, before['unique_id'])
    assert addition['version'] == '1'
    assert addition['message'] == 'contract enforcement turned on'
    assert report.breaking_count == 1 and report.models_compared == 1
    assert changed_type['before'] == '  INTEGER  ' and changed_type['after'] == 'VARCHAR'
    # Removing only the new finding reproduces the old comparison's other type behavior.
    before['contract']['enforced'] = True
    assert compare_manifests(manifest(before), manifest(after)).to_dict()['changes'] == [changed_type]


def test_enforcement_change_keeps_alias_and_unknown_type_behavior(manifest):
    for raw in ['INTEGER', 'custom_type']:
        before, after = node('INT' if raw == 'INTEGER' else raw, enforced=False), node(raw)
        report = compare_manifests(manifest(before), manifest(after)).to_dict()
        assert_addition(report['changes'][0])
        before['contract']['enforced'] = True
        original = compare_manifests(manifest(before), manifest(after)).to_dict()
        assert report['changes'][1:] == original['changes']
        assert report['warnings'] == original['warnings'] and report['equivalents'] == original['equivalents']


@pytest.mark.parametrize('mode', ['removed', 'disabled', 'added'])
def test_other_model_lifecycle_findings_do_not_gain_addition_details(manifest, mode):
    before, after = node(), node()
    if mode == 'disabled':
        after['config']['enabled'] = False
    a = manifest(nodes={}) if mode == 'added' else manifest(before)
    b = manifest(nodes={}) if mode == 'removed' else manifest(after)
    change, = compare_manifests(a, b).changes
    assert change.kind == 'model_' + mode
    if mode == 'removed':
        detail, = change.details
        assert detail['rule'] == 'model_removed'
    elif mode == 'disabled':
        detail, = change.details
        assert detail['rule'] == 'model_disabled'
    else:
        detail, = change.details
        assert detail['rule'] == 'model_added' and detail['path_segments'] == []


@pytest.mark.parametrize('severity', ['breaking', 'non_breaking'])
def test_enforcement_rule_remains_outside_policy_scope(severity):
    with pytest.raises(InputError, match='unknown rule'):
        ProjectPolicy.from_dict({'version': 1, 'rules': [{'model': 'model.shop.orders', 'column': None,
            'path_segments': [], 'rule': 'enforcement_added', 'severity': severity}]})
