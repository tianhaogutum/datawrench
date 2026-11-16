"""Main-input budgets stop with exact checked-prefix reports, never a tail verdict."""
import gzip
import io
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
from jsonschema import Draft202012Validator

from datawrench.tapcheck.engine import Validator
from .test_input_budget import TrackingRaw


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def capture(protocol):
    schema = {'type': 'object', 'properties': {'id': {}}}
    if protocol == 'singer':
        declaration = {'type': 'SCHEMA', 'stream': 's', 'schema': schema, 'key_properties': ['id']}
        record = {'type': 'RECORD', 'stream': 's', 'record': {'id': '☃😀'}}
        state = {'type': 'STATE', 'value': {}}
    else:
        declaration = {'type': 'CATALOG', 'catalog': {'streams': [
            {'name': 's', 'json_schema': schema, 'source_defined_primary_key': [['id']]}]}}
        record = {'type': 'RECORD', 'record': {'stream': 's', 'data': {'id': '☃😀'}, 'emitted_at': 0}}
        state = {'type': 'STATE', 'state': {'type': 'LEGACY', 'data': {}}}
    return tuple(map(encoded, (declaration, record, state)))


def cli(command, *args, **kwargs):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', command, *map(str, args)],
                          capture_output=True, **kwargs)


def assert_schema(report, quality):
    path = 'docs/tapcheck/quality-report.schema.json' if quality else 'docs/tapcheck/report.schema.json'
    Draft202012Validator(json.loads(Path(path).read_text())).validate(report)


def codes(report, quality):
    return report['errors_by_code'] if quality else {d['code']: 1 for d in report['diagnostics']}


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('variant', ['plain', 'crlf-blanks', 'no-lf'])
@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_cli_raw_boundaries(tmp_path, protocol, command, mode, variant, delta):
    raw = b''.join(capture(protocol))
    if variant == 'crlf-blanks':
        raw = b'\r\n \t\r\n' + raw.replace(b'\n', b'\r\n') + b'\r\n'
    elif variant == 'no-lf':
        raw = raw[:-1]
    path = tmp_path / 'capture.jsonl'
    path.write_bytes(raw)
    args = [path] if mode == 'file' else ['-']
    kwargs = {} if mode == 'file' else {'input': raw}
    if command == 'validate': args += ['--format', 'json']
    result = cli(command, *args, '--max-input-bytes', len(raw) + delta, **kwargs)
    assert result.stderr == b''
    report = json.loads(result.stdout)
    assert_schema(report, command == 'quality')
    assert result.returncode == report['exit_code'] == (2 if delta < 0 else 0)
    assert report['stopped_early'] == (delta < 0)
    assert sum(s['records'] for s in report['streams']) == 1
    if delta < 0:
        assert codes(report, command == 'quality') == {'TC020': 1}
        if command == 'quality':
            assert report['coverage'] == 'partial_input'
            assert 'final_state_coverage' in report['not_evaluated']
    else:
        baseline = cli(command, *args, **kwargs)
        assert (result.returncode, result.stdout, result.stderr) == (baseline.returncode, baseline.stdout, baseline.stderr)
    assert path.read_bytes() == raw


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('crossing', ['record', 'state', 'trailing-blank'])
def test_exact_prefix_and_no_false_final_state(quality, protocol, crossing):
    declaration, record, state = capture(protocol)
    prefix = declaration if crossing == 'record' else declaration + record
    if crossing == 'trailing-blank': prefix += state
    raw = prefix + (record + state if crossing == 'record' else state if crossing == 'state' else b'\n')
    report = Validator(quality=quality, max_input_bytes=len(prefix), key_example_limit=1).run(io.BytesIO(raw), source='cap')
    assert report['exit_code'] == 2 and report['stopped_early']
    assert codes(report, quality) == {'TC020': 1}
    assert report['input_lines'] == (1 if crossing == 'record' else 2 if crossing == 'state' else 3)
    assert report['messages'] == {('SCHEMA' if protocol == 'singer' else 'CATALOG'): 1,
                                   **({'RECORD': 1} if crossing != 'record' else {}),
                                   **({'STATE': 1} if crossing == 'trailing-blank' else {})}
    assert report['streams'][0]['records'] == (crossing != 'record')
    if quality:
        assert report['streams'][0]['states'] == (crossing == 'trailing-blank')
        assert report['unassigned']['examples'] == [{'code': 'TC020', 'source': 'cap',
                   'line': report['input_lines'] + 1, 'path': None}]
        assert report['streams'][0]['keys']['by_path'][0]['examples'] == []
    else:
        assert report['diagnostics'][0]['line'] == report['input_lines'] + 1
    assert_schema(report, quality)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('raw,budget,code', [(b'', 0, 'TC021'), (b'\n', 0, 'TC020'),
    (b' ', 0, 'TC020'), (b'\r\n', 2, 'TC021'), (b'\n'*100, 20, 'TC020')])
