import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from datawrench.seedtypes.check import check_project
from datawrench.seedtypes.csvio import SeedtypesError, discover
from datawrench.seedtypes.core import infer
from datawrench.seedtypes.emit import emit

ROOT = Path(__file__).resolve().parents[2]


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'seedtypes', *map(str, args)],
                          capture_output=True, text=True, timeout=30)


def project(tmp_path):
    target = tmp_path / 'project'
    shutil.copytree(ROOT / 'examples' / 'seedtypes' / 'dbt_project', target)
    return target


def test_cli_infer_and_emit(tmp_path):
    result = cli('infer', ROOT / 'examples' / 'seedtypes' / 'seeds', '--format', 'json')
    assert result.returncode == 0, result.stderr
    files = json.loads(result.stdout)
    assert len(files) == 2
    assert files[1]['columns'][0]['type'] == 'VARCHAR'
    generated = cli('emit', ROOT / 'examples' / 'seedtypes' / 'seeds' / 'zip_codes.csv', '--target', 'dbt', '--adapter', 'snowflake')
    assert generated.returncode == 0, generated.stderr
    assert yaml.safe_load(generated.stdout)['seeds'][0]['config']['column_types']['zip'] == 'varchar'


def test_check_exit_codes_and_generated_properties(tmp_path):
    root = project(tmp_path)
    result = cli('check', root, '--format', 'json')
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report['seeds_checked'] == 1
    assert any(i['column'] == 'zip' for i in report['issues'])
    result = infer(root / 'seeds' / 'zip_codes.csv')
    (root / 'seeds' / 'properties.yml').write_text(emit([result], 'dbt'))
    result = cli('check', root)
    assert result.returncode == 0, result.stderr
    assert '0 unpinned risks' in result.stdout


@pytest.mark.parametrize('level', ['root', 'project', 'folder', 'seed'])
def test_inherited_project_configuration(tmp_path, level):
    root = project(tmp_path)
    seeds = root / 'seeds'
    folder = seeds / 'nested'
    folder.mkdir()
    (seeds / 'zip_codes.csv').rename(folder / 'zip_codes.csv')
    config = {'+column_types': {'zip': 'varchar'}}
    nodes = ['example', 'nested', 'zip_codes']
    depth = ['root', 'project', 'folder', 'seed'].index(level)
    for key in reversed(nodes[:depth]):
        config = {key: config}
    doc = {'name': 'example', 'seed-paths': ['seeds'], 'seeds': config}
    (root / 'dbt_project.yml').write_text(yaml.safe_dump(doc))
    result = check_project(root)
    assert result['issues'] == []
    assert result['seeds'][0]['explicit_columns'] == ['zip']


def test_existing_explicit_types_are_presence_checks(tmp_path):
    root = project(tmp_path)
    # A deliberate explicit decision is not validated as a warehouse cast.
    (root / 'seeds' / 'properties.yml').write_text('seeds:\n  - name: zip_codes\n    config:\n      column_types: {zip: bigint}\n')
    assert not check_project(root)['issues']


@pytest.mark.parametrize('config,fragment', [
    ('name: example\nseed-paths: [../outside]\n', 'escapes'),
    ('name: example\nseed-paths: /tmp\n', 'list'),
    ('name: example\nname: other\n', 'unique'),
    ('name: example\nseeds: &x {example: *x}\n', 'aliases'),
    ('name: "{{ run_query(\'drop table secrets\') }}"\n', 'Jinja'),
    ('name: !!python/object/apply:os.system ["touch pwned"]\n', 'invalid'),
    ('name: example\nseeds: {example: {zip_codes: {+column_types: {ZIP: varchar}}}}\n', 'not in CSV'),
])
def test_static_config_failure_boundaries(tmp_path, config, fragment):
    root = project(tmp_path)
    (root / 'dbt_project.yml').write_text(config)
    result = cli('check', root)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert fragment in result.stderr
    assert not (root / 'pwned').exists()
    assert not (ROOT / 'pwned').exists()


def test_symlink_seed_path_is_rejected(tmp_path):
    root = project(tmp_path)
    (root / 'linked').symlink_to(root / 'seeds', target_is_directory=True)
    (root / 'dbt_project.yml').write_text('name: example\nseed-paths: [linked]\n')
    with pytest.raises(SeedtypesError, match='symlinks'):
        check_project(root)


@pytest.mark.parametrize('data,error', [(b'a,b\n1\n', 'line 2'), (b'a\n"bad\n', 'line 2'), (b'a\n\xff\n', 'non-UTF-8')])
def test_cli_input_errors(tmp_path, data, error):
    path = tmp_path / 'bad.csv'
    path.write_bytes(data)
    result = cli('infer', path)
    assert result.returncode == 2
    assert str(path) in result.stderr
    assert error in result.stderr
    assert 'Traceback' not in result.stderr


def test_unknown_adapter():
    result = cli('emit', ROOT / 'examples' / 'seedtypes' / 'seeds', '--target', 'dbt', '--adapter', 'oracle')
    assert result.returncode == 2
    for name in ('duckdb', 'postgres', 'snowflake', 'bigquery', 'redshift', 'databricks'):
        assert name in result.stderr


def test_emit_limits(tmp_path):
    empty = tmp_path / 'empty.csv'
    empty.write_text('')
    assert cli('emit', empty, '--target', 'duckdb').returncode == 2
    assert cli('emit', ROOT / 'examples' / 'seedtypes' / 'seeds', '--target', 'polars').returncode == 2
    result = cli('emit', ROOT / 'examples' / 'seedtypes' / 'seeds', '--target', 'dbt', '--null-token', 'NULL')
    assert result.returncode == 2
    assert 'cannot encode custom null tokens' in result.stderr


def test_duplicate_dbt_seed_names(tmp_path):
    for directory in ('a', 'b'):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 'same.csv').write_text('zip\n007\n')
    results = [infer(p) for p in discover([str(tmp_path)])]
    with pytest.raises(SeedtypesError, match='unique'):
        emit(results, 'dbt')


def test_no_csv_and_symlink_and_globs(tmp_path):
    with pytest.raises(SeedtypesError, match='no CSV'):
        discover([str(tmp_path)])
    real = tmp_path / 'real.csv'
    real.write_text('zip\n007\n')
    link = tmp_path / 'link.csv'
    link.symlink_to(real)
    with pytest.raises(SeedtypesError, match='no symlinks'):
        infer(link)
    wildcard = tmp_path / 'data[1].csv'
    wildcard.write_text('a\n1\n')
    with pytest.raises(SeedtypesError, match='glob'):
        infer(wildcard)


def test_compare_engine_error_exit_1(tmp_path):
    path = tmp_path / 'late.csv'
    path.write_text('id\n' + '123\n' * 25000 + 'oops\n')
    result = cli('compare', path, '--format', 'json')
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)['engines']['polars']['status'] == 'ERROR'


def test_empty_project_and_overlapping_paths(tmp_path):
    (tmp_path / 'dbt_project.yml').write_text('name: empty\n')
    assert check_project(tmp_path)['seeds_checked'] == 0
    (tmp_path / 'seeds').mkdir()
    (tmp_path / 'seeds' / 'nested').mkdir()
    (tmp_path / 'dbt_project.yml').write_text('name: empty\nseed-paths: [seeds, seeds/nested]\n')
    with pytest.raises(SeedtypesError, match='overlapping'):
        check_project(tmp_path)


def test_symlinked_parent_is_rejected(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    (real / 'data.csv').write_text('x\n007\n')
    link = tmp_path / 'linked'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SeedtypesError, match='no symlinks'):
        infer(link / 'data.csv')
