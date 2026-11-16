"""Accepted object records bound evaluation independently of parsing and bytes."""
import copy
import gzip
import io
import json
import xml.etree.ElementTree as ET

import pytest

from datawrench.tapcheck.engine import Validator
from datawrench.tapcheck.cli import render
from datawrench.tapcheck.model import MISSING
from .test_batch_budget import DECLARATION, INLINE, STATE, batch, payload, validate
from .test_validation_input_budget import encoded, capture, cli, assert_schema, codes


def issue(report, quality):
    return report['streams'][0]['examples'][-1] if quality else report['diagnostics'][-1]


@pytest.mark.parametrize('quality', [False, True])
def test_repeated_small_rows_regression(tmp_path, quality):
    raw = b'{"id":1}\n'*32
    name, _ = payload(tmp_path, raw, None)
    main = DECLARATION+batch(name)+STATE
    options = dict(quality=quality, max_batch_files=1, max_batch_bytes=288,
                   max_batch_raw_bytes=288)
    assert validate(tmp_path, main, **options)['streams'][0]['records'] == 32
    report = validate(tmp_path, main, max_records=2, **options)
    assert report['exit_code'] == 2 and report['stopped_early']
    assert codes(report, quality) == {'TC020': 1}
    assert report['streams'][0]['records'] == 2
    assert report['messages'] == {'SCHEMA': 1, 'BATCH': 1} and report['input_lines'] == 2
    d = issue(report, quality)
    assert (d['source'], d['line'], d['path']) == (name, 3, None)
    if quality:
        assert report['coverage'] == 'partial_input'
        assert {'remaining_input_after_stop', 'final_state_coverage'} <= set(report['not_evaluated'])
    else:
        assert '--max-records' in d['message'] and 'record 3' in d['message']
    assert_schema(report, quality)


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('limit', [0, 2, 3, 4, None, 2**63-1])
def test_inline_boundaries(protocol, quality, limit):
    declaration, row, state = capture(protocol)
    main = declaration+row*3+state
    report = Validator(quality=quality, max_records=limit).run(io.BytesIO(main), source='cap')
    assert_schema(report, quality)
    if limit is None or limit >= 3:
        assert report == Validator(quality=quality).run(io.BytesIO(main), source='cap')
        assert report['exit_code'] == 0
    else:
        assert report['exit_code'] == 2 and codes(report, quality) == {'TC020': 1}
        assert report['streams'][0]['records'] == limit
        assert report['messages']['RECORD'] == limit+1
        assert report['input_lines'] == limit+2
        assert (issue(report, quality)['source'], issue(report, quality)['line']) == ('cap', limit+2)
    # No object rows: zero does not suppress declarations/STATE or empty-input errors.
    for raw, expected in [(declaration+state, 0), (b'', 2)]:
        r = Validator(protocol=protocol, quality=quality, max_records=0).run(io.BytesIO(raw))
        assert r['exit_code'] == expected
        assert r == Validator(protocol=protocol, quality=quality).run(io.BytesIO(raw))


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('compression', [None, 'gzip'])
def test_zero_empty_batch_and_schema_invalid_count(tmp_path, quality, compression):
    name, _ = payload(tmp_path, b'\n \r\n', compression)
    assert validate(tmp_path, DECLARATION+batch(name, compression=compression)+STATE,
                    quality=quality, max_records=0)['exit_code'] == 0
    declaration = json.loads(DECLARATION)
    declaration['schema']['required'] = ['id']
    main = encoded(declaration)+encoded({'type': 'RECORD', 'stream': 's', 'record': {}})+INLINE+STATE
    report = validate(tmp_path, main, max_records=1, quality=quality, key_example_limit=2)
    assert report['exit_code'] == 2
    assert codes(report, quality) == {'TC006': 1, 'TC010': 1, 'TC020': 1}
    assert report['streams'][0]['records'] == 1
    if quality:
        assert report['streams'][0]['keys']['missing_fields'] == 1
        assert report['streams'][0]['keys']['by_path'][0]['examples'] == [
            {'kind': 'missing', 'source': 'capture', 'line': 2}]


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
def test_rejections_precede_admission(protocol, quality):
    declaration, row, state = capture(protocol)
    obj = json.loads(row)
    nonobject = copy.deepcopy(obj)
    undeclared = copy.deepcopy(obj)
    badname = copy.deepcopy(obj)
    if protocol == 'singer':
        nonobject['record'] = []
        undeclared['stream'] = 'unknown'
        badname['stream'] = []
        malformed = {'type': 'RECORD'}
    else:
        nonobject['record']['data'] = []
        undeclared['record']['stream'] = 'unknown'
        badname['record']['stream'] = []
        malformed = {'type': 'RECORD', 'record': []}
    prefix = declaration+b'{bad\n'+encoded(nonobject)+encoded(undeclared)+encoded(badname)+encoded(malformed)+state
    engine = Validator(quality=quality, max_records=0, example_limit=100)
    report = engine.run(io.BytesIO(prefix+row+state), source='cap')
    expected = ['TC001', 'TC003', 'TC005', 'TC003', 'TC003', 'TC020']
    assert report['exit_code'] == 2 and report['streams'][0]['records'] == 0
    if quality:
        assert report['errors_by_code'] == {'TC001': 1, 'TC003': 3, 'TC005': 1, 'TC020': 1}
        assert report['streams'][0]['states'] == 1
    else:
        assert [d['code'] for d in report['diagnostics']] == expected
    # STATE immediately before crossing row remains clean: no record dirty mutation.
    assert not engine.streams[('s', None)].dirty
    assert engine.records == 0
    assert_schema(report, quality)
    # At exact allowance, the same malformed/nonobject/undeclared tail is still checked.
    no_stop = Validator(quality=quality, max_records=0).run(io.BytesIO(prefix))
    assert no_stop['exit_code'] == 1 and not no_stop['stopped_early']
    assert 'TC020' not in codes(no_stop, quality)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('compression', [None, 'gzip'])