def test_zero_empty_and_blank_only(quality, raw, budget, code):
    report = Validator(quality=quality, max_input_bytes=budget).run(io.BytesIO(raw))
    assert report['exit_code'] == 2 and codes(report, quality) == {code: 1}
    assert report['input_lines'] == 0 and report['messages'] == {} and report['streams'] == []
    assert_schema(report, quality)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('budget', [0, 1, 8191, 8192, 10000])
@pytest.mark.parametrize('oversized', [False, True])
def test_raw_read_bound_and_no_tail_drain(quality, budget, oversized):
    data = (b' ' if oversized else b'\n') * 100000
    raw = TrackingRaw(data)
    with io.BufferedReader(raw) as handle:
        report = Validator(quality=quality, max_input_bytes=budget, max_line_bytes=8).run(handle)
        assert report['exit_code'] == 2 and codes(report, quality) == {'TC020': 1}
        assert raw.data.tell() == budget + 1 and raw.eof_reads == 0
        assert not handle.closed
        if not quality:
            assert report['diagnostics'][0]['line'] == (1 if oversized else budget + 1)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('suffix', [b'', b'\n\r\n'])
def test_exact_fit_real_eof_including_no_final_lf(quality, protocol, suffix):
    data = b''.join(capture(protocol)).rstrip(b'\n') + suffix
    raw = TrackingRaw(data)
    with io.BufferedReader(raw) as handle:
        report = Validator(quality=quality, max_input_bytes=len(data)).run(handle)
        assert report['exit_code'] == 0 and raw.data.tell() == len(data) and raw.eof_reads > 0


@pytest.mark.parametrize('quality', [False, True])
def test_prebuffered_input_consumed_in_order(quality):
    prefix = b''.join(capture('singer'))
    raw = TrackingRaw(prefix + b'\n'*100000)
    with io.BufferedReader(raw) as handle:
        assert handle.peek(1).startswith(prefix)
        before = raw.data.tell()
        report = Validator(quality=quality, max_input_bytes=len(prefix)).run(handle)
        assert report['messages'] == {'SCHEMA': 1, 'RECORD': 1, 'STATE': 1}
        assert report['exit_code'] == 2 and raw.data.tell() - before <= len(prefix) + 1


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('failure,code', [('missing-state', 'TC008'), ('bad-json', 'TC001'), ('oversized', 'TC020')])
def test_exact_fit_preserves_contract_checks(quality, failure, code):
    parts = capture('singer')
    raw = b''.join(parts[:-1]) if failure == 'missing-state' else b''.join(parts) + (b'{broken' if failure == 'bad-json' else b' '*500+b'\n')
    report = Validator(quality=quality, max_input_bytes=len(raw), max_line_bytes=200).run(io.BytesIO(raw))
    assert report['exit_code'] == 1 and not report['stopped_early']
    assert codes(report, quality) == {code: 1}
    if quality: assert report['coverage'] == 'complete_input'


@pytest.mark.parametrize('value', [-1, 2**63, True, False, 1.5, '12', float('inf')])
def test_invalid_api_budget(value):
    with pytest.raises(ValueError, match='max_input_bytes'):
        Validator(max_input_bytes=value)


@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('value', ['-1', '1.5', 'invalid', '9223372036854775808'])
def test_invalid_cli_budget_before_io(tmp_path, command, value):
    result = cli(command, tmp_path/'missing', '--catalog', tmp_path/'missing-catalog', '--max-input-bytes', value)
    assert result.returncode == 2 and result.stdout == b''
    assert b'--max-input-bytes' in result.stderr and b'usage:' in result.stderr
    assert b'Traceback' not in result.stderr and not list(tmp_path.iterdir())


@pytest.mark.parametrize('quality', [False, True])
def test_maximum_budget_without_preallocation(quality):
    raw = b''.join(capture('singer'))
    report = Validator(quality=quality, max_input_bytes=2**63-1).run(io.BytesIO(raw))
    assert report == Validator(quality=quality).run(io.BytesIO(raw))


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
def test_external_catalog_excluded(quality, protocol):
    declaration, record, state = capture(protocol)
    msg = json.loads(declaration)
    catalog = {'streams': [msg]} if protocol == 'singer' else msg['catalog']
    raw = record + state
    report = Validator(quality=quality, max_input_bytes=len(raw)).run(io.BytesIO(raw), catalog=catalog)
    assert report == Validator(quality=quality).run(io.BytesIO(raw), catalog=catalog)
    assert report['exit_code'] == 0


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('compression', [None, 'gzip'])
def test_batch_bytes_excluded_and_limits_preserved(tmp_path, quality, compression):
    declaration, _, state = capture('singer')
    rows = encoded({'id': 1}) + encoded({'id': 2})
    path = tmp_path/'rows'
    data = gzip.compress(rows, mtime=0) if compression else rows
    path.write_bytes(data)
    batch = encoded({'type': 'BATCH', 'stream': 's', 'encoding': {'format': 'jsonl', 'compression': compression}, 'manifest': ['rows']})
    prefix = declaration + batch
    report = Validator(quality=quality, batch_root=tmp_path, max_input_bytes=len(prefix)).run(io.BytesIO(prefix + state))
    assert report['exit_code'] == 2 and report['streams'][0]['records'] == 2
    assert codes(report, quality) == {'TC020': 1}
    report = Validator(quality=quality, batch_root=tmp_path, max_input_bytes=len(prefix+state), check_duplicates=True, duplicate_limit=1).run(io.BytesIO(prefix+state))
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC018': 1}
    assert report['streams'][0]['records'] == 2 and path.read_bytes() == data


