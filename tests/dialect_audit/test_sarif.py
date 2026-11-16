"""Current-run SARIF uses verified original evidence, never statement/target guesses."""
from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft4Validator, FormatChecker
from jsonschema.exceptions import FormatError
import pytest
from sqlglot.errors import ParseError

from datawrench.dialect_audit import __version__
from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.cli import main
from datawrench.dialect_audit.report import render

SCHEMA = json.loads((Path(__file__).parent / 'schemas/sarif-schema-2.1.0.json').read_text())
VALIDATOR = Draft4Validator(SCHEMA, format_checker=FormatChecker())


def sarif(output):
    data = json.loads(output)
    VALIDATOR.validate(data)
    assert data['version'] == '2.1.0'
    run, = data['runs']
    assert run['columnKind'] == 'unicodeCodePoints'
    assert run['tool']['driver']['name'] == 'dialect-audit'
    assert run['tool']['driver']['version'] == __version__
    rules = run['tool']['driver']['rules']
    assert [r['id'] for r in rules] == sorted({r['ruleId'] for r in run['results']})
    for result in run['results']:
        assert rules[result['ruleIndex']]['id'] == result['ruleId']
    return run


def audit(tmp_path, source, target='duckdb'):
    path = tmp_path / 'batch.sql'
    path.write_bytes(source.encode())
    return run_audit([str(path)], 'snowflake', target)


@pytest.mark.parametrize('prefix', ['', 'SELECT 1; '])
def test_duplicate_file_only_warnings_keep_enclosing_statement_indices(tmp_path, prefix):
    report = audit(tmp_path, prefix + 'SELECT NO_FN(1); SELECT NO_FN(1);', 'postgres')
    saved = deepcopy(report)
    run = sarif(render(report, 'sarif'))
    first, second = run['results']
    offset = int(bool(prefix))
    assert first['properties'] == {'statementIndex': offset + 1}
    assert second['properties'] == {'statementIndex': offset + 2}
    # The index is the only distinction: unknown diagnostic spans stay unknown.
    assert {k: v for k, v in first.items() if k != 'properties'} == {
        k: v for k, v in second.items() if k != 'properties'}
    assert first['locations'] == [{'physicalLocation': {
        'artifactLocation': {'uri': (tmp_path / 'batch.sql').as_uri()}}}]
    assert (first['ruleId'], first['level'], first['message']['text']) == (
        'ANONYMOUS_FUNCTION', 'warning', report['statements'][offset]['diagnostics'][0]['message'])
    assert run['properties']['summary'] == dict(files=1, statements=offset + 2, ok=offset,
        flagged=2, errors=0, warnings=2, skipped_files=0)
    assert report == saved


def test_statement_indices_restart_per_file_and_notices_have_no_properties(tmp_path):
    inputs = {
        'a.sql': 'SELECT 1; SELECT NO_FN(1);',
        'b.sql': 'SELECT NO_FN(1);',
        'empty.sql': '',
        'jinja.sql': 'SELECT {{ value }};',
        'binary.sql': '\x00',
    }
    for name, source in inputs.items():
        (tmp_path / name).write_text(source)
    report = run_audit([str(tmp_path / name) for name in inputs], 'snowflake', 'postgres')
    run = sarif(render(report, 'sarif'))
    diagnostics, notices = run['results'][:2], run['results'][2:]
    assert [r['properties'] for r in diagnostics] == [{'statementIndex': 2}, {'statementIndex': 1}]
    assert [r['ruleId'] for r in notices] == [n['category'] for n in report['notices']]
    assert len(notices) == 3 and all('properties' not in r for r in notices)
    for result, notice in zip(notices, report['notices']):
        assert result['locations'] == [{'physicalLocation': {
            'artifactLocation': {'uri': Path(notice['file']).as_uri()}}}]
        assert result['message']['text'] == notice['message']


