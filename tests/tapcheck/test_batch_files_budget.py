"""Manifest occurrences bound path work independently from BATCH byte delivery."""
import gzip
import io
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from datawrench.tapcheck.engine import Validator
from datawrench.tapcheck.cli import render
from .test_batch_budget import DECLARATION, INLINE, STATE, batch, payload, validate
from .test_validation_input_budget import encoded, cli, assert_schema, codes


def track_paths(monkeypatch):
    import datawrench.tapcheck.engine as engine
    events = {k: [] for k in ('parse', 'resolve', 'stat', 'open')}
    parse, resolve, stat, opening = engine.urlsplit, Path.resolve, Path.stat, engine.open_beneath
    def parsing(raw):
        events['parse'].append(raw)
        return parse(raw)
    def resolving(path, *a, **kw):
        events['resolve'].append(path.name)
        return resolve(path, *a, **kw)
    def stating(path, *a, **kw):
        events['stat'].append(path.name)
        return stat(path, *a, **kw)
    def opened(root, relative):
        events['open'].append(str(relative))
        return opening(root, relative)
    monkeypatch.setattr(engine, 'urlsplit', parsing)
    monkeypatch.setattr(Path, 'resolve', resolving)
    monkeypatch.setattr(Path, 'stat', stating)
    monkeypatch.setattr(engine, 'open_beneath', opened)
    return events


def issue(report, quality):
    return report['streams'][0]['examples'][-1] if quality else report['diagnostics'][-1]


@pytest.mark.parametrize('quality', [False, True])
def test_repeated_empty_files_stop_before_path_work(tmp_path, monkeypatch, quality):
    (tmp_path/'empty').write_bytes(b'')
    main = DECLARATION+batch(*(['empty']*32))+INLINE+STATE
    validator = Validator(batch_root=tmp_path, quality=quality, max_batch_files=2,
                          max_batch_bytes=0, max_batch_raw_bytes=0)
    events = track_paths(monkeypatch)
    report = validator.run(io.BytesIO(main), source='capture')
    assert report['exit_code'] == 2 and report['stopped_early']
    assert codes(report, quality) == {'TC020': 1}
    assert events['parse'] == events['resolve'] == events['open'] == ['empty']*2
    assert len(events['stat']) >= 2 and set(events['stat']) == {'empty'}
    assert report['streams'][0]['records'] == 0
    assert report['messages'] == {'SCHEMA': 1, 'BATCH': 1} and report['input_lines'] == 2
    d = issue(report, quality)
    assert (d['source'], d['line'], d['path']) == ('capture', 2, ['manifest', 2])
    if quality:
        assert report['coverage'] == 'partial_input'
        assert {'remaining_input_after_stop', 'final_state_coverage'} <= set(report['not_evaluated'])
    else:
        assert 'occurrence 3' in d['message'] and '--max-batch-files' in d['message']
    assert_schema(report, quality)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('compression', [None, 'gzip'])
@pytest.mark.parametrize('n', [0, 2, 3, 4, None, 2**63-1])
def test_occurrence_boundaries_and_unlimited(tmp_path, quality, compression, n):
    name, stored = payload(tmp_path, b'', compression)
    main = DECLARATION+batch(name, name, name, compression=compression)+STATE
    report = validate(tmp_path, main, quality=quality, max_batch_files=n)
    if n is None or n >= 3:
        assert report == validate(tmp_path, main, quality=quality)
        assert report['exit_code'] == 0
    else:
        assert report['exit_code'] == 2 and codes(report, quality) == {'TC020': 1}
        assert issue(report, quality)['path'] == ['manifest', n]
    assert (tmp_path/name).read_bytes() == stored
    # No BATCH occurrences: zero changes nothing.
    assert validate(tmp_path, DECLARATION+INLINE+STATE, quality=quality, max_batch_files=0) == validate(
        tmp_path, DECLARATION+INLINE+STATE, quality=quality)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('bad', ['missing', 'https://remote/x', '../escape', 'a\x00b', 'x?query', 'file://%00'])
