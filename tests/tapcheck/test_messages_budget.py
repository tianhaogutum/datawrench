"""Main processing events include parse diagnostics, but exclude BATCH rows."""
import copy
import gzip
import io
import json
import xml.etree.ElementTree as ET

import pytest

from datawrench.tapcheck.model import MISSING
from datawrench.tapcheck.engine import Validator
from datawrench.tapcheck.cli import render
from .test_validation_input_budget import capture, assert_schema, codes, encoded, cli
from .test_batch_budget import batch, payload
from .test_records_budget import Guarded


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
def test_repeated_state_regression(quality, protocol):
    state = capture(protocol)[2]
    raw = state*32
    options = dict(quality=quality, max_records=0, max_batch_files=0,
                   max_batch_bytes=0, max_batch_raw_bytes=0, max_input_bytes=len(raw))
    assert Validator(**options).run(io.BytesIO(raw))['messages'] == {'STATE':32}
    report = Validator(max_messages=2, **options).run(io.BytesIO(raw), source='cap')
    assert report['exit_code'] == 2 and report['stopped_early']
    assert report['messages'] == {'STATE':2} and report['input_lines'] == 2
    assert report['streams'] == [] and codes(report, quality) == {'TC020':1}
    d = report['unassigned']['examples'][0] if quality else report['diagnostics'][0]
    assert (d['source'], d['line']) == ('cap', 3)
    if quality:
        assert report['coverage'] == 'partial_input'
        assert {'remaining_input_after_stop', 'final_state_coverage'} <= set(report['not_evaluated'])
    else:
        assert '--max-messages' in d['message'] and 'event 3' in d['message']
    assert_schema(report, quality)


def unassigned(report, quality):
    return report['unassigned']['examples'] if quality else report['diagnostics']


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('limit', [0, 2, 3, 4, None, 2**63-1])
def test_boundaries_and_blank_physical_lines(protocol, quality, limit):
    declaration, row, state = capture(protocol)
    raw = b'\r\n'+declaration+b' \t\r\n'+row+state.rstrip(b'\n')
    engine = Validator(quality=quality, max_messages=limit)
    report = engine.run(io.BytesIO(raw), source='cap')
    assert_schema(report, quality)
    if limit is None or limit >= 3:
        assert report == Validator(quality=quality).run(io.BytesIO(raw), source='cap')
        assert report['exit_code'] == 0 and engine.processed_messages == 3
    else:
        assert report['exit_code'] == 2 and codes(report, quality) == {'TC020':1}
        assert report['input_lines'] == (0 if limit == 0 else 4)
        assert sum(report['messages'].values()) == engine.processed_messages == limit
        assert unassigned(report, quality)[-1]['line'] == (2 if limit == 0 else 5)
        if limit == 2:
            assert engine.streams[('s', None)].dirty
            assert report['streams'][0]['records'] == 1
            if quality: assert report['streams'][0]['states'] == 0


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('raw', [b'', b'\n \t\r\n', b' '*50])
def test_zero_empty_blanks(quality, raw):
    engine = Validator(quality=quality, max_messages=0)
    report = engine.run(io.BytesIO(raw))
    assert report == Validator(quality=quality).run(io.BytesIO(raw))
    assert codes(report, quality) == {'TC021':1}
    assert engine.processed_messages == 0 and not engine.seen_input