@pytest.mark.parametrize(('policy', 'expected'), [('error', 1), ('warn', 1), ('never', 0)])
def test_cli_batch_codes_levels_original_highlight_and_exit(tmp_path, capsys, policy, expected):
    path = tmp_path / 'batch.sql'
    path.write_text('SELECT 1; SELECT NO_FN(1); SELECT * FROM;')
    assert main(['run', str(path), '--read', 'snowflake', '--write', 'duckdb',
                 '--format', 'sarif', '--fail-on', policy]) == expected
    output = capsys.readouterr()
    assert output.err == ''
    run = sarif(output.out)
    assert run['properties'] == dict(read='snowflake', write='duckdb', binding='duckdb-explain',
        failOn=policy, summary=dict(files=1, statements=3, ok=1, flagged=2, errors=2,
                                   warnings=1, skipped_files=0))
    assert [(r['ruleId'], r['level']) for r in run['results']] == [
        ('ANONYMOUS_FUNCTION', 'warning'), ('BIND_ERROR', 'error'), ('PARSE_ERROR', 'error')]
    assert [r['properties'] for r in run['results']] == [
        {'statementIndex': 2}, {'statementIndex': 2}, {'statementIndex': 3}]
    first, second, third = [r['locations'][0]['physicalLocation'] for r in run['results']]
    assert first == second == {'artifactLocation': {'uri': path.as_uri()}}
    assert third == {'artifactLocation': {'uri': path.as_uri()},
                     'region': dict(startLine=1, startColumn=37, endLine=1, endColumn=41)}
    assert 'source_sql' not in output.out and 'target_sql' not in output.out and 'snippet' not in output.out


@pytest.mark.parametrize(('source', 'region'), [
    ("-- lead\n SELECT 1; SELECT 'é😀' FROM; SELECT 3;",
     dict(startLine=2, startColumn=24, endLine=2, endColumn=28)),
    ("\ufeff-- lead\r\n\tSELECT 1; SELECT 1 'é\r\n😀'; SELECT 3;",
     dict(startLine=2, startColumn=21, endLine=3, endColumn=3)),
])
def test_unicode_crlf_multiline_inclusive_to_exclusive(tmp_path, source, region):
    report = audit(tmp_path, source)
    run = sarif(render(report, 'sarif'))
    result, = run['results']
    assert result['ruleId'] == 'PARSE_ERROR'
    assert result['properties'] == {'statementIndex': 2}
    assert result['locations'][0]['physicalLocation']['region'] == region


def test_aggregate_related_highlights_do_not_become_primary(tmp_path):
    report = audit(tmp_path, '-- header\n  SELECT 1; SELECT (SELECT * FROM), (SELECT * FROM); SELECT 3;')
    result, = sarif(render(report, 'sarif'))['results']
    assert result['properties'] == {'statementIndex': 2}
    assert 'region' not in result['locations'][0]['physicalLocation']
    assert [r['id'] for r in result['relatedLocations']] == [1, 2]
    assert [r['physicalLocation']['region'] for r in result['relatedLocations']] == [
        dict(startLine=2, startColumn=c, endLine=2, endColumn=c + 1) for c in (34, 51)]
    assert [r['message']['text'] for r in result['relatedLocations']] == [
        r['description'] for r in report['statements'][1]['diagnostics'][0]['related_locations']]


def test_mixed_related_evidence_duplicates_and_unknowns(monkeypatch, tmp_path):
    valid = dict(description='missing "table"\n雪😀\\', line=1, col=13, highlight='FROM')
    def fail(*args, **kwargs):
        raise ParseError('aggregate', errors=[valid, dict(valid), None, dict(valid, col=99)])
    monkeypatch.setattr('datawrench.dialect_audit.audit.sqlglot.parse', fail)
    report = audit(tmp_path, '  SELECT * FROM;', 'postgres')
    result, = sarif(render(report, 'sarif'))['results']
    related = result['relatedLocations']
    assert [r['id'] for r in related] == [1, 2, 3, 4]
    assert related[0]['physicalLocation'] == related[1]['physicalLocation']
    assert related[0]['physicalLocation']['region'] == dict(
        startLine=1, startColumn=12, endLine=1, endColumn=16)
    assert related[0]['message']['text'] == valid['description']
    assert related[2]['message']['text'] == 'description unavailable'
    assert all('region' not in r['physicalLocation'] for r in related[2:])
    assert 'region' not in result['locations'][0]['physicalLocation']


