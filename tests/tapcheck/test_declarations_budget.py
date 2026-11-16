"""Registration attempts, including repeated and schema-invalid declarations."""
import copy
import io
import json

import pytest

from datawrench.tapcheck.engine import Validator
from .test_validation_input_budget import assert_schema, codes, encoded, capture, cli
from .test_records_budget import Guarded
from .test_batch_budget import batch


def entry(protocol, name='s', namespace=None, schema=None):
    schema = {'type':'object', 'properties':{'id':{'type':'integer'}}} if schema is None else schema
    if protocol == 'singer':
        return {'stream':name, 'schema':schema, 'key_properties':['id']}
    return {'stream':{'name':name, 'namespace':namespace, 'json_schema':schema}, 'primary_key':[['id']]}


def catalog_raw(entries):
    return encoded({'type':'CATALOG', 'catalog':{'streams':entries}})


def schema_raw(value):
    return encoded(dict(value, type='SCHEMA'))


def issue(report, quality, key=None):
    if not quality: return report['diagnostics'][-1]
    return (report['unassigned'] if key is None else next(s for s in report['streams'] if (s['name'], s['namespace']) == key))['examples'][-1]


@pytest.mark.parametrize('quality', [False, True])
def test_repeated_catalog_regression(quality):
    raw = catalog_raw([entry('airbyte')]*32)
    options = dict(quality=quality, max_messages=1, max_records=0,
                   max_batch_files=0, max_input_bytes=len(raw))
    baseline = Validator(**options).run(io.BytesIO(raw))
    assert baseline['exit_code'] == 0 and baseline['messages'] == {'CATALOG':1}
    if quality: assert baseline['streams'][0]['schemas'] == 32
    engine = Validator(max_declarations=2, **options)
    report = engine.run(io.BytesIO(raw), source='cap')
    assert report['exit_code'] == 2 and report['stopped_early']
    assert report['messages'] == {'CATALOG':1} and report['input_lines'] == 1
    assert codes(report, quality) == {'TC020':1}
    d = issue(report, quality, ('s', None))
    assert (d['source'], d['line'], d['path']) == ('cap', 1, ['streams', 2])
    if quality:
        assert report['streams'][0]['schemas'] == 2
        assert report['coverage'] == 'partial_input'
        assert {'remaining_input_after_stop', 'final_state_coverage'} <= set(report['not_evaluated'])
    else: assert '--max-declarations' in d['message'] and 'attempt 3' in d['message']
    assert_schema(report, quality)


