"""Output metadata bounds include inherited provenance and JSON/UTF-8 overhead."""
import json

import pytest

from datawrench.singer_replay.cassette import Cassette, trim
from datawrench.singer_replay.common import MAX_JSON, ReplayError, encode
from .test_trim_plan import cli, record, schema, snapshot


def encoded(value):
    return (encode(value) + '\n').encode()


def boundary_source(make_cassette, delta):
    a = schema('alpha')
    source = make_cassette([a, record('alpha', 1), a, record('alpha', 2), a])
    manifest = json.loads((source / 'manifest.json').read_bytes())
    manifest['scrub'] = [{'stream': 'alpha', 'field': '密\n"\\', 'mode': 'mask'}]
    expected = {**manifest, 'counts': {'alpha': 1}, 'message_count': 3, 'truncated': True,
                'trim': {'per_stream': 1, 'source_sha256': manifest['sha256'], 'schema_once': True}}
    # Source digests here have exactly the same encoded width as real output digests.
    padding = MAX_JSON + delta - len(encoded(expected))
    manifest['scrub'][0]['field'] += 'x' * padding
    assert len(encoded(expected)) == MAX_JSON + delta
    assert len(encoded(manifest)) <= MAX_JSON
    (source / 'manifest.json').write_bytes(encoded(manifest))
    with Cassette(source):
        pass
    return source, expected


def test_dry_run_rejects_generated_manifest_overflow(tmp_path, make_cassette):
    source, _ = boundary_source(make_cassette, 1)
    before = snapshot(tmp_path)
    result = cli(source, '--dry-run', '--per-stream', 1, '--schema-once')
    assert result.returncode == 2, result
    assert result.stdout == b''
    assert result.stderr == b'datawrench singer-replay: cassette metadata exceeds 16 MiB\n'
    assert snapshot(tmp_path) == before
    with pytest.raises(ReplayError, match='cassette metadata exceeds 16 MiB'):
        trim(source, per_stream=1, schema_once=True, dry_run=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('delta', [-1, 0])
def test_dry_run_manifest_boundary_matches_real_trim(tmp_path, make_cassette, monkeypatch, delta):
    source, expected = boundary_source(make_cassette, delta)
    before = snapshot(tmp_path)
    summary = {'counts': {'alpha': 1}, 'message_count': 3, 'truncated': True, 'streams': ['alpha']}
    # Planning must not hash/produce a pretend messages or catalog artifact.
    with monkeypatch.context() as patch:
        patch.setattr('datawrench.singer_replay.cassette.digest_file', lambda _: pytest.fail('output digest in plan'))
        patch.setattr('datawrench.singer_replay.cassette.write_private', lambda *_: pytest.fail('write in plan'))
        assert trim(source, per_stream=1, schema_once=True, dry_run=True) == summary
    result = cli(source, '--dry-run', '--per-stream', 1, '--schema-once')
    assert (result.returncode, result.stdout, result.stderr) == (0, encoded(summary), b'')
    assert snapshot(tmp_path) == before
    dest = tmp_path / 'real'
    result = cli(source, '-o', dest, '--per-stream', 1, '--schema-once')
    assert (result.returncode, result.stdout, result.stderr) == (
        0, b'', b'datawrench singer-replay: trimmed to 1 records; truncated=True\n')
    assert len((dest / 'manifest.json').read_bytes()) == MAX_JSON + delta
    with Cassette(dest) as cassette:
        assert {key: cassette.manifest[key] for key in summary if key != 'streams'} == {
            key: value for key, value in summary.items() if key != 'streams'}
        assert list(cassette.validator.schemas) == summary['streams']
        assert cassette.manifest['scrub'] == expected['scrub']
        assert cassette.manifest['recorded_at'] == expected['recorded_at']
        assert cassette.manifest['trim'] == expected['trim']
    assert snapshot(source) == {key.removeprefix(source.name + '/'): value
                                for key, value in before.items() if key.startswith(source.name + '/')}


def test_real_manifest_overflow_keeps_incomplete_output(tmp_path, make_cassette):
    source, _ = boundary_source(make_cassette, 1)
    before = snapshot(source)
    dest = tmp_path / 'real'
    result = cli(source, '-o', dest, '--per-stream', 1, '--schema-once')
    assert (result.returncode, result.stdout, result.stderr) == (
        2, b'', b'datawrench singer-replay: cassette metadata exceeds 16 MiB\n')
    assert json.loads((dest / 'manifest.json').read_bytes())['complete'] is False
    assert sorted(p.name for p in dest.iterdir()) == ['catalog.json', 'manifest.json', 'messages.jsonl.gz']
    with pytest.raises(ReplayError, match='incomplete'):
        with Cassette(dest):
            pass
    assert snapshot(source) == before


@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_writer_initial_manifest_boundary(tmp_path, monkeypatch, delta):
    from datawrench.singer_replay.cassette import _record_messages

    # A small bound isolates the initial write: successful initial writes remain
    # on disk when the larger final manifest fails. Above it, only mkdir occurs.
    bound = 1024
    metadata = {'recorded_at': '2024-01-01T00:00:00+00:00', 'scrub': ['密\n"\\']}
    initial = {'format_version': 1, 'complete': False, **metadata}
    metadata['scrub'][0] += 'x' * (bound + delta - len(encoded(initial)))
    assert len(encoded(initial)) == bound + delta
    monkeypatch.setattr('datawrench.singer_replay.cassette.MAX_JSON', bound)
    dest = tmp_path / 'recording'
    with pytest.raises(ReplayError, match='cassette metadata exceeds 16 MiB'):
        _record_messages(iter([schema('alpha')]), dest, metadata=metadata)
    if delta > 0:
        assert list(dest.iterdir()) == []
    else:
        assert (dest / 'manifest.json').read_bytes() == encoded(initial)
        assert (dest / 'catalog.json').is_file()
        assert (dest / 'messages.jsonl.gz').is_file()
