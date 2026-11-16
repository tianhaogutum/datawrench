"""Exact opt-in comparison scope; loading and validation remain artifact-wide."""
from copy import deepcopy
import json

import pytest

from datawrench.contract_diff.cli import main, render
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import InputError, Manifest
from datawrench.contract_diff.policy import ProjectPolicy
from .conftest import artifact, node

ORDERS = 'model.shop.orders'
CUSTOMERS = 'model.shop.customers'


def model(key, dtype='INT', **kwargs):
    n = node(dtype, **kwargs)
    n.update(unique_id=key, name=key.split('.')[2])
    return n


def manifest(*nodes, **kwargs):
    return Manifest.from_dict(artifact(nodes={n['unique_id']: n for n in nodes}, **kwargs))


def pair():
    old = [model(k) for k in (ORDERS, CUSTOMERS)]
    new = [model(k, 'VARCHAR') for k in (ORDERS, CUSTOMERS)]
    for n in old:
        n['columns']['alias'] = {'data_type': 'INT'}
    for n in new:
        n['columns']['alias'] = {'data_type': 'INTEGER'}
    return manifest(*old), manifest(*new)


def cli(tmp_path, capsys, base, head, *args):
    paths = [tmp_path / name for name in ('base.json', 'head.json')]
    for path, data in zip(paths, (base, head)):
        path.write_text(json.dumps(data))
    code = main(['--base', str(paths[0]), '--head', str(paths[1]), *args])
    output = capsys.readouterr()
    return code, output.out, output.err


def test_cli_only_requested_model_contributes(tmp_path, capsys):
    base = artifact(nodes={k: model(k) for k in (ORDERS, CUSTOMERS)})
    head = artifact(nodes={k: model(k, 'VARCHAR') for k in (ORDERS, CUSTOMERS)})
    code, out, err = cli(tmp_path, capsys, base, head, '--model', ORDERS, '--format', 'json')
    assert code == 1 and not err
    report = json.loads(out)
    assert report['selected_models'] == [ORDERS]
    assert report['models_compared'] == 1 and report['summary']['changes'] == 1
    assert {c['model'] for c in report['changes']} == {ORDERS}


def test_changes_equivalents_and_counts_scoped():
    base, head = pair()
    report = compare_manifests(base, head, selected_models=[ORDERS])
    assert report.models_compared == 1 and report.breaking_count == 1
    assert {c.model for c in report.changes} == {ORDERS}
    assert [e['model'] for e in report.equivalents] == [ORDERS]
    assert report.to_dict()['selected_models'] == [ORDERS]


def test_duplicates_order_and_no_option_schema():
    base, head = pair()
    first = compare_manifests(base, head, selected_models=[ORDERS, CUSTOMERS, ORDERS])
    second = compare_manifests(base, head, selected_models=[CUSTOMERS, ORDERS])
    assert first.to_dict() == second.to_dict()
    assert first.to_dict()['selected_models'] == [CUSTOMERS, ORDERS]
    assert [c.model for c in first.changes] == [CUSTOMERS, ORDERS]
    assert 'selected_models' not in compare_manifests(base, head).to_dict()
    # Explicit empty API selection compares nothing; CLI cannot request this.
    empty = compare_manifests(base, head, selected_models=[])
    assert empty.models_compared == 0 and empty.to_dict()['selected_models'] == []


@pytest.mark.parametrize('key', ['orders', 'model.shop.Orders', 'model.shop.*', ORDERS + ' ', '', 'source.shop.orders'])
def test_unknown_is_input_error(key):
    base, head = pair()
    with pytest.raises(InputError, match='unknown selected model unique_id'):
        compare_manifests(base, head, selected_models=[ORDERS, key])


@pytest.mark.parametrize('which', ['base', 'head'])
@pytest.mark.parametrize('damage', ['dtype', 'constraint', 'enforcement', 'identity', 'latest'])
def test_unselected_consumed_fields_still_validated(tmp_path, capsys, which, damage):
    base = artifact(nodes={ORDERS: model(ORDERS), CUSTOMERS: model(CUSTOMERS)})
    head = deepcopy(base)
    bad = (base if which == 'base' else head)['nodes'][CUSTOMERS]
    if damage == 'dtype':
        bad['columns']['id']['data_type'] = 42
    elif damage == 'constraint':
        bad['constraints'] = [{'type': 'custom'}]
    elif damage == 'enforcement':
        bad['config']['contract'] = {'enforced': False}
    elif damage == 'identity':
        bad['unique_id'] = ORDERS
    else:
        bad['latest_version'] = 1
        sibling = model(CUSTOMERS + '.v2', version=2, latest=2)
        (base if which == 'base' else head)['nodes'][sibling['unique_id']] = sibling
    code, out, err = cli(tmp_path, capsys, base, head, '--model', ORDERS)
    assert code == 2 and not out and 'input error:' in err


