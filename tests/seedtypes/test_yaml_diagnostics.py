"""Static YAML errors identify their file without echoing rejected content."""
from textwrap import indent

import pytest

from datawrench.seedtypes.check import check_project
from datawrench.seedtypes.csvio import SeedtypesError
from .test_cli_check import cli


UTF8_GUIDANCE = 'expected UTF-8 encoded YAML (optional BOM)'


def test_cli_invalid_utf8_project_has_safe_origin(tmp_path):
    source = tmp_path / 'dbt_project.yml'
    source.write_bytes(b'name: example\nPRIVATE_KEY: !PRIVATE_TAG PRIVATE_VALUE\n# \xff\n')
    result = cli('check', tmp_path, '--validate-column-types', '--show-examples', '--format', 'json')
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr == f'datawrench seedtypes: {source}: {UTF8_GUIDANCE}\n'


KEY_GUIDANCE = 'YAML keys must be unique scalars'
ALIAS_GUIDANCE = 'YAML aliases are not supported'
LOCATIONS = ['project', 'properties-yml', 'unmatched-properties-yaml']
BAD_YAML = [
    pytest.param('PRIVATE_KEY: PRIVATE_VALUE\nPRIVATE_KEY: other\n', KEY_GUIDANCE, id='duplicate-string'),
    pytest.param('17: PRIVATE_VALUE\n17: other\n', KEY_GUIDANCE, id='duplicate-int'),
    pytest.param('1.5: PRIVATE_VALUE\n1.5: other\n', KEY_GUIDANCE, id='duplicate-float'),
    pytest.param('1: PRIVATE_VALUE\ntrue: other\n', KEY_GUIDANCE, id='equal-bool-int'),
    pytest.param('1: PRIVATE_VALUE\n1.0: other\n', KEY_GUIDANCE, id='equal-float-int'),
    pytest.param('? [PRIVATE_KEY]\n: PRIVATE_VALUE\n', KEY_GUIDANCE, id='sequence-key'),
    pytest.param('? {PRIVATE_KEY: PRIVATE_VALUE}\n: other\n', KEY_GUIDANCE, id='mapping-key'),
    pytest.param('null: PRIVATE_VALUE\n', KEY_GUIDANCE, id='null-key'),
    pytest.param('first: &PRIVATE_ANCHOR PRIVATE_VALUE\nsecond: *PRIVATE_ANCHOR\n', ALIAS_GUIDANCE, id='alias'),
    pytest.param('first: &PRIVATE_ANCHOR {recursive: *PRIVATE_ANCHOR}\n', ALIAS_GUIDANCE, id='recursive-alias'),
    pytest.param('first: *PRIVATE_ANCHOR\n', ALIAS_GUIDANCE, id='undefined-alias'),
    pytest.param('first: &PRIVATE_ANCHOR {PRIVATE_KEY: PRIVATE_VALUE}\nsecond: {<<: *PRIVATE_ANCHOR}\n',
                 ALIAS_GUIDANCE, id='merge-alias'),
]


def invalid_yaml(root, location, fragment, *, with_csv=True):
    seeds = root / 'seeds'
    seeds.mkdir()
    if with_csv:
        (seeds / 'a_first.csv').write_text('zip\nPRIVATE_CSV_FIELD\n')
        # Static loader errors must win before CSV parsing/comparison.
        (seeds / 'codes.csv').write_text('zip\n"PRIVATE_CSV_FIELD\n')
    project = root / 'dbt_project.yml'
    project.write_text('name: example\n')
    if location == 'project':
        source = project
        source.write_text('name: example\nprivate_settings:\n' + indent(fragment, '  '))
    else:
        matched = not location.startswith('unmatched-')
        source = seeds / ('properties.yaml' if location.endswith('-yaml') else 'properties.yml')
        source.write_text('seeds:\n- name: a_first\n- name: ' + ('codes' if matched else 'missing')
                          + '\n  config:\n    private_settings:\n' + indent(fragment, '      '))
    return source


INVALID_GUIDANCE = 'invalid or excessively nested YAML'
SYNTAX_LOCATIONS = ['project', 'properties-yml', 'properties-yaml',
                    'unmatched-properties-yml', 'unmatched-properties-yaml']