@pytest.mark.parametrize('format', ['text', 'json', 'junit', 'quality'])
def test_structured_operational_formats(format):
    declaration, record, state = capture('singer')
    prefix = declaration + record
    command = 'quality' if format == 'quality' else 'validate'
    args = [] if format == 'quality' else ['--format', format]
    result = cli(command, *args, '--max-input-bytes', len(prefix), input=prefix+state)
    assert result.returncode == 2 and result.stderr == b'' and b'TC008' not in result.stdout
    if format == 'text':
        assert b'TC020' in result.stdout and b'1 record(s)' in result.stdout and b'(stopped early)' in result.stdout
    elif format == 'junit':
        root = ET.fromstring(result.stdout)
        assert root.attrib['errors'] == '1' and root.attrib['failures'] == '0'
        assert root.find('testcase/error').attrib['type'] == 'TC020'
        props = {p.attrib['name']: p.attrib['value'] for p in root.findall('properties/property')}
        assert props == {'stopped_early': 'true', 'input_lines': '2', 'records': '1'}
    else:
        report = json.loads(result.stdout)
        assert report['stopped_early'] and report['input_lines'] == 2
        assert codes(report, format == 'quality') == {'TC020': 1}


def test_quality_prefix_key_samples_and_disabled_general_samples():
    declaration, record, state = capture('singer')
    record = encoded({'type': 'RECORD', 'stream': 's', 'record': {}})
    prefix = b'\n' + declaration + record
    report = Validator(quality=True, example_limit=0, key_example_limit=1, max_input_bytes=len(prefix)).run(io.BytesIO(prefix+record+state))
    assert report['errors_by_code'] == {'TC010': 1, 'TC020': 1}
    assert report['input_lines'] == 3 and report['streams'][0]['records'] == 1
    assert report['unassigned']['examples'] == [] and report['unassigned']['omitted_examples'] == 1
    assert report['streams'][0]['keys']['by_path'][0] == {'path': ['id'], 'missing_fields': 1, 'null_fields': 0,
          'examples': [{'kind': 'missing', 'source': '<stdin>', 'line': 3}], 'omitted_examples': 0}
    assert_schema(report, True)


@pytest.mark.parametrize('command', ['validate', 'quality'])
def test_external_catalog_size_limit_stays_separate(tmp_path, monkeypatch, capsys, command):
    import datawrench.tapcheck.cli as module
    path = tmp_path/'catalog.json'
    path.write_bytes(b' '*101)
    monkeypatch.setattr(module, 'MAX_LINE_BYTES', 100)
    assert module.main([command, str(tmp_path/'missing-input'), '--catalog', str(path), '--max-input-bytes', '0']) == 2
    result = capsys.readouterr()
    report = json.loads(result.out) if command == 'quality' else None
    assert 'TC020' in result.out and result.err == ''
    if report: assert report['coverage'] == 'partial_input' and report['messages'] == {}
    assert path.read_bytes() == b' '*101


@pytest.mark.parametrize('quality', [False, True])
def test_batch_line_limit_stays_independent(tmp_path, quality):
    declaration, _, state = capture('singer')
    batch = encoded({'type': 'BATCH', 'stream': 's', 'encoding': {'format': 'jsonl'}, 'manifest': ['rows']})
    rows = encoded({'id': 'x'*500}) + encoded({'id': 2})
    (tmp_path/'rows').write_bytes(rows)
    raw = declaration + batch + state
    report = Validator(quality=quality, batch_root=tmp_path, max_line_bytes=200, max_input_bytes=len(raw)).run(io.BytesIO(raw))
    assert report['exit_code'] == 1 and not report['stopped_early']
    assert codes(report, quality) == {'TC020': 1} and report['streams'][0]['records'] == 1
    assert report['messages'] == {'SCHEMA': 1, 'BATCH': 1, 'STATE': 1}
    assert (tmp_path/'rows').read_bytes() == rows


def test_earlier_error_limit_wins_and_never_claims_tail_checked():
    declaration, record, state = capture('singer')
    raw = declaration + b'{bad-json\n' + record + state
    report = Validator(max_errors=1, max_input_bytes=len(raw)-1).run(io.BytesIO(raw))
    assert report['exit_code'] == 1 and report['stopped_early']
    assert codes(report, False) == {'TC001': 1} and report['input_lines'] == 2
    assert report['messages'] == {'SCHEMA': 1} and report['streams'][0]['records'] == 0
