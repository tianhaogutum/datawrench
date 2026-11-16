"""YAML provenance without widening static column_types validation scope."""
import json

import pytest
import yaml

from datawrench.seedtypes.check import check_project
from datawrench.seedtypes.csvio import SeedtypesError
from .test_cli_check import cli

LOCATIONS = ['root', 'project', 'folder', 'seed', 'properties', 'unused-properties']
TYPE_GUIDANCE = 'column_types must map exact column names to nonempty type strings'
CONFIG_GUIDANCE = 'dbt seed configuration must be a mapping'


def write_project(root, config):
    (root / 'dbt_project.yml').write_text(yaml.safe_dump({'name': 'example', 'seeds': config}))


def invalid_fixture(root, location, invalid_node, *, with_seeds=True):
    seeds = root / 'seeds'
    seeds.mkdir()
    if with_seeds:
        # A completed earlier comparison must not become a partial CLI report.
        (seeds / 'a_valid.csv').write_text('zip\nPRIVATE_CSV_FIELD\n')
        (seeds / 'nested').mkdir()
        (seeds / 'nested' / 'codes.csv').write_text('zip\n007\n')
    source = root / 'dbt_project.yml'
    if location in ('properties', 'unused-properties'):
        seed_name = 'codes' if location == 'properties' else 'missing'
        source = seeds / 'properties.yaml'
        source.write_text(yaml.safe_dump({'seeds': [
            {'name': 'a_valid'}, {'name': seed_name, 'config': invalid_node}]}))
        write_project(root, {})
        config_path = f'seed {seed_name!r}, seeds[1].config'
    else:
        components = ['example', 'nested', 'codes'][:LOCATIONS.index(location)]
        config = invalid_node
        for component in reversed(components):
            config = {component: config}
        write_project(root, config)
        config_path = 'seeds' + ''.join(f'[{part!r}]' for part in components)
    return source, config_path


@pytest.mark.parametrize('location', LOCATIONS)
@pytest.mark.parametrize('key', ['column_types', '+column_types'])
@pytest.mark.parametrize('bad', [None, [], 'PRIVATE_CONFIG_VALUE', {'zip': 1},
                                 {'zip': ''}, {'zip': ' \t'}, {1: 'PRIVATE_CONFIG_VALUE'},
                                 {'zip': None}, {'zip': True}])
def test_invalid_column_types_provenance(tmp_path, location, key, bad):
    source, config_path = invalid_fixture(tmp_path, location, {key: bad})
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{source}: {config_path}[{key!r}]: {TYPE_GUIDANCE}'
    assert 'PRIVATE_CONFIG_VALUE' not in str(error.value)
    assert 'PRIVATE_CSV_FIELD' not in str(error.value)


@pytest.mark.parametrize('location', LOCATIONS)
@pytest.mark.parametrize('bad', [None, [], 'PRIVATE_CONFIG_VALUE', False])
def test_malformed_configuration_provenance(tmp_path, location, bad):
    source, config_path = invalid_fixture(tmp_path, location, bad)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{source}: {config_path}: {CONFIG_GUIDANCE}'


@pytest.mark.parametrize('location', LOCATIONS)
@pytest.mark.parametrize('show_examples', [False, True])
def test_cli_provenance_without_partial_output(tmp_path, location, show_examples):
    # Never echo even a rejected column key. Config path metadata stays useful.
    source, config_path = invalid_fixture(
        tmp_path, location, {'column_types': {'PRIVATE_CONFIG_KEY': ['PRIVATE_CONFIG_VALUE']}})
    args = ['--format', 'json'] if not show_examples else ['--show-examples']
    result = cli('check', tmp_path, *args)
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr == f"datawrench seedtypes: {source}: {config_path}['column_types']: {TYPE_GUIDANCE}\n"
    assert 'PRIVATE_' not in result.stderr


@pytest.mark.parametrize('key', ['column_types', '+column_types'])
@pytest.mark.parametrize('location', ['root', 'unused-properties'])
def test_invalid_types_without_csvs(tmp_path, location, key):
    source, config_path = invalid_fixture(tmp_path, location, {key: None}, with_seeds=False)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{source}: {config_path}[{key!r}]: {TYPE_GUIDANCE}'