# Offsets are one-based within the fragment, before project/properties indentation.
INVALID_DOCUMENTS = [
    pytest.param('PRIVATE_KEY: [PRIVATE_CONFIG_VALUE, }\n', (1, 37), id='flow-parser'),
    pytest.param('PRIVATE_KEY: "PRIVATE_CONFIG_VALUE\\q"\n', (1, 36), id='escape-scanner'),
    pytest.param('PRIVATE_KEY: !PRIVATE_TAG PRIVATE_CONFIG_VALUE\n', (1, 14), id='tag-constructor'),
    pytest.param('first: &PRIVATE_ANCHOR PRIVATE_CONFIG_VALUE\n'
                 'PRIVATE_KEY: &PRIVATE_ANCHOR other\n', (2, 14), id='duplicate-anchor'),
    pytest.param('PRIVATE_KEY: PRIVATE_CONFIG_VALUE\x01\n', None, id='nonprintable-reader'),
    pytest.param('PRIVATE_KEY: ' + '[' * 2000 + 'PRIVATE_CONFIG_VALUE' + ']' * 2000 + '\n',
                 None, id='excessive-nesting'),
]


def invalid_diagnostic(source, location, position):
    message = f'{source}: {INVALID_GUIDANCE}'
    if position:
        line, column = position
        message += (f' at line {line + (2 if location == "project" else 5)}, '
                    f'column {column + (2 if location == "project" else 6)}')
    return message


@pytest.mark.parametrize('location', SYNTAX_LOCATIONS)
@pytest.mark.parametrize('fragment,position', INVALID_DOCUMENTS)
def test_invalid_yaml_diagnostic_has_only_safe_origin(tmp_path, monkeypatch, location, fragment, position):
    source = invalid_yaml(tmp_path, location, fragment)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SeedtypesError) as error:
        check_project('.')
    assert str(error.value) == invalid_diagnostic(source, location, position)


@pytest.mark.parametrize('location', SYNTAX_LOCATIONS)
@pytest.mark.parametrize('fragment,position', INVALID_DOCUMENTS[:3])
@pytest.mark.parametrize('show_examples', [False, True])
def test_cli_invalid_yaml_keeps_source_private(tmp_path, location, fragment, position, show_examples):
    source = invalid_yaml(tmp_path, location, fragment)
    args = ['check', tmp_path, '--format', 'text' if show_examples else 'json']
    if show_examples:
        args.append('--show-examples')
    result = cli(*args)
    assert result.returncode == 2
    assert result.stdout == ''
    for secret in ('PRIVATE_KEY', 'PRIVATE_CONFIG_VALUE', 'PRIVATE_TAG', 'PRIVATE_CSV_FIELD'):
        assert secret not in result.stderr
    assert result.stderr == f'datawrench seedtypes: {invalid_diagnostic(source, location, position)}\n'


@pytest.mark.parametrize('location', SYNTAX_LOCATIONS)
def test_invalid_yaml_without_csvs(tmp_path, location):
    fragment, position = 'PRIVATE_KEY: [PRIVATE_CONFIG_VALUE, }\n', (1, 37)
    source = invalid_yaml(tmp_path, location, fragment, with_csv=False)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == invalid_diagnostic(source, location, position)


def test_jinja_check_precedes_invalid_yaml(tmp_path):
    source = invalid_yaml(tmp_path, 'project', 'PRIVATE_KEY: ["{{ PRIVATE_CONFIG_VALUE }}", }\n')
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{source}: Jinja is outside the static check boundary; nothing was evaluated'


def test_invalid_properties_precede_root_config_and_csv_validation(tmp_path):
    fragment, position = 'PRIVATE_KEY: [PRIVATE_CONFIG_VALUE, }\n', (1, 37)
    source = invalid_yaml(tmp_path, 'unmatched-properties-yaml', fragment)
    (tmp_path / 'dbt_project.yml').write_text('name: example\nseeds: null\n')
    first = source.with_name('a_first.yml')
    first.write_text('PRIVATE_KEY: !PRIVATE_TAG PRIVATE_CONFIG_VALUE\n')
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{first}: {INVALID_GUIDANCE} at line 1, column 14'
    first.unlink()
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == invalid_diagnostic(source, 'unmatched-properties-yaml', position)


