"""The conversion budget counts serialized bytes across the entire output."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SINGER = b'{"type":"STATE","value":{"cursor":1}}\n'
AIRBYTE = b'{"type":"STATE","state":{"type":"LEGACY","data":{"cursor":1}}}\n'
DIRECTIONS = [pytest.param('airbyte', SINGER, AIRBYTE, id='singer-to-airbyte'),
              pytest.param('singer', AIRBYTE, SINGER, id='airbyte-to-singer')]


def cli(*args, **kwargs):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'tapcheck', 'convert', *map(str, args)],
                          capture_output=True, **kwargs)


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
@pytest.mark.parametrize('delta', [-1, 0, 1])
@pytest.mark.parametrize('output_mode', ['stdout', 'dash', 'file'])
def test_total_exact_byte_boundary(tmp_path, target, source, expected, delta, output_mode):
    # Each line fits by itself; the final LF is the one-byte failure boundary.
    expected *= 2
    destination = tmp_path / 'output.jsonl'
    old = b'previous output\x00\xff' * 100
    args = []
    if output_mode == 'file':
        destination.write_bytes(old)
        args = ['--output', destination]
    elif output_mode == 'dash':
        args = ['--output', '-']
    result = cli('--to', target, '--max-output-bytes', len(expected) + delta,
                 *args, input=source * 2)
    if delta < 0:
        assert result.returncode == 2 and b'TC020' in result.stderr
        assert b'max-output-bytes' in result.stderr and b'Traceback' not in result.stderr
        if output_mode == 'file':
            assert destination.read_bytes() == old and result.stdout == b''
            assert b'stdout' not in result.stderr
        else:
            assert result.stdout == expected[:len(expected) // 2]
            assert b'stdout may contain partial output' in result.stderr
    else:
        assert result.returncode == 0 and result.stderr == b''
        if output_mode == 'file':
            assert result.stdout == b'' and destination.read_bytes() == expected
        else:
            assert result.stdout == expected
    assert set(tmp_path.iterdir()) == ({destination} if output_mode == 'file' else set())


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
@pytest.mark.parametrize('existing', [False, True])
def test_budget_failure_rolls_back_file_input(tmp_path, target, source, expected, existing):
    capture = tmp_path / 'input.jsonl'
    capture.write_bytes(source * 3)
    destination = tmp_path / 'output.jsonl'
    old = b'previous complete output\x00\xff\n'
    if existing:
        destination.write_bytes(old)
    result = cli(capture, '--to', target, '--max-output-bytes', len(expected) * 3 - 1,
                 '--output', destination)
    assert result.returncode == 2 and b'TC020' in result.stderr
    assert result.stdout == b'' and b'Traceback' not in result.stderr
    assert destination.read_bytes() == old if existing else not destination.exists()
    assert capture.read_bytes() == source * 3
    assert set(tmp_path.iterdir()) == ({capture, destination} if existing else {capture})


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
def test_zero_budget_emits_nothing(tmp_path, target, source, expected):
    result = cli('--to', target, '--max-output-bytes', 0, input=source)
    assert result.returncode == 2 and b'TC020' in result.stderr and result.stdout == b''
    destination = tmp_path / 'output.jsonl'
    result = cli('--to', target, '--max-output-bytes', 0, '--output', destination, input=source)
    assert result.returncode == 2 and b'TC020' in result.stderr and result.stdout == b''
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('target', ['airbyte', 'singer'])
def test_unicode_counts_json_escapes_and_final_newline(target):
    singer = '{"type":"STATE","value":{"note":"☃😀"}}\n'.encode()
    airbyte = '{"type":"STATE","state":{"type":"LEGACY","data":{"note":"☃😀"}}}\n'.encode()
    source = singer if target == 'airbyte' else airbyte
    expected = (b'{"type":"STATE","state":{"type":"LEGACY","data":{"note":"\\u2603\\ud83d\\ude00"}}}\n'
                if target == 'airbyte' else b'{"type":"STATE","value":{"note":"\\u2603\\ud83d\\ude00"}}\n')
    exact = cli('--to', target, '--max-output-bytes', len(expected), input=source)
    assert exact.returncode == 0 and exact.stdout == expected and exact.stderr == b''
    short = cli('--to', target, '--max-output-bytes', len(expected) - 1, input=source)
    assert short.returncode == 2 and b'TC020' in short.stderr and short.stdout == b''


@pytest.mark.parametrize('target', ['airbyte', 'singer'])
@pytest.mark.parametrize('external', [False, True])
def test_catalog_generated_messages_share_budget(tmp_path, target, external):
    if target == 'singer':
        # One CATALOG expands to two SCHEMAs; do not reset between yielded messages.
        catalog = {'streams': [{'name': name, 'json_schema': {}} for name in ['a', 'b']]}
        envelope = {'type': 'CATALOG', 'catalog': catalog}
        state = AIRBYTE
    else:
        schemas = [{'type': 'SCHEMA', 'stream': name, 'schema': {}, 'key_properties': []}
                   for name in ['a', 'b']]
        catalog = {'streams': schemas}
        state = SINGER
    catalog_path = tmp_path / 'catalog.json'
    args = []
    if external:
        catalog_raw = json.dumps(catalog).encode()
        catalog_path.write_bytes(catalog_raw)
        args = ['--catalog', catalog_path]
        raw = state
    else:
        declarations = [envelope] if target == 'singer' else schemas
        raw = b''.join(json.dumps(msg).encode() + b'\n' for msg in declarations) + state
    baseline = cli('--to', target, *args, input=raw)
    assert baseline.returncode == 0, baseline.stderr
    lines = baseline.stdout.splitlines(keepends=True)
    assert len(lines) == 3
    exact = cli('--to', target, *args, '--max-output-bytes', len(baseline.stdout), input=raw)
    assert exact.returncode == 0 and exact.stdout == baseline.stdout
    # Exhaust during declaration output, before the final STATE is converted.
    budget = len(lines[0]) + len(lines[1]) - 1
    short = cli('--to', target, *args, '--max-output-bytes', budget, input=raw)
    assert short.returncode == 2 and b'TC020' in short.stderr
    assert short.stdout == lines[0]
    destination = tmp_path / 'output.jsonl'
    destination.write_bytes(b'old')
    failed = cli('--to', target, *args, '--max-output-bytes', budget,
                 '--output', destination, input=raw)
    assert failed.returncode == 2 and b'TC020' in failed.stderr and failed.stdout == b''
    assert destination.read_bytes() == b'old'
    if external:
        assert catalog_path.read_bytes() == catalog_raw
    assert set(tmp_path.iterdir()) == ({destination, catalog_path} if external else {destination})


@pytest.mark.parametrize('value', ['-1', '1.5', 'invalid', '9223372036854775808'])
def test_invalid_budget_rejected_before_output_is_opened(tmp_path, value):
    destination = tmp_path / 'output.jsonl'
    destination.write_bytes(b'old')
    result = cli('--to', 'airbyte', '--max-output-bytes', value, '--output', destination, input=SINGER)
    assert result.returncode == 2 and result.stdout == b''
    assert b'usage:' in result.stderr and b'--max-output-bytes' in result.stderr
    assert b'Traceback' not in result.stderr and destination.read_bytes() == b'old'
    assert set(tmp_path.iterdir()) == {destination}


def test_largest_budget_needs_no_preallocation():
    result = cli('--to', 'airbyte', '--max-output-bytes', 2**63 - 1, input=SINGER)
    assert result.returncode == 0 and result.stdout == AIRBYTE and result.stderr == b''


@pytest.mark.parametrize('target,protocol', [('airbyte', 'singer'), ('singer', 'airbyte')])
@pytest.mark.parametrize('failure,code', [('missing_state', b'TC008'), ('bad_json', b'TC001')])
def test_exact_budget_does_not_skip_final_validation(tmp_path, target, protocol, failure, code):
    # All bytes fit, but exhausting the budget is not permission to ignore EOF checks.
    source = Path(f'examples/tapcheck/good_{protocol}.jsonl').read_bytes().splitlines(keepends=True)
    incomplete = b''.join(source[:-1]) if failure == 'missing_state' else b''.join(source) + b'{truncated\n'
    baseline = cli('--to', target, input=incomplete)
    assert baseline.returncode == 2 and code in baseline.stderr
    destination = tmp_path / 'output.jsonl'
    destination.write_bytes(b'old')
    result = cli('--to', target, '--max-output-bytes', len(baseline.stdout),
                 '--output', destination, input=incomplete)
    assert result.returncode == 2 and code in result.stderr
    assert result.stdout == b'' and destination.read_bytes() == b'old'
    assert set(tmp_path.iterdir()) == {destination}


def test_total_budget_does_not_override_line_limit(tmp_path, monkeypatch, capsys):
    import datawrench.tapcheck.cli as module

    source = tmp_path / 'input.jsonl'
    source.write_bytes(SINGER)
    monkeypatch.setattr(module, 'MAX_LINE_BYTES', len(AIRBYTE) - 1)
    assert module.main(['convert', str(source), '--to', 'airbyte',
                        '--max-output-bytes', str(2**63 - 1)]) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'TC020' in captured.err and 'output line' in captured.err


@pytest.mark.parametrize('target,source,expected', DIRECTIONS)
def test_stdout_encoding_cannot_expand_budget(target, source, expected):
    env = dict(os.environ, PYTHONIOENCODING='utf-16')
    result = cli('--to', target, '--max-output-bytes', len(expected), input=source, env=env)
    assert result.returncode == 0 and result.stderr == b''
    assert result.stdout == expected  # UTF-8 JSONL, no UTF-16 BOM or expansion.
