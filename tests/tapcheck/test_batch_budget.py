"""Cumulative delivered BATCH bytes, exact row prefixes, and operational reports."""
import gzip
import io
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from datawrench.tapcheck.engine import Validator
from datawrench.tapcheck.cli import render
from .test_validation_input_budget import encoded, capture, cli, assert_schema, codes

DECLARATION, INLINE, STATE = capture('singer')


def batch(*names, compression=None, stream='s'):
    return encoded({'type': 'BATCH', 'stream': stream, 'encoding': {
        'format': 'jsonl', 'compression': compression}, 'manifest': list(names)})


def payload(tmp_path, raw, compression, name='rows'):
    name += '.gz' if compression == 'gzip' else ''
    stored = gzip.compress(raw, mtime=0) if compression == 'gzip' else raw
    (tmp_path/name).write_bytes(stored)
    return name, stored


def validate(tmp_path, main, **kwargs):
    return Validator(batch_root=tmp_path, **kwargs).run(io.BytesIO(main), source='capture')


@pytest.mark.parametrize('compression', [None, 'gzip'])
@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('variant', ['plain', 'utf8-crlf', 'no-lf'])
@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_payload_boundaries(tmp_path, compression, quality, variant, delta):
    row = encoded({'id': '☃😀'})
    raw = row * 2
    if variant == 'utf8-crlf': raw = b'\r\n \t\r\n' + raw.replace(b'\n', b'\r\n')
    if variant == 'no-lf': raw = raw[:-1]
    name, stored = payload(tmp_path, raw, compression)
    main = DECLARATION + batch(name, compression=compression) + STATE
    report = validate(tmp_path, main, quality=quality, max_batch_bytes=len(raw)+delta)
    assert_schema(report, quality)
    assert report['exit_code'] == (2 if delta < 0 else 0)
    assert report['stopped_early'] == (delta < 0)
    assert report['streams'][0]['records'] == (1 if delta < 0 else 2)
    if delta < 0:
        assert codes(report, quality) == {'TC020': 1}
        assert report['input_lines'] == 2 and report['messages'] == {'SCHEMA': 1, 'BATCH': 1}
        diagnostic = report['streams'][0]['examples'][0] if quality else report['diagnostics'][0]
        assert diagnostic['source'] == name and diagnostic['line'] == (4 if variant == 'utf8-crlf' else 2)
        if quality:
            assert report['coverage'] == 'partial_input'
            assert {'final_state_coverage', 'remaining_input_after_stop'} <= set(report['not_evaluated'])
        else:
            assert '--max-batch-bytes' in diagnostic['message'] and diagnostic['stream'] == 's'
    else:
        assert report == validate(tmp_path, main, quality=quality)
    assert (tmp_path/name).read_bytes() == stored


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('compression', [None, 'gzip'])
@pytest.mark.parametrize('raw,budget,exit_code,row', [
    (b'', 0, 0, 0), (b'\n', 0, 2, 1), (b' \r\n', 3, 0, 0),
    (b'\n\n', 1, 2, 2), (b' ', 0, 2, 1)])
