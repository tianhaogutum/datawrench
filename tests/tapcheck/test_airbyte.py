from copy import deepcopy
import json
from pathlib import Path

import pytest

from datawrench.tapcheck.engine import Validator
from .conftest import run

MESSAGES = [json.loads(line) for line in Path("examples/tapcheck/good_airbyte.jsonl").read_text().splitlines()]
CATALOG, RECORD, STATE = MESSAGES


def test_configured_inline_and_external_catalog():
    assert run(MESSAGES, strict=True, check_order=True, check_duplicates=True)["exit_code"] == 0
    v = Validator(protocol="auto")
    import io
    report = v.run(io.BytesIO((json.dumps(RECORD) + '\n' + json.dumps(STATE)).encode()), catalog=CATALOG["catalog"])
    assert report["exit_code"] == 0
    assert report["streams"][0]["namespace"] == "public"


@pytest.mark.parametrize("state", [
    {"type": "LEGACY", "data": {}}, {"data": None},
    {"type": "STREAM", "stream": {"stream_descriptor": {"name": "users", "namespace": "public"}, "stream_state": None}},
    {"type": "GLOBAL", "global": {"stream_states": [STATE['state']['stream']], "shared_state": {}}},
])
def test_state_shapes_positive(state):
    assert run([CATALOG, RECORD, {"type": "STATE", "state": state}])["exit_code"] == 0


@pytest.mark.parametrize("state", [None, [], {}, {"type": "INVALID"}, {"type": []},
    {"type": "LEGACY", "data": []}, {"type": "STREAM", "stream": {}},
    {"type": "GLOBAL", "global": {}}, {"type": "GLOBAL", "global": {"stream_states": [None]}},
    {"type": "STREAM", "stream": STATE['state']['stream'], "data": {}},
    {"type": "STREAM", "stream": {"stream_descriptor": {"name": None}}},
])
def test_state_shapes_negative(state):
    report = run([CATALOG, {"type": "STATE", "state": state}])
    assert report["diagnostics"][0]["code"] == "TC007"


def test_state_must_cover_each_dirty_stream():
    other = deepcopy(RECORD)
    other['record']['namespace'] = 'other'
    catalog = deepcopy(CATALOG)
    new_stream = deepcopy(CATALOG['catalog']['streams'][0])
    new_stream['stream']['namespace'] = 'other'
    catalog['catalog']['streams'].append(new_stream)
    report = run([catalog, RECORD, other, STATE])
    assert [d['code'] for d in report['diagnostics']] == ['TC008']
    assert 'other' in report['diagnostics'][0]['stream']
    assert run([catalog, RECORD, other, STATE, {'type': 'STATE', 'state': {'type': 'LEGACY', 'data': {}}}])["exit_code"] == 0


def test_nested_primary_cursor_and_namespace_isolation():
    catalog = deepcopy(CATALOG)
    entry = catalog['catalog']['streams'][0]
    entry['primary_key'] = [['nested', 'id']]
    entry['cursor_field'] = ['nested', 'cursor']
    entry['stream']['json_schema'] = {'type': 'object'}
    record = deepcopy(RECORD)
    record['record']['data'] = {'nested': {'id': 1, 'cursor': 2}}
    assert run([catalog, record, STATE], check_order=True)["exit_code"] == 0
    record['record']['data'] = {'nested': {'id': None, 'cursor': 1}}
    report = run([catalog, record, STATE], check_order=True)
    assert report['diagnostics'][0]['code'] == 'TC010'
    record['record']['namespace'] = None
    assert run([catalog, record, STATE])['diagnostics'][0]['code'] == 'TC005'


def test_passthrough_counts():
    messages = [{'type': t, t.lower(): {}} for t in ['TRACE', 'LOG', 'CONTROL']]
    report = run([CATALOG, *messages, STATE])
    assert report['exit_code'] == 0
    assert all(report['messages'][t] == 1 for t in ['TRACE', 'LOG', 'CONTROL'])


def test_auto_ambiguous_and_explicit_protocol():
    ambiguous = {"type": "STATE", "value": {}, "state": {"data": {}}}
    assert run([ambiguous])['exit_code'] == 2
    assert run([ambiguous], protocol='singer')['exit_code'] == 0
    assert run([ambiguous], protocol='airbyte')['exit_code'] == 0