@pytest.mark.parametrize('protocol,external', [('singer',False), ('singer',True), ('airbyte',False), ('airbyte',True)])
@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('limit', [0, 2, 3, 4, None, 2**63-1])
def test_boundaries(protocol, external, quality, limit):
    entries = [entry(protocol), entry(protocol), entry(protocol, 'other', 'ns' if protocol == 'airbyte' else None)]
    cat = {'streams':entries} if external else None
    state = capture(protocol)[2]
    raw = state if external else (b'\n'+b''.join(schema_raw(e) for e in entries)+state if protocol == 'singer'
                                 else b'\n'+catalog_raw(entries)+state)
    options = dict(protocol=protocol, quality=quality)
    engine = Validator(max_declarations=limit, **options)
    report = engine.run(io.BytesIO(raw), source='cap', catalog=cat)
    assert_schema(report, quality)
    if limit is None or limit >= 3:
        assert report == Validator(**options).run(io.BytesIO(raw), source='cap', catalog=cat)
        assert report['exit_code'] == 0 and engine.declarations == 3
    else:
        assert report['exit_code'] == 2 and codes(report, quality) == {'TC020':1}
        assert engine.declarations == limit
        assert len(engine.streams) == (0 if limit == 0 else 1)
        d = (report['diagnostics'][-1] if not quality else
             (report['unassigned']['examples'] or report['streams'][0]['examples'])[-1])
        assert d['source'] == ('<catalog>' if external else 'cap')
        assert d['line'] == (0 if external else limit+2 if protocol == 'singer' else 2)
        assert d['path'] == (['streams', limit] if external or protocol == 'airbyte' else None)
        assert report['input_lines'] == (0 if external else limit+2 if protocol == 'singer' else 2)
        if quality:
            assert sum(s['schemas'] for s in report['streams']) == limit
            assert report['coverage'] == 'partial_input'


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
def test_cumulative_external_and_inline(protocol, quality):
    first = entry(protocol)
    external = {'streams':[first, first]}
    declaration = schema_raw(first) if protocol == 'singer' else catalog_raw([first, first])
    reader = Guarded(declaration+capture(protocol)[2], 1)
    engine = Validator(protocol=protocol, quality=quality, max_declarations=2)
    report = engine.run(reader, source='main', catalog=external, catalog_source='catalog.json')
    assert report['exit_code'] == 2 and engine.declarations == 2 and reader.calls == 1
    assert report['input_lines'] == 1
    d = issue(report, quality, ('s', None))
    assert (d['source'], d['line']) == ('main', 1)
    if quality: assert report['streams'][0]['schemas'] == 2


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
def test_zero_empty_catalogs_and_no_declarations(protocol, quality):
    state = capture(protocol)[2]
    for raw, cat in [(state, None), (state, {'streams':[]}), (b'', None),
                     (catalog_raw([])+state, None)]:
        options = dict(protocol=protocol, quality=quality)
        engine = Validator(max_declarations=0, **options)
        assert engine.run(io.BytesIO(raw), catalog=cat) == Validator(**options).run(io.BytesIO(raw), catalog=cat)
        assert engine.declarations == 0
    # Empty external catalogs cannot infer an automatic protocol, as before.
    report = Validator(quality=quality, max_declarations=0).run(io.BytesIO(state), catalog={'streams':[]})
    assert codes(report, quality) == {'TC022':1}


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
def test_rejected_envelope_name_and_path_do_not_consume(protocol, quality):
    valid = entry(protocol)
    invalid = [None, {}, {'streams':None}, {'streams':[None]}]
    broken = []
    if protocol == 'singer':
        for field, value in [('stream',None), ('stream',2), ('key_properties',None),
                             ('key_properties',[1]), ('bookmark_properties',1), ('bookmark_properties',['a','b']),
                             ('replication_key',5)]:
            broken.append(dict(valid, **{field:value}))
        missing = dict(valid); del missing['schema']; broken.append(missing)
    else:
        for field, value in [('name',None), ('name',2), ('namespace',5)]:
            e = copy.deepcopy(valid); e['stream'][field] = value; broken.append(e)
        for field, value in [('primary_key',None), ('primary_key',[[]]), ('primary_key',[[1]]),
                             ('cursor_field',[1])]:
            broken.append(dict(valid, **{field:value}))
        broken += [{'stream':{}}, {'stream':2}]
    raws = [catalog_raw([e]) for e in broken] if protocol == 'airbyte' else [schema_raw(e) for e in broken]
    if protocol == 'airbyte': raws += [encoded({'type':'CATALOG', 'catalog':c}) for c in invalid]
    good = schema_raw(valid) if protocol == 'singer' else catalog_raw([valid])
    for bad in raws:
        engine = Validator(protocol=protocol, quality=quality, max_declarations=0, example_limit=100)
        report = engine.run(io.BytesIO(bad+good))
        assert engine.declarations == 0 and report['exit_code'] == 2
        assert codes(report, quality) == {'TC003':1, 'TC020':1}
        # External malformed catalogs remain operational before main input.
    for cat in invalid[1:] + [{'streams':[e]} for e in broken]:
        engine = Validator(protocol=protocol, quality=quality, max_declarations=0)
        report = engine.run(Guarded(b'no reads', 0), catalog=cat)
        assert codes(report, quality) == {'TC003':1} and engine.declarations == 0


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('variant,code', [('identical',None), ('changed','TC009'), ('invalid','TC004')])
def test_repeat_changed_invalid_attempts(protocol, quality, variant, code):
    first = entry(protocol)
    second = entry(protocol, schema={'type':'object'} if variant == 'changed' else {'type':'bad'}) if variant != 'identical' else first
    raw_entry = schema_raw if protocol == 'singer' else lambda e: catalog_raw([e])
    raw = raw_entry(first)+raw_entry(second)+raw_entry(first)+capture(protocol)[2]
    engine = Validator(quality=quality, max_declarations=2)
    report = engine.run(io.BytesIO(raw))
    assert report['exit_code'] == 2 and engine.declarations == 2
    assert codes(report, quality) == ({code:1, 'TC020':1} if code else {'TC020':1})
    assert engine.streams[('s',None)].schema == first.get('schema', first.get('stream',{}).get('json_schema') if protocol == 'airbyte' else None)
    if quality: assert report['streams'][0]['schemas'] == (2 if variant == 'identical' else 1)
    # At the exact allowance diagnostics remain ordinary contract errors.
    exact = raw_entry(first)+raw_entry(second)+capture(protocol)[2]
    assert Validator(quality=quality, max_declarations=2).run(io.BytesIO(exact)) == Validator(quality=quality).run(io.BytesIO(exact))


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('external', [False, True])
def test_crossing_invalid_schema_does_not_compile(protocol, quality, external, monkeypatch):
    import datawrench.tapcheck.engine as mod
    def forbidden(*args, **kwargs): raise AssertionError('crossing schema compiled')
    monkeypatch.setattr(mod, 'compile_schema', forbidden)
    value = entry(protocol, schema={'type':'bad'})
    cat = {'streams':[value]} if external else None
    raw = b'' if external else schema_raw(value) if protocol == 'singer' else catalog_raw([value])
    engine = Validator(protocol=protocol, quality=quality, max_declarations=0)
    report = engine.run(Guarded(raw, 0 if external else 1), catalog=cat)
    assert codes(report, quality) == {'TC020':1} and report['streams'] == []
    assert engine.declarations == 0