def test_zero_empty_blank(tmp_path, quality, compression, raw, budget, exit_code, row):
    name, _ = payload(tmp_path, raw, compression)
    report = validate(tmp_path, DECLARATION+batch(name, compression=compression)+STATE,
                      quality=quality, max_batch_bytes=budget)
    assert report['exit_code'] == exit_code and report['streams'][0]['records'] == 0
    if exit_code:
        diagnostic = report['streams'][0]['examples'][0] if quality else report['diagnostics'][0]
        assert diagnostic['code'] == 'TC020' and diagnostic['source'] == name and diagnostic['line'] == row


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('compression', [None, 'gzip'])
def test_multiple_repeated_manifest_and_inline_prefix(tmp_path, quality, split, compression):
    a = encoded({'id': 1})
    b = b'\n' + encoded({}) + encoded({'id': None}) + a + encoded({'id': 9})
    n1, _ = payload(tmp_path, a, compression, 'a')
    n2, _ = payload(tmp_path, b, compression, 'b')
    first = batch(n1, n1, compression=compression)
    second = batch(n2, compression=compression)
    if not split:
        first, second = batch(n1, n1, n2, compression=compression), b''
    main = DECLARATION + INLINE + first + second + INLINE + STATE
    limit = len(a)*2 + len(b) - len(encoded({'id': 9}))
    report = validate(tmp_path, main, quality=quality, check_duplicates=True,
                      key_example_limit=2, max_batch_bytes=limit)
    assert_schema(report, quality)
    assert report['exit_code'] == 2 and report['stopped_early']
    assert report['input_lines'] == (4 if split else 3)
    assert report['messages'] == {'SCHEMA': 1, 'RECORD': 1, 'BATCH': 2 if split else 1}
    stream = report['streams'][0]
    assert stream['records'] == 6
    if quality:
        assert report['errors_by_code'] == {'TC010': 2, 'TC016': 2, 'TC020': 1}
        assert stream['keys']['missing_fields'] == stream['keys']['null_fields'] == 1
        assert stream['duplicates']['duplicate_keys'] == 2 and stream['duplicates']['evaluated_records'] == 4
        assert stream['keys']['by_path'][0]['examples'] == [
            {'kind': 'missing', 'source': n2, 'line': 2}, {'kind': 'null', 'source': n2, 'line': 3}]
    else:
        assert [(d['code'], d['source'], d['line']) for d in report['diagnostics']] == [
            ('TC016', n1, 1), ('TC010', n2, 2), ('TC010', n2, 3), ('TC016', n2, 4), ('TC020', n2, 5)]


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('raw,expected', [(b'{bad\n', 'TC001'), (encoded({'id': 1}), 'TC008')])
def test_exact_eof_still_checks_json_and_final_state(tmp_path, quality, raw, expected):
    name, _ = payload(tmp_path, raw, 'gzip')
    report = validate(tmp_path, DECLARATION+batch(name, compression='gzip'),
                      quality=quality, max_batch_bytes=len(raw))
    assert report['exit_code'] == 1 and not report['stopped_early']
    assert codes(report, quality) == {expected: 1}


@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('value', ['-1', '1.5', 'bad', str(2**63)])
def test_invalid_cli(tmp_path, command, value):
    result = cli(command, tmp_path/'absent', '--max-batch-bytes', value)
    assert result.returncode == 2 and not result.stdout and b'--max-batch-bytes' in result.stderr


@pytest.mark.parametrize('value', [-1, 2**63, True, False, 1.0, '1', [], {}])
def test_invalid_api(value):
    with pytest.raises(ValueError, match='max_batch_bytes'):
        Validator(max_batch_bytes=value)


@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('limit', [None, 2**63-1, 18, 17])
def test_cli_formats_and_catalog_exclusion(tmp_path, command, mode, limit):
    row = b'{"id":1}\n'
    name, stored = payload(tmp_path, row*2, 'gzip', 'rows\x1b')
    catalog = tmp_path/'catalog.json'
    # Singer external catalog, excluded from both byte budgets.
    catalog_raw = json.dumps({'streams': [json.loads(DECLARATION)]}).encode()
    catalog.write_bytes(catalog_raw)
    main = batch(name, compression='gzip') + STATE
    path = tmp_path/'capture'
    path.write_bytes(main)
    args = [path if mode == 'file' else '-', '--catalog', catalog, '--batch-root', tmp_path,
            '--max-input-bytes', len(main)]
    kwargs = {'input': main} if mode == 'stdin' else {}
    if command == 'validate': args += ['--format', 'json']
    if limit is not None: args += ['--max-batch-bytes', limit]
    result = cli(command, *args, **kwargs)
    assert not result.stderr
    report = json.loads(result.stdout)
    assert_schema(report, command == 'quality')
    assert result.returncode == report['exit_code'] == (2 if limit == 17 else 0)
    assert report['streams'][0]['records'] == (1 if limit == 17 else 2)
    if limit == 17 and command == 'validate':
        text = render(report, 'text')
        assert 'line 2 TC020' in text and 'rows\\x1b.gz' in text and '\x1b' not in text
        assert 'stopped early' in text
        xml = ET.fromstring(render(report, 'junit'))
        assert xml.find('testcase/error').attrib['type'] == 'TC020'
        assert 'rows\\u001b.gz:2' in xml.find('testcase/error').text
        assert xml.find("properties/property[@name='records']").attrib['value'] == '1'
    assert path.read_bytes() == main and catalog.read_bytes() == catalog_raw
    assert (tmp_path/name).read_bytes() == stored