@pytest.mark.parametrize(('content', 'category', 'level'), [
    (b'', 'EMPTY_INPUT', 'warning'),
    (b'-- comment only', 'EMPTY_INPUT', 'warning'),
    (b'SELECT {{ value }}', 'JINJA_SKIPPED', 'warning'),
    (b'\xff', 'INPUT_SKIPPED', 'error'),
    (b'SELECT \x00', 'INPUT_SKIPPED', 'error'),
    (b"SELECT 1;\nSELECT 'unfinished", 'PARSE_ERROR', 'error'),
])
def test_empty_skipped_and_tokenization_errors_are_file_only(tmp_path, content, category, level):
    path = tmp_path / 'input.sql'
    path.write_bytes(content)
    report = run_audit([str(path)], 'snowflake', 'duckdb')
    result, = sarif(render(report, 'sarif'))['results']
    assert (result['ruleId'], result['level']) == (category, level)
    assert result['locations'] == [{'physicalLocation': {'artifactLocation': {'uri': path.as_uri()}}}]
    if category == 'PARSE_ERROR':  # Existing synthetic whole-file statement, not a notice.
        assert result['properties'] == {'statementIndex': 1}
    else:
        assert 'properties' not in result


def test_no_findings_valid_empty_results_no_sql_or_extra_reads(monkeypatch, tmp_path):
    report = audit(tmp_path, "SELECT 'private 😀';")
    saved = deepcopy(report)
    def fail(*args, **kwargs):
        pytest.fail('SARIF rendering must not read or resolve input files')
    monkeypatch.setattr(Path, 'read_text', fail)
    monkeypatch.setattr(Path, 'read_bytes', fail)
    monkeypatch.setattr(Path, 'resolve', fail)
    output = render(report, 'sarif')
    run = sarif(output)
    assert run['results'] == [] and run['tool']['driver']['rules'] == []
    assert 'private' not in output
    assert run['artifacts'] == [{'location': {'uri': (tmp_path / 'batch.sql').as_uri()}}]
    assert render(report, 'sarif') == output and report == saved


def test_artifacts_mixed_clean_empty_and_skipped_cli(tmp_path, capsys):
    directory = tmp_path / 'sql'
    directory.mkdir()
    for name, content in {'a.sql': 'SELECT 1;\n', 'b.sql': 'SELECT 2;\n',
                          'empty.sql': '-- empty\n', 'skipped.sql': 'SELECT {{ value }};\n'}.items():
        (directory / name).write_text(content)
    assert main(['run', str(directory), '--read', 'snowflake', '--write', 'duckdb',
                 '--format', 'sarif']) == 0
    output = capsys.readouterr()
    assert output.err == ''
    run = sarif(output.out)
    assert run['artifacts'] == [{'location': {'uri': (directory / name).as_uri()}}
                                for name in ('a.sql', 'b.sql', 'empty.sql', 'skipped.sql')]
    assert run['properties']['summary'] == dict(files=4, statements=2, ok=2, flagged=0,
        errors=0, warnings=2, skipped_files=2)
    assert [(r['ruleId'], r['level']) for r in run['results']] == [
        ('EMPTY_INPUT', 'warning'), ('JINJA_SKIPPED', 'warning')]
    assert all('properties' not in r for r in run['results'])
    assert [r['locations'] for r in run['results']] == [[{'physicalLocation': {
        'artifactLocation': {'uri': (directory / name).as_uri()}}}]
        for name in ('empty.sql', 'skipped.sql')]


