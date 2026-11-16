import pytest

from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import InputError, Manifest
from .conftest import artifact, node


def test_fixture_end_to_end():
    from pathlib import Path
    from datawrench.contract_diff.manifest import load_manifest
    root = Path(__file__).parent / 'fixtures'
    result = compare_manifests(load_manifest(root / 'base/manifest.json'), load_manifest(root / 'head/manifest.json'))
    assert result.models_compared == 1
    assert len(result.changes) == 10
    assert result.breaking_count == 6
    assert len(result.equivalents) == 1
    assert result.equivalents[0]['column'] == 'score'
    assert any('probable rename' in n for n in result.notices)
    assert not result.warnings


def test_no_contracts_and_opt_in(manifest):
    a, b = node(enforced=False), node('VARCHAR', enforced=False)
    result = compare_manifests(manifest(a), manifest(b))
    assert not result.changes and result.notices and result.exit_code('breaking') == 0
    assert compare_manifests(manifest(a), manifest(b), include_unenforced=True).breaking_count == 1


@pytest.mark.parametrize('mode,kind', [('removed', 'model_removed'), ('disabled', 'model_disabled'), ('config_disabled', 'model_disabled')])
def test_model_lifecycle(manifest, mode, kind):
    old = manifest()
    if mode == 'config_disabled':
        n = node()
        n['config']['enabled'] = False
        new = manifest(n)
    elif mode == 'disabled':
        new = manifest(nodes={}, disabled={'model.shop.orders': [node()]})
    else:
        new = manifest(nodes={})
    result = compare_manifests(old, new)
    assert result.changes[0].kind == kind and result.changes[0].breaking
    added = compare_manifests(new, old)
    assert added.changes[0].kind == 'model_added' and not added.breaking_count


def test_enforcement_changes(manifest):
    yes, no = manifest(node()), manifest(node(enforced=False))
    result = compare_manifests(yes, no)
    assert result.changes[0].kind == 'enforcement_removed' and result.breaking_count == 1
    result = compare_manifests(no, yes)
    assert result.changes[0].kind == 'enforcement_added' and not result.breaking_count


@pytest.mark.parametrize('scope', ['column', 'model'])
@pytest.mark.parametrize('constraint', [
    {'type': 'not_null'}, {'type': 'primary_key', 'columns': ['id']},
    {'type': 'unique', 'columns': ['id']}, {'type': 'check', 'expression': 'id > 0'},
    {'type': 'foreign_key', 'columns': ['id'], 'to': 'ref_table', 'to_columns': ['id']},
])
def test_constraint_add_remove(manifest, scope, constraint):
    a, b = node(), node()
    target = b['columns']['id'] if scope == 'column' else b
    target['constraints'] = [constraint]
    add = compare_manifests(manifest(a), manifest(b))
    assert add.changes[0].kind == 'constraint_added' and not add.breaking_count
    remove = compare_manifests(manifest(b), manifest(a))
    assert remove.changes[0].kind == 'constraint_removed' and remove.breaking_count == 1
    detail, = remove.changes[0].details
    assert detail['rule'] == 'constraint_guarantee_removed' and detail['certainty'] == 'known'
    assert detail['before'] == constraint and detail['after'] is None
    assert detail['reason'] and detail['remediation']


def test_constraint_semantics_and_metadata(manifest):
    a, b = node(), node()
    a['constraints'] = [{'type': 'check', 'expression': "id != 'a b'", 'name': 'old', 'warn_unenforced': True}]
    b['constraints'] = [{'type': 'check', 'expression': "id != 'a b'", 'name': 'new', 'warn_unenforced': False}]
    assert not compare_manifests(manifest(a), manifest(b)).changes
    b['constraints'][0]['expression'] = "id != 'a  b'"
    result = compare_manifests(manifest(a), manifest(b))
    assert len(result.changes) == 2 and result.breaking_count == 1


def test_fk_mapping_order_is_significant(manifest):
    a, b = node(), node()
    a['constraints'] = [{'type': 'foreign_key', 'columns': ['a', 'b'], 'to': 't', 'to_columns': ['x', 'y']}]
    b['constraints'] = [{'type': 'foreign_key', 'columns': ['a', 'b'], 'to': 't', 'to_columns': ['y', 'x']}]
    result = compare_manifests(manifest(a), manifest(b))
    assert len(result.changes) == 2 and result.breaking_count == 1


def versioned(dtype, version, latest):
    n = node(dtype, version=version, latest=latest)
    n['unique_id'] = f'model.shop.orders.v{version}'
    return n


def test_existing_version_breaking_requires_bump(manifest):
    a, b = versioned('INT', 1, 1), versioned('VARCHAR', 1, 1)
    report = compare_manifests(manifest(a), manifest(b))
    assert report.breaking_count == 1
    assert 'breaking change without version bump (version 1)' in report.changes[0].message


