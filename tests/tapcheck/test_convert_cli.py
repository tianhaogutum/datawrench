import io
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from datawrench.tapcheck.convert import convert, singer_name, airbyte_name, STATE_KEY
from datawrench.tapcheck.io import InputError
from .conftest import run, SCHEMA, RECORD, STATE


def encode(messages):
    return io.BytesIO(('\n'.join(json.dumps(m) for m in messages) + '\n').encode())


def cli(*args, input=None):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', *args], input=input, capture_output=True)


def test_singer_roundtrip_real_fixture():
    messages = [json.loads(s) for s in Path('examples/tapcheck/good_singer.jsonl').read_text().splitlines()]
    ab = list(convert(encode(messages), 'airbyte'))
    assert run(ab, protocol='airbyte', strict=True)['exit_code'] == 0
    singer = list(convert(encode(ab), 'singer'))
    assert singer == messages
    assert run(singer, strict=True)['exit_code'] == 0


def test_airbyte_roundtrip_namespace_state():
    original = [json.loads(s) for s in Path('examples/tapcheck/good_airbyte.jsonl').read_text().splitlines()]
    singer = list(convert(encode(original), 'singer'))
    assert singer[-1]['value'] == {STATE_KEY: original[-1]['state']}
    ab = list(convert(encode(singer), 'airbyte'))
    assert ab[1:] == original[1:]
    assert ab[0]['catalog']['streams'][0]['namespace'] == 'public'
    assert run(ab)['exit_code'] == 0


@pytest.mark.parametrize('name,namespace', [('users', None), ('', ''), ('a.b', 'c'), ('b', 'c.a'), ('tapcheck:ab:foo', None), ('a/☃', 'n:\\s')])
def test_namespace_encoding_collision_free(name, namespace):
    assert airbyte_name(singer_name(name, namespace)) == (name, namespace)


@pytest.mark.parametrize('message', [
    {'type':'ACTIVATE_VERSION', 'stream':'users', 'version':1},
    dict(RECORD, time_extracted=None), dict(RECORD, time_extracted='2026-01-01T00:00:00.123456Z'),
])
def test_converter_refuses_loss(message):
    with pytest.raises(InputError) as exc:
        list(convert(encode([SCHEMA, message, STATE]), 'airbyte'))
    assert exc.value.code == 'TC023'


def test_reserved_invalid_name_and_state():
    with pytest.raises(InputError): airbyte_name('tapcheck:ab:INVALID')
    with pytest.raises(InputError):
        list(convert(encode([{'type':'STATE','value':{STATE_KEY: {}, 'extra':1}}]), 'airbyte'))


def test_nested_airbyte_keys_refused():
    cat = {'type':'CATALOG','catalog': {'streams':[{'name':'x','json_schema':{}, 'source_defined_primary_key':[['a','b']]}]}}
    with pytest.raises(InputError, match='nested'):
        list(convert(encode([cat]), 'singer'))


def test_external_catalog_conversion():
    catalog = {'streams': [dict(SCHEMA, tap_stream_id='users')]}
    ab = list(convert(encode([RECORD, STATE]), 'airbyte', catalog=catalog))
    assert ab[0]['type'] == 'CATALOG'
    assert run(ab)['exit_code'] == 0


def test_convert_error_exit_and_no_fake_timestamp():
    result = cli('convert', '--to', 'airbyte', input=encode([SCHEMA, dict(RECORD, time_extracted=None), STATE]).read())
    assert result.returncode == 2 and b'TC023' in result.stderr
    assert len(result.stdout.splitlines()) == 1  # streaming output is explicitly partial on failure


@pytest.mark.parametrize('format', ['text', 'json', 'junit'])
@pytest.mark.parametrize('file,code', [('examples/tapcheck/good_singer.jsonl', 0), ('tests/tapcheck/fixtures/singer_tools_issue_19.jsonl', 1), ('not-present.jsonl', 2)])
def test_cli_exit_and_formats(format, file, code):
    result = cli('validate', file, '--format', format)
    assert result.returncode == code, result.stderr
    if format == 'json': assert json.loads(result.stdout)['exit_code'] == code
    elif format == 'junit': assert ET.fromstring(result.stdout).tag == 'testsuite'
    else: assert f'exit {code}'.encode() in result.stdout