@pytest.mark.parametrize('duplicate_paths', [False, True])
def test_artifacts_all_clean_sorted_keys_and_discovery_dedup(tmp_path, monkeypatch, duplicate_paths):
    monkeypatch.chdir(tmp_path)
    directory = Path('sql')
    directory.mkdir()
    names = ['z.sql', 'a #.sql', '雪 😀.sql']
    for name in names:
        (directory / name).write_text('SELECT 1;')
    paths = [str(directory)]
    if duplicate_paths:
        paths += [str(directory / name) for name in reversed(names)]
        paths += [str((directory / name).absolute()) for name in names] + [str(directory)]
    report = run_audit(paths, 'snowflake', 'duckdb')
    assert report['summary']['files'] == report['summary']['statements'] == 3
    saved = deepcopy(report)
    first = sarif(render(report, 'sarif'))
    assert first['artifacts'] == [{'location': {'uri': (directory / name).absolute().as_uri()}}
                                  for name in sorted(names)]
    assert first['results'] == [] and first['tool']['driver']['rules'] == []
    assert report == saved
    report['file_summaries'] = dict(reversed(list(report['file_summaries'].items())))
    assert sarif(render(report, 'sarif')) == first


def test_artifacts_error_files_without_extra_evidence(tmp_path):
    inputs = {'clean.sql': b'SELECT 1;', 'binary.sql': b'\x00', 'invalid.sql': b'\xff',
              'parse.sql': b'SELECT (SELECT * FROM), (SELECT * FROM);',
              'token.sql': b"SELECT 'unfinished"}
    for name, content in inputs.items():
        (tmp_path / name).write_bytes(content)
    report = run_audit([str(tmp_path / name) for name in inputs], 'snowflake', 'duckdb')
    saved = deepcopy(report)
    run = sarif(render(report, 'sarif'))
    assert run['artifacts'] == [{'location': {'uri': (tmp_path / name).as_uri()}}
                                for name in sorted(inputs)]
    assert [r['ruleId'] for r in run['results']] == [
        'PARSE_ERROR', 'PARSE_ERROR', 'INPUT_SKIPPED', 'INPUT_SKIPPED']
    assert all(r['level'] == 'error' for r in run['results'])
    assert all('region' not in r['locations'][0]['physicalLocation'] for r in run['results'])
    assert [r.get('properties') for r in run['results']] == [
        {'statementIndex': 1}, {'statementIndex': 1}, None, None]
    assert len(run['results'][0]['relatedLocations']) == 2
    assert all('region' in r['physicalLocation'] for r in run['results'][0]['relatedLocations'])
    assert report == saved


@pytest.mark.parametrize('absolute', [False, True])
def test_artifacts_special_uris_and_deleted_inputs_no_reads(tmp_path, monkeypatch, absolute):
    monkeypatch.chdir(tmp_path)
    path = Path('雪 😀 #?%"\\\t\n.sql')
    path.write_text("SELECT 'private'; SELECT NO_FN(1);")
    report = run_audit([str(path.absolute() if absolute else path)], 'snowflake', 'postgres')
    uri = path.absolute().as_uri()
    saved = deepcopy(report)
    path.unlink()
    def fail(*args, **kwargs):
        pytest.fail('SARIF rendering must use retained evidence without input I/O')
    with monkeypatch.context() as blocked:
        for method in ('read_text', 'read_bytes', 'resolve', 'open', 'stat', 'glob', 'rglob'):
            blocked.setattr(Path, method, fail)
        blocked.setattr('builtins.open', fail)
        blocked.setattr('datawrench.dialect_audit.audit.read_text', fail)
        output = render(report, 'sarif')
        run = sarif(output)
        assert run['artifacts'] == [{'location': {'uri': uri}}]
        assert run['results'][0]['locations'][0]['physicalLocation']['artifactLocation'] == {'uri': uri}
        assert all(fragment in uri for fragment in (
            '%20', '%23', '%3F', '%25', '%22', '%5C', '%09', '%0A', '%E9%9B%AA', '%F0%9F%98%80'))
        assert 'private' not in output
        assert render(report, 'sarif') == output and report == saved