def test_invalid_paths_charge_but_over_budget_paths_untouched(tmp_path, monkeypatch, quality, bad):
    (tmp_path/'empty').write_bytes(b'')
    validator = Validator(batch_root=tmp_path, quality=quality, max_batch_files=1)
    events = track_paths(monkeypatch)
    report = validator.run(io.BytesIO(DECLARATION+batch(bad, 'empty', 'later')+STATE))
    assert codes(report, quality) == {'TC017': 1, 'TC020': 1} and report['exit_code'] == 2
    assert events['parse'] == [bad] and events['open'] == []
    assert 'empty' not in events['resolve']+events['stat']
    assert issue(report, quality)['path'] == ['manifest', 1]


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('variant', ['empty', 'nonlist', 'nonstring', 'encoding', 'undeclared', 'bad-stream'])
def test_rejected_envelopes_keep_priority_and_do_not_charge(tmp_path, quality, variant):
    (tmp_path/'empty').write_bytes(b'')
    msg = json.loads(batch('empty'))
    if variant == 'empty': msg['manifest'] = []
    if variant == 'nonlist': msg['manifest'] = 'empty'
    if variant == 'nonstring': msg['manifest'] += [None]
    if variant == 'encoding': msg['encoding']['format'] = 'parquet'
    if variant == 'undeclared': msg['stream'] = 'unknown'
    if variant == 'bad-stream': msg['stream'] = None
    main = DECLARATION+encoded(msg)+batch('empty')+STATE
    code = {'undeclared': 'TC005', 'bad-stream': 'TC003'}.get(variant, 'TC017')
    report = validate(tmp_path, main, quality=quality, max_batch_files=1)
    assert report['exit_code'] == 1 and not report['stopped_early']
    assert codes(report, quality) == {code: 1} and report['messages']['STATE'] == 1
    report = validate(tmp_path, DECLARATION+encoded(msg)+STATE, quality=quality, max_batch_files=0)
    assert codes(report, quality) == {code: 1} and report['exit_code'] == 1


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('example_limit', [0, 100])
def test_mixed_streams_envelopes_exact_prefix(tmp_path, monkeypatch, quality, example_limit):
    plain, a = payload(tmp_path, encoded({})+encoded({'id': None})+encoded({'id': 1}), None, 'plain')
    zipped, b = payload(tmp_path, encoded({'id': 1}), 'gzip', 'zipped')
    declaration = json.loads(DECLARATION)
    declaration['stream'] = 't'
    main = b'\n'+DECLARATION+encoded(declaration)+INLINE+batch(zipped, stream='t', compression='gzip')
    main += batch(plain, plain)+batch('never\x1b', 'later')+INLINE+STATE
    validator = Validator(batch_root=tmp_path, quality=quality, max_batch_files=3,
                          example_limit=example_limit, key_example_limit=2, check_duplicates=True)
    events = track_paths(monkeypatch)
    report = validator.run(io.BytesIO(main), source='capture\x1b')
    assert report['exit_code'] == 2 and report['input_lines'] == 7
    assert report['messages'] == {'SCHEMA': 2, 'RECORD': 1, 'BATCH': 3}
    assert [s['records'] for s in report['streams']] == [7, 1]
    assert events['open'] == events['parse'] == [zipped, plain, plain]
    assert 'never\x1b' not in events['stat']+events['resolve']
    if quality:
        s = report['streams'][0]
        assert s['keys']['missing_fields'] == s['keys']['null_fields'] == 2
        assert s['duplicates']['duplicate_keys'] == 1
        assert s['duplicates']['evaluated_records'] == 3
        assert s['keys']['by_path'][0]['examples'] == [
            {'kind': 'missing', 'source': plain, 'line': 1}, {'kind': 'null', 'source': plain, 'line': 2}]
        assert report['errors_by_code'] == {'TC010': 4, 'TC016': 1, 'TC020': 1}
        assert len(s['examples']) == (6 if example_limit else 0)
        assert s['omitted_examples'] == (0 if example_limit else 6)
    else:
        d = issue(report, quality)
        assert (d['source'], d['line'], d['path']) == ('capture\x1b', 7, ['manifest', 0])
        assert 'occurrence 4' in d['message'] and 'never' not in d['message']
        assert '\x1b' not in render(report, 'text')
        ET.fromstring(render(report, 'junit'))
    assert_schema(report, quality)
    assert (tmp_path/plain).read_bytes() == a and (tmp_path/zipped).read_bytes() == b


