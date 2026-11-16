"""Exact exclusion is applied after eligibility; input validation stays global."""
from copy import deepcopy
import json

import pytest

from datawrench.contract_diff.cli import render
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import InputError
from datawrench.contract_diff.policy import ProjectPolicy
from .conftest import artifact
from .test_model_selection import ORDERS, CUSTOMERS, model, manifest, cli


def pair():
    return (manifest(model(ORDERS), model(CUSTOMERS, 'BIGINT')),
            manifest(model(ORDERS, 'BIGINT'), model(CUSTOMERS)))


def test_exclusion_contract():
    base, head = pair()
    report = compare_manifests(base, head, excluded_models=[CUSTOMERS])
    d = report.to_dict()
    assert d['excluded_models'] == [CUSTOMERS] and 'selected_models' not in d
    assert d['models_compared'] == 1
    change, = d['changes']
    assert change['model'] == ORDERS and change['kind'] == 'type_widening'
    assert change['details'][0]['rule'] == 'integer_range'
    assert not change['breaking'] and not d['warnings'] and not d['equivalents']
    assert [report.exit_code(g) for g in ('breaking', 'any', 'none')] == [0, 1, 0]
    full = compare_manifests(base, head)
    assert full.models_compared == 2 and full.breaking_count == 1
    assert 'excluded_models' not in full.to_dict()


@pytest.mark.parametrize('selected', [None, [ORDERS, CUSTOMERS, ORDERS], [CUSTOMERS], []])
def test_selection_and_deduplicated_exclusions(selected):
    base, head = pair()
    report = compare_manifests(base, head, selected_models=selected,
                               excluded_models=[ORDERS, CUSTOMERS, ORDERS])
    assert report.to_dict()['excluded_models'] == [CUSTOMERS, ORDERS]
    assert report.selected_models == (None if selected is None else sorted(set(selected)))
    assert report.models_compared == 0 and not report.changes
    assert any('exclusions' in n for n in report.notices)
    assert all(report.exit_code(g) == 0 for g in ('breaking', 'any', 'none'))


@pytest.mark.parametrize('key', ['orders', 'model.shop.Orders', 'model.shop.*', ORDERS + ' ', '', 'source.shop.orders', ORDERS + '.v1'])
def test_unknown_is_error_even_when_everything_selected_is_excluded(key):
    base, head = pair()
    with pytest.raises(InputError, match='unknown excluded model unique_id'):
        compare_manifests(base, head, selected_models=[ORDERS], excluded_models=[ORDERS, key])
    with pytest.raises(InputError, match='unknown selected model unique_id'):
        compare_manifests(base, head, selected_models=[key], excluded_models=[CUSTOMERS])


@pytest.mark.parametrize('representation', ['base-only', 'head-only', 'disabled', 'config-disabled', 'unenforced'])
def test_known_ids_from_either_artifact_and_eligibility(representation):
    base, head = manifest(), manifest()
    if representation == 'base-only':
        base = manifest(model(CUSTOMERS))
    elif representation == 'head-only':
        head = manifest(model(CUSTOMERS, 'opaque'))
    elif representation == 'disabled':
        base = manifest(disabled={CUSTOMERS: [model(CUSTOMERS)]})
    elif representation == 'config-disabled':
        n = model(CUSTOMERS); n['config']['enabled'] = False
        head = manifest(n)
    else:
        base, head = manifest(model(CUSTOMERS, enforced=False)), manifest(model(CUSTOMERS, 'VARCHAR', enforced=False))
    for include in (False, True):
        report = compare_manifests(base, head, include_unenforced=include, excluded_models=[CUSTOMERS])
        assert report.models_compared == 0 and not report.changes and not report.warnings


def test_exact_keys_no_version_expansion_or_case_folding():
    keys = [ORDERS, 'model.shop.Orders', ORDERS + '.v1', ORDERS + '.more', 'literal."a.b"']
    base = manifest(*(model(k) if k != keys[-1] else dict(model(ORDERS), unique_id=k) for k in keys))
    head = deepcopy(base)
    for n in head.models.values():
        n['columns']['id']['data_type'] = 'VARCHAR'
    for excluded in keys:
        report = compare_manifests(base, head, excluded_models=[excluded])
        assert report.models_compared == len(keys) - 1
        assert {c.model for c in report.changes} == set(keys) - {excluded}


