"""Opt-in checks validate effective declarations without changing presence-only checks."""
import csv
import json
from pathlib import Path

import pytest
import yaml

from datawrench.seedtypes.check import check_project
from datawrench.seedtypes.csvio import SeedtypesError
from .test_cli_check import cli


def project(root, data='zip\n007\n', types=None):
    seeds = root / 'seeds'
    seeds.mkdir()
    (seeds / 'codes.csv').write_text(data)
    (root / 'dbt_project.yml').write_text(yaml.safe_dump({
        'name': 'example', 'seeds': {'+column_types': types or {'zip': 'BIGINT'}}}))
    return root


def test_default_presence_then_opt_in_exact_round_trip(tmp_path):
    root = project(tmp_path)
    default = cli('check', root, '--format', 'json')
    assert default.returncode == 0, default.stderr
    before = json.loads(default.stdout)
    assert before['issues'] == []
    assert 'column_type_failures' not in before
    assert 'column_type_validation' not in before['seeds'][0]
    checked = cli('check', root, '--validate-column-types', '--format', 'json')
    assert checked.returncode == 1, checked.stderr
    report = json.loads(checked.stdout)
    assert report['issues'] == [] and len(report['column_type_failures']) == 1
    col = report['seeds'][0]['column_type_validation']['columns'][0]
    assert col == {'name': 'zip', 'requested_type': 'BIGINT', 'checked': 1,
                   'non_null': 1, 'nulls': 0, 'valid': False,
                   'source': str(root / 'dbt_project.yml'), 'config_path': "seeds['+column_types']",
                   'rejection': {'line': 2, 'code': 'leading_zeros',
                                 'reason': 'leading zeros would be removed'}}
    (root / 'dbt_project.yml').write_text('name: example\nseeds:\n  +column_types: {zip: VARCHAR}\n')
    good = cli('check', root, '--validate-column-types', '--format', 'json')
    assert good.returncode == 0, good.stderr
    assert json.loads(good.stdout)['seeds'][0]['column_type_validation']['valid'] is True


def test_inheritance_overrides_and_winning_provenance(tmp_path):
    root = project(tmp_path, 'zip,amount,note\n007,1.10,hello\n',
                   {'zip': 'BIGINT', 'amount': 'DECIMAL(3,2)', 'note': 'BIGINT'})
    nested = root / 'seeds' / 'nested'
    nested.mkdir()
    (root / 'seeds' / 'codes.csv').rename(nested / 'codes.csv')
    config = {'+column_types': {'zip': 'BIGINT', 'amount': 'DECIMAL(3,2)', 'note': 'BIGINT'},
              'example': {'+column_types': {'note': 'DATE'}, 'nested': {
                  'column_types': {'zip': 'BIGINT', 'amount': 'DECIMAL(2,1)'},
                  '+column_types': {'zip': 'VARCHAR'},
                  'codes': {'column_types': {'note': 'BOOLEAN'}}}}}
    (root / 'dbt_project.yml').write_text(yaml.safe_dump({'name': 'example', 'seeds': config}))
    source = nested / 'properties.yaml'
    source.write_text('seeds:\n- name: codes\n  config:\n    column_types: {note: VARCHAR}\n')
    report = check_project(root, validate_column_types=True)
    assert report['issues'] == report['column_type_failures'] == []
    columns = report['seeds'][0]['column_type_validation']['columns']
    assert [c['requested_type'] for c in columns] == ['VARCHAR', 'DECIMAL(3,2)', 'VARCHAR']
    assert [c['config_path'] for c in columns] == [
        "seeds['example']['nested']['+column_types']", "seeds['+column_types']",
        "seed 'codes', seeds[0].config['column_types']"]
    assert [c['source'] for c in columns] == [str(root / 'dbt_project.yml')] * 2 + [str(source)]