def events(protocol):
    declaration, row, state = capture(protocol)
    ancillary = ({'type':'ACTIVATE_VERSION', 'stream':'s', 'version':1} if protocol == 'singer'
                 else {'type':'LOG', 'log':{}})
    return [(declaration, {}), (row, {}), (state, {}), (encoded(ancillary), {}),
            (encoded({'type':'unknown'}), {'TC002':1}), (b'{bad\n', {'TC001':1}),
            (b'\xff\n', {'TC001':1}), (b'{"type":"STATE","type":"STATE"}\n', {'TC001':1}),
            (b'1e999\n', {'TC001':1}), (b'[]\n', {'TC003':1}),
            (b'{}\n', {'TC003':1}), (b'{"type":3}\n', {'TC003':1}),
            (encoded({'type':'STATE'}), {'TC007':1})]


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
def test_all_event_kinds_count_even_without_typed_message(protocol, quality):
    declaration, _, state = capture(protocol)
    for event, expected in events(protocol):
        engine = Validator(protocol=protocol, quality=quality, max_messages=2, example_limit=100)
        report = engine.run(io.BytesIO(declaration+event+b'\n'+state+state), source='cap')
        assert report['exit_code'] == 2 and report['input_lines'] == 2
        assert engine.processed_messages == 2
        assert codes(report, quality) == {**expected, 'TC020':1}
        assert unassigned(report, quality)[-1]['line'] == 4
        assert_schema(report, quality)
        # At the exact event count, keep original JSON/envelope diagnostics and EOF rules.
        raw = declaration+event+state
        assert Validator(protocol=protocol, quality=quality, max_messages=3).run(io.BytesIO(raw)) == Validator(
            protocol=protocol, quality=quality).run(io.BytesIO(raw))


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
def test_crossing_has_no_dispatch_or_state_side_effects(protocol, quality, monkeypatch):
    import datawrench.tapcheck.engine as mod
    import datawrench.tapcheck.io as iomod
    declaration, row, state = capture(protocol)
    crossings = [v for v, _ in events(protocol)] + [batch('never-open'),
        encoded({'type':'STATE', 'value':{}, 'state':{'data':{}}})]
    for crossing in crossings:
        engine = Validator(quality=quality, max_messages=3, check_duplicates=True,
                           check_order=True, key_example_limit=3)
        consume, error, decode = engine.consume, engine.error, iomod.decode
        calls, decoded, before = [], [], {}
        def forbidden(*a, **kw):
            raise AssertionError('crossing dispatched or BATCH opened')
        def traced_decode(raw):
            decoded.append(raw)
            return decode(raw)
        def traced_error(code, *a, **kw):
            assert code == 'TC020', 'crossing diagnostic was reported'
            return error(code, *a, **kw)
        def traced_consume(msg):
            calls.append(msg['type'])
            consume(msg)
            if len(calls) == 3:
                before['streams'] = copy.deepcopy({k:v for k,v in vars(engine.streams[('s', None)]).items() if k != 'validator'}, {id(MISSING): MISSING})
                before['keys'] = set(engine.seen_keys)
                before['messages'] = dict(engine.messages)
                if quality: before['quality'] = copy.deepcopy(vars(engine.quality.streams[('s', None)]))
                for hook in ['consume', 'register', 'cover_state', 'record', 'batch', 'load_catalog']:
                    monkeypatch.setattr(engine, hook, forbidden)
                monkeypatch.setattr(engine, 'error', traced_error)
        monkeypatch.setattr(engine, 'consume', traced_consume)
        monkeypatch.setattr(mod, 'open_beneath', forbidden)
        monkeypatch.setattr(iomod, 'decode', traced_decode)
        handle = Guarded(b'\n'+declaration+row+state+b' \r\n'+crossing+batch('tail')+b'{bad\n', 6)
        report = engine.run(handle, source='main')
        monkeypatch.setattr(iomod, 'decode', decode)
        assert handle.calls == 6 and not handle.closed
        assert len(calls) == 3 and engine.processed_messages == 3
        assert len(decoded) == (3 if crossing == b'\xff\n' else 4)
        assert dict(engine.messages) == before['messages']
        assert engine.seen_keys == before['keys']
        assert {k:v for k,v in vars(engine.streams[('s', None)]).items() if k != 'validator'} == before['streams']
        assert engine.protocol == protocol and not engine.streams[('s', None)].dirty
        if quality: assert vars(engine.quality.streams[('s', None)]) == before['quality']
        assert codes(report, quality) == {'TC020':1} and report['input_lines'] == 4
        assert (unassigned(report, quality)[0]['source'], unassigned(report, quality)[0]['line']) == ('main', 6)
        assert_schema(report, quality)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('compression', [None, 'gzip'])