@pytest.mark.parametrize('level', ['root', 'project', 'folder'])
@pytest.mark.parametrize('key', ['column_types', '+column_types'])
def test_invalid_ancestor_is_not_hidden_by_deeper_types(tmp_path, level, key):
    seeds = tmp_path / 'seeds' / 'nested'
    seeds.mkdir(parents=True)
    (seeds / 'codes.csv').write_text('zip\n007\n')
    (seeds / 'properties.yml').write_text('seeds:\n- name: codes\n  config:\n    column_types: {zip: varchar}\n')
    config = {key: {'zip': 'varchar'}, 'example': {key: {'zip': 'varchar'}, 'nested': {
        key: {'zip': 'varchar'}, 'codes': {key: {'zip': 'varchar'}}}}}
    components = ['example', 'nested'][:LOCATIONS.index(level)]
    node = config
    for part in components:
        node = node[part]
    node[key] = {'zip': 1}
    write_project(tmp_path, config)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    config_path = 'seeds' + ''.join(f'[{part!r}]' for part in [*components, key])
    assert str(error.value) == f'{tmp_path / "dbt_project.yml"}: {config_path}: {TYPE_GUIDANCE}'


@pytest.mark.parametrize('location', LOCATIONS)
def test_prefixed_types_keep_precedence_and_validation_scope(tmp_path, location):
    invalid_fixture(tmp_path, location, {'column_types': None, '+column_types': {'zip': 'varchar'}})
    # Unlike delimiter, only the selected column_types spelling is validated.
    report = check_project(tmp_path)
    codes = next(s for s in report['seeds'] if s['path'].endswith('codes.csv'))
    assert codes['explicit_columns'] == ([] if location == 'unused-properties' else ['zip'])


@pytest.mark.parametrize('location', LOCATIONS)
def test_invalid_prefixed_types_are_selected(tmp_path, location):
    source, config_path = invalid_fixture(
        tmp_path, location, {'column_types': {'zip': 'varchar'}, '+column_types': None})
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f"{source}: {config_path}['+column_types']: {TYPE_GUIDANCE}"


def test_unused_project_subtrees_are_not_validated(tmp_path):
    (tmp_path / 'seeds').mkdir()
    write_project(tmp_path, {'example': {'column_types': None}, 'unused_project': None})
    assert check_project(tmp_path)['seeds_checked'] == 0
    (tmp_path / 'seeds' / 'codes.csv').write_text('zip\n007\n')
    write_project(tmp_path, {'example': {'column_types': {}, 'unused_folder': None},
                             'unused_project': {'column_types': None}})
    report = check_project(tmp_path)
    assert report['seeds_checked'] == 1
    assert report['issues']
    assert 'PRIVATE_CSV_FIELD' not in json.dumps(report)


def test_empty_absent_and_arbitrary_explicit_types_remain_valid(tmp_path):
    (tmp_path / 'seeds').mkdir()
    write_project(tmp_path, {})
    assert check_project(tmp_path)['seeds_checked'] == 0
    (tmp_path / 'seeds' / 'codes.csv').write_text('zip;note\n007;PRIVATE_CSV_FIELD\n')
    write_project(tmp_path, {'+delimiter': ';', '+column_types': {'note': 'root type'},
                             'example': {'column_types': {}, 'codes': {'column_types': {'zip': 'seed type'}}}})
    props = tmp_path / 'seeds' / 'properties.yml'
    props.write_text('seeds:\n- name: codes\n  config:\n    column_types: {zip: "arbitrary warehouse type"}\n')
    report = check_project(tmp_path)
    assert report['issues'] == []
    assert report['seeds'][0]['explicit_columns'] == ['note', 'zip']
    assert report['seeds'][0]['comparison']['inference']['delimiter'] == ';'
    assert 'PRIVATE_CSV_FIELD' not in json.dumps(report)
