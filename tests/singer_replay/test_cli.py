import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # datawrench repository root
# Former console scripts, now datawrench subcommands.
COMMANDS = {
    'singer-replay': [sys.executable, '-m', 'datawrench', 'singer-replay'],
    'tap-replay': [sys.executable, '-m', 'datawrench', 'singer-replay', 'tap'],
}
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'fake_tap.py'
EXAMPLES = ROOT / 'examples' / 'singer_replay'


def cli(name, *args, input=None):
    return subprocess.run([*COMMANDS[name], *map(str, args)], input=input,
                          capture_output=True, timeout=15)


def config(tmp_path, path, **settings):
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'cassette_path': str(path), **settings}))
    return cfg


def test_record_subprocess_and_discover(tmp_path):
    path = tmp_path / 'shop'
    result = cli('singer-replay', 'record', '-o', path, '--scrub', 'customers.email=hash',
                 '--', sys.executable, FIXTURE)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b''
    cfg = config(tmp_path, path)
    result = cli('tap-replay', '--config', cfg, '--discover')
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)['streams']) == 2
    replay = cli('tap-replay', '--config', cfg)
    assert replay.returncode == 0, replay.stderr
    assert len(replay.stdout.splitlines()) == 8
    assert b'alice@example.invalid' not in replay.stdout


def test_nested_scrub_record_discover_replay_cli(tmp_path):
    path = tmp_path / 'nested'
    result = cli('singer-replay', 'record', '-o', path,
                 '--scrub', 'customers:profile.phone=hash',
                 '--', sys.executable, FIXTURE)
    assert result.returncode == 0, result.stderr
    cfg = config(tmp_path, path)
    discovery = cli('tap-replay', '--config', cfg, '--discover')
    assert discovery.returncode == 0, discovery.stderr
    profile = json.loads(discovery.stdout)['streams'][0]['schema']['properties']['profile']
    assert profile['properties']['phone'] == {'type': ['string', 'null']}
    result = cli('tap-replay', '--config', cfg)
    assert result.returncode == 0, result.stderr
    output = [json.loads(line) for line in result.stdout.splitlines()]
    profiles = [m['record']['profile'] for m in output
                if m['type'] == 'RECORD' and m['stream'] == 'customers']
    assert len(profiles) == 3
    assert len({p['phone'] for p in profiles}) == 1 and len(profiles[0]['phone']) == 64
    assert all(p['city'] == 'Demo City' for p in profiles)
    assert b'555-0100' not in result.stdout


def test_nonzero_tap_marks_incomplete(tmp_path):
    path = tmp_path / 'incomplete'
    result = cli('singer-replay', 'record', '-o', path, '--', sys.executable, FIXTURE,
                 '--exit-code', '9')
    assert result.returncode == 2
    manifest = json.loads((path / 'manifest.json').read_text())
    assert manifest['complete'] is False and manifest['source_exit_code'] == 9
    result = cli('tap-replay', '--config', config(tmp_path, path))
    assert result.returncode == 2 and result.stdout == b''
    assert b'incomplete' in result.stderr


def test_limit_stops_running_source(tmp_path):
    path = tmp_path / 'limited'
    script = "import sys,time;print(open(sys.argv[1]).read(),flush=True);time.sleep(30)"
    result = cli('singer-replay', 'record', '-o', path, '--limit', '1', '--',
                 sys.executable, '-c', script, FIXTURE.with_name('shop.jsonl'))
    assert result.returncode == 0, result.stderr
    manifest = json.loads((path / 'manifest.json').read_text())
    assert manifest['complete'] is True and manifest['truncated'] is True
    assert sum(manifest['counts'].values()) == 1


def test_salt_file_determinism_and_no_shell(tmp_path):
    salt = tmp_path / 'salt'
    salt.write_bytes(b'test-only-salt-not-a-real-secret!' * 2)
    paths = [tmp_path / 'a', tmp_path / 'b']
    for path in paths:
        result = cli('singer-replay', 'record', '-o', path, '--scrub', 'customers.email=hash',
                     '--salt-file', salt, input=FIXTURE.with_name('shop.jsonl').read_bytes())
        assert result.returncode == 0, result.stderr
    assert (paths[0] / 'messages.jsonl.gz').read_bytes() == (paths[1] / 'messages.jsonl.gz').read_bytes()
    sentinel = tmp_path / 'must-not-exist'
    result = cli('singer-replay', 'record', '-o', tmp_path / 'noshell', '--',
                 'echo shell-command; touch ' + str(sentinel))
    assert result.returncode == 2 and not sentinel.exists()


def test_corrupt_cassette_emits_no_partial_stdout(tmp_path, messages, make_cassette):
    path = make_cassette(messages)
    (path / 'messages.jsonl.gz').write_bytes(b'corrupt')
    result = cli('tap-replay', '--config', config(tmp_path, path))
    assert result.returncode == 2 and result.stdout == b''
    assert b'checksum mismatch' in result.stderr


def invalid_offset(messages, location):
    # Python used to normalize this to 12:00Z, satisfying all bookmark checks.
    bad = '2024-01-01T13:00:00+00:60'
    if location == 'record':
        messages[2]['record']['updated_at'] = bad
    elif location == 'time_extracted':
        messages[2]['time_extracted'] = bad
    else:
        messages[4]['value']['bookmarks']['customers']['replication_key_value'] = bad


