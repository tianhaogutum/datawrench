"""Static project field diagnostics carry safe, absolute YAML provenance."""
import pytest

from .test_cli_check import cli


NAME_GUIDANCE = 'a static project name is required'
PATHS_GUIDANCE = 'seed-paths must be a list of local relative directories'


@pytest.mark.parametrize('document,guidance', [
    ('PRIVATE_KEY: PRIVATE_VALUE\n', NAME_GUIDANCE),
    ('name: example\nseed-paths: {PRIVATE_KEY: PRIVATE_VALUE}\n', PATHS_GUIDANCE),
])
def test_cli_project_shape_has_absolute_origin(tmp_path, monkeypatch, document, guidance):
    source = tmp_path / 'dbt_project.yml'
    source.write_text(document, encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    result = cli('check', '.', '--validate-column-types', '--show-examples', '--format', 'json')
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr == f'datawrench seedtypes: {source}: {guidance}\n'


# Keep these as YAML text so absent, null, quoted-empty and scalar types remain distinct.
BAD_SHAPES = [
    pytest.param('PRIVATE_KEY: PRIVATE_VALUE\n', NAME_GUIDANCE, id='absent-name'),
    pytest.param('name: ""\n', NAME_GUIDANCE, id='empty-name'),
    pytest.param('name:\n', NAME_GUIDANCE, id='implicit-null-name'),
    pytest.param('name: null\n', NAME_GUIDANCE, id='null-name'),
    pytest.param('name: 17\n', NAME_GUIDANCE, id='integer-name'),
    pytest.param('name: false\n', NAME_GUIDANCE, id='boolean-name'),
    pytest.param('name: [PRIVATE_VALUE]\n', NAME_GUIDANCE, id='list-name'),
    pytest.param('name: {PRIVATE_KEY: PRIVATE_VALUE}\n', NAME_GUIDANCE, id='mapping-name'),
    pytest.param('name: 2024-02-29\n', NAME_GUIDANCE, id='date-name'),
    pytest.param('name: example\nseed-paths: PRIVATE_VALUE\n', PATHS_GUIDANCE, id='scalar-paths'),
    pytest.param('name: example\nseed-paths: null\n', PATHS_GUIDANCE, id='null-paths'),
    pytest.param('name: example\nseed-paths: {PRIVATE_KEY: PRIVATE_VALUE}\n', PATHS_GUIDANCE, id='mapping-paths'),
    pytest.param('name: example\nseed-paths: [seeds, 17]\n', PATHS_GUIDANCE, id='integer-member'),
    pytest.param('name: example\nseed-paths: [seeds, false]\n', PATHS_GUIDANCE, id='boolean-member'),
    pytest.param('name: example\nseed-paths: [seeds, null]\n', PATHS_GUIDANCE, id='null-member'),
    pytest.param('name: example\nseed-paths: [seeds, {PRIVATE_KEY: PRIVATE_VALUE}]\n', PATHS_GUIDANCE, id='mapping-member'),
    pytest.param('name: example\nseed-paths: [seeds, [PRIVATE_VALUE]]\n', PATHS_GUIDANCE, id='list-member'),
]


def shape_project(root, document, with_csv):
    source = root / 'dbt_project.yml'
    source.write_text(document + 'PRIVATE_KEY_OTHER: PRIVATE_CONFIG_VALUE\n', encoding='utf-8')
    seeds = root / 'seeds'
    seeds.mkdir()
    # Shape validation must still precede properties loading and CSV parsing.
    (seeds / 'properties.yml').write_text('PRIVATE_KEY: *PRIVATE_ANCHOR\n')
    if with_csv:
        (seeds / 'codes.csv').write_text('zip\nPRIVATE_CSV_FIELD,extra\n')
    return source


@pytest.mark.parametrize('document,guidance', BAD_SHAPES)
@pytest.mark.parametrize('with_csv', [False, True])
@pytest.mark.parametrize('validate', [False, True])
def test_project_shape_api_relative_origin(tmp_path, monkeypatch, document, guidance, with_csv, validate):
    from datawrench.seedtypes.check import check_project
    from datawrench.seedtypes.csvio import SeedtypesError

    source = shape_project(tmp_path, document, with_csv)
    monkeypatch.chdir(tmp_path.parent)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path.name, validate_column_types=validate)
    assert str(error.value) == f'{source}: {guidance}'