@pytest.mark.parametrize('split', [False, True])
def test_cumulative_streams_repeats_and_sources(tmp_path, quality, compression, split):
    name, stored = payload(tmp_path, encoded({'id': 1}), compression, 'a')
    other, _ = payload(tmp_path, b'\n'+encoded({})+encoded({'id': None})+encoded({'id': 9}), compression, 'b')
    second = json.loads(DECLARATION)
    second['stream'] = 't'
    first = batch(name, name, compression=compression)
    rest = batch(other, compression=compression, stream='t')
    if split:
        first = batch(name, compression=compression)*2
    main = DECLARATION+encoded(second)+INLINE+first+rest+batch('never-open')+INLINE+STATE
    engine = Validator(batch_root=tmp_path, quality=quality, max_records=5,
                       check_duplicates=True, key_example_limit=3, example_limit=100)
    report = engine.run(io.BytesIO(main), source='cap')
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC010': 2 if quality else 1, 'TC016': 1, 'TC020': 1}
    assert [s['records'] for s in report['streams']] == [3, 2]
    assert sum(s.count for s in engine.streams.values()) == engine.records == 5
    assert report['messages'] == {'SCHEMA': 2, 'RECORD': 1, 'BATCH': 3 if split else 2}
    d = report['streams'][1]['examples'][-1] if quality else report['diagnostics'][-1]
    assert (d['source'], d['line']) == (other, 4)
    if quality:
        a, b = report['streams']
        assert a['duplicates']['duplicate_keys'] == 1
        assert b['keys']['missing_fields'] == b['keys']['null_fields'] == 1
        assert b['keys']['by_path'][0]['examples'] == [
            {'kind': 'missing', 'source': other, 'line': 2}, {'kind': 'null', 'source': other, 'line': 3}]
    assert (tmp_path/name).read_bytes() == stored
    assert_schema(report, quality)