@pytest.mark.parametrize('location', LOCATIONS)
@pytest.mark.parametrize('fragment,guidance', BAD_YAML)
def test_loader_error_provenance(tmp_path, monkeypatch, location, fragment, guidance):
    source = invalid_yaml(tmp_path, location, fragment)
    monkeypatch.chdir(tmp_path)
    # A relative project argument must still produce an absolute YAML filename.
    with pytest.raises(SeedtypesError) as error:
        check_project('.')
    assert str(error.value) == f'{source}: {guidance}'


@pytest.mark.parametrize('location', LOCATIONS)
@pytest.mark.parametrize('fragment,guidance', [BAD_YAML[0], BAD_YAML[5], BAD_YAML[8]])
@pytest.mark.parametrize('show_examples', [False, True])
def test_cli_loader_error_is_static_and_has_no_partial_output(tmp_path, location, fragment, guidance, show_examples):
    source = invalid_yaml(tmp_path, location, fragment)
    args = ['check', tmp_path, '--format', 'text' if show_examples else 'json']
    if show_examples:
        args.append('--show-examples')
    result = cli(*args)
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr == f'datawrench seedtypes: {source}: {guidance}\n'
    for secret in ('PRIVATE_KEY', 'PRIVATE_VALUE', 'PRIVATE_ANCHOR', 'PRIVATE_CSV_FIELD'):
        assert secret not in result.stderr


@pytest.mark.parametrize('location', LOCATIONS)
@pytest.mark.parametrize('fragment,guidance', [BAD_YAML[0], BAD_YAML[8]])
def test_loader_errors_without_csvs(tmp_path, location, fragment, guidance):
    source = invalid_yaml(tmp_path, location, fragment, with_csv=False)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{source}: {guidance}'


def test_jinja_check_precedes_static_loader(tmp_path):
    source = invalid_yaml(tmp_path, 'project', 'PRIVATE_KEY: "{{ PRIVATE_VALUE }}"\nPRIVATE_KEY: *PRIVATE_ANCHOR\n')
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{source}: Jinja is outside the static check boundary; nothing was evaluated'


def test_alias_composition_precedes_duplicate_key_construction(tmp_path):
    source = invalid_yaml(tmp_path, 'project', 'PRIVATE_KEY: PRIVATE_VALUE\nPRIVATE_KEY: other\nlast: *PRIVATE_ANCHOR\n')
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{source}: {ALIAS_GUIDANCE}'


def test_properties_load_order_precedes_project_config_and_csv_validation(tmp_path):
    source = invalid_yaml(tmp_path, 'unmatched-properties-yaml', 'PRIVATE_KEY: 1\nPRIVATE_KEY: 2\n')
    (tmp_path / 'dbt_project.yml').write_text('name: example\nseeds: null\n')
    first = source.with_name('a_first.yml')
    first.write_text('private_settings: *PRIVATE_ANCHOR\n')
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{first}: {ALIAS_GUIDANCE}'
    first.unlink()
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{source}: {KEY_GUIDANCE}'


def test_valid_static_mappings_and_unreferenced_anchors_remain_supported(tmp_path):
    (tmp_path / 'seeds').mkdir()
    # Distinct allowed scalar keys and unused project nodes retain their scope.
    (tmp_path / 'dbt_project.yml').write_text(
        'name: example\nseeds: {unused_project: null}\nprivate_settings: &unused\n'
        '  PRIVATE_KEY: PRIVATE_VALUE\n  2: integer\n  3.5: float\n  false: boolean\n')
    (tmp_path / 'seeds' / 'properties.yaml').write_text('seeds: [{name: missing, config: {column_types: {}}}]\n')
    assert check_project(tmp_path) == {'project': str(tmp_path), 'seeds_checked': 0, 'issues': [], 'seeds': []}