@pytest.mark.parametrize('document,guidance', BAD_SHAPES)
@pytest.mark.parametrize('with_csv', [False, True])
@pytest.mark.parametrize('validate', [False, True])
@pytest.mark.parametrize('show_examples', [False, True])
def test_cli_project_shape_is_static_and_private(tmp_path, monkeypatch, document, guidance,
                                                with_csv, validate, show_examples):
    source = shape_project(tmp_path, document, with_csv)
    monkeypatch.chdir(tmp_path)
    args = ['check', '.', '--format', 'text' if show_examples else 'json']
    if validate:
        args.append('--validate-column-types')
    if show_examples:
        args.append('--show-examples')
    result = cli(*args)
    assert result.returncode == 2
    assert result.stdout == ''
    # Exact equality excludes keys/values, snippets, anchors, CSV data and tracebacks.
    assert result.stderr == f'datawrench seedtypes: {source}: {guidance}\n'


@pytest.mark.parametrize('fragment,guidance', [
    (b'# \xff\n', 'expected UTF-8 encoded YAML (optional BOM)'),
    (b'PRIVATE_KEY: "{{ PRIVATE_VALUE }}"\n',
     'Jinja is outside the static check boundary; nothing was evaluated'),
    (b'PRIVATE_KEY: *PRIVATE_ANCHOR\n', 'YAML aliases are not supported'),
    (b'PRIVATE_KEY: 1\nPRIVATE_KEY: 2\n', 'YAML keys must be unique scalars'),
    (b'PRIVATE_KEY: [PRIVATE_VALUE, }\n', 'invalid or excessively nested YAML at line 3, column 30'),
])
@pytest.mark.parametrize('validate', [False, True])
def test_project_loading_still_precedes_shape_validation(tmp_path, fragment, guidance, validate):
    from datawrench.seedtypes.check import check_project
    from datawrench.seedtypes.csvio import SeedtypesError

    source = tmp_path / 'dbt_project.yml'
    source.write_bytes(b'name: null\nseed-paths: null\n' + fragment)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path, validate_column_types=validate)
    assert str(error.value) == f'{source}: {guidance}'


@pytest.mark.parametrize('validate', [False, True])
def test_project_name_still_precedes_path_shape_and_root_config(tmp_path, validate):
    from datawrench.seedtypes.check import check_project
    from datawrench.seedtypes.csvio import SeedtypesError

    source = shape_project(tmp_path, 'name: null\nseed-paths: null\nseeds: null\n', True)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path, validate_column_types=validate)
    assert str(error.value) == f'{source}: {NAME_GUIDANCE}'


@pytest.mark.parametrize('name,paths,directory,expected_seeds', [
    pytest.param('example', None, 'seeds', 1, id='default-paths'),
    pytest.param('東京_café_😀', ['données/東京'], 'données/東京', 1, id='unicode'),
    pytest.param(' ', ['seeds/./nested'], 'seeds/nested', 1, id='existing-whitespace-name'),
    pytest.param('123', [], 'seeds', 0, id='empty-path-list'),
    pytest.param('example', [''], 'seeds', 1, id='empty-path-string'),
    pytest.param('example', ['.'], 'seeds', 1, id='project-root-path'),
])
@pytest.mark.parametrize('with_csv', [False, True])
@pytest.mark.parametrize('validate', [False, True])
def test_accepted_project_names_and_paths_remain_unchanged(tmp_path, monkeypatch, name, paths,
                                                          directory, expected_seeds, with_csv, validate):
    import yaml
    from datawrench.seedtypes.check import check_project

    project = {'name': name}
    if paths is not None:
        project['seed-paths'] = paths
    (tmp_path / 'dbt_project.yml').write_text(yaml.safe_dump(project, allow_unicode=True), encoding='utf-8')
    seeds = tmp_path / directory
    seeds.mkdir(parents=True)
    (seeds / 'properties.yaml').write_text(
        'seeds: [{name: codes, config: {column_types: {zip: VARCHAR}}}]\n')
    if with_csv:
        (seeds / 'codes.csv').write_text('zip\n東京_café_😀\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    report = check_project('.', validate_column_types=validate, show_examples=True)
    assert report['project'] == str(tmp_path)
    assert report['seeds_checked'] == (expected_seeds if with_csv else 0)
    assert report['issues'] == []
    assert ('column_type_failures' in report) == validate
    if validate:
        assert report['column_type_failures'] == []
        for seed in report['seeds']:
            assert seed['column_type_validation']['valid'] is True