@pytest.mark.parametrize('quality', [False, True])
def test_airbyte_namespaces_share_allowance(quality):
    declaration, row, state = map(json.loads, capture('airbyte'))
    declaration['catalog']['streams'] *= 2
    declaration['catalog']['streams'] = [dict(s, namespace=n) for s, n in zip(declaration['catalog']['streams'], ['a', 'b'])]
    rows = [encoded(dict(row, record=dict(row['record'], namespace=n))) for n in ['a', 'b', 'a']]
    report = Validator(quality=quality, max_records=2, check_duplicates=True).run(
        io.BytesIO(encoded(declaration)+b''.join(rows)+encoded(state)))
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC020': 1}
    assert [s['records'] for s in report['streams']] == [1, 1]


class Guarded(io.BytesIO):
    def __init__(self, raw, requests):
        super().__init__(raw)
        self.requests = requests
        self.calls = 0
    def readline(self, size=-1):
        self.calls += 1
        assert self.calls <= self.requests, 'unread tail requested'
        return super().readline(size)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('location', ['inline', 'plain', 'gzip'])
def test_crossing_has_no_record_side_effects_or_tail_requests(tmp_path, monkeypatch, quality, location):
    import datawrench.tapcheck.engine as mod
    declaration = json.loads(DECLARATION)
    declaration['replication_key'] = 'id'
    admitted = {'id': 3}
    crossing = {'id': None, 'unseen': None}
    engine = Validator(batch_root=tmp_path, quality=quality, max_records=1, strict=True,
                       check_order=True, check_duplicates=True, key_example_limit=5, example_limit=100)
    engine.consume(declaration)
    # Run an admitted object then cover it; the crossing row must leave dirty=False.
    engine.record(('s', None), admitted, '2026-01-01T00:00:00Z')
    engine.consume(json.loads(STATE))
    stream = engine.streams[('s', None)]
    before = copy.deepcopy({k:v for k,v in vars(stream).items() if k != 'validator'})
    q_before = copy.deepcopy(vars(engine.quality.streams[('s', None)])) if quality else None
    keys = set(engine.seen_keys)
    def forbidden(*a, **kw):
        raise AssertionError('crossing record evaluated')
    class NoSchema:
        iter_errors = forbidden
    stream.validator = NoSchema()
    monkeypatch.setattr(mod, 'at_path', forbidden)
    monkeypatch.setattr(mod, 'timestamp', forbidden)
    monkeypatch.setattr(mod.hashlib, 'sha256', forbidden)
    monkeypatch.setattr(engine, 'cursor_value', forbidden)
    if quality:
        monkeypatch.setattr(type(engine.quality.streams[('s', None)]), 'record', forbidden)
    opened, requested = [], []
    if location == 'inline':
        row = encoded({'type': 'RECORD', 'stream': 's', 'record': crossing})
        handle = Guarded(row+STATE+b'{bad\n', 1)
    else:
        compression = 'gzip' if location == 'gzip' else None
        name, _ = payload(tmp_path, encoded(crossing)+b'{bad\n'+encoded({})*20, compression)
        opening, reader = mod.open_beneath, mod.lines
        def open_file(root, relative):
            opened.append(str(relative))
            assert opened == [name]
            if compression is None:
                return Guarded((tmp_path/relative).read_bytes(), 1)
            return opening(root, relative)
        def guarded_lines(h, source, *a, **kw):
            for number, value in reader(h, source, *a, **kw):
                if source == name:
                    requested.append(number)
                    assert requested == [1]
                yield number, value
        monkeypatch.setattr(mod, 'open_beneath', open_file)
        monkeypatch.setattr(mod, 'lines', guarded_lines)
        handle = Guarded(batch(name, 'later', compression=compression)+STATE, 1)
    report = engine.run(handle, source='cap')
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC020': 1}
    assert engine.records == stream.count == 1 and engine.seen_keys == keys
    assert {k:v for k,v in vars(stream).items() if k != 'validator'} == before
    if quality:
        # Only the operational diagnostic is allowed; no record/key examples/counters.
        after = vars(engine.quality.streams[('s', None)])
        assert {k:v for k,v in after.items() if k != 'issues'} == {k:v for k,v in q_before.items() if k != 'issues'}
        assert report['streams'][0]['examples'] == [{'code': 'TC020', 'source': 'cap' if location == 'inline' else name, 'line': 1, 'path': None}]
    assert handle.calls == 1


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('failure', ['schema', 'max-errors', 'capacity', 'properties', 'main-bytes', 'batch-bytes', 'raw-bytes', 'manifest'])
def test_earlier_failures_retain_priority(tmp_path, quality, failure):
    if failure == 'max-errors' and quality:
        pytest.skip('quality scans through ordinary errors')
    options = dict(quality=quality, max_records=1)
    main = DECLARATION+INLINE+INLINE+STATE
    if failure == 'schema':
        # First admitted row still counts when schema evaluation fails.
        engine = Validator(**options)
        engine.consume(json.loads(DECLARATION))
        class Broken:
            def iter_errors(self, data): raise RuntimeError()
        engine.streams[('s', None)].validator = Broken()
        report = engine.run(io.BytesIO(INLINE*2+STATE))
        assert codes(report, quality) == {'TC004': 1, 'TC020': 1}
        assert report['streams'][0]['records'] == engine.records == 1
        return
    if failure == 'max-errors':
        options['max_errors'] = 1
        main = DECLARATION+encoded({'type':'RECORD','stream':'s','record':{}})+INLINE+STATE
        expected = 'TC010'
    elif failure == 'capacity':
        options.update(check_duplicates=True, duplicate_limit=1, max_records=2)
        new = json.loads(INLINE); new['record']['id'] = 99
        main = DECLARATION+INLINE+encoded(new)+INLINE+STATE
        expected = 'TC018'
    elif failure == 'properties':
        options['max_properties'] = 1
        new = json.loads(INLINE); new['record']['extra'] = None
        main = DECLARATION+encoded(new)+INLINE+STATE
        expected = 'TC020'
    elif failure == 'main-bytes':
        options['max_input_bytes'] = len(DECLARATION+INLINE)
        expected = 'TC020'
    else:
        name, _ = payload(tmp_path, b'{"id":1}\n'*3, None)
        main = DECLARATION+batch(name)+STATE
        options[{'batch-bytes':'max_batch_bytes','raw-bytes':'max_batch_raw_bytes','manifest':'max_batch_files'}[failure]] = 0
        expected = 'TC020'
    report = validate(tmp_path, main, **options)
    assert codes(report, quality) == {expected: 1}
    assert report['exit_code'] == (1 if failure == 'max-errors' else 2)
    d = report['unassigned']['examples'][-1] if quality and failure == 'main-bytes' else issue(report, quality)
    if expected == 'TC020':
        assert '--max-records' not in d.get('message', '')
    if failure in ['batch-bytes', 'raw-bytes', 'manifest']:
        assert report['streams'][0]['records'] == 0


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('tail,expected', [('state', {}), ('missing', {'TC008': 1}), ('bad', {'TC001': 1})])
def test_exact_count_keeps_eof_checks(protocol, quality, tail, expected):
    declaration, row, state = capture(protocol)
    main = declaration+row+{'state':state,'missing':b'', 'bad':state+b'{bad'}[tail]
    r = Validator(quality=quality, max_records=1).run(io.BytesIO(main))
    assert codes(r, quality) == expected and not r['stopped_early']
    assert r == Validator(quality=quality).run(io.BytesIO(main))


