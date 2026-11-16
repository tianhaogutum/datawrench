"""Raw main-input byte budgets preserve exact-fit validation and atomic output."""
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from datawrench.tapcheck.convert import convert
from datawrench.tapcheck.io import InputError, lines

SINGER = b'{"type":"STATE","value":{"cursor":1}}\n'
AIRBYTE = b'{"type":"STATE","state":{"type":"LEGACY","data":{"cursor":1}}}\n'
DIRECTIONS = [('airbyte', SINGER, AIRBYTE), ('singer', AIRBYTE, SINGER)]


def cli(*args, **kwargs):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', 'convert', *map(str, args)],
                          capture_output=True, **kwargs)


def incoming(tmp_path, raw, mode):
    if mode == 'stdin':
        return [], {'input': raw}
    path = tmp_path / 'input.jsonl'
    path.write_bytes(raw)
    return [path], {}


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('variant', ['plain', 'crlf-blanks', 'utf8-no-lf'])
@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_raw_byte_boundaries(tmp_path, target, source, expected, mode, variant, delta):
    if variant == 'crlf-blanks':
        source = b'\r\n \t\r\n' + source.replace(b'\n', b'\r\n') + b'\n\r\n'
    elif variant == 'utf8-no-lf':
        source = source.replace(b'1', '"☃😀"'.encode()).rstrip(b'\n')
        expected = expected.replace(b'1', b'"\\u2603\\ud83d\\ude00"')
    args, kwargs = incoming(tmp_path, source, mode)
    result = cli(*args, '--to', target, '--max-input-bytes', len(source) + delta, **kwargs)
    if delta < 0:
        assert result.returncode == 2 and b'TC020' in result.stderr
        assert b'--max-input-bytes' in result.stderr
        assert b'stdout may contain partial output' in result.stderr
    else:
        assert result.returncode == 0 and result.stdout == expected and result.stderr == b''
    assert b'Traceback' not in result.stderr
    if mode == 'file':
        assert Path(args[0]).read_bytes() == source


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('existing', [False, True])
def test_trailing_blank_overflow_rolls_back(tmp_path, target, source, expected, mode, existing):
    raw = source + b'\n'  # A complete valid prefix is not EOF.
    args, kwargs = incoming(tmp_path, raw, mode)
    destination = tmp_path / 'output.jsonl'
    if existing:
        destination.write_bytes(b'old\x00\xff')
    before = set(tmp_path.iterdir())
    result = cli(*args, '--to', target, '--max-input-bytes', len(source),
                 '--output', destination, **kwargs)
    assert result.returncode == 2 and b'TC020' in result.stderr
    assert b'--max-input-bytes' in result.stderr and b'stdout' not in result.stderr
    assert result.stdout == b''
    assert destination.read_bytes() == b'old\x00\xff' if existing else not destination.exists()
    assert set(tmp_path.iterdir()) == before
    if mode == 'file':
        assert Path(args[0]).read_bytes() == raw


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('raw,code', [(b'', b'TC021'), (b'\n', b'TC020'), (b' ', b'TC020')])
def test_zero_and_empty(tmp_path, target, source, expected, mode, raw, code):
    args, kwargs = incoming(tmp_path, raw, mode)
    result = cli(*args, '--to', target, '--max-input-bytes', 0, '--output', '-', **kwargs)
    assert result.returncode == 2 and code in result.stderr and result.stdout == b''
    assert b'stdout may contain partial output' in result.stderr


