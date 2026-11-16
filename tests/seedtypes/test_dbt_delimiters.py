"""Static dbt delimiter contracts; real comparisons, no dbt/warehouse execution."""
import csv
import json
from pathlib import Path

import pytest
import yaml

from datawrench.seedtypes.check import check_project
from datawrench.seedtypes.core import infer
from datawrench.seedtypes.csvio import SeedtypesError
from datawrench.seedtypes.emit import ADAPTERS, emit
from .test_cli_check import cli

EXAMPLE = Path(__file__).resolve().parents[2] / 'examples' / 'seedtypes' / 'dbt_delimiters'
DELIMITER_GUIDANCE = 'delimiter must be comma (","), tab ("\\t"), or semicolon (";")'


def write_seed(path, delimiter):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(['zip', 'note'])
        writer.writerows([['123', 'quoted,;\t"note\nline'], ['007', 'plain']])
    return path


def write_project(root, config):
    (root / 'dbt_project.yml').write_text(yaml.safe_dump({'name': 'example', 'seeds': config}))


@pytest.mark.parametrize('level', ['root', 'project', 'folder', 'seed', 'properties'])
@pytest.mark.parametrize('key', ['+delimiter', 'delimiter'])
@pytest.mark.parametrize('delimiter', [';', '\t'])
def test_inherited_delimiter(tmp_path, level, key, delimiter):
    path = write_seed(tmp_path / 'seeds' / 'nested' / 'codes.csv', delimiter)
    config = {key: delimiter}
    if level == 'properties':
        # Property delimiter must retain inherited column types.
        props = {'seeds': [{'name': 'codes', 'config': {key: delimiter}}]}
        (path.parent / 'properties.yml').write_text(yaml.safe_dump(props))
        config = {'+column_types': {'note': 'varchar'}}
    else:
        depth = ['root', 'project', 'folder', 'seed'].index(level)
        for component in reversed(['example', 'nested', 'codes'][:depth]):
            config = {component: config}
    write_project(tmp_path, config)
    report = check_project(tmp_path)
    seed = report['seeds'][0]
    inference = seed['comparison']['inference']
    assert inference['delimiter'] == delimiter
    assert [col['name'] for col in inference['columns']] == ['zip', 'note']
    assert [col['type'] for col in inference['columns']] == ['VARCHAR', 'VARCHAR']
    assert inference['columns'][0]['rejected']['BIGINT']['line'] == 4
    assert {(i['column'], i['line']) for i in report['issues']} == {('zip', 4)}
    assert all(not i['examples'] for i in report['issues'])
    if level == 'properties':
        assert seed['explicit_columns'] == ['note']


def test_mixed_project_precedence_and_emitted_properties(tmp_path):
    import shutil
    root = tmp_path / 'project'
    shutil.copytree(EXAMPLE, root)
    response = cli('check', root, '--format', 'json')
    assert response.returncode == 1, response.stderr
    report = json.loads(response.stdout)
    expected = {'root_codes': ';', 'tab_codes': '\t', 'comma_codes': ',', 'property_codes': ';'}
    assert report['seeds_checked'] == 4
    assert len(report['issues']) == 4
    for seed in report['seeds']:
        path = Path(seed['path'])
        inference = seed['comparison']['inference']
        assert inference['delimiter'] == expected[path.stem]
        assert [c['name'] for c in inference['columns']] == ['zip', 'note']
        assert seed['explicit_columns'] == ['note']
        assert inference['columns'][0]['rejected']['BIGINT']['line'] == 4
    inferred = [infer(seed['path'], delimiter=expected[Path(seed['path']).stem]) for seed in report['seeds']]
    # Replace property overrides with generated properties for all seeds.
    (root / 'seeds' / 'properties.yml').write_text(emit(inferred, 'dbt'))
    response = cli('check', root, '--format', 'json')
    assert response.returncode == 0, response.stderr
    checked = json.loads(response.stdout)
    assert checked['issues'] == []
    assert [s['comparison']['inference'] for s in checked['seeds']] == [r.json() for r in inferred]