def test_artifacts_empty_inventory_and_empty_directory_rejection(tmp_path, capsys):
    report = dict(read='snowflake', write='duckdb', binding='duckdb-explain',
        statements=[], notices=[], file_summaries={},
        summary=dict(files=0, statements=0, ok=0, flagged=0, errors=0, warnings=0, skipped_files=0))
    run = sarif(render(report, 'sarif'))
    assert run['artifacts'] == run['results'] == run['tool']['driver']['rules'] == []
    assert main(['run', str(tmp_path), '--read', 'snowflake', '--write', 'duckdb',
                 '--format', 'sarif', '--fail-on', 'never']) == 2
    output = capsys.readouterr()
    assert output.out == '' and 'no .sql files' in output.err


@pytest.mark.parametrize('absolute', [False, True])
def test_local_uri_special_characters_and_output_escapes(monkeypatch, tmp_path, absolute):
    monkeypatch.chdir(tmp_path)
    path = Path('雪 😀 #?%"\\\t\n.sql')
    path.write_text('SELECT NO_FN(1);')
    report = run_audit([str(path.absolute() if absolute else path)], 'snowflake', 'postgres')
    original = report['statements'][0]['diagnostics'][0]
    original['message'] = 'quote " slash \\ tab\t CRLF\r\n\x01 雪😀'
    # Exercise both input path spellings without changing source evidence.
    report['statements'][0]['file'] = str(path.absolute() if absolute else path)
    output = render(report, 'sarif')
    result, = sarif(output)['results']
    uri = result['locations'][0]['physicalLocation']['artifactLocation']['uri']
    assert uri == path.absolute().as_uri()
    assert all(fragment in uri for fragment in ['%20', '%23', '%3F', '%25', '%22', '%5C', '%09', '%0A'])
    assert result['message']['text'] == original['message']
    assert '\\ud83d\\ude00' in output and '\\u0001' in output and '\\r\\n' in output


def test_bounded_messages_summary_and_no_report_mutation(tmp_path):
    report = audit(tmp_path, 'SELECT (SELECT * FROM), (SELECT * FROM);')
    item = report['statements'][0]['diagnostics'][0]
    item['message'] = '😀' * 5000
    item['related_locations'][0]['description'] = '雪' * 5000
    saved = deepcopy(report)
    run = sarif(render(report, 'sarif'))
    result, = run['results']
    assert result['message']['text'] == '😀' * 4095 + '…'
    assert result['relatedLocations'][0]['message']['text'] == '雪' * 4095 + '…'
    assert set(run['properties']) == {'read', 'write', 'binding', 'failOn', 'summary'}
    assert all(type(v) is int for v in run['properties']['summary'].values())
    assert report == saved


@pytest.mark.parametrize(('source', 'target', 'exits'), [
    ('SELECT 1;', 'duckdb', [0, 0, 0]),
    ('SELECT NO_FN(1);', 'postgres', [0, 1, 0]),
    ('SELECT * FROM;', 'duckdb', [1, 1, 0]),
    ('', 'duckdb', [0, 1, 0]),
])
def test_all_fail_on_modes_do_not_change_sarif_levels(tmp_path, capsys, source, target, exits):
    path = tmp_path / 'policy.sql'
    path.write_text(source)
    results = []
    for policy, code in zip(['error', 'warn', 'never'], exits):
        assert main(['run', str(path), '--read', 'snowflake', '--write', target,
                     '--format', 'sarif', '--fail-on', policy]) == code
        output = capsys.readouterr()
        assert output.err == ''
        results.append(sarif(output.out)['results'])
    assert results[0] == results[1] == results[2]


@pytest.mark.parametrize('command', ['run', 'diff'])
def test_baseline_sarif_usage_error_before_audit(monkeypatch, tmp_path, capsys, command):
    def fail(*args, **kwargs):
        pytest.fail('invalid SARIF/baseline combination must fail before audit')
    monkeypatch.setattr('datawrench.dialect_audit.cli.run_audit', fail)
    path = tmp_path / 'baseline.json'
    path.write_text('leave untouched')
    with pytest.raises(SystemExit) as exc:
        main([command, 'missing.sql', '--read', 'snowflake', '--write', 'duckdb',
              '--baseline', str(path), '--format', 'sarif'])
    assert exc.value.code == 2
    assert 'usage:' in capsys.readouterr().err
    assert path.read_text() == 'leave untouched'