def test_no_excluded_type_warnings_equivalents_rename_or_constraint_findings():
    a, b = model(CUSTOMERS, 'opaque_a'), model(CUSTOMERS, 'opaque_b')
    a['columns'].update(old={'data_type': 'INT'}, alias={'data_type': 'INT'})
    b['columns'].update(new={'data_type': 'INT'}, alias={'data_type': 'INTEGER'})
    a['constraints'] = [{'type': 'unique', 'columns': ['id']}]
    report = compare_manifests(manifest(model(ORDERS), a), manifest(model(ORDERS), b), excluded_models=[CUSTOMERS])
    assert report.models_compared == 1
    assert not report.changes and not report.warnings and not report.notices and not report.equivalents


@pytest.mark.parametrize('selected', [None, [ORDERS + '.v1', ORDERS + '.v2']])
def test_latest_only_remaining_eligible_records(selected):
    v1, v2, v3 = [ORDERS + '.v' + str(i) for i in (1, 2, 3)]
    base = manifest(model(v1, version=1, latest=1), model(v2, version=2, latest=1), model(v3, version=3, latest=1, enforced=False))
    head = manifest(model(v1, version=1, latest=2), model(v2, version=2, latest=2), model(v3, version=3, latest=2, enforced=False))
    report = compare_manifests(base, head, selected_models=selected, excluded_models=[v2])
    latest, = report.changes
    assert report.models_compared == 1 and latest.model == v1 and latest.kind == 'latest_version_changed'
    assert latest.details[0]['after']['latest_version'] == 2
    for m in (base, head):
        m.models[v1]['latest_version'] = None
    assert not compare_manifests(base, head, selected_models=selected, excluded_models=[v2]).changes
    # Explicit empty exclusions also request scoped accounting, but change no eligibility.
    assert not compare_manifests(base, head, selected_models=[v1], excluded_models=[]).changes


@pytest.mark.parametrize('which', ['base', 'head'])
@pytest.mark.parametrize('damage', ['dtype', 'constraint', 'enforcement', 'identity', 'family'])
def test_excluded_consumed_fields_still_validated(tmp_path, capsys, which, damage):
    base = artifact(nodes={ORDERS: model(ORDERS), CUSTOMERS: model(CUSTOMERS)})
    head = deepcopy(base)
    data = base if which == 'base' else head
    n = data['nodes'][CUSTOMERS]
    if damage == 'dtype': n['columns']['id']['data_type'] = 42
    elif damage == 'constraint': n['constraints'] = [{'type': 'custom'}]
    elif damage == 'enforcement': n['config']['contract'] = {'enforced': False}
    elif damage == 'identity': n['unique_id'] = ORDERS
    else:
        n['latest_version'] = 1
        data['nodes'][CUSTOMERS + '.v2'] = model(CUSTOMERS + '.v2', latest=2)
    code, out, err = cli(tmp_path, capsys, base, head, '--exclude-model', CUSTOMERS)
    assert code == 2 and not out and 'input error:' in err


def test_family_validation_even_with_empty_eligibility():
    base = manifest(model(ORDERS, latest=1, enforced=False), model(ORDERS + '.v2', latest=2, enforced=False))
    with pytest.raises(InputError, match='inconsistent latest_version'):
        compare_manifests(base, manifest(), excluded_models=[ORDERS])


def test_metadata_and_dialect_remain_global():
    base = manifest(model(CUSTOMERS), adapter='duckdb')
    head = manifest(model(CUSTOMERS), adapter='postgres')
    report = compare_manifests(base, head, excluded_models=[CUSTOMERS])
    assert report.dialect == 'postgres' and any('adapter_type' in w for w in report.warnings)
    assert report.models_compared == 0