@pytest.mark.parametrize('value', ['-1', '1.5', 'invalid', '9223372036854775808'])
def test_invalid_option_before_open(tmp_path, value):
    destination = tmp_path / 'output.jsonl'
    destination.write_bytes(b'old')
    result = cli('--to', 'airbyte', '--max-input-bytes', value,
                 '--output', destination, input=SINGER)
    assert result.returncode == 2 and result.stdout == b''
    assert b'usage:' in result.stderr and b'--max-input-bytes' in result.stderr
    assert b'Traceback' not in result.stderr
    assert destination.read_bytes() == b'old' and set(tmp_path.iterdir()) == {destination}


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
def test_maximum_no_preallocation_and_combined_output_budget(target, source, expected):
    result = cli('--to', target, '--max-input-bytes', 2**63 - 1,
                 '--max-output-bytes', len(expected), input=source)
    assert result.returncode == 0 and result.stdout == expected and result.stderr == b''
    result = cli('--to', target, '--max-input-bytes', len(source),
                 '--max-output-bytes', len(expected) - 1, input=source)
    assert result.returncode == 2 and b'--max-output-bytes' in result.stderr


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
@pytest.mark.parametrize('mode', ['file', 'stdin'])
def test_external_catalog_excluded_and_preserved(tmp_path, target, source, expected, mode):
    catalog = {'streams': ([{'type': 'SCHEMA', 'stream': 's', 'schema': {}, 'key_properties': []}]
                           if target == 'airbyte' else [{'name': 's', 'json_schema': {}}])}
    catalog_path = tmp_path / 'catalog.json'
    catalog_bytes = json.dumps(catalog).encode()
    catalog_path.write_bytes(catalog_bytes)
    args, kwargs = incoming(tmp_path, source, mode)
    baseline = cli(*args, '--to', target, '--catalog', catalog_path, **kwargs)
    assert baseline.returncode == 0
    destination = tmp_path / 'output.jsonl'
    result = cli(*args, '--to', target, '--catalog', catalog_path,
                 '--max-input-bytes', len(source), '--output', destination, **kwargs)
    assert result.returncode == 0 and destination.read_bytes() == baseline.stdout
    before = set(tmp_path.iterdir())
    result = cli(*args, '--to', target, '--catalog', catalog_path,
                 '--max-input-bytes', len(source) - 1, '--output', destination, **kwargs)
    assert result.returncode == 2 and b'TC020' in result.stderr
    assert result.stdout == b'' and destination.read_bytes() == baseline.stdout
    assert catalog_path.read_bytes() == catalog_bytes and set(tmp_path.iterdir()) == before
    if mode == 'file':
        assert Path(args[0]).read_bytes() == source


@pytest.mark.parametrize('target,protocol', [('airbyte', 'singer'), ('singer', 'airbyte')])
@pytest.mark.parametrize('mode', ['file', 'stdin'])
@pytest.mark.parametrize('failure,code', [('missing_state', b'TC008'), ('bad_json', b'TC001')])
def test_exact_fit_still_validates(tmp_path, target, protocol, mode, failure, code):
    raw = Path(f'examples/tapcheck/good_{protocol}.jsonl').read_bytes()
    raw = b''.join(raw.splitlines(keepends=True)[:-1]) if failure == 'missing_state' else raw + b'{broken'
    args, kwargs = incoming(tmp_path, raw, mode)
    destination = tmp_path / 'output.jsonl'
    destination.write_bytes(b'old')
    before = set(tmp_path.iterdir())
    result = cli(*args, '--to', target, '--max-input-bytes', len(raw),
                 '--output', destination, **kwargs)
    assert result.returncode == 2 and code in result.stderr
    assert result.stdout == b'' and destination.read_bytes() == b'old'
    assert set(tmp_path.iterdir()) == before
    if mode == 'file':
        assert Path(args[0]).read_bytes() == raw