@pytest.mark.parametrize('adapter', ADAPTERS)
@pytest.mark.parametrize('delimiter', [',', '\t', ';'])
def test_emit_dbt_pins_literal_delimiter(tmp_path, adapter, delimiter):
    result = infer(write_seed(tmp_path / 'codes.csv', delimiter), delimiter=delimiter)
    config = yaml.safe_load(emit([result], 'dbt', adapter))['seeds'][0]['config']
    assert config['delimiter'] == delimiter
    assert set(config['column_types']) == {'zip', 'note'}
    # Even comma output must override an inherited non-comma project default.
    write_project(tmp_path, {'+delimiter': ';'})
    target = write_seed(tmp_path / 'seeds' / 'codes.csv', delimiter)
    (target.parent / 'properties.yml').write_text(emit([result], 'dbt', adapter))
    assert check_project(tmp_path)['issues'] == []


@pytest.mark.parametrize('bad', ['|', '::', '', 'tab', '\\t', '，', None, True, 1, [], {}])
@pytest.mark.parametrize('location', ['root', 'seed', 'properties'])
def test_unsupported_delimiters_fail_without_partial_json(tmp_path, bad, location):
    write_seed(tmp_path / 'seeds' / 'codes.csv', ',')
    config = {'+delimiter': bad}
    if location == 'seed':
        config = {'example': {'codes': config}}
    elif location == 'properties':
        config = {}
        props = {'seeds': [{'name': 'codes', 'config': {'delimiter': bad}}]}
        (tmp_path / 'seeds' / 'properties.yml').write_text(yaml.safe_dump(props))
    write_project(tmp_path, config)
    response = cli('check', tmp_path, '--format', 'json')
    assert response.returncode == 2, response.stdout
    assert response.stdout == ''
    assert 'delimiter' in response.stderr
    assert 'Traceback' not in response.stderr


@pytest.mark.parametrize('location', ['root', 'project', 'folder', 'seed', 'properties', 'unused-properties'])
@pytest.mark.parametrize('key', ['delimiter', '+delimiter'])
@pytest.mark.parametrize('bad', ['|', None, {'PRIVATE_CONFIG_VALUE': 'do not echo'}])
def test_invalid_delimiter_provenance(tmp_path, location, key, bad):
    # The earlier valid seed must never leak a partial report or field evidence.
    first = write_seed(tmp_path / 'seeds' / 'a_valid.csv', ';')
    first.write_text('secret\nPRIVATE_CSV_FIELD\n')
    write_seed(tmp_path / 'seeds' / 'nested' / 'codes.csv', '\t')
    config = {'+delimiter': ';', 'example': {'nested': {'+delimiter': '\t'}}}
    source = tmp_path / 'dbt_project.yml'
    if location in ('properties', 'unused-properties'):
        seed_name = 'codes' if location == 'properties' else 'missing'
        source = tmp_path / 'seeds' / 'properties.yaml'
        props = {'seeds': [{'name': 'a_valid'}, {'name': seed_name, 'config': {key: bad}}]}
        source.write_text(yaml.safe_dump(props))
        setting = f'seed {seed_name!r}, seeds[1].config[{key!r}]'
    else:
        components = ['example', 'nested', 'codes'][:['root', 'project', 'folder', 'seed'].index(location)]
        node = config
        for component in components:
            node = node.setdefault(component, {})
        node[key] = bad
        setting = 'seeds' + ''.join(f'[{part!r}]' for part in [*components, key])
    write_project(tmp_path, config)
    response = cli('check', tmp_path, '--format', 'json')
    assert response.returncode == 2
    assert response.stdout == ''
    assert response.stderr == f'datawrench seedtypes: {source}: {setting}: {DELIMITER_GUIDANCE}\n'
    assert 'PRIVATE_CONFIG_VALUE' not in response.stderr
    assert 'PRIVATE_CSV_FIELD' not in response.stderr