@pytest.mark.parametrize('bad', [-1, 2**63, True, False, 1.5, '1', [], {}])
def test_invalid_api(bad):
    with pytest.raises(ValueError, match='max_declarations must be between'):
        Validator(max_declarations=bad)


@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('bad', ['-1', str(2**63), '1.5', 'true', 'no'])
def test_invalid_cli(command, bad):
    result = cli(command, 'absent-file', '--catalog', 'absent-catalog', '--max-declarations', bad)
    assert result.returncode == 2 and result.stdout == b''
    assert b'--max-declarations' in result.stderr and b'TC021' not in result.stderr


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('protocol,external', [('singer',False), ('singer',True), ('airbyte',False), ('airbyte',True)])
@pytest.mark.parametrize('crossing', ['repeat', 'new', 'invalid'])
def test_gate_has_no_compilation_signature_or_registration_effects(quality, protocol, external, crossing, monkeypatch):
    import datawrench.tapcheck.engine as mod
    from datawrench.tapcheck.model import MISSING
    engine = Validator(protocol=protocol, quality=quality, max_declarations=2, check_duplicates=True,
                       key_example_limit=5, example_limit=100)
    first = entry(protocol)
    engine.source, engine.line = 'prefix', 1
    if protocol == 'singer': engine.singer_schema(first)
    else: engine.catalog({'streams':[first]})
    engine.record(('s',None), {'id':None}, 0 if protocol == 'airbyte' else None)
    engine.record(('s',None), {'id':1}, 0 if protocol == 'airbyte' else None)
    engine.cover_state([('s',None)])
    value = first if crossing == 'repeat' else entry(protocol, 'new' if crossing == 'new' else 's', schema={'type':'bad'})
    # One admitted repeat then the crossing entry; a poisoned later entry must not be touched.
    class Untouched(dict):
        def get(self, *a, **kw): raise AssertionError('later catalog entry inspected')
    entries = [first, value, Untouched()]
    original_compile = mod.compile_schema
    original_dumps = json.dumps
    compiles, signatures, registrations = [], [], []
    def compile_spy(schema, strict):
        compiles.append(schema)
        assert len(compiles) == 1, 'crossing compile'
        return original_compile(schema, strict)
    def dumps_spy(value, *a, **kw):
        if isinstance(value, list) and len(value) == 3 and isinstance(value[1], list):
            signatures.append(value)
            assert len(signatures) == 1, 'crossing signature serialized'
        return original_dumps(value, *a, **kw)
    def snapshot():
        return copy.deepcopy((
            {k:{n:v for n,v in vars(s).items() if n != 'validator'} for k,s in engine.streams.items()},
            vars(engine.quality) if quality else None, engine.seen_keys, engine.records, engine.declarations),
            {id(MISSING):MISSING})
    original_register = engine.register
    def register_spy(*a, **kw):
        registrations.append(a[0])
        assert len(registrations) <= 2
        before = snapshot()
        if len(registrations) == 1: return original_register(*a, **kw)
        # Observe immediately before the allowed stop diagnostic changes error buckets.
        original_error = engine.error
        def stop_error(code, *args, **kwargs):
            assert code == 'TC020' and kwargs['operational']
            assert snapshot() == before
            return original_error(code, *args, **kwargs)
        monkeypatch.setattr(engine, 'error', stop_error)
        return original_register(*a, **kw)
    def forbidden(*a, **kw): raise AssertionError('later BATCH open or row request')
    raw = b'\n'+schema_raw(first)+schema_raw(value)+batch('never') if protocol == 'singer' else b'\n'+catalog_raw(entries[:2])+batch('never')
    monkeypatch.setattr(mod, 'compile_schema', compile_spy)
    monkeypatch.setattr(json, 'dumps', dumps_spy)
    monkeypatch.setattr(engine, 'register', register_spy)
    monkeypatch.setattr(mod, 'open_beneath', forbidden)
    # External entries can carry the poison object directly, rather than JSON encode it.
    reader = Guarded(raw, 0 if external else 3 if protocol == 'singer' else 2)
    report = engine.run(reader, source='cap', catalog={'streams':entries} if external else None,
                        catalog_source='external.json')
    assert report['exit_code'] == 2 and engine.declarations == 2
    assert len(compiles) == len(signatures) == 1 and len(registrations) == 2
    assert not engine.streams[('s',None)].dirty and engine.streams[('s',None)].count == 2
    if quality:
        assert report['streams'][0]['schemas'] == 2
        assert report['streams'][0]['keys']['by_path'][0]['examples'] == [{'kind':'null', 'source':'prefix', 'line':1}]
    assert reader.calls == (0 if external else 3 if protocol == 'singer' else 2)
    assert_schema(report, quality)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('stop', ['messages','bytes','records','files','batch-bytes','batch-raw','streams','properties','errors'])
