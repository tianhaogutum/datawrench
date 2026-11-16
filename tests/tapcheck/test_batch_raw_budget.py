"""Raw BATCH reads beneath gzip, independent from decompressed payload delivery."""
import gzip
import io
import json
import os
import xml.etree.ElementTree as ET

import pytest

from datawrench.tapcheck.engine import Validator
from datawrench.tapcheck.cli import render
from .test_batch_budget import DECLARATION, INLINE, STATE, batch, payload, validate
from .test_validation_input_budget import encoded, cli, assert_schema, codes


def track_raw(monkeypatch):
    import datawrench.tapcheck.engine as engine
    original = engine.open_beneath
    opened, reads = [], []

    class Tracking:
        def __init__(self, handle, name): self.handle, self.name = handle, name
        def reading(self, method, size):
            before = os.lseek(self.handle.fileno(), 0, os.SEEK_CUR)
            data = getattr(self.handle, method)(size)
            after = os.lseek(self.handle.fileno(), 0, os.SEEK_CUR)
            reads.append((self.name, method, size, len(data), after-before))
            return data
        def read(self, size=-1): return self.reading('read', size)
        def read1(self, size=-1): return self.reading('read1', size)
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()

    def opening(root, relative):
        opened.append(str(relative))
        return Tracking(original(root, relative), str(relative))
    monkeypatch.setattr(engine, 'open_beneath', opening)
    return opened, reads


def diagnostic(report, quality):
    return report['streams'][0]['examples'][-1] if quality else report['diagnostics'][-1]


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('limit_kind', ['zero', 'header', 'member', 'exact'])
def test_long_header_empty_members_raw_cap(tmp_path, monkeypatch, quality, limit_kind):
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, filename='x'*1000, mode='wb', mtime=0): pass
    member = out.getvalue()
    stored = member + gzip.compress(b'', mtime=0)*100
    (tmp_path/'empty.gz').write_bytes(stored)
    opened, reads = track_raw(monkeypatch)
    n = {'zero': 0, 'header': 310, 'member': len(member), 'exact': len(stored)}[limit_kind]
    main = DECLARATION+batch('empty.gz', 'never-open', compression='gzip')+STATE
    if limit_kind == 'exact': main = DECLARATION+batch('empty.gz', compression='gzip')+STATE
    report = validate(tmp_path, main, quality=quality, max_batch_bytes=0, max_batch_raw_bytes=n)
    assert report['exit_code'] == (0 if limit_kind == 'exact' else 2)
    assert report['streams'][0]['records'] == 0
    assert opened == ['empty.gz']
    assert sum(r[3] for r in reads) == sum(r[4] for r in reads) == min(n+1, len(stored))
    assert all(0 <= r[3] <= r[2] for r in reads)
    if limit_kind != 'exact':
        assert codes(report, quality) == {'TC020': 1}
        assert (diagnostic(report, quality)['source'], diagnostic(report, quality)['line']) == ('empty.gz', 1)
        assert report['messages'] == {'SCHEMA': 1, 'BATCH': 1}
    else:
        assert reads[-1][3:] == (0, 0)  # real raw EOF probe
    assert (tmp_path/'empty.gz').read_bytes() == stored


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('compression', [None, 'gzip'])
@pytest.mark.parametrize('variant', ['rows', 'utf8-crlf', 'no-lf'])
@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_raw_boundaries(tmp_path, quality, compression, variant, delta):
    row = encoded({'id': '☃😀'})
    raw = row*2
    if variant == 'utf8-crlf': raw = b'\r\n \t\r\n'+raw.replace(b'\n', b'\r\n')
    if variant == 'no-lf': raw = raw[:-1]
    name, stored = payload(tmp_path, raw, compression)
    main = DECLARATION+batch(name, compression=compression)+STATE
    report = validate(tmp_path, main, quality=quality, max_batch_raw_bytes=len(stored)+delta)
    assert_schema(report, quality)
    assert report['exit_code'] == (2 if delta < 0 else 0)
    if delta < 0:
        assert codes(report, quality) == {'TC020': 1} and report['stopped_early']
        d = diagnostic(report, quality)
        assert d['source'] == name and d['line'] >= 1
        if compression is None:
            assert d['line'] == (4 if variant == 'utf8-crlf' else 2)
            assert report['streams'][0]['records'] == 1
        if quality:
            assert report['coverage'] == 'partial_input'
            assert {'remaining_input_after_stop', 'final_state_coverage'} <= set(report['not_evaluated'])
        else: assert '--max-batch-raw-bytes' in d['message']
    else: assert report == validate(tmp_path, main, quality=quality)
    assert (tmp_path/name).read_bytes() == stored


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('compression,raw,n,expected', [
    (None, b'', 0, 0), (None, b'\n', 0, 2), (None, b' \r\n', 3, 0),
    ('gzip', b'', 0, 2), ('gzip', b'', 20, 0), ('gzip', b'\n', 0, 2)])