@pytest.mark.parametrize('dtype,field,valid,code', [
    ('BIGINT', '-0', False, 'integer_negative_zero'),
    ('BIGINT', '9223372036854775808', False, 'integer_range'),
    ('DECIMAL(3,2)', '1.10', True, None),
    ('DECIMAL(3,2)', '10.00', False, 'decimal_precision'),
    ('DECIMAL(3,2)', '1.1', False, 'decimal_scale'),
    ('DECIMAL(3,2)', '-0.00', False, 'decimal_negative_zero'),
    ('DECIMAL(2,2)', '0.10', False, 'round_trip'),
    ('BOOLEAN', 'true', True, None), ('BOOLEAN', 'TRUE', False, 'round_trip'),
    ('DATE', '2024-02-29', True, None), ('DATE', '2023-02-29', False, 'invalid_calendar'),
    ('TIMESTAMP', '2024-02-29 01:02:03', True, None),
    ('TIMESTAMP', '2024-02-29T01:02:03', False, 'timestamp_notation'),
    ('VARCHAR', '007', True, None),
])
def test_exact_shared_requested_type_rules(tmp_path, dtype, field, valid, code):
    root = project(tmp_path, f'zip\n{field}\n', {'zip': dtype})
    result = check_project(root, validate_column_types=True)
    col = result['seeds'][0]['column_type_validation']['columns'][0]
    assert col['valid'] is valid
    assert bool(result['column_type_failures']) is not valid
    assert (col['rejection'] or {}).get('code') == code


@pytest.mark.parametrize('dtype', ['varchar', 'INT', 'DOUBLE', 'VARCHAR(5)', 'BIGINT ',
                                 'DECIMAL(39,0)', 'DECIMAL(2,3)', 'DECIMAL(03,2)',
                                 'DECIMAL(3, 2)', 'DECIMAL(٣,٢)',
                                 'BIGINT); SELECT PRIVATE_SQL; --'])
def test_unsupported_declarations_are_private_configuration_errors(tmp_path, dtype):
    root = project(tmp_path, 'zip\nPRIVATE_FIELD\n', {'zip': dtype})
    # Default remains a presence decision, even for warehouse aliases/expressions.
    assert check_project(root)['issues'] == []
    result = cli('check', root, '--validate-column-types', '--show-examples', '--format', 'json')
    assert result.returncode == 2 and result.stdout == ''
    assert str(root / 'dbt_project.yml') + ": seeds['+column_types']: requested types must be" in result.stderr
    assert 'PRIVATE_' not in result.stderr and 'Traceback' not in result.stderr


@pytest.mark.parametrize('location', ['root-empty', 'root-overridden', 'project', 'directory',
                                     'seed', 'properties', 'unmatched-properties'])
def test_all_selected_declarations_validated_before_engines(tmp_path, monkeypatch, location):
    import datawrench.seedtypes.check as module
    root = project(tmp_path)
    config = {'+column_types': {'zip': 'VARCHAR'}, 'example': {'nested': {'codes': {}}}}
    nested = root / 'seeds' / 'nested'
    nested.mkdir()
    (root / 'seeds' / 'codes.csv').rename(nested / 'codes.csv')
    source = root / 'dbt_project.yml'
    if location in ['properties', 'unmatched-properties']:
        name = 'codes' if location == 'properties' else 'unused'
        source = root / 'seeds' / 'properties.yml'
        source.write_text(yaml.safe_dump({'seeds': [{'name': name, 'config': {
            'column_types': {'PRIVATE_KEY': 'PRIVATE_SQL'}}}]}))
    else:
        node = config
        for key in {'root-empty': [], 'root-overridden': [], 'project': ['example'],
                    'directory': ['example', 'nested'], 'seed': ['example', 'nested', 'codes']}[location]:
            node = node[key]
        node['+column_types'] = {'zip': 'PRIVATE_SQL'}
        if location == 'root-overridden':
            config['example']['+column_types'] = {'zip': 'VARCHAR'}
        if location == 'root-empty':
            (nested / 'codes.csv').unlink()
    (root / 'dbt_project.yml').write_text(yaml.safe_dump({'name': 'example', 'seeds': config}))

    def no_engine(*a, **kw):
        pytest.fail('engine must not run before all configuration is validated')

    monkeypatch.setattr(module, 'compare', no_engine)
    monkeypatch.setattr(module, 'validate_columns', no_engine)
    with pytest.raises(SeedtypesError) as exc:
        check_project(root, validate_column_types=True)
    assert str(source) in str(exc.value)
    assert 'PRIVATE_' not in str(exc.value)


@pytest.mark.parametrize('bad_yaml', ['x: !!int PRIVATE_SQL\n', 'x: [PRIVATE_SQL, }\n',
                                    'x: 2026-99-99\n', 'x: *PRIVATE_ALIAS\n'])
def test_complete_yaml_load_precedes_optional_semantics_and_csv(tmp_path, bad_yaml):
    root = project(tmp_path, 'zip\n"malformed\n', {'zip': 'PRIVATE_SQL'})
    (root / 'seeds' / 'a_first.yml').write_text(
        'seeds: [{name: unused, config: {column_types: {zip: PRIVATE_SQL}}}]\n')
    source = root / 'seeds' / 'z_last.yaml'
    source.write_text(bad_yaml)
    checked = cli('check', root, '--validate-column-types', '--show-examples')
    assert checked.returncode == 2 and checked.stdout == ''
    assert str(source) in checked.stderr and 'PRIVATE_' not in checked.stderr


