"""Rejected seed directories identify their YAML entry without echoing its value."""
import json

import pytest
import yaml

from datawrench.seedtypes.check import check_project
from datawrench.seedtypes.csvio import SeedtypesError
from .test_cli_check import cli


ESCAPE = 'escapes the project directory'
SYMLINK = 'symlinks are unsupported'
DIRECTORY = 'expected a directory'


def rejected_path(root, kind):
    private = 'PRIVATE_PATH_東京\nSEGMENT'
    if kind == 'parent':
        return '../' + private, ESCAPE
    if kind == 'absolute-external':
        return str(root.parent / private), ESCAPE
    if kind == 'absolute-internal':
        return str(root / private), ESCAPE
    if kind == 'parent-contained':
        (root / 'nested').mkdir()
        return 'nested/../' + private, ESCAPE
    if kind == 'file':
        (root / private).write_text('PRIVATE_FILE_CONTENT', encoding='utf-8')
        return private, DIRECTORY
    external = kind in ('symlink-external', 'dangling-external')
    target = (root.parent if external else root) / 'PRIVATE_TARGET'
    if not kind.startswith('dangling'):
        target.mkdir()
        (target / 'child').mkdir()
    (root / private).symlink_to(target, target_is_directory=True)
    item = private + '/child' if kind == 'symlink-ancestor' else private
    # Resolution containment has always preceded the explicit symlink test.
    return item, ESCAPE if external else SYMLINK


def write_project(root, paths):
    source = root / 'dbt_project.yml'
    source.write_text(yaml.safe_dump({'name': 'example', 'seed-paths': paths,
                                     'PRIVATE_YAML_KEY': 'PRIVATE_YAML_VALUE'}, allow_unicode=True),
                      encoding='utf-8')
    return source


@pytest.mark.parametrize('kind', ['parent', 'symlink-internal', 'file'])
@pytest.mark.parametrize('validate', [False, True])
def test_cli_rejected_seed_directory_has_private_indexed_origin(tmp_path, monkeypatch, kind, validate):
    item, guidance = rejected_path(tmp_path, kind)
    source = write_project(tmp_path, ['missing', item])
    monkeypatch.chdir(tmp_path)
    args = ['check', '.', '--format', 'json', '--show-examples']
    if validate:
        args.append('--validate-column-types')
    result = cli(*args)
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr == f'datawrench seedtypes: {source}: seed-paths[1]: {guidance}\n'


KINDS = ['parent', 'absolute-external', 'absolute-internal', 'parent-contained',
         'symlink-internal', 'symlink-external', 'symlink-ancestor',
         'dangling-internal', 'dangling-external', 'file']


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('validate', [False, True])
@pytest.mark.parametrize('show_examples', [False, True])
def test_cli_path_diagnostics_and_validation_order(tmp_path, monkeypatch, kind, validate, show_examples):
    root = tmp_path / 'project'
    root.mkdir()
    item, guidance = rejected_path(root, kind)
    earlier = root / 'earlier'
    earlier.mkdir()
    # A previous valid root must not cause YAML or CSV processing before this error.
    (earlier / 'properties.yml').write_text('PRIVATE_KEY: *PRIVATE_ANCHOR\n')
    (earlier / 'codes.csv').write_text('zip\nPRIVATE_CSV_FIELD,extra\n')
    prefix = ['missing', 'earlier'] if show_examples else []
    source = write_project(root, prefix + [item, '../LATER_REJECTED_PATH'])
    monkeypatch.chdir(tmp_path if show_examples else root)
    args = ['check', root.name if show_examples else '.',
            '--format', 'text' if show_examples else 'json']
    if validate:
        args.append('--validate-column-types')
    if show_examples:
        args.append('--show-examples')
    result = cli(*args)
    assert result.returncode == 2
    assert result.stdout == ''
    # Equality excludes rejected strings (including newline/Unicode), targets,
    # YAML snippets, CSV fields and tracebacks in both output formats.
    assert result.stderr == f'datawrench seedtypes: {source}: seed-paths[{len(prefix)}]: {guidance}\n'


@pytest.mark.parametrize('validate', [False, True])
def test_default_seed_path_symlink_has_index_zero(tmp_path, validate):
    (tmp_path / 'target').mkdir()
    (tmp_path / 'seeds').symlink_to(tmp_path / 'target', target_is_directory=True)
    source = tmp_path / 'dbt_project.yml'
    source.write_text('name: example\n')
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path, validate_column_types=validate)
    assert str(error.value) == f'{source}: seed-paths[0]: {SYMLINK}'


@pytest.mark.parametrize('document,guidance', [
    ('name: null\nseed-paths: [../PRIVATE_PATH]\n', 'a static project name is required'),
    ('name: example\nseed-paths: [../PRIVATE_PATH, 17]\n',
     'seed-paths must be a list of local relative directories'),
    ('name: example\nseed-paths: [../PRIVATE_PATH]\nPRIVATE_KEY: *PRIVATE_ANCHOR\n',
     'YAML aliases are not supported'),
])
@pytest.mark.parametrize('validate', [False, True])
def test_loading_name_and_whole_list_shape_still_precede_path_checks(tmp_path, document, guidance, validate):
    source = tmp_path / 'dbt_project.yml'
    source.write_text(document)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path, validate_column_types=validate)
    assert str(error.value) == f'{source}: {guidance}'


@pytest.mark.parametrize('validate', [False, True])
@pytest.mark.parametrize('show_examples', [False, True])
def test_cli_unicode_and_missing_directories_remain_accepted(tmp_path, monkeypatch, validate, show_examples):
    directory = tmp_path / 'données' / '東京'
    directory.mkdir(parents=True)
    (directory / 'codes.csv').write_text('zip\n東京_café_😀\n', encoding='utf-8')
    (directory / 'properties.yaml').write_text(
        'seeds: [{name: codes, config: {column_types: {zip: VARCHAR}}}]\n')
    write_project(tmp_path, ['absent', 'données/./東京', 'other/missing'])
    monkeypatch.chdir(tmp_path.parent)
    args = ['check', tmp_path.name, '--format', 'json']
    if validate:
        args.append('--validate-column-types')
    if show_examples:
        args.append('--show-examples')
    result = cli(*args)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ''
    report = json.loads(result.stdout)
    assert report['seeds_checked'] == 1
    assert report['issues'] == []
    if validate:
        assert report['column_type_failures'] == []


@pytest.mark.parametrize('validate', [False, True])
def test_only_missing_directories_still_make_an_empty_project(tmp_path, validate):
    write_project(tmp_path, ['missing', 'absent/nested'])
    report = check_project(tmp_path, validate_column_types=validate)
    assert report['seeds_checked'] == 0
    assert report['issues'] == []