@pytest.mark.parametrize('quality', [False, True])
def test_exact_count_keeps_gzip_integrity(tmp_path, quality):
    raw = gzip.compress(b'{"id":1}\n', mtime=0)
    (tmp_path/'bad.gz').write_bytes(raw[:-8]+bytes([raw[-8]^1])+raw[-7:])
    main = DECLARATION+batch('bad.gz', compression='gzip')+STATE
    report = validate(tmp_path, main, max_records=1, quality=quality)
    assert report['exit_code'] == 1 and codes(report, quality) == {'TC017': 1}
    assert not report['stopped_early']
    assert report == validate(tmp_path, main, quality=quality)


@pytest.mark.parametrize('value', [-1, 2**63, True, False, 1.0, '1', [], {}])
def test_invalid_api(value):
    with pytest.raises(ValueError, match='max_records'):
        Validator(max_records=value)


@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('value', ['-1', '1.5', 'bad', str(2**63)])
def test_invalid_cli(tmp_path, command, value):
    result = cli(command, tmp_path/'absent', '--max-records', value)
    assert result.returncode == 2 and not result.stdout and b'--max-records' in result.stderr


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('limit', [0, 1, 2, 2**63-1])
def test_cli_catalogs_formats_and_immutable_input(tmp_path, protocol, command, mode, limit):
    declaration, row, state = capture(protocol)
    obj = json.loads(declaration)
    catalog = {'streams':[obj]} if protocol == 'singer' else obj['catalog']
    path, cp = tmp_path/'capture', tmp_path/'catalog'
    main = row*2+state
    catalog_raw = json.dumps(catalog).encode()
    path.write_bytes(main); cp.write_bytes(catalog_raw)
    args = [path if mode == 'file' else '-', '--protocol', protocol, '--catalog', cp,
            '--max-input-bytes', len(main), '--max-records', limit]
    if command == 'validate': args += ['--format', 'json']
    result = cli(command, *args, **({'input':main} if mode == 'stdin' else {}))
    assert not result.stderr
    r = json.loads(result.stdout)
    assert result.returncode == r['exit_code'] == (2 if limit < 2 else 0)
    assert r['streams'][0]['records'] == min(2, limit)
    assert_schema(r, command == 'quality')
    if command == 'validate' and limit < 2:
        text = render(r, 'text')
        assert '--max-records' in text and 'stopped early' in text
        xml = ET.fromstring(render(r, 'junit'))
        assert xml.find('testcase/error').attrib['type'] == 'TC020'
        assert xml.find("properties/property[@name='records']").attrib['value'] == str(limit)
    assert path.read_bytes() == main and cp.read_bytes() == catalog_raw