def test_mixed_configured_delimiters_all_seed_paths(tmp_path):
    seeds = tmp_path / 'seeds'
    seeds.mkdir()
    (tmp_path / 'other').mkdir()
    (seeds / 'nested').mkdir()
    (tmp_path / 'dbt_project.yml').write_text(
        'name: example\nseed-paths: [seeds, other]\nseeds:\n'
        '  +delimiter: ";"\n  +column_types: {zip: BIGINT, note: VARCHAR}\n'
        '  example:\n    nested:\n      +delimiter: "\\t"\n'
        '      comma:\n        +delimiter: ","\n')
    (seeds / 'properties.yml').write_text(
        'seeds:\n- name: property\n  config:\n    delimiter: ";"\n    column_types: {zip: VARCHAR}\n')
    cases = [(seeds / 'root.csv', ';'), (seeds / 'nested' / 'tab.csv', '\t'),
             (seeds / 'nested' / 'comma.csv', ','), (seeds / 'nested' / 'property.csv', ';'),
             (tmp_path / 'other' / 'extra.csv', ';')]
    for path, delimiter in cases:
        with path.open('w', newline='') as stream:
            csv.writer(stream, delimiter=delimiter).writerows([
                ['zip', 'note'], ['12', f'quoted{delimiter}field\nnext"line'], ['007', 'end']])
    report = check_project(tmp_path, validate_column_types=True)
    assert report['seeds_checked'] == 5 and len(report['column_type_failures']) == 4
    for seed in report['seeds']:
        validation = seed['column_type_validation']
        assert validation['delimiter'] == dict((str(p), d) for p, d in cases)[seed['path']]
        assert seed['comparison']['inference']['delimiter'] == validation['delimiter']
        col = validation['columns'][0]
        if Path(seed['path']).stem == 'property':
            assert col['valid'] and col['requested_type'] == 'VARCHAR'
        else:
            assert col['rejection']['line'] == 4


@pytest.mark.parametrize('data,tokens,rows,non_null,nulls,valid', [
    ('zip\n', [], 0, 0, 0, True),
    ('zip\nNULL\n"NULL"\n', ['NULL'], 2, 0, 2, True),
    ('zip\n""\n', [], 1, 1, 0, False),
    ('zip\n""\n', [''], 1, 0, 1, True),
])
def test_null_and_header_only_policy(tmp_path, data, tokens, rows, non_null, nulls, valid):
    root = project(tmp_path, data)
    options = sum([['--null-token', t] for t in tokens], [])
    result = cli('check', root, '--validate-column-types', '--format', 'json', *options)
    assert result.returncode == int(not valid), result.stderr
    report = json.loads(result.stdout)
    validation = report['seeds'][0]['column_type_validation']
    col = validation['columns'][0]
    assert (validation['rows'], col['checked'], col['non_null'], col['nulls']) == (rows, rows, non_null, nulls)
    assert bool(validation['notices']) == (non_null == 0)
    assert validation['null_tokens'] == tokens


def test_late_failure_and_full_counts(tmp_path):
    root = project(tmp_path, 'zip,note\n12,"two\nlines"\n' + '12,ok\n' * 21000
                   + '007,end\n-0,end\n8,end\n', {'zip': 'BIGINT', 'note': 'VARCHAR'})
    result = check_project(root, validate_column_types=True)
    col = result['seeds'][0]['column_type_validation']['columns'][0]
    assert col['checked'] == col['non_null'] == 21004
    assert col['rejection']['line'] == 21004 and col['rejection']['code'] == 'leading_zeros'


@pytest.mark.parametrize('tail', ['one\n', '"malformed\n'])
def test_malformed_trailing_csv_is_atomic(tmp_path, tail):
    root = project(tmp_path, 'zip,note\n007,"two\nlines"\n' + '12,ok\n' * 1026 + tail,
                   {'zip': 'BIGINT'})
    result = cli('check', root, '--validate-column-types', '--show-examples', '--format', 'json')
    assert result.returncode == 2 and result.stdout == ''
    assert 'line 1030' in result.stderr and '007' not in result.stderr and 'Traceback' not in result.stderr


