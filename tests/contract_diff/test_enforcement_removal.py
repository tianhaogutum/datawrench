"""Enforcement removal explains declarations without claiming runtime validation."""
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
    assert change['kind'] == 'enforcement_removed' and change['breaking']
    # Existing top-level fields stay unchanged; booleans belong to the new detail.
    assert change['before'] is change['after'] is None
    detail, = change['details']
    assert detail['path'] == 'contract' and detail['path_segments'] == []
    assert detail['before'] is True and detail['after'] is False
    assert detail['classification'] == detail['rule'] == 'enforcement_removed'
    assert detail['certainty'] == 'known' and detail['breaking']
    assert 'declaration' in detail['reason'] and 'not validated' in detail['reason']
    assert 'SQL' in detail['reason'] and 'data' in detail['reason']
    assert 'Restore' in detail['remediation'] and 'consumers' in detail['remediation']
    assert 'before_type' not in detail and 'after_type' not in detail and 'policy' not in detail


def test_enforcement_removed_fixture_exact_declaration_and_gates():
    result = compare_fixture('enforcement-removed')
    assert result.returncode == 1 and not result.stderr
    report = json.loads(result.stdout)
    assert report['schema_version'] == 1
    assert report['summary'] == {'models_compared': 1, 'changes': 1, 'breaking': 1,
                                  'non_breaking': 0, 'equivalents': 0, 'warnings': 0}
    assert not report['notices'] and not report['policy_diagnostics']
    change, = report['changes']
    assert_removal(change)
    assert change['message'] == 'contract enforcement turned off'
    for gate, code in [('breaking', 1), ('any', 1), ('none', 0)]:
        gated = compare_fixture('enforcement-removed', 'json', '--fail-on', gate)
        assert gated.returncode == code and gated.stdout == result.stdout and not gated.stderr


@pytest.mark.parametrize('fmt', ['text', 'markdown', 'github'])
def test_enforcement_removed_human_formats(fmt):
    result = compare_fixture('enforcement-removed', fmt)
    assert result.returncode == 1 and not result.stderr
    assert 'model.shop.orders' in result.stdout and 'enforcement_removed' in result.stdout
    assert 'contract:' in result.stdout and 'True' in result.stdout and 'False' in result.stdout
    assert 'Action:' in result.stdout and 'not validated' in result.stdout


def declaration(value, scope):
    n = node(enforced=value)
    if scope in ('config', 'both', 'node_empty'):
        n['config']['contract'] = {'enforced': value}
    if scope == 'config':
        del n['contract']
    if scope == 'node_empty':
        n['contract'] = {}
    if scope == 'config_empty':
        n['config']['contract'] = {}
    return n


@pytest.mark.parametrize('base_scope,head_scope', [
    ('node', 'node'), ('config', 'config'), ('node', 'config'), ('config', 'node'),
    ('both', 'both'), ('node_empty', 'node_empty'), ('config_empty', 'config_empty'),
])
def test_effective_node_and_config_removals(manifest, base_scope, head_scope):
    report = compare_manifests(manifest(declaration(True, base_scope)),
                               manifest(declaration(False, head_scope)))
    change, = report.to_dict()['changes']
    assert_removal(change)


@pytest.mark.parametrize('scope', ['absent', 'empty', 'config_empty'])
def test_missing_head_enforcement_is_effective_false(manifest, scope):
    head = node()
    del head['contract']
    if scope == 'empty':
        head['contract'] = {}
    if scope == 'config_empty':
        head['config']['contract'] = {}
    change, = compare_manifests(manifest(), manifest(head)).to_dict()['changes']
    assert_removal(change)


@pytest.mark.parametrize('value', [True, False])
@pytest.mark.parametrize('included', [True, False])
def test_unchanged_effective_enforcement_has_no_removal(manifest, value, included):
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
    before = node('  INTEGER  ', version=1)
    before['unique_id'] = 'model.Shop."Orders.v1"'
    after = deepcopy(before)
    after['contract']['enforced'] = False
    after['columns']['id']['data_type'] = 'VARCHAR'
    report = compare_manifests(manifest(before), manifest(after))
    removal, changed_type = report.to_dict()['changes']
    assert_removal(removal, before['unique_id'])
    assert removal['version'] == '1'
    assert removal['message'] == 'contract enforcement turned off; breaking change without version bump (version 1)'
    assert report.breaking_count == 2 and report.models_compared == 1
    assert changed_type['before'] == '  INTEGER  ' and changed_type['after'] == 'VARCHAR'
    # Removing only the new detail reproduces the old comparison's other type behavior.
    after['contract']['enforced'] = True
    assert compare_manifests(manifest(before), manifest(after)).to_dict()['changes'] == [changed_type]


def test_enforcement_change_keeps_alias_and_unknown_type_behavior(manifest):
    for raw in ['INTEGER', 'custom_type']:
        before, after = node('INT' if raw == 'INTEGER' else raw), node(raw, enforced=False)
        report = compare_manifests(manifest(before), manifest(after)).to_dict()
        assert_removal(report['changes'][0])
        after['contract']['enforced'] = True
        original = compare_manifests(manifest(before), manifest(after)).to_dict()
        assert report['changes'][1:] == original['changes']
        assert report['warnings'] == original['warnings'] and report['equivalents'] == original['equivalents']


@pytest.mark.parametrize('mode', ['removed', 'disabled', 'added', 'enforcement_added'])
def test_other_model_lifecycle_findings_do_not_gain_removal_details(manifest, mode):
    before, after = node(), node()
    if mode == 'disabled':
        after['config']['enabled'] = False
    a = manifest(nodes={}) if mode == 'added' else manifest(before)
    b = manifest(nodes={}) if mode == 'removed' else manifest(after)
    if mode == 'enforcement_added':
        a = manifest(node(enforced=False))
    change, = compare_manifests(a, b).changes
    assert change.kind == ('model_' + mode if mode != 'enforcement_added' else mode)
    if mode == 'enforcement_added':
        detail, = change.details
        assert detail['rule'] == 'enforcement_added'
    elif mode == 'removed':
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
            'path_segments': [], 'rule': 'enforcement_removed', 'severity': severity}]})