@pytest.mark.parametrize('compression', [None, 'gzip'])
@pytest.mark.parametrize('oversized', [False, True])
def test_delivered_read_cap_no_drain_or_later_open(tmp_path, monkeypatch, compression, oversized):
    import datawrench.tapcheck.engine as engine
    row = encoded({'id': 1})
    first, _ = payload(tmp_path, row, compression, 'first')
    raw = row + (b'x'*100000 if oversized else b' '*100000) + b'\n' + row
    second, _ = payload(tmp_path, raw, compression, 'second')
    opened, delivered = [], []
    original_open, original_gzip = engine.open_beneath, gzip.GzipFile

    class Tracking:
        def __init__(self, handle): self.handle = handle
        def read1(self, size):
            value = self.handle.read1(size)
            delivered.append((size, len(value)))
            return value
        def read(self, size): return self.read1(size)
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()

    def opening(root, relative):
        opened.append(str(relative))
        handle = original_open(root, relative)
        return Tracking(handle) if compression is None else handle

    monkeypatch.setattr(engine, 'open_beneath', opening)
    if compression == 'gzip':
        monkeypatch.setattr(engine.gzip, 'GzipFile', lambda **kwargs: Tracking(original_gzip(**kwargs)))
    main = DECLARATION + batch(first, first, second, 'never-open', compression=compression) + STATE
    budget = len(row)*3 + 300
    report = validate(tmp_path, main, max_batch_bytes=budget, max_line_bytes=200)
    assert report['exit_code'] == 2 and report['streams'][0]['records'] == 3
    assert [(d['code'], d['source'], d['line']) for d in report['diagnostics']] == [('TC020', second, 2)]
    assert sum(n for _, n in delivered) == budget+1
    assert all(0 <= n <= requested <= budget+1 for requested, n in delivered)
    assert opened == [first, first, second]


def test_exact_fit_probes_actual_decompressed_eof(tmp_path, monkeypatch):
    original = gzip.GzipFile.read1
    reads = []
    def read1(self, size=-1):
        data = original(self, size)
        reads.append((size, len(data)))
        return data
    monkeypatch.setattr(gzip.GzipFile, 'read1', read1)
    raw = encoded({'id': 1})
    name, _ = payload(tmp_path, raw, 'gzip')
    report = validate(tmp_path, DECLARATION+batch(name, compression='gzip')+STATE, max_batch_bytes=len(raw))
    assert report['exit_code'] == 0
    assert sum(n for _, n in reads) == len(raw) and reads[-1] == (1, 0)


@pytest.mark.parametrize('quality', [False, True])
def test_independent_limits_and_zero_examples(tmp_path, quality):
    rows = encoded({'id': 'x'*500}) + encoded({'id': 2})
    name, _ = payload(tmp_path, rows, None)
    main = DECLARATION+batch(name)+STATE
    report = validate(tmp_path, main, quality=quality, max_batch_bytes=len(rows), max_line_bytes=200)
    assert report['exit_code'] == 1 and not report['stopped_early']
    assert report['streams'][0]['records'] == 1 and codes(report, quality) == {'TC020': 1}
    rows = encoded({'id': 1}) + encoded({'id': 2})
    (tmp_path/name).write_bytes(rows)
    report = validate(tmp_path, main, quality=quality, max_batch_bytes=len(rows), check_duplicates=True, duplicate_limit=1)
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC018': 1}
    report = validate(tmp_path, main, quality=quality, max_batch_bytes=0, max_input_bytes=len(DECLARATION))
    assert report['exit_code'] == 2 and report['messages'] == {'SCHEMA': 1}
    report = validate(tmp_path, main, quality=quality, max_batch_bytes=0, example_limit=0, key_example_limit=0)
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC020': 1}
    if quality:
        assert report['streams'][0]['examples'] == [] and report['streams'][0]['omitted_examples'] == 1