@pytest.mark.parametrize('kind', ['added', 'removed', 'disabled', 'config_disabled', 'enforcement_removed', 'enforcement_added'])
def test_selected_lifecycle(kind):
    old, new = model(ORDERS), model(ORDERS)
    base, head = manifest(old), manifest(new)
    expected = kind
    if kind == 'added':
        base = manifest()
        expected = 'model_added'
    elif kind == 'removed':
        head = manifest()
        expected = 'model_removed'
    elif kind == 'disabled':
        head = manifest(disabled={ORDERS: [new]})
        expected = 'model_disabled'
    elif kind == 'config_disabled':
        new['config']['enabled'] = False
        head = manifest(new)
        expected = 'model_disabled'
    elif kind == 'enforcement_removed':
        new['contract']['enforced'] = False
        head = manifest(new)
    else:
        old['contract']['enforced'] = False
        base = manifest(old)
    report = compare_manifests(base, head, selected_models=[ORDERS])
    assert report.models_compared == 1
    change, = report.changes
    assert change.kind == expected and change.model == ORDERS
    if kind == 'removed':
        assert change.details[0]['rule'] == 'model_removed'
        assert change.details[0]['before'] == ORDERS


def test_selection_does_not_include_unenforced_and_disabled_only_is_known():
    base = manifest(model(ORDERS, enforced=False), model(CUSTOMERS))
    head = manifest(model(ORDERS, 'VARCHAR', enforced=False), model(CUSTOMERS, 'VARCHAR'))
    report = compare_manifests(base, head, selected_models=[ORDERS])
    assert report.models_compared == 0 and not report.changes
    assert report.to_dict()['selected_models'] == [ORDERS]
    assert any('selected scope' in n for n in report.notices)
    assert compare_manifests(base, head, selected_models=[ORDERS], include_unenforced=True).breaking_count == 1
    disabled = manifest(disabled={ORDERS: [model(ORDERS)]})
    assert compare_manifests(disabled, manifest(), selected_models=[ORDERS]).models_compared == 0
    n = model(ORDERS)
    n['config']['enabled'] = False
    assert compare_manifests(manifest(n), manifest(), selected_models=[ORDERS]).models_compared == 0


def test_latest_uses_selected_active_records_not_sibling_keys():
    v1, v2 = ORDERS + '.v1', ORDERS + '.v2'
    base = manifest(model(v1, version=1, latest=1), model(v2, version=2, latest=1),
                    model(CUSTOMERS, latest=1))
    head = manifest(model(v1, version=1, latest=2), model(v2, version=2, latest=2),
                    model(CUSTOMERS, latest=2))
    report = compare_manifests(base, head, selected_models=[v2])
    latest, = report.changes
    assert latest.kind == 'latest_version_changed' and latest.model == v2
    assert latest.before == 1 and latest.after == 2 and report.models_compared == 1
    both = compare_manifests(base, head, selected_models=[v2, v1])
    assert len(both.changes) == 1 and both.changes[0].model == v2  # legacy last sorted representative
    # A selected record without latest metadata cannot inherit a sibling's change.
    for m in (base, head):
        m.models[v2]['latest_version'] = None
    assert not compare_manifests(base, head, selected_models=[v2]).changes


def test_full_family_validation_before_empty_enforcement_scope():
    a = model(ORDERS, enforced=False)
    b = model(CUSTOMERS, latest=1)
    c = model(CUSTOMERS + '.v2', latest=2)
    with pytest.raises(InputError, match='inconsistent latest_version'):
        compare_manifests(manifest(a, b, c), manifest(a), selected_models=[ORDERS])