@pytest.mark.parametrize('limit', [1, 2, 3, 4])
def test_batch_envelope_not_payload_counts(tmp_path, monkeypatch, quality, compression, limit):
    import datawrench.tapcheck.engine as mod
    declaration, _, state = capture('singer')
    name, stored = payload(tmp_path, encoded({'id':1})*32, compression)
    envelope = batch(name, compression=compression)
    opened, opening = [], mod.open_beneath
    def open_file(root, relative):
        opened.append(str(relative))
        assert len(opened) <= max(0, min(2, limit-1))
        return opening(root, relative)
    monkeypatch.setattr(mod, 'open_beneath', open_file)
    engine = Validator(quality=quality, max_messages=limit, batch_root=tmp_path)
    report = engine.run(io.BytesIO(declaration+envelope*2+state), source='cap')
    assert report['exit_code'] == (0 if limit == 4 else 2)
    assert engine.processed_messages == limit
    assert len(opened) == min(2, limit-1)
    assert report['streams'][0]['records'] == len(opened)*32
    assert report['messages'] == {'SCHEMA':1, **({'BATCH':len(opened)} if opened else {}),
                                  **({'STATE':1} if limit == 4 else {})}
    assert (tmp_path/name).read_bytes() == stored
    assert_schema(report, quality)


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
def test_external_and_inline_catalogs(protocol, quality):
    declaration, row, state = capture(protocol)
    obj = json.loads(declaration)
    catalog = {'streams':[obj, dict(obj, stream='t')]} if protocol == 'singer' else obj['catalog']
    if protocol == 'airbyte':
        catalog = {'streams':[dict(catalog['streams'][0], namespace=n) for n in ['a', 'b']]}
    engine = Validator(protocol=protocol, quality=quality, max_messages=0)
    report = engine.run(io.BytesIO(state), catalog=catalog)
    assert len(report['streams']) == 2 and report['messages'] == {} and report['input_lines'] == 0
    assert codes(report, quality) == {'TC020':1} and engine.processed_messages == 0
    if quality: assert [s['schemas'] for s in report['streams']] == [1, 1]
    if protocol == 'airbyte':
        r = Validator(quality=quality, max_messages=1).run(io.BytesIO(encoded({'type':'CATALOG','catalog':catalog})+state))
        assert len(r['streams']) == 2 and r['messages'] == {'CATALOG':1}
    # External invalid catalog fails before reading even when main allowance is zero.
    r = Validator(protocol=protocol, quality=quality, max_messages=0).run(Guarded(state, 0), catalog=[])
    assert codes(r, quality) == {'TC003':1}