def test_policy_matches_are_comparison_local():
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': [
        {'model': k, 'column': 'id', 'path_segments': [], 'rule': 'integer_range', 'severity': 'non_breaking'}
        for k in (ORDERS, CUSTOMERS)]})
    base, head = pair()
    first = compare_manifests(base, head, policy=policy, excluded_models=[CUSTOMERS])
    assert [d['model'] for d in first.policy_diagnostics] == [CUSTOMERS]
    assert first.changes[0].details[0]['policy']['pointer'] == '/rules/0'
    assert not compare_manifests(base, head, policy=policy).policy_diagnostics
    assert first.to_dict() == compare_manifests(base, head, policy=policy, excluded_models=[CUSTOMERS]).to_dict()


@pytest.mark.parametrize('fmt', ['text', 'markdown', 'github'])
def test_exclusion_scope_escaping(fmt):
    key = 'model.shop.<orders>|`\n::error::x%\x1b'
    report = compare_manifests(manifest(model(key)), manifest(model(key)), selected_models=[key], excluded_models=[key])
    out = render(report, fmt)
    assert 'Excluded models (exact unique_ids):' in out and 'Selected models (exact unique_ids):' in out
    assert '\x1b' not in out and '\n::error::x' not in out
    if fmt == 'markdown': assert '&lt;orders&gt;&#124;&#96;' in out
    if fmt == 'github':
        assert r'\n::error::x%25' in out
        assert len(out.splitlines()) == 4  # summary, selection, exclusion, empty-scope notice


@pytest.mark.parametrize('fmt', ['text', 'json', 'markdown', 'github'])
def test_unknown_cli_no_report(tmp_path, capsys, fmt):
    code, out, err = cli(tmp_path, capsys, artifact(), artifact(), '--exclude-model', 'model.shop.Orders', '--format', fmt)
    assert code == 2 and not out and 'unknown excluded model unique_id' in err


@pytest.mark.parametrize('kind', ['model_added', 'model_removed', 'model_disabled', 'enforcement_added', 'enforcement_removed', 'model_identity_changed'])
def test_exclusion_removes_lifecycle_and_preserves_remaining_details(kind):
    a = model(ORDERS); b = model(ORDERS)
    left, right = manifest(a), manifest(b)
    if kind == 'model_added': left = manifest()
    elif kind == 'model_removed': right = manifest()
    elif kind == 'model_disabled': right = manifest(disabled={ORDERS: [b]})
    elif kind == 'enforcement_added':
        a['contract']['enforced'] = False; left = manifest(a)
    elif kind == 'enforcement_removed':
        b['contract']['enforced'] = False; right = manifest(b)
    else:
        b['version'] = 2; right = manifest(b)
    left.models[CUSTOMERS] = model(CUSTOMERS)
    right.models[CUSTOMERS] = model(CUSTOMERS, 'BIGINT')
    full = compare_manifests(left, right)
    retained = compare_manifests(left, right, excluded_models=[CUSTOMERS])
    assert [c for c in full.changes if c.model == ORDERS] == retained.changes
    assert retained.changes[0].details[0]['rule'] == kind
    removed = compare_manifests(left, right, excluded_models=[ORDERS])
    assert [c.model for c in removed.changes] == [CUSTOMERS]
    assert removed.models_compared == 1 and not removed.breaking_count


def test_exclusion_does_not_include_unenforced():
    base = manifest(model(ORDERS, enforced=False), model(CUSTOMERS))
    head = manifest(model(ORDERS, 'BIGINT', enforced=False), model(CUSTOMERS))
    assert compare_manifests(base, head, excluded_models=[CUSTOMERS]).models_compared == 0
    included = compare_manifests(base, head, excluded_models=[CUSTOMERS], include_unenforced=True)
    assert included.models_compared == 1 and included.changes[0].model == ORDERS


def test_empty_exclusion_iterable_preserves_eligibility_but_records_opt_in():
    base, head = pair()
    full = compare_manifests(base, head).to_dict()
    explicit = compare_manifests(base, head, excluded_models=iter([])).to_dict()
    assert explicit.pop('excluded_models') == []
    assert explicit == full