@pytest.mark.parametrize('quality', [False, True])
def test_no_post_stop_main_read_or_tail_scan(tmp_path, quality):
    prefix = DECLARATION+batch('missing', 'never-open')
    class Guard(io.BytesIO):
        def readline(self, size=-1):
            assert self.tell() < len(prefix), 'main tail requested after occurrence stop'
            return super().readline(size)
    handle = Guard(prefix+b'{bad-json\n'+INLINE+STATE+b' '*100000)
    report = Validator(batch_root=tmp_path, quality=quality, max_batch_files=0).run(handle)
    assert handle.tell() == len(prefix) and report['exit_code'] == 2
    assert report['input_lines'] == 2 and codes(report, quality) == {'TC020': 1}


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('failure,code', [('json', 'TC001'), ('state', 'TC008'), ('gzip', 'TC017')])
def test_exact_fit_preserves_eof_errors(tmp_path, quality, failure, code):
    raw = b'{bad\n' if failure == 'json' else encoded({'id': 1})
    name, _ = payload(tmp_path, raw, None)
    compression = None
    if failure == 'gzip':
        compression = 'gzip'
        (tmp_path/name).write_bytes(bytes.fromhex('1f8b080000000000020307')+b'\0'*8)
    main = DECLARATION+batch(name, compression=compression)+(b'' if failure == 'state' else STATE)
    report = validate(tmp_path, main, quality=quality, max_batch_files=1)
    assert report['exit_code'] == 1 and not report['stopped_early']
    assert codes(report, quality) == {code: 1}


@pytest.mark.parametrize('quality', [False, True])
def test_recoverable_open_failure_stays_charged(tmp_path, monkeypatch, quality):
    import datawrench.tapcheck.engine as engine
    (tmp_path/'empty').write_bytes(b'')
    opened = []
    def denied(root, relative):
        opened.append(str(relative))
        raise OSError('synthetic open failure')
    monkeypatch.setattr(engine, 'open_beneath', denied)
    report = validate(tmp_path, DECLARATION+batch('empty', 'empty')+STATE, quality=quality, max_batch_files=1)
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC017': 1, 'TC020': 1}
    assert opened == ['empty']


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('kind', ['raw', 'payload', 'main', 'line', 'duplicate'])
def test_independent_limits_and_earlier_priority(tmp_path, quality, kind):
    rows = encoded({'id': 1})+encoded({'id': 2})
    (tmp_path/'rows').write_bytes(rows)
    opts = {'raw': {'max_batch_raw_bytes': 0}, 'payload': {'max_batch_bytes': 0},
            'main': {'max_input_bytes': len(DECLARATION)}, 'line': {'max_line_bytes': 200},
            'duplicate': {'check_duplicates': True, 'duplicate_limit': 1}}[kind]
    if kind == 'line': (tmp_path/'rows').write_bytes(encoded({'id': 'x'*500}))
    report = validate(tmp_path, DECLARATION+batch('rows', 'never')+STATE, quality=quality, max_batch_files=1, **opts)
    assert report['exit_code'] == 2
    if kind == 'duplicate': assert codes(report, quality) == {'TC018': 1}
    elif kind == 'line':
        assert report['errors'] == 2
        assert issue(report, quality)['path'] == ['manifest', 1]
    else:
        assert codes(report, quality) == {'TC020': 1}
        d = report['unassigned']['examples'][-1] if quality and kind == 'main' else issue(report, quality)
        assert d['path'] is None
    # Occurrence stop precedes both byte allowances for the first inadmissible file.
    report = validate(tmp_path, DECLARATION+batch('rows')+STATE, quality=quality,
                      max_batch_files=0, max_batch_raw_bytes=0, max_batch_bytes=0)
    assert issue(report, quality)['path'] == ['manifest', 0]