def test_zero_empty_blank(tmp_path, quality, compression, raw, n, expected):
    name, _ = payload(tmp_path, raw, compression)
    report = validate(tmp_path, DECLARATION+batch(name, compression=compression)+STATE,
                      quality=quality, max_batch_raw_bytes=n)
    assert report['exit_code'] == expected and report['streams'][0]['records'] == 0


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('split', [False, True])
def test_repeated_mixed_manifests_prefix(tmp_path, monkeypatch, quality, split):
    row = encoded({'id': 1})
    plain, a = payload(tmp_path, row, None, 'plain')
    zipped, b = payload(tmp_path, row, 'gzip', 'zipped')
    tail = b'\n'+encoded({})+encoded({'id': None})+row+encoded({'id': 99})+b' '*10000
    last, _ = payload(tmp_path, tail, None, 'last')
    opened, reads = track_raw(monkeypatch)
    first = batch(plain, plain)
    end = batch(last, 'never-open')
    if not split: first, end = b'', batch(plain, plain, last, 'never-open')
    main = DECLARATION+INLINE+batch(zipped, compression='gzip')+first+end+INLINE+STATE
    limit = len(b)+2*len(a)+len(tail.splitlines(keepends=True)[0])+len(encoded({}))+len(encoded({'id': None}))+len(row)
    report = validate(tmp_path, main, quality=quality, check_duplicates=True,
                      key_example_limit=2, example_limit=100, max_batch_raw_bytes=limit)
    assert_schema(report, quality)
    assert report['exit_code'] == 2 and report['streams'][0]['records'] == 7
    assert report['messages'] == {'SCHEMA': 1, 'RECORD': 1, 'BATCH': 3 if split else 2}
    assert sum(r[3] for r in reads) == sum(r[4] for r in reads) == limit+1
    assert opened == [zipped, plain, plain, last]
    d = diagnostic(report, quality)
    assert (d['code'], d['source'], d['line']) == ('TC020', last, 5)
    if quality:
        s = report['streams'][0]
        assert s['keys']['missing_fields'] == s['keys']['null_fields'] == 1
        assert s['duplicates']['duplicate_keys'] == 3
        assert s['keys']['by_path'][0]['examples'] == [
            {'kind': 'missing', 'source': last, 'line': 2}, {'kind': 'null', 'source': last, 'line': 3}]
        assert report['errors_by_code'] == {'TC010': 2, 'TC016': 3, 'TC020': 1}
    else: assert 'TC008' not in codes(report, quality)


@pytest.mark.parametrize('compression', [None, 'gzip'])
@pytest.mark.parametrize('kind', ['oversized', 'blank', 'members'])
def test_raw_descriptor_cap_no_drain(tmp_path, monkeypatch, compression, kind):
    if kind == 'members' and compression is None: pytest.skip('members require gzip')
    row = encoded({'id': 1})
    first, a = payload(tmp_path, row, compression, 'first')
    raw = row+(b'x' if kind == 'oversized' else b' ')*100000+b'\n'+row
    second, b = payload(tmp_path, raw, compression, 'second')
    if kind == 'members':
        b = gzip.compress(b'', mtime=0)*1000+gzip.compress(raw, mtime=0)
        (tmp_path/second).write_bytes(b)
    opened, reads = track_raw(monkeypatch)
    n = 2*len(a)+min(50, len(b)-1)
    report = validate(tmp_path, DECLARATION+batch(first, first, second, 'never-open', compression=compression)+STATE,
                      max_batch_raw_bytes=n, max_line_bytes=200)
    assert report['exit_code'] == 2 and codes(report, False)['TC020'] == 1
    assert sum(r[3] for r in reads) == sum(r[4] for r in reads) == n+1
    assert opened == [first, first, second]
    assert report['streams'][0]['records'] >= 2