class TrackingRaw(io.RawIOBase):
    """Observe actual raw reads underneath buffering, including EOF probes."""
    def __init__(self, data):
        self.data = io.BytesIO(data)
        self.requests = []
        self.eof_reads = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        self.requests.append(len(buffer))
        count = self.data.readinto(buffer)
        self.eof_reads += count == 0
        return count


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
@pytest.mark.parametrize('blank_prefix', [False, True])
def test_overflow_raw_read_bound_including_buffered_input(target, source, expected, blank_prefix):
    prefix = b'\n' * 10000 if blank_prefix else source
    budget = len(prefix)
    raw = TrackingRaw(prefix + b'\n' * 100000)
    with io.BufferedReader(raw) as handle:
        with pytest.raises(InputError, match='--max-input-bytes') as caught:
            list(convert(handle, target, max_input_bytes=budget))
        assert caught.value.code == 'TC020'
        assert raw.data.tell() == budget + 1
        assert not handle.closed  # The converter does not own its source.


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
def test_exact_fit_checks_real_eof(target, source, expected):
    raw = TrackingRaw(source.rstrip(b'\n'))
    with io.BufferedReader(raw) as handle:
        assert list(convert(handle, target, max_input_bytes=len(source) - 1))
        assert raw.data.tell() == len(source) - 1 and raw.eof_reads > 0


def test_budget_bounds_oversized_line_drain_and_keeps_line_limit():
    raw = TrackingRaw(b' ' * 100000)
    with io.BufferedReader(raw) as handle:
        with pytest.raises(InputError, match='--max-input-bytes') as caught:
            list(lines(handle, 'capture', limit=8, max_bytes=32))
        assert caught.value.code == 'TC020' and raw.data.tell() == 33
    # If total bytes fit, the existing independent per-line diagnostic remains.
    issues = list(lines(io.BytesIO(b' ' * 9 + b'\n'), 'capture', limit=8, max_bytes=10))
    assert len(issues) == 1 and issues[0][1].code == 'TC020'
    assert issues[0][1].message == 'line exceeds byte limit'


def test_catalog_limit_remains_independent(tmp_path, monkeypatch, capsys):
    import datawrench.tapcheck.cli as module

    source = tmp_path / 'input.jsonl'
    source.write_bytes(SINGER)
    catalog = tmp_path / 'catalog.json'
    catalog.write_bytes(b' ' * 101)
    monkeypatch.setattr(module, 'MAX_LINE_BYTES', 100)
    assert module.main(['convert', str(source), '--to', 'airbyte', '--catalog', str(catalog),
                        '--max-input-bytes', str(len(SINGER))]) == 2
    assert 'catalog exceeds' in capsys.readouterr().err
    assert source.read_bytes() == SINGER and catalog.read_bytes() == b' ' * 101


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
def test_cumulative_budget_and_partial_explicit_stdout(target, source, expected):
    raw = source * 2
    result = cli('--to', target, '--max-input-bytes', len(raw), input=raw)
    assert result.returncode == 0 and result.stdout == expected * 2 and result.stderr == b''
    result = cli('--to', target, '--max-input-bytes', len(raw) - 1, '--output', '-', input=raw)
    assert result.returncode == 2 and b'TC020' in result.stderr
    assert b'stdout may contain partial output' in result.stderr and result.stdout == expected


@pytest.mark.parametrize('budget', [0, 1, 8191, 8192])
def test_blank_overflow_reads_only_one_extra_raw_byte(budget):
    raw = TrackingRaw(b'\n' * 100000)
    with io.BufferedReader(raw) as handle:
        with pytest.raises(InputError, match='--max-input-bytes'):
            list(convert(handle, 'airbyte', max_input_bytes=budget))
        assert raw.data.tell() == budget + 1


def test_prebuffered_input_bytes_are_not_skipped():
    raw = TrackingRaw(SINGER + b'\n' * 10000)
    with io.BufferedReader(raw) as handle:
        handle.peek(1)  # A Python caller may already have read ahead before conversion.
        assert raw.data.tell() > len(SINGER)
        with pytest.raises(InputError, match='--max-input-bytes'):
            list(convert(handle, 'airbyte', max_input_bytes=len(SINGER)))
        # The extra byte was consumed from the existing buffer; no further raw read.
        assert raw.data.tell() == 8192
