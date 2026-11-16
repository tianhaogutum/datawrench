"""External registration owns a temporary, entry-level diagnostic location."""
from collections import Counter
import copy
import io

import pytest

from datawrench.tapcheck.engine import Validator
from .test_declarations_budget import entry, issue, schema_raw, catalog_raw
from .test_validation_input_budget import assert_schema, codes, capture, encoded
from .test_records_budget import Guarded
from .test_batch_budget import batch


@pytest.mark.parametrize('protocol', ['singer', 'airbyte'])
@pytest.mark.parametrize('quality', [False, True])
def test_invalid_external_schema_regression(protocol, quality):
    engine = Validator(protocol=protocol, quality=quality)
    reader = Guarded(b'no main reads', 0)
    report = engine.run(reader, source='main.jsonl', catalog_source='discovery.json',
                        catalog={'streams':[entry(protocol), entry(protocol, schema={'type':'bad'})]})
    assert codes(report, quality) == {'TC004':1} and report['exit_code'] == 2
    d = issue(report, quality)
    assert (d['source'], d['line'], d['path']) == ('discovery.json', 0, ['streams', 1])
    assert engine.declarations == 2 and len(engine.streams) == 1
    assert report['input_lines'] == 0 and report['messages'] == {} and reader.calls == 0
    assert_schema(report, quality)


FLAVORS = ['singer', 'airbyte-discovery', 'airbyte-configured']


def external_entry(flavor, schema=None):
    protocol = flavor.split('-')[0]
    value = entry(protocol, schema=schema)
    if flavor == 'singer':
        value['tap_stream_id'] = value.pop('stream')
        value['metadata'] = [{'breadcrumb':[], 'metadata':{'replication-key':'id'}}]
    if flavor == 'airbyte-discovery':
        value = dict(value['stream'], source_defined_primary_key=[['id']], default_cursor_field=['id'])
    return value


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('flavor,failure', [(flavor, failure) for flavor in FLAVORS
    for failure in ['entry', 'missing-schema', 'name', 'namespace', 'primary', 'cursor', 'schema']
    if flavor != 'singer' or failure != 'namespace'])
def test_external_entry_failures(flavor, quality, failure):
    good = external_entry(flavor)
    bad = copy.deepcopy(good)
    inner = bad.get('stream', bad) if flavor != 'singer' else bad
    if failure == 'entry': bad = 17
    elif failure == 'missing-schema': del inner['schema' if flavor == 'singer' else 'json_schema']
    elif failure == 'name': inner['tap_stream_id' if flavor == 'singer' else 'name'] = 4
    elif failure == 'namespace': inner['namespace'] = 4
    elif failure == 'primary': bad['key_properties' if flavor == 'singer' else 'primary_key'] = [1]
    elif failure == 'cursor': bad['replication_key' if flavor == 'singer' else 'cursor_field'] = [1]
    else: inner['schema' if flavor == 'singer' else 'json_schema'] = {'type':'bad'}
    reader = Guarded(b'no main reads', 0)
    engine = Validator(quality=quality)
    report = engine.run(reader, source='main', catalog={'streams':[good, bad, good]})
    assert codes(report, quality) == {('TC004' if failure == 'schema' else 'TC003'):1}
    assert report['exit_code'] == 2 and report['stopped_early']
    d = issue(report, quality)
    assert (d['source'], d['line'], d['path']) == ('<catalog>', 0, ['streams', 1])
    assert engine.declarations == (2 if failure == 'schema' else 1)
    assert len(engine.streams) == 1 and report['messages'] == {} and report['input_lines'] == 0
    assert reader.calls == 0
    assert (engine.source, engine.line, engine._catalog_source, engine._catalog_path) == ('main', 0, None, None)
    if quality:
        assert report['streams'][0]['schemas'] == 1
        assert report['coverage'] == 'partial_input'
        assert {'remaining_input_after_stop', 'final_state_coverage'} <= set(report['not_evaluated'])
    assert_schema(report, quality)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('protocol', ['auto', 'singer', 'airbyte'])