@pytest.mark.parametrize('typed', [False, True])
def test_untyped_risks_distinct_from_typed_failures(tmp_path, typed):
    root = project(tmp_path, 'zip,other\n007,008\n', {'zip': 'BIGINT' if typed else 'VARCHAR'})
    default = check_project(root)
    report = check_project(root, validate_column_types=True)
    assert report['issues'] == default['issues']
    assert all(i['column'] == 'other' for i in report['issues']) and report['issues']
    assert [c['column'] for c in report['column_type_failures']] == (['zip'] if typed else [])
    result = cli('check', root, '--validate-column-types')
    assert result.returncode == 1 and 'unpinned risks' in result.stdout
    assert f'{int(typed)} explicit column type failures' in result.stdout


@pytest.mark.parametrize('form', ['text', 'json'])
def test_evidence_opt_in_and_bounded(tmp_path, form):
    secret = 'PRIVATE_FIELD_' + 'x' * 300
    root = project(tmp_path, f'zip\n{secret}\nSECOND_PRIVATE\n')
    options = ['check', root, '--validate-column-types', '--format', form]
    private = cli(*options)
    assert private.returncode == 1 and 'PRIVATE' not in private.stdout + private.stderr
    public = cli(*options, '--show-examples')
    assert public.returncode == 1 and 'PRIVATE_FIELD' in public.stdout
    assert secret not in public.stdout and 'SECOND_PRIVATE' not in public.stdout
    if form == 'json':
        assert len(json.loads(public.stdout)['column_type_failures'][0]['rejection']['value']) == 160


def test_in_memory_api_whitelist_and_no_temporary_types_files(tmp_path, monkeypatch):
    import datawrench.seedtypes.validate as module
    root = project(tmp_path)

    def no_file(*a, **kw):
        pytest.fail('check must consume effective mapping without a JSON types document')

    monkeypatch.setattr(module, 'load_types', no_file)
    assert check_project(root, validate_column_types=True)['column_type_failures']
    with pytest.raises(SeedtypesError, match='requested types must be'):
        module.validate_columns(root / 'seeds' / 'codes.csv', {'zip': 'BIGINT); SELECT 1; --'})


@pytest.mark.parametrize('data', ['zip\n007\n', 'zip\n', ''])
def test_no_explicit_columns_retains_untyped_check(tmp_path, data):
    root = project(tmp_path, data)
    (root / 'dbt_project.yml').write_text('name: example\n')
    before = check_project(root)
    after = check_project(root, validate_column_types=True)
    assert after['issues'] == before['issues']
    assert after['column_type_failures'] == []
    validation = after['seeds'][0]['column_type_validation']
    assert validation['valid'] and validation['columns'] == []


@pytest.mark.parametrize('data', ['', 'other\n007\n'])
def test_explicit_types_require_exact_headers(tmp_path, data):
    root = project(tmp_path, data)
    result = cli('check', root, '--validate-column-types')
    assert result.returncode == 2 and result.stdout == '' and 'not in CSV header' in result.stderr


def test_empty_project_and_null_option_boundaries(tmp_path):
    (tmp_path / 'dbt_project.yml').write_text('name: example\n')
    result = cli('check', tmp_path, '--validate-column-types', '--format', 'json')
    assert result.returncode == 0
    assert json.loads(result.stdout)['column_type_failures'] == []
    assert cli('check', tmp_path, '--null-token', 'NULL').returncode == 2
    for options in [['--null-token', 'x' * 1025],
                    sum([['--null-token', str(i)] for i in range(65)], [])]:
        bad = cli('check', tmp_path, '--validate-column-types', *options)
        assert bad.returncode == 2 and bad.stdout == ''
        assert 'at most 64 null tokens' in bad.stderr


def test_selected_spelling_and_unused_project_scope(tmp_path):
    root = project(tmp_path)
    (root / 'dbt_project.yml').write_text(
        'name: example\nseeds:\n  column_types: {zip: PRIVATE_SQL}\n'
        '  +column_types: {zip: VARCHAR}\n  unused: {+column_types: {zip: PRIVATE_SQL}}\n')
    assert not check_project(root, validate_column_types=True)['column_type_failures']


def test_input_must_remain_stable_between_comparison_and_validation(tmp_path, monkeypatch):
    import datawrench.seedtypes.check as module
    root = project(tmp_path)
    original = module.compare

    def changing(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        path.write_text('zip\n7\n')
        return result

    monkeypatch.setattr(module, 'compare', changing)
    with pytest.raises(SeedtypesError, match='input changed during scan'):
        check_project(root, validate_column_types=True)