@pytest.mark.parametrize('example_limit', [0, 1])
def test_quality_error_sample_does_not_control_allowance(example_limit):
    row = encoded({'type':'RECORD','stream':'s','record':{}})
    report = Validator(quality=True, max_records=2, example_limit=example_limit, key_example_limit=1).run(
        io.BytesIO(DECLARATION+row*4+STATE))
    assert report['errors_by_code'] == {'TC010':2, 'TC020':1}
    assert report['streams'][0]['records'] == 2
    assert len(report['streams'][0]['examples']) == example_limit
    assert report['streams'][0]['keys']['by_path'][0]['omitted_examples'] == 1
    assert report['coverage'] == 'partial_input'


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('compression', [None, 'gzip'])
def test_batch_rejections_before_admission_and_oversized_line(tmp_path, quality, compression):
    # Only the final object is admitted. Malformed/nonobject/oversized rows
    # keep their existing diagnostics even at zero record allowance.
    rows = b'{bad\n[]\n'+b' '*2048+b'\n'+b'{}\n'+b'{bad\n'
    name, _ = payload(tmp_path, rows, compression)
    engine = Validator(batch_root=tmp_path, quality=quality, max_records=0,
                       max_line_bytes=1024, example_limit=100)
    report = engine.run(io.BytesIO(DECLARATION+batch(name, compression=compression)+STATE))
    assert report['exit_code'] == 2 and report['streams'][0]['records'] == 0
    assert engine.records == 0
    if quality:
        assert report['errors_by_code'] == {'TC001':1, 'TC003':1, 'TC020':2}
        examples = report['streams'][0]['examples']
    else:
        examples = report['diagnostics']
    assert [(d['code'], d['source'], d['line']) for d in examples] == [
        ('TC001', name, 1), ('TC003', name, 2), ('TC020', name, 3), ('TC020', name, 4)]
    assert_schema(report, quality)