@pytest.mark.parametrize('catalog', [[], {}, {'streams':None}, {'streams':[]}, {'streams':[False]}])
def test_root_and_inference_locations(quality, protocol, catalog):
    # Empty catalogs with an explicit protocol succeed and proceed to the main STATE.
    raw = capture('singer' if protocol == 'singer' else 'airbyte')[2]
    success = protocol != 'auto' and catalog == {'streams':[]}
    engine = Validator(protocol=protocol, quality=quality)
    report = engine.run(io.BytesIO(raw), source='main', catalog=catalog, catalog_source='catalog.json')
    assert report['exit_code'] == (0 if success else 2)
    if not success:
        d = issue(report, quality)
        assert (d['source'], d['line'], d['path']) == ('catalog.json', 0, ['streams',0] if catalog == {'streams':[False]} else None)
        assert codes(report, quality) == {('TC022' if protocol == 'auto' and catalog != {'streams':[False]} else 'TC003'):1}
    assert engine._catalog_source is engine._catalog_path is None
    assert_schema(report, quality)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('flavor', FLAVORS)
@pytest.mark.parametrize('capacity', ['streams', 'properties', 'declarations'])
def test_external_capacity_priority_and_prefix(flavor, quality, capacity, monkeypatch):
    import datawrench.tapcheck.engine as mod
    good = external_entry(flavor)
    bad = copy.deepcopy(good)
    inner = bad.get('stream', bad) if flavor != 'singer' else bad
    if capacity == 'streams': inner['tap_stream_id' if flavor == 'singer' else 'name'] = 'new'
    elif capacity == 'properties': inner['schema' if flavor == 'singer' else 'json_schema']['properties']['other'] = {}
    options = {'max_'+capacity:1}
    # Streams wins over the exhausted declaration allowance; declarations wins over invalid schema.
    if capacity == 'streams': options['max_declarations'] = 1
    if capacity == 'declarations': inner['schema' if flavor == 'singer' else 'json_schema'] = {'type':'bad'}
    class Untouched(dict):
        def get(self, *a, **kw): raise AssertionError('later entry inspected')
    def forbidden(*a, **kw): raise AssertionError('BATCH opened after catalog stop')
    monkeypatch.setattr(mod, 'open_beneath', forbidden)
    engine = Validator(quality=quality, **options)
    reader = Guarded(b'no main reads', 0)
    report = engine.run(reader, source='main', catalog_source='capacity.json',
                        catalog={'streams':[good, bad, Untouched()]})
    assert codes(report, quality) == {'TC020':1} and report['exit_code'] == 2
    d = issue(report, quality, ('s', None) if capacity != 'streams' else None)
    assert (d['source'], d['line'], d['path']) == ('capacity.json', 0, ['streams',1])
    if not quality: assert ('schema property' if capacity == 'properties' else 'stream limit' if capacity == 'streams' else '--max-declarations') in d['message']
    assert engine.declarations == (2 if capacity == 'properties' else 1)
    assert len(engine.streams) == 1 and reader.calls == 0
    assert report['input_lines'] == 0 and report['messages'] == {}
    if quality: assert report['streams'][0]['schemas'] == 1 and report['coverage'] == 'partial_input'
    assert_schema(report, quality)


@pytest.mark.parametrize('flavor', FLAVORS)
@pytest.mark.parametrize('limit', [0, 1, 2])
def test_changed_contracts_and_bounded_samples(flavor, limit):
    good = external_entry(flavor)
    changed = external_entry(flavor, {'type':'object'})
    protocol = flavor.split('-')[0]
    engine = Validator(quality=True, example_limit=limit, max_errors=1)
    report = engine.run(io.BytesIO(capture(protocol)[2]), source='main', catalog_source='changes.json',
                        catalog={'streams':[good, good, changed, changed, good]})
    assert report['exit_code'] == 1 and not report['stopped_early'] and report['coverage'] == 'complete_input'
    assert codes(report, True) == {'TC009':2} and engine.declarations == 5
    stream = report['streams'][0]
    assert stream['schemas'] == 3 and stream['states'] == 1 and stream['records'] == 0
    assert len(stream['examples']) == limit and stream['omitted_examples'] == 2-limit
    for index, d in enumerate(stream['examples'], 2):
        assert (d['source'], d['line'], d['path']) == ('changes.json', 0, ['streams', index])
    assert engine.streams[('s',None)].schema != {'type':'object'}
    assert (engine.source, engine.line, engine._catalog_path) == ('main', 1, None)
    assert_schema(report, True)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('max_errors', [1, 100])