@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('value', ['-1', '1.5', 'bad', str(2**63)])
def test_invalid_cli(tmp_path, command, value):
    result = cli(command, tmp_path/'absent', '--max-batch-raw-bytes', value)
    assert result.returncode == 2 and not result.stdout
    assert b'--max-batch-raw-bytes' in result.stderr and b'TC021' not in result.stderr


@pytest.mark.parametrize('value', [-1, 2**63, True, False, 1.0, '1', [], {}])
def test_invalid_api(value):
    with pytest.raises(ValueError, match='max_batch_raw_bytes'): Validator(max_batch_raw_bytes=value)


@pytest.mark.parametrize('command', ['validate', 'quality'])
@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('n', [None, 2**63-1, 18, 17])
def test_cli_catalog_reports(tmp_path, command, mode, n):
    name, stored = payload(tmp_path, encoded({'id': 1})*2, None, 'rows\x1b')
    main = batch(name)+STATE
    path, catalog = tmp_path/'main', tmp_path/'catalog'
    path.write_bytes(main)
    catalog_raw = json.dumps({'streams': [json.loads(DECLARATION)]}).encode()
    catalog.write_bytes(catalog_raw)
    args = ['--catalog', str(catalog), '--batch-root', str(tmp_path), '--max-input-bytes', str(len(main)), '--max-batch-bytes', '18']
    if n is not None: args += ['--max-batch-raw-bytes', str(n)]
    if command == 'validate': args += ['--format', 'json']
    result = cli(command, path if mode == 'file' else '-', *args, input=main if mode == 'stdin' else None)
    assert result.returncode == (2 if n == 17 else 0) and not result.stderr
    report = json.loads(result.stdout)
    assert_schema(report, command == 'quality')
    assert report['streams'][0]['records'] == (1 if n == 17 else 2)
    if n == 17 and command == 'validate':
        assert 'stopped early' in render(report, 'text')
        xml = ET.fromstring(render(report, 'junit'))
        assert xml.find('testcase/error').attrib['type'] == 'TC020'
        assert 'rows\\u001b:2' in xml.find('testcase/error').text
    assert path.read_bytes() == main and catalog.read_bytes() == catalog_raw
    assert (tmp_path/name).read_bytes() == stored


@pytest.mark.parametrize('quality', [False, True])
@pytest.mark.parametrize('error', ['json', 'state', 'crc', 'deflate', 'truncated'])
def test_exact_fit_preserves_error_priority(tmp_path, quality, error):
    row = b'{bad\n' if error == 'json' else encoded({'id': 1})
    stored = bytearray(gzip.compress(row, mtime=0))
    if error == 'crc': stored[-8] ^= 1
    if error == 'deflate': stored = bytes.fromhex('1f8b080000000000020307')+b'\0'*8
    if error == 'truncated': stored = stored[:-3]
    (tmp_path/'bad.gz').write_bytes(stored)
    main = DECLARATION+batch('bad.gz', compression='gzip')+(b'' if error == 'state' else STATE)
    report = validate(tmp_path, main, quality=quality, max_batch_raw_bytes=len(stored))
    expected = {'json': 'TC001', 'state': 'TC008'}.get(error, 'TC017')
    assert report['exit_code'] == 1 and not report['stopped_early']
    assert codes(report, quality) == {expected: 1}
    if expected == 'TC017': assert report['messages']['STATE'] == 1


@pytest.mark.parametrize('quality', [False, True])
def test_corruption_charge_persists_and_raw_error_not_tc017(tmp_path, monkeypatch, quality):
    bad = bytes.fromhex('1f8b080000000000020307')+b'\0'*8
    (tmp_path/'bad.gz').write_bytes(bad)
    good, _ = payload(tmp_path, encoded({'id': 1}), 'gzip', 'good')
    opened, reads = track_raw(monkeypatch)
    report = validate(tmp_path, DECLARATION+batch('bad.gz', good, 'never-open', compression='gzip')+STATE,
                      quality=quality, max_batch_raw_bytes=len(bad))
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC017': 1, 'TC020': 1}
    assert opened == ['bad.gz', good]
    assert sum(r[3] for r in reads) == sum(r[4] for r in reads) == len(bad)+1
    d = diagnostic(report, quality)
    assert (d['code'], d['source'], d['line']) == ('TC020', good, 1)