@pytest.mark.parametrize('location', ['record', 'time_extracted', 'state'])
def test_record_invalid_offset_is_incomplete(tmp_path, messages, location):
    invalid_offset(messages, location)
    path = tmp_path / 'bad-offset'
    result = cli('singer-replay', 'record', '-o', path,
                 input=('\n'.join(json.dumps(m) for m in messages) + '\n').encode())
    assert result.returncode == 2 and result.stdout == b''
    assert b'RFC3339' in result.stderr
    assert b'+00:60' not in result.stderr
    assert json.loads((path / 'manifest.json').read_text())['complete'] is False
    result = cli('tap-replay', '--config', config(tmp_path, path))
    assert result.returncode == 2 and result.stdout == b''


@pytest.mark.parametrize('location', ['record', 'time_extracted', 'state'])
@pytest.mark.parametrize('discover', [False, True])
def test_replay_invalid_offset_preflight(tmp_path, messages, make_cassette, location, discover):
    path = make_cassette(messages)
    invalid_offset(messages, location)
    data = gzip.compress(('\n'.join(json.dumps(m) for m in messages) + '\n').encode())
    (path / 'messages.jsonl.gz').write_bytes(data)
    manifest = json.loads((path / 'manifest.json').read_text())
    manifest['sha256']['messages.jsonl.gz'] = hashlib.sha256(data).hexdigest()
    (path / 'manifest.json').write_text(json.dumps(manifest))
    # Matching checksums must not bypass semantic validation of older cassettes.
    result = cli('tap-replay', '--config', config(tmp_path, path),
                 *(['--discover'] if discover else []))
    assert result.returncode == 2 and result.stdout == b''
    assert b'RFC3339' in result.stderr


@pytest.mark.parametrize('setting', ['time_shift', 'state'])
def test_replay_invalid_offset_inputs_emit_nothing(tmp_path, messages, make_cassette, setting):
    path = make_cassette(messages)
    if setting == 'time_shift':
        args = ['--config', config(tmp_path, path, time_shift='2030-01-01T12:00:00+00:60')]
    else:
        invalid_offset(messages, 'state')
        state = tmp_path / 'bad-state.json'
        state.write_text(json.dumps(messages[4]['value']))
        args = ['--config', config(tmp_path, path), '--state', state]
    result = cli('tap-replay', *args)
    assert result.returncode == 2 and result.stdout == b''
    assert b'RFC3339' in result.stderr


def test_fault_exit_code_and_zero(tmp_path, messages, make_cassette):
    path = make_cassette(messages)
    for count in [0, 2]:
        result = cli('tap-replay', '--config', config(tmp_path, path, fail_after_records=count,
                                                     fail_exit_code=23))
        assert result.returncode == 23
        output = [json.loads(x) for x in result.stdout.splitlines()]
        assert sum(m['type'] == 'RECORD' for m in output) == count
        assert output[-1]['type'] == 'STATE'


def test_about_and_missing_inputs(tmp_path):
    result = cli('tap-replay', '--about', '--format', 'json')
    assert result.returncode == 0
    assert 'cassette_path' in json.loads(result.stdout)['settings']['properties']
    assert cli('tap-replay').returncode == 2
    assert cli('tap-replay', '--config', tmp_path / 'missing.json').returncode == 2
    assert cli('tap-replay', '--config', config(tmp_path, tmp_path / 'missing-cassette')).returncode == 2


def test_unknown_stream_warning(tmp_path, messages, make_cassette):
    cat = tmp_path / 'catalog.json'
    cat.write_text(json.dumps({'streams': [{'tap_stream_id': 'unknown'}]}))
    result = cli('tap-replay', '--config', config(tmp_path, make_cassette(messages)), '--catalog', cat)
    assert result.returncode == 0 and b'unknown catalog stream' in result.stderr
    assert all(json.loads(line)['type'] == 'STATE' for line in result.stdout.splitlines())


def test_real_target_jsonl_pipeline(tmp_path, messages, make_cassette):
    # Optional real loader, installed separately in its own environment (see docs/singer-replay.md).
    found = shutil.which('target-jsonl')
    if found is None:
        pytest.skip('Optional third-party loader absent: put an isolated target-jsonl==0.1.4 on PATH')
    target = Path(found)
    from datawrench.singer_replay.cassette import scrub_rules
    path = make_cassette(messages, rules=scrub_rules(['customers.email=hash']), salt=b'T' * 32)
    cfg = config(tmp_path, path)
    destination = tmp_path / 'target-output'
    target_cfg = tmp_path / 'target.json'
    target_cfg.write_text(json.dumps({'destination_path': str(destination), 'do_timestamp_file': False}))
    with subprocess.Popen([*COMMANDS['tap-replay'], '--config', str(cfg),
                           '--catalog', str(EXAMPLES / 'catalog.selected.json'),
                           '--state', str(EXAMPLES / 'state.json')],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as tap:
        with subprocess.Popen([str(target), '--config', str(target_cfg)], stdin=tap.stdout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path) as loader:
            tap.stdout.close()
            state, errors = loader.communicate(timeout=15)
            assert loader.returncode == 0, errors.decode()
        tap.wait(timeout=15)
        assert tap.returncode == 0, tap.stderr.read().decode()
    output = [json.loads(x) for x in (destination / 'customers.jsonl').read_text().splitlines()]
    assert [row['id'] for row in output] == [2, 3]
    assert all(set(row) == {'id', 'email', 'updated_at'} for row in output)
    assert all(len(row['email']) == 64 for row in output)
    assert not (destination / 'products.jsonl').exists()
    assert json.loads(state) == messages[-1]['value']