def test_new_version_alongside_old_allowed_and_latest_reported_once(manifest):
    old = versioned('INT', 1, 1)
    v1, v2 = versioned('INT', 1, 2), versioned('VARCHAR', 2, 2)
    report = compare_manifests(manifest(old), manifest(nodes={x['unique_id']: x for x in (v1, v2)}))
    assert not report.breaking_count
    assert [x.kind for x in report.changes].count('latest_version_changed') == 1
    assert [x.kind for x in report.changes].count('model_added') == 1


def test_new_version_does_not_excuse_old_removal_or_old_break(manifest):
    a = versioned('INT', 1, 1)
    b = versioned('VARCHAR', 2, 2)
    result = compare_manifests(manifest(a), manifest(b))
    assert result.breaking_count == 1
    assert 'without version bump' in next(x.message for x in result.changes if x.breaking)
    bad_old = versioned('VARCHAR', 1, 2)
    result = compare_manifests(manifest(a), manifest(nodes={x['unique_id']: x for x in (bad_old, b)}))
    assert result.breaking_count == 1


def test_cannot_change_version_under_same_id(manifest):
    a, b = node(version=1), node(version=2)
    result = compare_manifests(manifest(a), manifest(b))
    assert result.changes[0].kind == 'model_identity_changed' and result.breaking_count == 1


def test_conflicting_latest_versions_rejected(manifest):
    a, b = versioned('INT', 1, 1), versioned('INT', 2, 2)
    bad = manifest(nodes={x['unique_id']: x for x in (a, b)})
    with pytest.raises(InputError, match='inconsistent latest_version'):
        compare_manifests(manifest(a), bad)


def test_latest_version_metadata_removed(manifest):
    result = compare_manifests(manifest(node(version=1, latest=1)), manifest(node(version=1)))
    assert result.changes[0].kind == 'latest_version_changed'
    assert result.changes[0].after is None


def test_project_adapter_mismatch_warning_and_override():
    a = artifact(adapter='duckdb')
    b = artifact(adapter='bigquery')
    b['metadata']['project_id'] = 'elsewhere'
    b['metadata']['project_name'] = 'other'
    a['metadata']['project_name'] = 'shop'
    result = compare_manifests(Manifest.from_dict(a), Manifest.from_dict(b))
    assert len(result.warnings) == 3 and result.dialect == 'bigquery'
    assert compare_manifests(Manifest.from_dict(a), Manifest.from_dict(b), dialect='postgres').dialect == 'postgres'


def test_missing_adapter_and_spark(manifest):
    a = artifact()
    a['metadata'] = {}
    report = compare_manifests(Manifest.from_dict(a), Manifest.from_dict(a))
    assert report.dialect == 'duckdb' and report.warnings
    assert compare_manifests(manifest(adapter='spark'), manifest(adapter='spark')).dialect == 'databricks'


def test_unknown_types_are_findings_even_when_equal(manifest):
    a = manifest(node('custom_type'))
    result = compare_manifests(a, a)
    assert result.changes[0].kind == 'type_unparseable'
    assert not result.breaking_count and result.exit_code('any') == 1 and result.warnings


def test_ambiguous_rename_not_claimed(manifest):
    a, b = node(), node()
    a['columns'] = {k: {'data_type': 'INT'} for k in ('a', 'b')}
    b['columns'] = {k: {'data_type': 'INT'} for k in ('c', 'd')}
    result = compare_manifests(manifest(a), manifest(b))
    assert result.breaking_count == 2 and not result.notices


def test_config_contract_supported(manifest):
    n = node()
    del n['contract']
    n['config']['contract'] = {'enforced': True}
    assert compare_manifests(manifest(n), manifest(n)).models_compared == 1


def test_strict_widening_and_fail_modes(manifest):
    a, b = manifest(), manifest(node('BIGINT'))
    result = compare_manifests(a, b)
    assert result.exit_code('breaking') == 0 and result.exit_code('any') == 1 and result.exit_code('none') == 0
    result = compare_manifests(a, b, strict_widening=True)
    assert result.breaking_count == 1 and result.exit_code('breaking') == 1


def test_unparseable_added_model_and_column_warn(manifest):
    added_model = compare_manifests(manifest(nodes={}), manifest(node('custom_type')))
    assert not added_model.breaking_count and added_model.warnings
    assert added_model.changes[0].kind == 'model_added'
    a, b = node(), node()
    b['columns']['new'] = {'data_type': 'custom_type'}
    added_column = compare_manifests(manifest(a), manifest(b))
    assert not added_column.breaking_count and added_column.warnings
    assert added_column.changes[0].kind == 'column_added'