# Pinned SafeConstructor failures include conversion ValueError, empty numeric
# IndexError, boolean KeyError and explicit timestamp regex AttributeError.
INVALID_SCALARS = [
    pytest.param('2026-99-99', id='invalid-date'),
    pytest.param('2025-02-29', id='invalid-leap-day'),
    pytest.param('2026-01-01T25:00:00', id='invalid-time'),
    pytest.param('2026-01-01T01:02:03+24:00', id='invalid-offset'),
    pytest.param('!!timestamp PRIVATE_CONFIG_VALUE', id='explicit-timestamp'),
    pytest.param('!!timestamp ""', id='empty-timestamp'),
    pytest.param('!!int PRIVATE_CONFIG_VALUE', id='explicit-int'),
    pytest.param('!!int ""', id='empty-int'),
    pytest.param('!!int "+"', id='sign-int'),
    pytest.param('!!float PRIVATE_CONFIG_VALUE', id='explicit-float'),
    pytest.param('!!float ""', id='empty-float'),
    pytest.param('!!float "+"', id='sign-float'),
    pytest.param('!!bool PRIVATE_CONFIG_VALUE', id='explicit-bool'),
    pytest.param('!!float "' + '1:' * 175 + '1"', id='sexagesimal-float-overflow'),
    pytest.param('!!binary "a"', id='invalid-binary'),
    pytest.param('!!binary "é"', id='non-ascii-binary'),
]


@pytest.mark.parametrize('location', SYNTAX_LOCATIONS)
@pytest.mark.parametrize('scalar', INVALID_SCALARS)
@pytest.mark.parametrize('with_csv', [True, False])
def test_scalar_conversion_errors_have_safe_origin(tmp_path, monkeypatch, location, scalar, with_csv):
    source = invalid_yaml(tmp_path, location, f'PRIVATE_KEY: {scalar}\n', with_csv=with_csv)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SeedtypesError) as error:
        check_project('.')
    assert str(error.value) == invalid_diagnostic(source, location, (1, 14))


@pytest.mark.parametrize('location', SYNTAX_LOCATIONS)
@pytest.mark.parametrize('scalar', [INVALID_SCALARS[i] for i in (0, 2, 4, 6, 9, 12)])
@pytest.mark.parametrize('show_examples', [False, True])
def test_cli_scalar_conversion_errors_stay_private(tmp_path, location, scalar, show_examples):
    source = invalid_yaml(tmp_path, location, f'PRIVATE_KEY: {scalar}\n')
    args = ['check', tmp_path, '--format', 'text' if show_examples else 'json']
    if show_examples:
        args.append('--show-examples')
    result = cli(*args)
    assert result.returncode == 2
    assert result.stdout == ''
    # Exact equality excludes traceback, rejected tags/keys/values and CSV data.
    assert result.stderr == f'datawrench seedtypes: {invalid_diagnostic(source, location, (1, 14))}\n'


@pytest.mark.parametrize('location', SYNTAX_LOCATIONS)
def test_valid_scalar_conversions_are_preserved(tmp_path, location):
    import datetime
    from datawrench.seedtypes.check import load_yaml

    fragment = ('date: 2024-02-29\ntime: 2024-02-29T01:02:03.123456+02:30\n'
                'explicit_time: !!timestamp 2024-02-29\ninteger: !!int "0x10"\n'
                'float: !!float "1.25"\nboolean: !!bool "yes"\n'
                'binary: !!binary "SGk="\nstring: !!str 2026-99-99\n'
                'quoted: "2026-99-99"\nnothing: !!null null\n')
    source = invalid_yaml(tmp_path, location, fragment, with_csv=False)
    doc = load_yaml(source)
    values = doc['private_settings'] if location == 'project' else doc['seeds'][1]['config']['private_settings']
    assert values == {
        'date': datetime.date(2024, 2, 29),
        'time': datetime.datetime(2024, 2, 29, 1, 2, 3, 123456,
                                  tzinfo=datetime.timezone(datetime.timedelta(hours=2, minutes=30))),
        'explicit_time': datetime.date(2024, 2, 29), 'integer': 16, 'float': 1.25,
        'boolean': True, 'binary': b'Hi', 'string': '2026-99-99',
        'quoted': '2026-99-99', 'nothing': None,
    }
    assert check_project(tmp_path)['seeds_checked'] == 0