def test_earlier_error_limit_wins(tmp_path):
    report = validate(tmp_path, DECLARATION+batch('missing', 'never')+STATE, max_batch_files=1, max_errors=1)
    assert report['exit_code'] == 1 and report['stopped_early']
    assert codes(report, False) == {'TC017': 1}


@pytest.mark.parametrize('value', [-1, 2**63, True, False, 1.0, '1', [], {}])
def test_invalid_api(value):
    with pytest.raises(ValueError, match='max_batch_files'): Validator(max_batch_files=value)


@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('value', ['-1', '1.5', 'bad', str(2**63)])
def test_invalid_cli_before_io(tmp_path, command, value):
    result = cli(command, tmp_path/'missing', '--catalog', tmp_path/'missing-catalog', '--max-batch-files', value)
    assert result.returncode == 2 and not result.stdout
    assert b'--max-batch-files' in result.stderr and b'TC021' not in result.stderr


@pytest.mark.parametrize('format', ['text', 'json', 'junit', 'quality'])
@pytest.mark.parametrize('mode', ['stdin', 'file'])
def test_cli_formats_catalog_and_immutable_inputs(tmp_path, format, mode):
    (tmp_path/'empty').write_bytes(b'')
    path, catalog = tmp_path/'main', tmp_path/'catalog'
    main = batch('empty', 'empty')+STATE
    path.write_bytes(main)
    catalog_raw = json.dumps({'streams': [json.loads(DECLARATION)]}).encode()
    catalog.write_bytes(catalog_raw)
    cmd = 'quality' if format == 'quality' else 'validate'
    args = ['--catalog', catalog, '--batch-root', tmp_path, '--max-batch-files', 1,
            '--max-input-bytes', len(main), '--max-batch-bytes', 0, '--max-batch-raw-bytes', 0]
    if format != 'quality': args += ['--format', format]
    result = cli(cmd, path if mode == 'file' else '-', *args, input=main if mode == 'stdin' else None)
    assert result.returncode == 2 and not result.stderr and b'TC008' not in result.stdout
    if format == 'text': assert b'TC020' in result.stdout and b'occurrence 2' in result.stdout and b'stopped early' in result.stdout
    elif format == 'junit':
        xml = ET.fromstring(result.stdout)
        assert xml.find('testcase/error').attrib['type'] == 'TC020'
        assert "['manifest', 1]" in xml.find('testcase/error').text
    else:
        report = json.loads(result.stdout)
        assert_schema(report, format == 'quality')
        assert issue(report, format == 'quality')['line'] == 1
        assert report['input_lines'] == 1 and report['messages'] == {'BATCH': 1}
    assert path.read_bytes() == main and catalog.read_bytes() == catalog_raw
    assert (tmp_path/'empty').read_bytes() == b''


@pytest.mark.parametrize('quality', [False, True])
def test_zero_blocks_even_invalid_uri_before_all_path_work(tmp_path, monkeypatch, quality):
    validator = Validator(batch_root=tmp_path, quality=quality, max_batch_files=0)
    events = track_paths(monkeypatch)
    report = validator.run(io.BytesIO(DECLARATION+batch('https://bad\x1b', 'missing')+STATE))
    assert all(not values for values in events.values())
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC020': 1}
    assert issue(report, quality)['path'] == ['manifest', 0]
    assert_schema(report, quality)


@pytest.mark.parametrize('quality', [False, True])
def test_zero_does_not_change_airbyte_contracts(quality):
    from .test_validation_input_budget import capture
    main = b''.join(capture('airbyte'))
    assert Validator(quality=quality, max_batch_files=0).run(io.BytesIO(main)) == Validator(quality=quality).run(io.BytesIO(main))
    main += batch('unused')
    report = Validator(protocol='airbyte', quality=quality, max_batch_files=0).run(io.BytesIO(main))
    assert report['exit_code'] == 1 and codes(report, quality) == {'TC002': 1}