def test_earlier_stops_keep_priority(quality, stop, tmp_path):
    declaration, row, state = capture('singer')
    later = schema_raw(entry('singer'))
    options = dict(protocol='singer', quality=quality, max_declarations=1)
    raw = declaration
    expected = '--'+{'messages':'max-messages', 'bytes':'max-input-bytes', 'records':'max-records',
                     'files':'max-batch-files','batch-bytes':'max-batch-bytes','batch-raw':'max-batch-raw-bytes'}.get(stop, '')
    if stop == 'messages': options['max_messages'] = 1; raw += later
    elif stop == 'bytes': options['max_input_bytes'] = len(raw); raw += later
    elif stop == 'records': options['max_records'] = 0; raw += row+later
    elif stop in {'files','batch-bytes','batch-raw'}:
        (tmp_path/'rows').write_bytes(b'{"id":1}\n')
        options['batch_root'] = tmp_path
        options[{'files':'max_batch_files','batch-bytes':'max_batch_bytes','batch-raw':'max_batch_raw_bytes'}[stop]] = 0
        raw += batch('rows')+later
    elif stop == 'streams':
        options['max_streams'] = 1; raw += schema_raw(entry('singer', 'other')); expected = 'stream limit'
    elif stop == 'properties':
        options['max_properties'] = 1
        e = entry('singer', schema={'properties':{'a':{}, 'b':{}}})
        raw = schema_raw(e)+later; expected = 'schema property limit'
    else:
        if quality: pytest.skip('quality scans through ordinary errors independently of max_errors')
        options['max_errors'] = 1; raw += b'{}\n'+later; expected = 'TC003'
    engine = Validator(**options)
    report = engine.run(io.BytesIO(raw))
    if stop == 'errors':
        assert report['exit_code'] == 1 and codes(report, quality) == {'TC003':1}
    else:
        assert report['exit_code'] == 2 and codes(report, quality) == {'TC020':1}
        if not quality: assert expected in report['diagnostics'][-1]['message']
    assert engine.declarations == 1