def test_changed_contract_error_priority_and_main_location(quality, max_errors):
    good, changed = entry('singer'), entry('singer', schema={'type':'object'})
    reader = Guarded(b'{bad\n', 0 if not quality and max_errors == 1 else 2)
    engine = Validator(quality=quality, max_errors=max_errors)
    report = engine.run(reader, source='main', catalog={'streams':[good, changed]})
    assert codes(report, quality) == ({'TC009':1} if reader.calls == 0 else {'TC009':1, 'TC001':1})
    assert report['exit_code'] == 1 and report['stopped_early'] == (max_errors == 1 and not quality)
    first = issue(report, quality, ('s',None)) if quality else report['diagnostics'][0]
    assert (first['source'], first['line'], first['path']) == ('<catalog>', 0, ['streams',1])
    if reader.calls:
        last = issue(report, quality)
        assert (last['source'], last['line'], last['path']) == ('main', 1, None)
    assert engine._catalog_source is engine._catalog_path is None
    assert_schema(report, quality)


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('compression', [None, 'gzip'])
def test_restores_main_and_batch_physical_locations(tmp_path, quality, compression):
    import gzip
    rows = b'\n{bad\n{}\n'
    name = 'rows.gz' if compression else 'rows'
    (tmp_path/name).write_bytes(gzip.compress(rows) if compression else rows)
    good, changed = entry('singer'), entry('singer', schema={'type':'object'})
    raw = b'\n'+batch(name, compression=compression)+b'\n'+encoded({'type':'RECORD','stream':'s','record':{}})
    engine = Validator(quality=quality, batch_root=tmp_path, key_example_limit=5)
    report = engine.run(io.BytesIO(raw), source='capture.jsonl', catalog={'streams':[good, changed]})
    counts = report['errors_by_code'] if quality else dict(Counter(d['code'] for d in report['diagnostics']))
    assert report['exit_code'] == 1 and counts == {'TC009':1,'TC001':1,'TC010':2,'TC008':1}
    ds = report['streams'][0]['examples'] if quality else report['diagnostics']
    if quality:
        assert report['unassigned']['examples'] == []
        assert {'code':'TC001','source':name,'line':2,'path':None} in ds
        assert report['streams'][0]['keys']['by_path'][0]['examples'] == [
            {'kind':'missing','source':name,'line':3}, {'kind':'missing','source':'capture.jsonl','line':4}]
    locations = [(d['code'], d['source'], d['line'], d['path']) for d in ds]
    assert ('TC009','<catalog>',0,['streams',1]) in locations
    assert ('TC010',name,3,['id']) in locations
    assert ('TC010','capture.jsonl',4,['id']) in locations
    assert ('TC008','capture.jsonl',4,None) in locations
    assert report['messages'] == {'BATCH':1, 'RECORD':1} and report['input_lines'] == 4
    assert report['streams'][0]['records'] == 2
    assert_schema(report, quality)


@pytest.mark.parametrize('protocol', ['singer','airbyte'])
@pytest.mark.parametrize('quality', [False,True])
def test_inline_registration_keeps_physical_locations(protocol, quality):
    good = entry(protocol)
    invalid = entry(protocol, schema={'type':'bad'})
    declaration = schema_raw(invalid) if protocol == 'singer' else catalog_raw([good,invalid])
    raw = b'\n\n'+declaration+capture(protocol)[2]
    engine = Validator(protocol=protocol, quality=quality)
    report = engine.run(io.BytesIO(raw), source='inline', catalog={'streams':[good]})
    assert report['exit_code'] == 1 and not report['stopped_early']
    d = issue(report, quality)
    assert (d['source'], d['line'], d['path']) == ('inline', 3, None)
    assert_schema(report, quality)


@pytest.mark.parametrize('limit', [0, 1])
def test_quality_stop_location_can_be_omitted(limit):
    good = entry('airbyte')
    changed = entry('airbyte', schema={'type':'object'})
    engine = Validator(quality=True, example_limit=limit, max_declarations=2)
    report = engine.run(Guarded(b'no reads',0), source='main', catalog_source='bounded.json',
                        catalog={'streams':[good, changed, good]})
    assert report['errors_by_code'] == {'TC009':1, 'TC020':1}
    assert report['errors'] == 2 and report['coverage'] == 'partial_input' and report['exit_code'] == 2
    stream = report['streams'][0]
    assert len(stream['examples']) == limit and stream['omitted_examples'] == 2-limit
    assert all(d['code'] == 'TC009' and d['source'] == 'bounded.json' and d['line'] == 0 and d['path'] == ['streams',1]
               for d in stream['examples'])
    assert_schema(report, True)
