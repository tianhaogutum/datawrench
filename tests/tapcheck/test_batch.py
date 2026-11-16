import gzip
from pathlib import Path

import pytest

from datawrench.tapcheck.engine import Validator
from .conftest import SCHEMA, STATE, run

FIXTURES = Path('tests/tapcheck/fixtures')


def batch(path, compression=None):
    return {'type': 'BATCH', 'stream': 'users', 'encoding': {'format': 'jsonl', 'compression': compression}, 'manifest': [path]}


@pytest.mark.parametrize('name', ['batch_plain.jsonl', 'batch_gzip.jsonl'])
def test_real_fixture(name):
    with (FIXTURES / name).open('rb') as handle:
        report = Validator(batch_root=FIXTURES, strict=True, check_order=True).run(handle)
    assert report['exit_code'] == 0
    assert report['streams'][0]['records'] == 2
    assert report['streams'][0]['properties']['name']['null_rate'] == .5


@pytest.mark.parametrize('path', ['../outside.jsonl', 'file:///etc/passwd', 'https://example.invalid/batch', 's3://bucket/key', 'missing', 'file://batch.jsonl?token=secret'])
def test_path_boundaries(path):
    report = run([SCHEMA, batch(path), STATE], batch_root=FIXTURES)
    assert report['diagnostics'][0]['code'] == 'TC017'
    assert 'secret' not in str(report)


def test_symlink_escape_and_nonregular_file(tmp_path):
    (tmp_path / 'link').symlink_to('/etc/passwd')
    for path in ['link', '.']:
        assert run([SCHEMA, batch(path), STATE], batch_root=tmp_path)['diagnostics'][0]['code'] == 'TC017'


def test_bad_gzip_and_bad_records(tmp_path):
    (tmp_path / 'bad.gz').write_bytes(b'not gzip')
    report = run([SCHEMA, batch('bad.gz', 'gzip'), STATE], batch_root=tmp_path)
    assert report['diagnostics'][0]['code'] == 'TC017'
    (tmp_path / 'bad.gz').write_bytes(gzip.compress(b'{"id":"wrong"}\n{}\n\xff\n', mtime=0))
    report = run([SCHEMA, batch('bad.gz', 'gzip'), STATE], batch_root=tmp_path)
    assert {'TC006', 'TC010', 'TC001'} <= {d['code'] for d in report['diagnostics']}
    assert report['diagnostics'][0]['source'] == 'bad.gz'
    assert report['diagnostics'][0]['line'] == 1


def test_corrupt_deflate_cli_returns_json_and_continues(tmp_path):
    import json
    import subprocess
    import sys

    # Valid gzip header, reserved DEFLATE block type (BTYPE=3), zero trailer.
    corrupt = bytes.fromhex('1f8b0800000000000203') + b'\x07' + bytes(8)
    compressed = tmp_path / 'corrupt.jsonl.gz'
    compressed.write_bytes(corrupt)
    (tmp_path / 'good.jsonl').write_bytes(b'{"id":2}\n')
    messages = [SCHEMA, batch(compressed.name, 'gzip'), batch('good.jsonl'), STATE]
    capture = tmp_path / 'capture.jsonl'
    raw = ('\n'.join(json.dumps(msg) for msg in messages) + '\n').encode()
    capture.write_bytes(raw)

    result = subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', 'validate', str(capture),
                             '--format', 'json'], capture_output=True)

    assert result.returncode == 1
    assert result.stderr == b''
    report = json.loads(result.stdout)
    assert report['exit_code'] == 1 and report['stopped_early'] is False
    assert [(d['code'], d['line'], d['source']) for d in report['diagnostics']] == [
        ('TC017', 2, str(capture))]
    assert report['messages'] == {'SCHEMA': 1, 'BATCH': 2, 'STATE': 1}
    assert report['streams'][0]['records'] == 1
    assert capture.read_bytes() == raw and compressed.read_bytes() == corrupt


@pytest.mark.parametrize('encoding', [{'format':'parquet'}, {'format':'jsonl','compression':'zstd'}, {'format':'jsonl','compression':[]}, None])
def test_unsupported_encoding(encoding):
    msg = batch('batch.jsonl')
    msg['encoding'] = encoding
    assert run([SCHEMA, msg, STATE], batch_root=FIXTURES)['diagnostics'][0]['code'] == 'TC017'


def test_duplicate_tracking_spans_batch_files(tmp_path):
    (tmp_path / 'a').write_text('{"id":1}\n')
    (tmp_path / 'b').write_text('{"id":1}\n')
    msg = batch('a')
    msg['manifest'].append('b')
    report = run([SCHEMA, msg, STATE], batch_root=tmp_path, check_duplicates=True)
    assert [d['code'] for d in report['diagnostics']] == ['TC016']
    assert report['diagnostics'][0]['source'] == 'b'