def test_cli_pipeline_and_stdin():
    result = cli('convert','--to','airbyte','examples/tapcheck/good_singer.jsonl')
    assert result.returncode == 0
    checked = cli('validate','-','--protocol','airbyte','--strict', input=result.stdout)
    assert checked.returncode == 0
    empty = cli('validate', input=b'')
    assert empty.returncode == 2 and b'TC021' in empty.stdout


def test_junit_escapes_xml_controls():
    from datawrench.tapcheck.cli import render
    report = run([dict(SCHEMA, stream='bad\x00name'), dict(RECORD, stream='bad\x00name', record={}), STATE])
    ET.fromstring(render(report, 'junit'))


def test_no_sensitive_values_echoed():
    report = run([SCHEMA, dict(RECORD, record={'id':'SECRET_PASSWORD'}), STATE])
    assert 'SECRET_PASSWORD' not in json.dumps(report)


def test_converter_does_not_truncate_extra_fraction_digits():
    with pytest.raises(InputError) as exc:
        list(convert(encode([SCHEMA, dict(RECORD, time_extracted='2026-01-01T00:00:00.000000001Z'), STATE]), 'airbyte'))
    assert exc.value.code == 'TC023'


@pytest.mark.parametrize('field', ['meta', 'version', 'unknown'])
def test_converter_rejects_unmapped_record_fields(field):
    with pytest.raises(InputError) as exc:
        list(convert(encode([SCHEMA, dict(RECORD, **{field: {}}), STATE]), 'airbyte'))
    assert exc.value.code == 'TC023'


def test_converter_rejects_catalog_sync_config_loss():
    cat = {'type':'CATALOG','catalog': {'streams':[{'stream':{'name':'x','json_schema':{}}, 'sync_mode':'incremental'}]}}
    with pytest.raises(InputError) as exc:
        list(convert(encode([cat]), 'singer'))
    assert exc.value.code == 'TC023'


def test_airbyte_timestamp_large_year_exact_milliseconds():
    from datawrench.tapcheck.model import timestamp
    assert timestamp(253402300799999, 'airbyte') == '9999-12-31T23:59:59.999Z'
    assert timestamp(-1, 'airbyte') == '1969-12-31T23:59:59.999Z'


def test_empty_and_mixed_converter_input():
    for raw in [b'', b'{"type":"STATE","value":{}}\n{"type":"STATE","state":{"data":{}}}\n']:
        with pytest.raises(InputError): list(convert(io.BytesIO(raw), 'airbyte'))


def test_airbyte_reset_state_roundtrip():
    original = [{'type':'STATE','state':{'type':'LEGACY','data':None}}]
    singer = list(convert(encode(original), 'singer'))
    assert list(convert(encode(singer), 'airbyte')) == original


def test_catalog_rejects_nonfinite_json_and_cli_size_bounds(tmp_path):
    catalog = tmp_path / 'catalog.json'
    catalog.write_text('{"streams":[],"x":1e999}')
    result = cli('validate','examples/tapcheck/good_singer.jsonl','--catalog',str(catalog),'--format','json')
    assert result.returncode == 2
    assert json.loads(result.stdout)['diagnostics'][0]['code'] == 'TC004'
    for value in ['0', '-1', '99999999999999999999']:
        result = cli('validate','--max-line-bytes',value)
        assert result.returncode == 2 and b'usage:' in result.stderr


@pytest.mark.parametrize('command', [
    ['validate', 'examples/tapcheck/good_singer.jsonl'],
    ['quality', 'examples/tapcheck/good_singer.jsonl'],
    ['convert', '--to', 'airbyte', 'examples/tapcheck/good_singer.jsonl'],
])
def test_null_catalog_file_is_rejected_not_ignored(tmp_path, command):
    # JSON null decodes to None, which must not be confused with "no --catalog".
    catalog = tmp_path / 'catalog.json'
    catalog.write_text('null')
    result = cli(*command, '--catalog', str(catalog))
    assert result.returncode == 2
    assert b'TC003' in result.stdout + result.stderr


def test_converter_checks_encoded_output_size(monkeypatch, capsys):
    import datawrench.tapcheck.cli as module
    monkeypatch.setattr(module, 'MAX_LINE_BYTES', 50)
    assert module.main(['convert','--to','airbyte','examples/tapcheck/good_singer.jsonl']) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'TC020' in captured.err