@pytest.mark.parametrize('quality', [False, True])
def test_independent_budgets_and_earlier_stops(tmp_path, quality):
    raw = encoded({'id': 1})+encoded({'id': 2})
    name, stored = payload(tmp_path, raw, 'gzip')
    main = DECLARATION+batch(name, compression='gzip')+STATE
    # Payload can stop while raw still has space, and vice versa.
    report = validate(tmp_path, main, quality=quality, max_batch_bytes=9, max_batch_raw_bytes=len(stored))
    assert report['exit_code'] == 2 and report['streams'][0]['records'] == 1
    if not quality: assert '--max-batch-bytes' in diagnostic(report, quality)['message']
    report = validate(tmp_path, main, quality=quality, max_batch_bytes=len(raw), max_batch_raw_bytes=0)
    assert report['exit_code'] == 2 and report['streams'][0]['records'] == 0
    report = validate(tmp_path, main, quality=quality, max_batch_raw_bytes=0, max_input_bytes=len(DECLARATION))
    assert report['exit_code'] == 2 and report['messages'] == {'SCHEMA': 1}
    report = validate(tmp_path, main, quality=quality, max_batch_raw_bytes=len(stored), check_duplicates=True, duplicate_limit=1)
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC018': 1}
    name, stored = payload(tmp_path, encoded({'id': 'x'*300})+encoded({'id': 1}), None)
    report = validate(tmp_path, DECLARATION+batch(name)+STATE, quality=quality,
                      max_batch_raw_bytes=len(stored), max_line_bytes=200)
    assert report['exit_code'] == 1 and not report['stopped_early']
    assert codes(report, quality) == {'TC020': 1} and report['streams'][0]['records'] == 1
    report = validate(tmp_path, DECLARATION+INLINE+STATE, quality=quality, max_batch_raw_bytes=0)
    assert report['exit_code'] == 0
    report = validate(tmp_path, b'', quality=quality, max_batch_raw_bytes=0)
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC021': 1}


def test_earlier_max_errors_and_no_samples(tmp_path):
    name, stored = payload(tmp_path, b'{bad\n'+b'x'*10000, None)
    main = DECLARATION+batch(name)+STATE
    report = validate(tmp_path, main, max_batch_raw_bytes=20, max_errors=1)
    assert report['exit_code'] == 1 and codes(report, False) == {'TC001': 1}
    report = validate(tmp_path, main, quality=True, max_batch_raw_bytes=0, example_limit=0, key_example_limit=0)
    assert report['exit_code'] == 2 and codes(report, True) == {'TC020': 1}
    assert report['streams'][0]['examples'] == []
    assert report['streams'][0]['omitted_examples'] == 1


@pytest.mark.parametrize('quality', [False, True])
def test_raw_cumulative_across_streams(tmp_path, quality):
    row = encoded({'id': 1})
    a, raw = payload(tmp_path, row, 'gzip', 'a')
    b, _ = payload(tmp_path, row, None, 'b')
    other = json.loads(DECLARATION)
    other['stream'] = 'other'
    main = DECLARATION+encoded(other)+batch(a, compression='gzip')+batch(b, stream='other')+STATE
    report = validate(tmp_path, main, quality=quality, max_batch_raw_bytes=len(raw))
    assert report['exit_code'] == 2 and codes(report, quality) == {'TC020': 1}
    streams = {s['name']: s for s in report['streams']}
    assert streams['s']['records'] == 1 and streams['other']['records'] == 0
    d = streams['other']['examples'][0] if quality else report['diagnostics'][0]
    assert (d['source'], d['line']) == (b, 1)
    assert report['messages'] == {'SCHEMA': 2, 'BATCH': 2}


@pytest.mark.parametrize('quality', [False, True])
def test_empty_physical_gzip_zero_raw_budget(tmp_path, monkeypatch, quality):
    (tmp_path/'empty.gz').write_bytes(b'')
    opened, reads = track_raw(monkeypatch)
    report = validate(tmp_path, DECLARATION+batch('empty.gz', compression='gzip')+STATE,
                      quality=quality, max_batch_raw_bytes=0, max_batch_bytes=0)
    assert report['exit_code'] == 0 and sum(r[4] for r in reads) == 0
    assert opened == ['empty.gz'] and reads[-1][2:] == (1, 0, 0)