@pytest.mark.parametrize('level', ['root', 'project', 'folder'])
def test_invalid_inherited_delimiter_is_not_hidden_by_override(tmp_path, level):
    path = write_seed(tmp_path / 'seeds' / 'nested' / 'codes.csv', ',')
    config = {'+delimiter': ';', 'example': {'+delimiter': ';', 'nested': {
        '+delimiter': '\t', 'codes': {'+delimiter': ','}}}}
    components = ['example', 'nested'][:['root', 'project', 'folder'].index(level)]
    node = config
    for part in components:
        node = node[part]
    node['+delimiter'] = '|'
    (path.parent / 'properties.yml').write_text('seeds:\n- name: codes\n  config: {delimiter: ","}\n')
    write_project(tmp_path, config)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    setting = 'seeds' + ''.join(f'[{part!r}]' for part in [*components, '+delimiter'])
    assert str(error.value) == f'{tmp_path / "dbt_project.yml"}: {setting}: {DELIMITER_GUIDANCE}'


def test_validate_delimiters_without_matching_seeds(tmp_path):
    write_project(tmp_path, {'+delimiter': '|'})
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f"{tmp_path / 'dbt_project.yml'}: seeds['+delimiter']: {DELIMITER_GUIDANCE}"
    write_project(tmp_path, {})
    (tmp_path / 'seeds').mkdir()
    (tmp_path / 'seeds' / 'properties.yml').write_text('seeds:\n- name: missing\n  config: {delimiter: "|"}\n')
    with pytest.raises(SeedtypesError, match='delimiter'):
        check_project(tmp_path)


def test_noncomma_check_reports_malformed_original_line(tmp_path):
    path = write_seed(tmp_path / 'seeds' / 'codes.csv', ';')
    with path.open('a') as stream:
        stream.write('ragged\n')
    write_project(tmp_path, {'+delimiter': ';'})
    response = cli('check', tmp_path, '--format', 'json')
    assert response.returncode == 2 and response.stdout == ''
    assert f'{path}: line 5: ragged row' in response.stderr


def test_dbt_rejects_tsv_properties_for_undiscoverable_seed(tmp_path):
    path = write_seed(tmp_path / 'codes.tsv', '\t')
    response = cli('emit', path, '--delimiter', 'tab', '--target', 'dbt')
    assert response.returncode == 2 and response.stdout == ''
    assert '.csv' in response.stderr


def test_property_types_preserve_inherited_delimiter_and_types(tmp_path):
    path = write_seed(tmp_path / 'seeds' / 'codes.csv', ';')
    write_project(tmp_path, {'+delimiter': ';', '+column_types': {'note': 'varchar'}})
    props = {'seeds': [{'name': 'codes', 'config': {'column_types': {'zip': 'varchar'}}}]}
    (path.parent / 'properties.yml').write_text(yaml.safe_dump(props))
    report = check_project(tmp_path)
    assert report['issues'] == []
    assert report['seeds'][0]['explicit_columns'] == ['note', 'zip']
    assert report['seeds'][0]['comparison']['inference']['delimiter'] == ';'


@pytest.mark.parametrize('plain,prefixed,expected', [(',', ';', ';'), ('|', ';', None), (';', '|', None)])
def test_both_delimiter_spellings_are_validated(tmp_path, plain, prefixed, expected):
    write_seed(tmp_path / 'seeds' / 'codes.csv', ';')
    write_project(tmp_path, {'delimiter': plain, '+delimiter': prefixed})
    if expected is None:
        with pytest.raises(SeedtypesError) as error:
            check_project(tmp_path)
        key = 'delimiter' if plain == '|' else '+delimiter'
        assert str(error.value) == f'{tmp_path / "dbt_project.yml"}: seeds[{key!r}]: {DELIMITER_GUIDANCE}'
    else:
        report = check_project(tmp_path)
        assert report['seeds'][0]['comparison']['inference']['delimiter'] == expected