@pytest.mark.parametrize('quality', [False, True])
def test_oversized_diagnostic_is_one_event_and_draining_precedes_gate(quality):
    # Existing reader drains a complete oversized physical line before yielding it,
    # even if it consists of whitespace and the processing allowance is exhausted.
    state = capture('singer')[2]
    raw = state+b' '*100+b'\n'+state
    for allowance in [0, 1, 2]:
        engine = Validator(protocol='singer', quality=quality, max_messages=allowance, max_line_bytes=40, example_limit=100)
        r = engine.run(io.BytesIO(raw))
        assert r['input_lines'] == allowance and engine.processed_messages == allowance
        assert r['exit_code'] == 2
        errors = unassigned(r, quality)
        assert len(errors) == (2 if allowance == 2 else 1)
        assert errors[-1]['line'] == allowance+1
        if not quality: assert '--max-messages' in errors[-1]['message']
    handle = Guarded(raw, 4)  # state + three bounded chunks of oversized whitespace
    r = Validator(max_messages=1, max_line_bytes=40).run(handle)
    assert handle.tell() == len(state)+101 and handle.calls == 4
    assert r['input_lines'] == 1 and r['diagnostics'][0]['line'] == 2


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('earlier', ['main-byte', 'record', 'manifest', 'payload-byte', 'raw-byte', 'streams', 'properties', 'duplicates', 'max-errors'])
def test_earlier_stops_keep_priority(tmp_path, quality, earlier):
    declaration, row, state = capture('singer')
    options = dict(quality=quality, max_messages=2, batch_root=tmp_path)
    main = declaration+row+state
    marker = {'main-byte':'--max-input-bytes', 'record':'--max-records', 'manifest':'--max-batch-files',
              'payload-byte':'--max-batch-bytes', 'raw-byte':'--max-batch-raw-bytes',
              'streams':'stream limit', 'properties':'statistics property', 'duplicates':'duplicate tracking',
              'max-errors':'invalid UTF-8'}[earlier]
    if earlier == 'main-byte': options['max_input_bytes'] = len(declaration)
    elif earlier == 'record': options['max_records'] = 0
    elif earlier in {'manifest', 'payload-byte', 'raw-byte'}:
        name, _ = payload(tmp_path, b'{"id":1}\n', None)
        main = declaration+batch(name)+state
        options[{'manifest':'max_batch_files', 'payload-byte':'max_batch_bytes', 'raw-byte':'max_batch_raw_bytes'}[earlier]] = 0
    elif earlier == 'streams':
        options['max_streams'] = 1
        main = declaration+encoded(dict(json.loads(declaration), stream='t'))+state
    elif earlier == 'properties':
        options['max_properties'] = 1
        main = declaration+encoded({'type':'RECORD','stream':'s','record':{'id':1,'x':2}})+state
    elif earlier == 'duplicates':
        options.update(duplicate_limit=1, check_duplicates=True, max_messages=3)
        main = declaration+row+encoded({'type':'RECORD','stream':'s','record':{'id':2}})+state
    else:
        options['max_errors'] = 1
        main = declaration+b'{bad\n'+state
    engine = Validator(**options)
    r = engine.run(io.BytesIO(main))
    if earlier == 'max-errors' and quality:
        assert r['errors_by_code'] == {'TC001':1,'TC020':1}
    else:
        assert r['exit_code'] == (1 if earlier == 'max-errors' else 2)
        if quality:
            examples = r['unassigned']['examples']+[d for s in r['streams'] for d in s['examples']]
            assert len(examples) == 1
        else:
            assert marker in r['diagnostics'][-1]['message']
            assert '--max-messages' not in r['diagnostics'][-1]['message']
    assert r['stopped_early']


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('tail,expected', [('state', {}), ('missing', {'TC008':1}), ('bad', {'TC001':1})])
def test_exact_count_retains_eof_json_and_final_state(protocol, quality, tail, expected):
    declaration, row, state = capture(protocol)
    raw = declaration+row+{'state':state+b' \r\n', 'missing':b'', 'bad':state+b'{bad'}[tail]
    allowance = {'state':3,'missing':2,'bad':4}[tail]
    r = Validator(quality=quality, max_messages=allowance).run(io.BytesIO(raw))
    assert r == Validator(quality=quality).run(io.BytesIO(raw))
    assert codes(r, quality) == expected and not r['stopped_early']


@pytest.mark.parametrize('quality', [False, True])
def test_batch_diagnostics_and_gzip_integrity_not_hidden(tmp_path, quality):
    declaration, _, state = capture('singer')
    raw = gzip.compress(b'{bad\n[]\n{"id":1}\n', mtime=0)
    stored = raw[:-8]+bytes([raw[-8]^1])+raw[-7:]
    (tmp_path/'bad.gz').write_bytes(stored)
    main = declaration+batch('bad.gz', compression='gzip')+state
    r = Validator(quality=quality, max_messages=3, batch_root=tmp_path).run(io.BytesIO(main))
    assert r == Validator(quality=quality, batch_root=tmp_path).run(io.BytesIO(main))
    assert codes(r, quality) == {'TC001':1, 'TC003':1, 'TC017':1}
    assert r['streams'][0]['records'] == 1
    assert (tmp_path/'bad.gz').read_bytes() == stored