@pytest.mark.parametrize('protocol', ['singer','airbyte'])
@pytest.mark.parametrize('quality', [False, True])
def test_exact_boundary_keeps_eof_parse_and_final_state(protocol, quality):
    declaration, row, state = capture(protocol)
    for tail, expected in [(state+b'\n', {}), (b'', {'TC008':1}), (b'{bad\n'+state, {'TC001':1})]:
        raw = declaration+row+tail
        options = dict(quality=quality, max_declarations=1)
        report = Validator(**options).run(io.BytesIO(raw))
        assert report == Validator(quality=quality).run(io.BytesIO(raw))
        assert codes(report, quality) == expected
    # Earlier record causes dirty state; crossing repeat must skip the tail STATE verdict.
    report = Validator(quality=quality, max_declarations=1).run(Guarded(declaration+row+declaration+state, 3))
    assert codes(report, quality) == {'TC020':1} and report['streams'][0]['records'] == 1


@pytest.mark.parametrize('protocol', ['singer','airbyte'])
@pytest.mark.parametrize('command', ['validate','quality'])
@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('limit', [0, 1, 2**63-1])
def test_cli_commands_catalog_locations_and_immutable_inputs(tmp_path, protocol, command, external, limit):
    e = entry(protocol)
    raw = capture(protocol)[2] if external else schema_raw(e) if protocol == 'singer' else catalog_raw([e])
    path = tmp_path/'main'; path.write_bytes(raw)
    cat = tmp_path/'catalog'; cat.write_bytes(encoded({'streams':[e]}))
    before = (path.read_bytes(), cat.read_bytes())
    args = [path, '--protocol', protocol, '--max-declarations', limit]
    if external: args += ['--catalog',cat]
    quality = command == 'quality'
    if not quality: args += ['--format','json']
    result = cli(command, *args)
    report = json.loads(result.stdout)
    assert_schema(report, quality)
    assert result.stderr == b'' and result.returncode == report['exit_code'] == (2 if limit == 0 else 0)
    if limit == 0:
        d = issue(report, quality)
        assert (d['source'], d['line']) == (str(cat) if external else str(path), 0 if external else 1)
        assert d['path'] == (['streams',0] if external or protocol == 'airbyte' else None)
    assert (path.read_bytes(), cat.read_bytes()) == before


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
def test_schema_invalid_attempts_before_any_success_and_external_abort(protocol, quality):
    e = entry(protocol, schema={'type':'bad'})
    declaration = schema_raw(e) if protocol == 'singer' else catalog_raw([e])
    engine = Validator(protocol=protocol, quality=quality, max_declarations=2)
    report = engine.run(Guarded(declaration*4, 3))
    assert engine.declarations == 2 and report['exit_code'] == 2 and report['streams'] == []
    assert report['errors'] == 3
    if quality: assert report['errors_by_code'] == {'TC004':2, 'TC020':1}
    else: assert [d['code'] for d in report['diagnostics']] == ['TC004','TC004','TC020']
    engine = Validator(protocol=protocol, quality=quality, max_declarations=2)
    report = engine.run(Guarded(b'no main reads', 0), catalog={'streams':[entry(protocol), e, e]})
    assert engine.declarations == 2 and codes(report, quality) == {'TC004':1} and report['exit_code'] == 2
    # All external registration failures use the catalog's honest entry location.
    d = issue(report, quality)
    assert (d['source'], d['line'], d['path']) == ('<catalog>', 0, ['streams', 1])