def test_policy_is_comparison_local(tmp_path, capsys):
    rules = [{'model': k, 'column': 'id', 'path_segments': [], 'rule': 'scalar_incompatible',
              'severity': 'non_breaking'} for k in (ORDERS, CUSTOMERS)]
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': rules})
    base, head = pair()
    report = compare_manifests(base, head, selected_models=[ORDERS], policy=policy)
    assert report.breaking_count == 0 and len(report.changes) == 1
    assert [d['model'] for d in report.policy_diagnostics] == [CUSTOMERS]
    assert not compare_manifests(base, head, policy=policy).policy_diagnostics
    assert [d['model'] for d in compare_manifests(base, head, selected_models=[ORDERS], policy=policy).policy_diagnostics] == [CUSTOMERS]
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'version': 1, 'rules': rules}))
    a = artifact(nodes={k: model(k) for k in (ORDERS, CUSTOMERS)})
    b = artifact(nodes={k: model(k, 'VARCHAR') for k in (ORDERS, CUSTOMERS)})
    code, out, err = cli(tmp_path, capsys, a, b, '--model', ORDERS, '--policy', str(path), '--fail-on', 'none', '--fail-on-unused-policy', '--format', 'json')
    assert code == 1 and not err and json.loads(out)['policy_diagnostics'][0]['model'] == CUSTOMERS


@pytest.mark.parametrize('fmt', ['text', 'json', 'markdown', 'github'])
def test_unknown_cli_no_report(tmp_path, capsys, fmt):
    code, out, err = cli(tmp_path, capsys, artifact(), artifact(), '--model', ORDERS, '--model', 'model.shop.Orders', '--format', fmt)
    assert code == 2 and not out and 'unknown selected model unique_id' in err


@pytest.mark.parametrize('fmt', ['text', 'markdown', 'github'])
def test_scope_is_explained_and_escaped(fmt):
    key = 'model.shop.<orders>|`\n::error::x%\x1b'
    report = compare_manifests(manifest(model(key)), manifest(model(key)), selected_models=[key])
    out = render(report, fmt)
    assert 'Selected models (exact unique_ids):' in out
    assert '\x1b' not in out and '\n::error::x' not in out
    if fmt == 'markdown':
        assert '&lt;orders&gt;&#124;&#96;' in out
    if fmt == 'github':
        assert r'\n::error::x%25' in out
        assert len(out.splitlines()) == 2


def test_unselected_type_warnings_and_rename_notices_do_not_leak():
    a, b = model(CUSTOMERS, 'opaque_type'), model(CUSTOMERS, 'other_opaque')
    a['columns']['old'] = {'data_type': 'INT'}
    b['columns']['new'] = {'data_type': 'INT'}
    report = compare_manifests(manifest(model(ORDERS), a), manifest(model(ORDERS), b), selected_models=[ORDERS])
    assert report.models_compared == 1 and not report.changes
    assert not report.warnings and not report.notices
    assert all(report.exit_code(mode) == 0 for mode in ('breaking', 'any', 'none'))


def test_artifact_metadata_warnings_and_dialect_remain_global():
    base = manifest(model(ORDERS), adapter='duckdb')
    head = manifest(model(ORDERS), adapter='postgres')
    report = compare_manifests(base, head, selected_models=[ORDERS])
    assert report.dialect == 'postgres' and any('adapter_type' in w for w in report.warnings)
    assert not report.changes


def test_exact_distinct_case_and_literal_dot_keys():
    keys = [ORDERS, 'model.shop.Orders', 'model.shop.orders.more']
    base = manifest(*(model(k) for k in keys))
    head = manifest(*(model(k, 'VARCHAR') for k in keys))
    for key in keys:
        report = compare_manifests(base, head, selected_models=[key])
        assert report.models_compared == 1 and [c.model for c in report.changes] == [key]


def test_selected_version_removal_is_not_excused_by_unselected_replacement():
    v1, v2 = ORDERS + '.v1', ORDERS + '.v2'
    report = compare_manifests(manifest(model(v1, version=1, latest=1)),
                               manifest(model(v2, version=2, latest=2)), selected_models=[v1])
    assert report.models_compared == 1 and report.breaking_count == 1
    assert {c.model for c in report.changes} == {v1}
    removal = next(c for c in report.changes if c.kind == 'model_removed')
    assert removal.details[0]['rule'] == 'model_removed' and 'without version bump' in removal.message
    latest = next(c for c in report.changes if c.kind == 'latest_version_changed')
    assert latest.before == 1 and latest.after is None