@pytest.mark.parametrize('value', [-1, 2**63, True, False, 1.0, '1', [], {}])
def test_invalid_api(value):
    with pytest.raises(ValueError, match='max_messages'):
        Validator(max_messages=value)


@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('value', ['-1', '1.5', 'bad', str(2**63)])
def test_invalid_cli_before_input_and_catalog_open(tmp_path, command, value):
    result = cli(command, tmp_path/'absent', '--catalog', tmp_path/'absent-catalog', '--max-messages', value)
    assert result.returncode == 2 and not result.stdout and b'--max-messages' in result.stderr


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('limit', [0, 2, 3, 4, None, 2**63-1])
def test_cli_catalog_boundaries_and_immutable_input(tmp_path, protocol, command, external, limit):
    declaration, row, state = capture(protocol)
    obj = json.loads(declaration)
    catalog = {'streams':[obj]} if protocol == 'singer' else obj['catalog']
    main = b'\n'+(b'' if external else declaration)+row+state+b' \r\n'
    path, cp = tmp_path/'capture', tmp_path/'catalog'
    path.write_bytes(main)
    catalog_raw = encoded(catalog)
    cp.write_bytes(catalog_raw)
    # File and stdin coverage split across catalog variants.
    args = [path if external else '-', '--protocol', protocol, '--max-input-bytes', len(main)]
    if external: args += ['--catalog', cp]
    if limit is not None: args += ['--max-messages', limit]
    if command == 'validate': args += ['--format', 'json']
    result = cli(command, *args, **({} if external else {'input':main}))
    r = json.loads(result.stdout)
    stopped = limit is not None and limit < (2 if external else 3)
    assert result.returncode == r['exit_code'] == (2 if stopped else 0)
    assert not result.stderr
    assert_schema(r, command == 'quality')
    if stopped and command == 'validate':
        assert 'stopped early' in render(r, 'text') and '--max-messages' in render(r, 'text')
        xml = ET.fromstring(render(r, 'junit'))
        assert xml.find('testcase/error').attrib['type'] == 'TC020'
        assert xml.find("properties/property[@name='input_lines']").attrib['value'] == str(r['input_lines'])
    assert path.read_bytes() == main and cp.read_bytes() == catalog_raw


@pytest.mark.parametrize('example_limit', [0, 1])
def test_quality_allowance_independent_of_error_samples(example_limit):
    state = capture('singer')[2]
    r = Validator(quality=True, protocol='singer', max_messages=2, example_limit=example_limit).run(
        io.BytesIO(b'{bad\n'*2+state))
    assert r['errors_by_code'] == {'TC001':2, 'TC020':1}
    assert len(r['unassigned']['examples']) == example_limit
    assert r['coverage'] == 'partial_input' and r['messages'] == {}
    assert r['input_lines'] == 2


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('kind', ['TRACE', 'CONTROL'])
def test_airbyte_other_ancillary_events(quality, kind):
    event = encoded({'type':kind, kind.lower():{}})
    r = Validator(protocol='airbyte', quality=quality, max_messages=1).run(io.BytesIO(event*2))
    assert codes(r, quality) == {'TC020':1} and r['messages'] == {kind:1}
    assert r['input_lines'] == 1


@pytest.mark.parametrize('quality', [False, True])
def test_byte_read_failure_wins_while_producing_crossing_event(quality):
    declaration, _, state = capture('singer')
    r = Validator(quality=quality, max_messages=1, max_input_bytes=len(declaration)+1).run(
        io.BytesIO(declaration+state))
    assert r['input_lines'] == 1 and r['messages'] == {'SCHEMA':1}
    assert codes(r, quality) == {'TC020':1}
    if not quality:
        assert '--max-input-bytes' in r['diagnostics'][0]['message']
        assert '--max-messages' not in r['diagnostics'][0]['message']