@pytest.mark.parametrize('quality', [False, True])
def test_singer_catalog_alias_and_metadata(quality):
    e = entry('singer'); e['tap_stream_id'] = e.pop('stream')
    e['metadata'] = [{'breadcrumb':[], 'metadata':{'replication-key':'id'}}]
    engine = Validator(protocol='singer', quality=quality, max_declarations=1)
    report = engine.run(Guarded(b'', 0), catalog={'streams':[e,e]}, catalog_source='discovery.json')
    assert report['exit_code'] == 2 and engine.streams[('s',None)].cursor == ('id',)
    d = issue(report, quality, ('s',None))
    assert (d['source'],d['line'],d['path']) == ('discovery.json',0,['streams',1])


@pytest.mark.parametrize('quality', [False, True])
def test_batch_prefix_readers_and_later_open_guard(tmp_path, monkeypatch, quality):
    import datawrench.tapcheck.engine as mod
    declaration = schema_raw(entry('singer'))
    (tmp_path/'rows').write_bytes(b'{"id":1}\n')
    (tmp_path/'later').write_bytes(b'never read')
    calls, handles = [], []
    def guarded_open(root, relative):
        calls.append(str(relative))
        assert calls == ['rows'], 'later BATCH opened'
        handle = Guarded(b'{"id":1}\n',2); handles.append(handle)
        return handle
    monkeypatch.setattr(mod,'open_beneath',guarded_open)
    raw = declaration+batch('rows')+declaration+batch('later')+capture('singer')[2]
    engine = Validator(protocol='singer', quality=quality, max_declarations=1, batch_root=tmp_path)
    main = Guarded(raw,3)
    report = engine.run(main)
    assert main.calls == 3 and handles[0].calls == 2 and handles[0].closed
    assert report['streams'][0]['records'] == 1 and engine.streams[('s',None)].dirty
    assert report['messages'] == {'SCHEMA':2,'BATCH':1} and codes(report,quality) == {'TC020':1}


@pytest.mark.parametrize('quality', [False, True])
def test_exact_declaration_allowance_preserves_gzip_failure(tmp_path, quality):
    import gzip
    declaration = schema_raw(entry('singer'))
    (tmp_path/'rows.gz').write_bytes(gzip.compress(b'{"id":1}\n')[:-4])
    raw = declaration+batch('rows.gz','gzip')+capture('singer')[2]
    options = dict(protocol='singer', quality=quality, batch_root=tmp_path)
    assert Validator(max_declarations=1,**options).run(io.BytesIO(raw)) == Validator(**options).run(io.BytesIO(raw))


@pytest.mark.parametrize('quality', [False, True])
def test_duplicate_capacity_precedes_later_declaration(quality):
    declaration = schema_raw(entry('singer'))
    raw = declaration+b'{"type":"RECORD","stream":"s","record":{"id":1}}\n'+b'{"type":"RECORD","stream":"s","record":{"id":2}}\n'+declaration
    engine = Validator(quality=quality, max_declarations=1, check_duplicates=True, duplicate_limit=1)
    report = engine.run(io.BytesIO(raw))
    assert codes(report,quality) == {'TC018':1} and engine.declarations == 1


@pytest.mark.parametrize('quality', [False, True])
def test_same_name_different_namespaces_share_allowance(quality):
    entries = [entry('airbyte','s','a'),entry('airbyte','s','b'),entry('airbyte','s','a')]
    engine = Validator(quality=quality,max_declarations=2)
    report = engine.run(Guarded(catalog_raw(entries)+capture('airbyte')[2],1))
    assert report['exit_code'] == 2 and set(engine.streams) == {('s','a'),('s','b')}
    assert engine.declarations == 2 and codes(report,quality) == {'TC020':1}
    if quality: assert [(s['namespace'],s['schemas']) for s in report['streams']] == [('a',1),('b',1)]
    d = issue(report,quality,('s','a'))
    assert d['path'] == ['streams',2] and d['line'] == 1