def test_corrupt_gzip_retains_delivered_charge(tmp_path):
    # Valid first member delivers a row, corrupt following header is TC017;
    # that delivered row still consumes budget before the next manifest.
    row = encoded({'id': 1})
    (tmp_path/'bad.gz').write_bytes(gzip.compress(row, mtime=0)+b'not gzip')
    name, _ = payload(tmp_path, row, 'gzip', 'good')
    report = validate(tmp_path, DECLARATION+batch('bad.gz', name, compression='gzip')+STATE,
                      max_batch_bytes=len(row))
    assert report['exit_code'] == 2
    assert [(d['code'], d['source'], d['line']) for d in report['diagnostics']] == [
        ('TC017', 'capture', 2), ('TC020', name, 1)]
    assert report['streams'][0]['records'] == 1


@pytest.mark.parametrize('quality', [False, True])
def test_shared_across_streams_and_mixed_compression(tmp_path, quality):
    row = encoded({'id': 1})
    name, _ = payload(tmp_path, row, None)
    compressed, _ = payload(tmp_path, row, 'gzip')
    other = json.loads(DECLARATION)
    other['stream'] = 'other'
    main = DECLARATION + encoded(other) + batch(name) + INLINE + batch(compressed, compression='gzip', stream='other') + STATE
    report = validate(tmp_path, main, quality=quality, max_batch_bytes=len(row))
    assert report['exit_code'] == 2 and report['messages'] == {'SCHEMA': 2, 'BATCH': 2, 'RECORD': 1}
    streams = {s['name']: s for s in report['streams']}
    assert streams['s']['records'] == 2 and streams['other']['records'] == 0
    d = streams['other']['examples'][0] if quality else report['diagnostics'][0]
    assert (d['code'], d['source'], d['line']) == ('TC020', compressed, 1)


@pytest.mark.parametrize('quality', [False, True])
def test_exact_payload_still_checks_gzip_trailer(tmp_path, quality):
    row = encoded({'id': 1})
    stored = bytearray(gzip.compress(row, mtime=0))
    stored[-8] ^= 1  # CRC corruption must surface on EOF probe after exact fit.
    (tmp_path/'bad.gz').write_bytes(stored)
    report = validate(tmp_path, DECLARATION+batch('bad.gz', compression='gzip')+STATE,
                      quality=quality, max_batch_bytes=len(row))
    assert report['exit_code'] == 1 and not report['stopped_early']
    assert codes(report, quality) == {'TC017': 1}
    assert report['streams'][0]['records'] == 1 and report['messages']['STATE'] == 1
    assert (tmp_path/'bad.gz').read_bytes() == stored


def test_zero_budget_empty_gzip_metadata_is_not_a_raw_read_cap(tmp_path, monkeypatch):
    import datawrench.tapcheck.engine as engine
    original = engine.open_beneath
    raw_reads = []
    # Long filename metadata, plus another empty member: no payload bytes.
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, filename='x'*300, mode='wb', mtime=0): pass
    stored = out.getvalue() + gzip.compress(b'', mtime=0)
    (tmp_path/'empty.gz').write_bytes(stored)
    class RawTracking:
        def __init__(self, handle): self.handle = handle
        def read(self, size=-1):
            raw = self.handle.read(size)
            raw_reads.append(len(raw))
            return raw
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()
    monkeypatch.setattr(engine, 'open_beneath', lambda *args: RawTracking(original(*args)))
    report = validate(tmp_path, DECLARATION+batch('empty.gz', compression='gzip')+STATE, max_batch_bytes=0)
    assert report['exit_code'] == 0 and sum(raw_reads) == len(stored) > 1


@pytest.mark.parametrize('quality', [False, True])
def test_no_batch_zero_does_not_limit_inline_or_empty_main(tmp_path, quality):
    report = validate(tmp_path, DECLARATION+INLINE+STATE, quality=quality, max_batch_bytes=0)
    assert report['exit_code'] == 0 and report['streams'][0]['records'] == 1
    report = validate(tmp_path, b'', quality=quality, max_batch_bytes=0)
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC021': 1}


def test_earlier_max_errors_preserves_existing_stop(tmp_path):
    name, _ = payload(tmp_path, b'{bad\n'+b'x'*1000, None)
    report = validate(tmp_path, DECLARATION+batch(name)+STATE, max_batch_bytes=20, max_errors=1)
    assert report['exit_code'] == 1 and report['stopped_early']
    assert codes(report, False) == {'TC001': 1} and report['streams'][0]['records'] == 0
