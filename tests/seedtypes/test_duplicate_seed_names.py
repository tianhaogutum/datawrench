"""Duplicate dbt seeds identify every conflicting filename before reading data."""
import json

import pytest

from datawrench.seedtypes.check import check_project
from datawrench.seedtypes.csvio import SeedtypesError
from .test_cli_check import cli


GUIDANCE = 'duplicate dbt seed names across seed paths are unsupported'


def write_project(root, paths, files, *, config=None):
    root.mkdir(exist_ok=True)
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately malformed data must never be read for duplicate names.
        path.write_text('zip\nPRIVATE_CSV_FIELD,extra\n', encoding='utf-8')
    (root / 'dbt_project.yml').write_text(json.dumps(
        {'name': 'example', 'seed-paths': paths, 'seeds': config or {}},
        ensure_ascii=False), encoding='utf-8')


@pytest.mark.parametrize('validate', [False, True])
def test_cli_duplicate_names_identify_conflicting_csvs(tmp_path, monkeypatch, validate):
    write_project(tmp_path, ['b', 'a'], ['b/same.csv', 'a/same.csv'])
    monkeypatch.chdir(tmp_path)
    args = ['check', '.', '--format', 'json', '--show-examples']
    if validate:
        args.append('--validate-column-types')
    result = cli(*args)
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr == (f'datawrench seedtypes: {GUIDANCE}: '
                             f"'same': {str(tmp_path / 'a/same.csv')!r}, "
                             f"{str(tmp_path / 'b/same.csv')!r}\n")


@pytest.mark.parametrize('layout', ['roots', 'nested', 'multiple'])
@pytest.mark.parametrize('validate', [False, True])
@pytest.mark.parametrize('show_examples', [False, True])
def test_cli_all_collisions_are_sorted_and_private(tmp_path, monkeypatch, layout, validate, show_examples):
    root = tmp_path / 'projet_東京'
    if layout == 'roots':
        paths = ['z', 'a']
        names = ['z/café.csv', 'a/café.CSV']
    elif layout == 'nested':
        paths = ['seeds']
        names = ['seeds/z/東京.csv', 'seeds/a/東京.csv', 'seeds/m/東京.csv']
    else:
        paths = ['z', 'a', 'm']
        names = ['z/東京.csv', 'm/東京.csv', 'a/東京.csv',
                 'z/café.csv', 'a/café.csv']
    # Reversed creation and root order must not affect diagnostic ordering.
    write_project(root, paths, names + [f'{paths[0]}/unique.csv'],
                  config={'+column_types': {'zip': 'PRIVATE_INVALID_TYPE'}})
    (root / paths[0] / 'properties.yaml').write_text('PRIVATE_KEY: *PRIVATE_ANCHOR\n')
    monkeypatch.chdir(tmp_path)
    args = ['check', root.name, '--format', 'text' if show_examples else 'json']
    if validate:
        args.append('--validate-column-types')
    if show_examples:
        args.append('--show-examples')
    result = cli(*args)
    assert result.returncode == 2
    assert result.stdout == ''
    if layout == 'roots':
        details = f"'café': {str(root / 'a/café.CSV')!r}, {str(root / 'z/café.csv')!r}"
    elif layout == 'nested':
        details = (f"'東京': {str(root / 'seeds/a/東京.csv')!r}, "
                   f"{str(root / 'seeds/m/東京.csv')!r}, {str(root / 'seeds/z/東京.csv')!r}")
    else:
        details = (f"'café': {str(root / 'a/café.csv')!r}, {str(root / 'z/café.csv')!r}; "
                   f"'東京': {str(root / 'a/東京.csv')!r}, {str(root / 'm/東京.csv')!r}, "
                   f"{str(root / 'z/東京.csv')!r}")
    # Exact equality excludes unique filenames, field data, YAML snippets,
    # invalid type strings and tracebacks, even with --show-examples.
    assert result.stderr == f'datawrench seedtypes: {GUIDANCE}: {details}\n'


@pytest.mark.parametrize('validate', [False, True])
def test_duplicate_check_precedes_properties_types_and_csv(tmp_path, monkeypatch, validate):
    import datawrench.seedtypes.check as module

    write_project(tmp_path, ['seeds'], ['seeds/a/same.csv', 'seeds/b/same.csv'])
    original_load = module.load_yaml

    def project_only(path):
        assert path == tmp_path / 'dbt_project.yml', 'properties must not be loaded'
        return original_load(path)

    def unexpected(*args, **kwargs):
        pytest.fail('type validation or CSV processing ran before duplicate detection')

    (tmp_path / 'seeds/properties.yml').write_text('seeds: []\n')
    monkeypatch.setattr(module, 'load_yaml', project_only)
    for name in ('column_types', 'validate_requested_types', 'CSVSource', 'compare', 'validate_columns'):
        monkeypatch.setattr(module, name, unexpected)
    with pytest.raises(SeedtypesError, match=GUIDANCE):
        check_project(tmp_path, validate_column_types=validate, show_examples=True)


@pytest.mark.parametrize('validate', [False, True])
def test_filename_control_characters_are_escaped(tmp_path, validate):
    stem = "café\n'quoted"
    names = [f'a/{stem}.csv', f'b/{stem}.csv']
    write_project(tmp_path, ['b', 'a'], names)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path, validate_column_types=validate)
    assert str(error.value) == (f'{GUIDANCE}: {stem!r}: '
                                f'{str(tmp_path / names[0])!r}, {str(tmp_path / names[1])!r}')
    assert '\n' not in str(error.value)


@pytest.mark.parametrize('validate', [False, True])
@pytest.mark.parametrize('show_examples', [False, True])
def test_distinct_case_sensitive_stems_remain_accepted(tmp_path, monkeypatch, validate, show_examples):
    names = ['a/Same.csv', 'b/nested/same.CSV', 'b/café.csv', 'a/nested/東京.csv']
    write_project(tmp_path, ['b', 'missing', 'a'], names,
                  config={'+column_types': {'zip': 'VARCHAR'}})
    for name in names:
        (tmp_path / name).write_text('zip\n007\n')
    # Static dbt scanning still ignores TSV files, even with a colliding stem.
    (tmp_path / 'a/same.tsv').write_text('PRIVATE_MALFORMED_TSV')
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
    assert report['seeds_checked'] == 4
    assert {seed['path'] for seed in report['seeds']} == {str(tmp_path / n) for n in names}
    assert report['issues'] == []
    if validate:
        assert report['column_type_failures'] == []


@pytest.mark.parametrize('validate', [False, True])
def test_path_validation_still_precedes_duplicates(tmp_path, validate):
    write_project(tmp_path, ['seeds'], ['seeds/a/same.csv', 'seeds/b/same.csv'])
    (tmp_path / 'seeds/b/same.csv').unlink()
    (tmp_path / 'seeds/b/same.csv').symlink_to(tmp_path / 'seeds/a/same.csv')
    with pytest.raises(SeedtypesError, match='no symlinks'):
        check_project(tmp_path, validate_column_types=validate)