@pytest.mark.parametrize('fragment,guidance', [
    ('PRIVATE_KEY: 2026-99-99\nlast: "{{ PRIVATE_CONFIG_VALUE }}"\n',
     'Jinja is outside the static check boundary; nothing was evaluated'),
    ('PRIVATE_KEY: 2026-99-99\nlast: *PRIVATE_ANCHOR\n', ALIAS_GUIDANCE),
    ('duplicate: 1\nduplicate: 2\nPRIVATE_KEY: 2026-99-99\n', KEY_GUIDANCE),
])
def test_scalar_conversion_preserves_earlier_loader_errors(tmp_path, fragment, guidance):
    source = invalid_yaml(tmp_path, 'project', fragment)
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{source}: {guidance}'


def test_scalar_properties_load_before_root_config_and_csv(tmp_path):
    source = invalid_yaml(tmp_path, 'unmatched-properties-yaml', 'PRIVATE_KEY: 2026-99-99\n')
    (tmp_path / 'dbt_project.yml').write_text('name: example\nseeds: null\n')
    first = source.with_name('a_first.yml')
    first.write_text('PRIVATE_KEY: !!int PRIVATE_CONFIG_VALUE\n')
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{first}: {INVALID_GUIDANCE} at line 1, column 14'
    first.unlink()
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == invalid_diagnostic(source, 'unmatched-properties-yaml', (1, 14))


@pytest.mark.parametrize('boundary', ['yaml-load', 'mapping', 'scalar'])
@pytest.mark.parametrize('exception', [ValueError, TypeError, RuntimeError])
def test_scalar_normalization_does_not_mask_unrelated_errors(tmp_path, monkeypatch, boundary, exception):
    import yaml
    from datawrench.seedtypes.check import StaticLoader, load_yaml

    source = tmp_path / 'dbt_project.yml'
    source.write_text('name: example\n')

    def fail(*args, **kwargs):
        raise exception('unrelated programming error')

    if boundary == 'yaml-load':
        monkeypatch.setattr(yaml, 'load', fail)
    else:
        monkeypatch.setattr(StaticLoader, 'construct_' + boundary, fail)
    with pytest.raises(exception, match='unrelated programming error'):
        load_yaml(source)


INVALID_UTF8 = [
    pytest.param(b'\xff', id='invalid-start'),
    pytest.param(b'\xe2\x82', id='truncated-multibyte'),
    pytest.param(b'\xc0\xaf', id='overlong'),
    pytest.param(b'\xed\xa0\x80', id='surrogate'),
]


def invalid_utf8(root, location, payload, *, with_csv=True, bom=False):
    source = invalid_yaml(root, location, 'PRIVATE_KEY: !PRIVATE_TAG PRIVATE_VALUE\n', with_csv=with_csv)
    source.write_bytes((b'\xef\xbb\xbf' if bom else b'') + source.read_bytes() + b'# ' + payload)
    return source


@pytest.mark.parametrize('location', SYNTAX_LOCATIONS)
@pytest.mark.parametrize('payload', INVALID_UTF8)
@pytest.mark.parametrize('with_csv', [False, True])
@pytest.mark.parametrize('validate', [False, True])
def test_invalid_utf8_has_safe_origin_before_yaml_or_csv(tmp_path, monkeypatch, location, payload, with_csv, validate):
    source = invalid_utf8(tmp_path, location, payload, with_csv=with_csv)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SeedtypesError) as error:
        check_project('.', validate_column_types=validate)
    assert str(error.value) == f'{source}: {UTF8_GUIDANCE}'


