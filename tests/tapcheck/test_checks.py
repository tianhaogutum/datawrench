"""A positive and negative stream for each validation diagnostic."""
from copy import deepcopy
import io
import json

import pytest
from jsonschema import Draft202012Validator

from datawrench.tapcheck.engine import Validator
from datawrench.tapcheck.io import decode, lines, Diagnostic
from .conftest import SCHEMA, RECORD, STATE, run


def changed(message, **kwargs):
    return dict(deepcopy(message), **kwargs)


GOOD = [SCHEMA, RECORD, STATE]
CASES = [
    ("TC001", b'\xff\n', GOOD, {"protocol": "singer"}),
    ("TC002", [SCHEMA, {"type": "BOGUS"}], GOOD, {}),
    ("TC003", [SCHEMA, {"type": "RECORD", "stream": "users", "record": []}, STATE], GOOD, {}),
    ("TC004", [changed(SCHEMA, schema={"type": "potato"})], GOOD, {}),
    ("TC005", [RECORD, STATE], GOOD, {}),
    ("TC006", [SCHEMA, changed(RECORD, record={"id": "bad"}), STATE], GOOD, {}),
    ("TC007", [SCHEMA, {"type": "STATE", "value": []}], GOOD, {}),
    ("TC008", [SCHEMA, RECORD], GOOD, {}),
    ("TC009", [SCHEMA, changed(SCHEMA, key_properties=[])], GOOD, {}),
    ("TC010", [SCHEMA, changed(RECORD, record={}), STATE], GOOD, {}),
    ("TC011", [SCHEMA, changed(RECORD, record={"id": 1}), STATE], GOOD, {"strict": True}),
    ("TC012", [SCHEMA, changed(RECORD, record={**RECORD['record'], "extra": 1}), STATE], GOOD, {"strict": True}),
    ("TC013", [SCHEMA, changed(RECORD, record={**RECORD['record'], "created": "yesterday"}), STATE], GOOD, {"strict": True}),
    ("TC014", [SCHEMA, changed(RECORD, record={"id": 1}), STATE], GOOD, {"check_order": True}),
    ("TC015", [SCHEMA, RECORD, changed(RECORD, record={"id": 2, "updated": 0}), STATE], GOOD, {"check_order": True}),
    ("TC016", [SCHEMA, RECORD, RECORD, STATE], GOOD, {"check_duplicates": True}),
    ("TC017", [SCHEMA, {"type": "BATCH", "stream": "users", "encoding": {"format": "jsonl"}, "manifest": ["missing.jsonl"]}, STATE], GOOD, {}),
    ("TC018", [SCHEMA, RECORD, changed(RECORD, record={"id": 2}), STATE], GOOD, {"check_duplicates": True, "duplicate_limit": 1}),
    ("TC019", [SCHEMA, changed(RECORD, time_extracted="bad"), STATE], GOOD, {}),
    ("TC020", b' ' * 1000 + b'\n', [STATE], {"protocol": "singer", "max_line_bytes": 100}),
    ("TC021", b'', GOOD, {}),
    ("TC022", [SCHEMA, {"type": "CATALOG", "catalog": {"streams": []}}], GOOD, {}),
]


@pytest.mark.parametrize("code,bad,good,options", CASES, ids=[c[0] for c in CASES])
def test_check_negative(code, bad, good, options):
    report = run(bad, **options)
    assert code in {d["code"] for d in report["diagnostics"]}
    assert report["exit_code"] in (1, 2)


@pytest.mark.parametrize("code,bad,good,options", CASES, ids=[c[0] for c in CASES])
def test_check_positive(code, bad, good, options):
    report = run(good, **options)
    assert report["diagnostics"] == []
    assert report["exit_code"] == 0


@pytest.mark.parametrize("raw", [b'{', b'{"type":"STATE","value":NaN}', b'{"type":"STATE","value":1e999}', b'{"x":1,"x":2}', b'{"x":"\xff"}', b'[]\n{"type":"STATE","value":{}}'])
def test_invalid_input_never_crashes(raw):
    assert run(raw, protocol="singer")["exit_code"] == 1