@pytest.mark.parametrize('extra', [['--read', 'not_a_dialect'], ['--schema', 'missing.sql'],
                                  ['--emit-dir', 'occupied']])
def test_configuration_errors_no_sarif_even_fail_on_never(tmp_path, monkeypatch, capsys, extra):
    monkeypatch.chdir(tmp_path)
    Path('query.sql').write_text('SELECT 1;')
    Path('occupied').mkdir()
    Path('occupied/report.json').write_text('untouched')
    assert main(['run', 'query.sql', '--read', 'snowflake', '--write', 'duckdb',
                 '--format', 'sarif', '--fail-on', 'never', *extra]) == 2
    output = capsys.readouterr()
    assert output.out == '' and 'dialect-audit:' in output.err
    assert Path('occupied/report.json').read_text() == 'untouched'


def test_emit_keeps_native_artifacts_and_default_format(tmp_path, capsys):
    path = tmp_path / 'batch.sql'
    path.write_text('SELECT 1; SELECT * FROM;')
    directory = tmp_path / 'emitted'
    assert main(['run', str(path), '--read', 'snowflake', '--write', 'duckdb',
                 '--format', 'sarif', '--emit-dir', str(directory)]) == 1
    result, = sarif(capsys.readouterr().out)['results']
    assert result['properties'] == {'statementIndex': 2}
    report = json.loads((directory / 'report.json').read_text())
    assert report['version'] == 1 and report['statements'][0]['source_sql'] == 'SELECT 1'
    assert all('properties' not in stmt and 'statementIndex' not in stmt for stmt in report['statements'])
    assert len(list(directory.glob('*.sql'))) == 1
    assert len(list(directory.iterdir())) == 2
    assert next(directory.glob('*.sql')).read_text() == 'SELECT 1;\n'
    assert main(['run', str(path), '--read', 'snowflake', '--write', 'duckdb']) == 1
    assert 'L1-1 C1-8 OK (binding: bound)' in capsys.readouterr().out


def test_all_existing_diagnostic_codes_keep_rule_identity_and_levels(tmp_path):
    from datawrench.dialect_audit.audit import diagnostic, SEVERITIES
    report = audit(tmp_path, 'SELECT 1;')
    report['statements'][0]['diagnostics'] = [diagnostic(code, 'existing message') for code in SEVERITIES]
    run = sarif(render(report, 'sarif'))
    assert [(r['ruleId'], r['level']) for r in run['results']] == [
        (code, {'error': 'error', 'warn': 'warning'}[severity]) for code, severity in SEVERITIES.items()]
    report['statements'][0]['diagnostics'][0]['message'] = 'different message'
    assert run['tool'] == sarif(render(report, 'sarif'))['tool']


def test_file_read_error_has_no_fabricated_region(monkeypatch, tmp_path):
    path = tmp_path / 'denied.sql'
    path.write_text('SELECT 1;')
    def denied(*args, **kwargs):
        raise PermissionError('synthetic permission denied')
    monkeypatch.setattr('datawrench.dialect_audit.audit.read_text', denied)
    report = run_audit([str(path)], 'snowflake', 'duckdb')
    result, = sarif(render(report, 'sarif'))['results']
    assert result['ruleId'] == 'INPUT_SKIPPED' and result['level'] == 'error'
    assert 'properties' not in result
    assert result['locations'] == [{'physicalLocation': {'artifactLocation': {'uri': path.as_uri()}}}]


def test_missing_path_configuration_error(tmp_path, capsys):
    assert main(['run', str(tmp_path / 'missing.sql'), '--read', 'snowflake', '--write', 'duckdb',
                 '--format', 'sarif', '--fail-on', 'never']) == 2
    output = capsys.readouterr()
    assert output.out == '' and 'input does not exist' in output.err


def test_retained_schema_and_uri_format_checking_are_active():
    Draft4Validator.check_schema(SCHEMA)
    checker = VALIDATOR.format_checker
    assert {'uri', 'uri-reference'} <= checker.checkers.keys()
    with pytest.raises(FormatError):
        checker.check('file:///bad path#%', 'uri-reference')