@pytest.mark.parametrize('location', SYNTAX_LOCATIONS)
@pytest.mark.parametrize('payload', INVALID_UTF8[:2])
@pytest.mark.parametrize('with_csv', [False, True])
@pytest.mark.parametrize('validate', [False, True])
@pytest.mark.parametrize('show_examples', [False, True])
def test_cli_invalid_utf8_stays_private(tmp_path, location, payload, with_csv, validate, show_examples):
    source = invalid_utf8(tmp_path, location, payload, with_csv=with_csv, bom=True)
    args = ['check', tmp_path, '--format', 'text' if show_examples else 'json']
    if validate:
        args.append('--validate-column-types')
    if show_examples:
        args.append('--show-examples')
    result = cli(*args)
    assert result.returncode == 2
    assert result.stdout == ''
    # Exact equality excludes decoder text, snippets, tags, keys, values and CSV evidence.
    assert result.stderr == f'datawrench seedtypes: {source}: {UTF8_GUIDANCE}\n'


@pytest.mark.parametrize('location', SYNTAX_LOCATIONS)
@pytest.mark.parametrize('bom', [False, True])
@pytest.mark.parametrize('validate', [False, True])
def test_valid_utf8_bom_and_non_ascii_are_preserved(tmp_path, location, bom, validate):
    from datawrench.seedtypes.check import load_yaml

    source = invalid_yaml(tmp_path, location, 'café: "東京 — naïve 😀"\n', with_csv=False)
    project = tmp_path / 'dbt_project.yml'
    project.write_text(project.read_text() + 'seeds:\n  +column_types: {zip: VARCHAR}\n', encoding='utf-8')
    if bom:
        source.write_bytes(b'\xef\xbb\xbf' + source.read_bytes())
    doc = load_yaml(source)
    values = doc['private_settings'] if location == 'project' else doc['seeds'][1]['config']['private_settings']
    assert values == {'café': '東京 — naïve 😀'}
    (tmp_path / 'seeds' / 'codes.csv').write_text('zip\n東京\n', encoding='utf-8')
    report = check_project(tmp_path, validate_column_types=validate)
    assert report['seeds_checked'] == 1 and report['issues'] == []
    if validate:
        assert report['column_type_failures'] == []
        assert report['seeds'][0]['column_type_validation']['valid'] is True


@pytest.mark.parametrize('fragment', [
    'PRIVATE_KEY: "{{ PRIVATE_VALUE }}"\n',
    'PRIVATE_KEY: [PRIVATE_VALUE, }\n',
    'PRIVATE_KEY: *PRIVATE_ANCHOR\n',
    'PRIVATE_KEY: 1\nPRIVATE_KEY: 2\n',
    'PRIVATE_KEY: !!int PRIVATE_VALUE\n',
])
def test_utf8_decoding_still_precedes_jinja_and_yaml_loading(tmp_path, fragment):
    source = invalid_yaml(tmp_path, 'project', fragment, with_csv=False)
    source.write_bytes(source.read_bytes() + b'# \xe2\x82')
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path)
    assert str(error.value) == f'{source}: {UTF8_GUIDANCE}'


@pytest.mark.parametrize('validate', [False, True])
def test_utf8_properties_load_before_root_semantics_and_csv(tmp_path, validate):
    source = invalid_utf8(tmp_path, 'unmatched-properties-yaml', b'\xe2\x82')
    (tmp_path / 'dbt_project.yml').write_text('name: example\nseeds: null\n')
    first = source.with_name('a_first.yml')
    first.write_text('PRIVATE_KEY: *PRIVATE_ANCHOR\n')
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path, validate_column_types=validate)
    assert str(error.value) == f'{first}: {ALIAS_GUIDANCE}'
    first.unlink()
    with pytest.raises(SeedtypesError) as error:
        check_project(tmp_path, validate_column_types=validate)
    assert str(error.value) == f'{source}: {UTF8_GUIDANCE}'


@pytest.mark.parametrize('boundary', ['read', 'yaml-load'])
def test_utf8_normalization_is_limited_to_decoding_the_file(tmp_path, monkeypatch, boundary):
    from pathlib import Path
    import yaml
    from datawrench.seedtypes.check import load_yaml

    source = tmp_path / 'dbt_project.yml'
    source.write_text('name: example\n')
    failure = (OSError('unrelated read error') if boundary == 'read'
               else UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'unrelated application error'))

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(Path, 'read_text', fail) if boundary == 'read' else monkeypatch.setattr(yaml, 'load', fail)
    with pytest.raises(type(failure)) as error:
        load_yaml(source)
    assert error.value is failure