def test_recover_after_bad_line_and_max_errors():
    raw = b'{\n\xff\n' + json.dumps(STATE).encode() + b'\n'
    report = run(raw, protocol="singer")
    assert [d["line"] for d in report["diagnostics"]] == [1, 2]
    assert report["messages"] == {"STATE": 1}
    report = run(raw, protocol="singer", max_errors=1)
    assert report["errors"] == 1 and report["stopped_early"]
    assert report["messages"] == {}


def test_long_line_drain_preserves_physical_lines():
    raw = b'x' * 100 + b'\n' + json.dumps(STATE).encode() + b'\n'
    report = run(raw, protocol="singer", max_line_bytes=40)
    assert report["errors"] == 1
    assert report["input_lines"] == 2
    assert report["messages"] == {"STATE": 1}


def test_stats_missing_and_null_are_distinct():
    record2 = changed(RECORD, record={"id": 2, "updated": 4}, time_extracted="2026-01-01T02:00:00+01:00")
    stats = run([SCHEMA, RECORD, record2, STATE])["streams"][0]
    assert stats["records"] == 2
    assert stats["properties"]["note"] == {"present": 1, "nulls": 1, "missing": 1, "null_rate": .5}
    assert (stats["replication_min"], stats["replication_max"]) == (1, 4)
    assert stats["first_extraction_time"] == "2026-01-01T00:00:00.000Z"
    assert stats["last_extraction_time"] == "2026-01-01T01:00:00.000Z"


def test_security_no_remote_reference_retrieval(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: pytest.fail("network called"))
    for ref in ["https://example.invalid/schema", "file:///etc/passwd", "other.json#/x"]:
        report = run([changed(SCHEMA, schema={"$ref": ref})])
        assert report["diagnostics"][0]["code"] == "TC004"


def test_local_reference_and_schema_data_that_is_not_executable():
    schema = {"$defs": {"row": {"type": "object", "properties": {"id": {"type": "integer"}}}}, "$ref": "#/$defs/row", "default": {"$ref": "https://example.invalid/not-a-schema"}}
    assert run([changed(SCHEMA, schema=schema), RECORD, STATE])["exit_code"] == 0
    assert run([changed(SCHEMA, schema={"$ref": "#/missing"}), RECORD, STATE])["diagnostics"][0]["code"] == "TC004"


def test_schema_change_order_of_properties_not_change():
    reversed_schema = deepcopy(SCHEMA)
    reversed_schema["schema"]["properties"] = dict(reversed(list(SCHEMA["schema"]["properties"].items())))
    assert run([SCHEMA, reversed_schema, RECORD, STATE])["exit_code"] == 0


def test_nullable_and_pattern_properties_strict():
    schema = changed(SCHEMA, schema={"type": "object", "properties": {"id": {"type": "integer"}, "note": {"type": ["string", "null"]}}, "patternProperties": {"^x_": {"type": "string"}}})
    assert run([schema, changed(RECORD, record={"id": 1, "x_a": "a"}), STATE], strict=True)["exit_code"] == 0


def test_nested_datetime_strict():
    schema = changed(SCHEMA, schema={"type": "object", "properties": {"id": {"type": "integer"}, "obj": {"type": "object", "properties": {"dt": {"format": "date-time"}}}}})
    report = run([schema, changed(RECORD, record={"id": 1, "obj": {"dt": "bad"}}), STATE], strict=True)
    assert report["diagnostics"][0]["code"] == "TC013"
    assert report["diagnostics"][0]["path"] == ["obj", "dt"]


def test_duplicate_composite_numeric_keys():
    schema = changed(SCHEMA, key_properties=["id", "note"])
    r1 = changed(RECORD, record={"id": 1, "note": "A"})
    r2 = changed(RECORD, record={"id": 1.0, "note": "A"})
    r3 = changed(RECORD, record={"id": 1, "note": "B"})
    report = run([schema, r1, r3, r2, STATE], check_duplicates=True)
    assert [d["code"] for d in report["diagnostics"]] == ["TC016"]


def test_cursor_equal_allowed_and_timezone_order():
    schema = changed(SCHEMA, bookmark_properties=["created"])
    a = changed(RECORD, record={"id": 1, "created": "2026-01-01T02:00:00+02:00"})
    b = changed(RECORD, record={"id": 2, "created": "2026-01-01T00:30:00Z"})
    assert run([schema, a, b, b, STATE], check_order=True)["exit_code"] == 0


def test_activate_version_and_lowercase_singer_types():
    activate = {"type": "ACTIVATE_VERSION", "stream": "users", "version": 42}
    assert run([changed(SCHEMA, type="schema"), RECORD, activate, STATE])["exit_code"] == 0
    report = run([SCHEMA, changed(activate, version="42")])
    assert report["diagnostics"][0]["code"] == "TC003"


def test_report_schema():
    schema = json.load(open("docs/tapcheck/report.schema.json"))
    for report in [run(GOOD), run([RECORD]), run(b'')]:
        Draft202012Validator(schema).validate(report)


def test_submillisecond_datetime_cursor_order():
    schema = changed(SCHEMA, bookmark_properties=['created'])
    a = changed(RECORD, record={'id': 1, 'created': '2026-01-01T00:00:00.000000002Z'})
    b = changed(RECORD, record={'id': 2, 'created': '2026-01-01T00:00:00.000000001Z'})
    report = run([schema, a, b, STATE], check_order=True)
    assert [d['code'] for d in report['diagnostics']] == ['TC015']


def test_limits_fail_explicitly():
    assert run([SCHEMA, changed(SCHEMA, stream='other')], max_streams=1)['exit_code'] == 2
    schema = changed(SCHEMA, schema={'type': 'object'})
    assert run([schema, changed(RECORD, record={'a':1,'b':2}), STATE], max_properties=1)['exit_code'] == 2


def test_reordered_catalog_keys_identical_contract():
    assert run([SCHEMA, deepcopy(SCHEMA), RECORD, STATE])['exit_code'] == 0


def test_strict_ref_property_and_all_supported_drafts():
    for draft in ['http://json-schema.org/draft-04/schema#', 'http://json-schema.org/draft-06/schema#', 'http://json-schema.org/draft-07/schema#', 'https://json-schema.org/draft/2019-09/schema', 'https://json-schema.org/draft/2020-12/schema']:
        schema = changed(SCHEMA, schema={'$schema':draft,'definitions':{'identifier':{'type':'integer'}},'type':'object','properties':{'id':{'$ref':'#/definitions/identifier'}}})
        report = run([schema, changed(RECORD, record={}), STATE], strict=True)
        assert 'TC011' in {d['code'] for d in report['diagnostics']}


def test_recursive_schema_and_unsupported_dialect_fail():
    report = run([changed(SCHEMA, schema={'$ref':'#'}), RECORD, STATE])
    assert report['diagnostics'][0]['code'] == 'TC004'
    for schema in [{'$schema':'https://example.invalid/dialect'}, {'$dynamicRef':'#x'}, {'$recursiveRef':'#'}]:
        assert run([changed(SCHEMA, schema=schema)])['diagnostics'][0]['code'] == 'TC004'


@pytest.mark.parametrize('date', ['bad', '2026-01-01T00:00:00.' + '0' * 5000 + '1Z'])
def test_invalid_datetime_cursor_fails_without_crash(date):
    schema = changed(SCHEMA, bookmark_properties=['created'])
    report = run([schema, changed(RECORD, record={'id': 1, 'created': date}), STATE], check_order=True)
    assert [d['code'] for d in report['diagnostics']] == ['TC014']


def test_cursor_type_change_is_explicit():
    schema = changed(SCHEMA, schema={'type':'object'})
    second = changed(RECORD, record={'id':2,'updated':'two'})
    report = run([schema, RECORD, second, STATE], check_order=True)
    assert [d['code'] for d in report['diagnostics']] == ['TC014']
