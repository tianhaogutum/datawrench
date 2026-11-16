"""Offline scrub must preserve source boundaries, keys and inherited metadata."""
import copy
import gzip
import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from datawrench.singer_replay.cassette import Cassette, scrub_rules
from datawrench.singer_replay.common import ReplayError, encode


def cli(source, dest, *rules):
    args = [sys.executable, '-m', 'datawrench', 'singer-replay', 'scrub', str(source),
            '-o', str(dest)]
    for rule in rules:
        args += ['--scrub', rule]
    return subprocess.run(args, capture_output=True, timeout=15)


def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
            p.read_bytes() if p.is_file() else None) for p in [root, *root.rglob('*')]}


@pytest.fixture
def alpha():
    schema = {'type': 'SCHEMA', 'stream': 'alpha', 'key_properties': ['id'],
              'replication_key': 'at', 'schema': {'type': 'object', 'properties': {
                  'id': {'type': 'integer'}, 'at': {'type': 'string', 'format': 'date-time'},
                  'secret': {'type': ['string', 'null'], 'default': 'synthetic-private'},
                  'old': {'type': 'string'}, 'safe': {'type': 'boolean'}},
              'required': ['id', 'at', 'secret', 'old', 'safe']}}
    messages = [schema]
    for n in (1, 2):
        messages += [{'type': 'RECORD', 'stream': 'alpha', 'record': {
            'id': n, 'at': f'2024-01-0{n}T00:00:00Z', 'secret': 'synthetic-private',
            'old': 'inherited-private', 'safe': True}, 'time_extracted': '2024-02-01T00:00:00Z'},
            {'type': 'STATE', 'value': {'bookmarks': {'alpha': {
                'replication_key': 'at', 'replication_key_value': f'2024-01-0{n}T00:00:00Z'}}}},
            copy.deepcopy(schema)]
    return messages


def test_scrub_cli_preserves_boundaries_and_provenance(tmp_path, make_cassette, alpha):
    source = make_cassette(alpha, rules=scrub_rules(['alpha.old=mask']))
    before = snapshot(source)
    dest = tmp_path / 'cleaned'
    result = cli(source, dest, 'alpha.secret=mask')
    assert result.returncode == 0, result.stderr
    assert result.stdout == b''
    assert result.stderr == b'datawrench singer-replay: scrubbed 2 records; truncated=False\n'
    with Cassette(source) as original, Cassette(dest) as cleaned:
        expected = list(original.messages())
        for m in expected:
            if m['type'] == 'SCHEMA':
                m['schema']['properties']['secret'] = {'type': ['string', 'null']}
            elif m['type'] == 'RECORD':
                m['record']['secret'] = '***'
        assert list(cleaned.messages()) == expected
        for key in ('recorded_at', 'counts', 'message_count', 'truncated'):
            assert cleaned.manifest[key] == original.manifest[key]
        new = [{'stream': 'alpha', 'field': 'secret', 'mode': 'mask'}]
        assert cleaned.manifest['scrub'] == original.manifest['scrub'] + new
        assert cleaned.manifest['rescrub'] == {'rules': new, 'source_sha256': original.manifest['sha256']}
        assert cleaned.manifest['source_exit_code'] is None
        for name, digest in cleaned.manifest['sha256'].items():
            assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    assert b'synthetic-private' not in gzip.decompress((dest / 'messages.jsonl.gz').read_bytes())
    assert b'synthetic-private' not in (dest / 'catalog.json').read_bytes()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in dest.iterdir())
    assert snapshot(source) == before


@pytest.mark.parametrize('mode', ['mask', 'drop'])
@pytest.mark.parametrize('target', ['alpha.secret', 'alpha:secret', 'alpha:/secret'])
def test_modes_and_path_forms(tmp_path, make_cassette, alpha, mode, target):
    from datawrench.singer_replay.cassette import scrub
    alpha[4]['record']['secret'] = None
    source = make_cassette(alpha)
    dest = tmp_path / 'cleaned'
    scrub(source, dest, rules=[f'{target}={mode}'])
    with Cassette(dest) as c:
        records = [m['record'] for m in c.messages() if m['type'] == 'RECORD']
        if mode == 'drop':
            assert all('secret' not in r for r in records)
            assert 'secret' not in c.catalog['streams'][0]['schema']['properties']
            assert 'secret' not in c.catalog['streams'][0]['schema']['required']
        else:
            assert [r['secret'] for r in records] == ['***', None]
        assert all(r['old'] == 'inherited-private' and r['safe'] for r in records)


def test_nested_pointer_and_dotted_stream(tmp_path, make_cassette):
    from datawrench.singer_replay.cassette import scrub
    s = {'type': 'SCHEMA', 'stream': 'audit.alpha', 'key_properties': [],
         'schema': {'type': 'object', 'properties': {'profile': {'type': 'object',
         'properties': {'a/b~.雪': {'type': 'string'}, 'public': {'type': 'integer'}},
         'required': ['a/b~.雪'], 'default': {'a/b~.雪': 'nested-private'}}},
         'examples': [{'profile': {'a/b~.雪': 'nested-private'}}]}}
    r = {'type': 'RECORD', 'stream': 'audit.alpha',
         'record': {'profile': {'a/b~.雪': 'nested-private', 'public': 7}}}
    source = make_cassette([s, r, s])
    dest = tmp_path / 'cleaned'
    scrub(source, dest, rules=['audit.alpha:/profile/a~1b~0.雪=drop'])
    with Cassette(dest) as c:
        assert list(c.messages())[1]['record'] == {'profile': {'public': 7}}
        assert c.manifest['scrub'] == [{'stream': 'audit.alpha', 'path': ['profile', 'a/b~.雪'], 'mode': 'drop'}]
        assert c.manifest['message_count'] == 3
    assert b'nested-private' not in gzip.decompress((dest / 'messages.jsonl.gz').read_bytes())
    assert b'nested-private' not in (dest / 'catalog.json').read_bytes()


@pytest.mark.parametrize('rules', [[], ['bad'], ['alpha.secret=hash'], ['alpha.secret=bogus'],
    ['unknown.secret=mask'], ['alpha.missing=mask'], ['alpha.id=drop'], ['alpha.at=mask'],
    ['alpha:/id=mask'], ['alpha.secret=mask', 'alpha:/secret=drop'],
    ['alpha:secret=mask', 'alpha:secret.child=drop'], ['alpha:/bad~2=mask'],
    ['alpha:secret.child=mask']])
def test_invalid_rules_precede_output(tmp_path, make_cassette, alpha, rules):
    source = make_cassette(alpha)
    before = snapshot(tmp_path)
    result = cli(source, tmp_path / 'cleaned', *rules)
    assert result.returncode == 2 and result.stdout == b'' and result.stderr
    assert b'synthetic-private' not in result.stderr and b'Traceback' not in result.stderr
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('rules', [None, {}, 'alpha.secret=mask', [None], [1], ()])
def test_api_rules_types(tmp_path, rules):
    from datawrench.singer_replay.cassette import scrub
    with pytest.raises(ReplayError):
        scrub(tmp_path / 'absent', tmp_path / 'out', rules=rules)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('case', ['source', 'exists', 'nested', 'alias', 'alias-nested', 'dangling',
                                  'loop-nested'])
def test_destination_guards(tmp_path, make_cassette, alpha, case):
    source = make_cassette(alpha)
    dest = tmp_path / 'dest'
    if case == 'source':
        dest = source / '..' / source.name
    elif case == 'exists':
        dest.mkdir()
    elif case == 'nested':
        dest = source / 'dest'
    elif case in ('alias', 'alias-nested'):
        dest.symlink_to(source, target_is_directory=True)
        if case == 'alias-nested':
            dest = dest / 'child'
    elif case == 'dangling':
        dest.symlink_to(tmp_path / 'absent')
    elif case == 'loop-nested':
        # Python 3.11 raises RuntimeError from resolve(); it must still be a clean refusal.
        loop = tmp_path / 'loop'
        loop.symlink_to(loop)
        dest = loop / 'child'
    before = snapshot(source)
    result = cli(source, dest, 'alpha.secret=mask')
    assert result.returncode == 2 and result.stdout == b''
    assert snapshot(source) == before
    assert not (source / 'dest').exists() and not (source / 'child').exists()
    assert not (tmp_path / 'absent').exists()


def rewrite(source, messages=None, **changes):
    manifest = json.loads((source / 'manifest.json').read_bytes())
    if messages is not None:
        (source / 'messages.jsonl.gz').write_bytes(gzip.compress(
            ''.join(encode(m) + '\n' for m in messages).encode(), mtime=0))
    for name in manifest['sha256']:
        manifest['sha256'][name] = hashlib.sha256((source / name).read_bytes()).hexdigest()
    manifest.update(changes)
    (source / 'manifest.json').write_text(encode(manifest) + '\n')


@pytest.mark.parametrize('case', ['late-record', 'late-state', 'evolution', 'checksum',
    'catalog', 'counts', 'incomplete', 'gzip', 'member', 'provenance'])
def test_whole_source_rejected_before_output(tmp_path, make_cassette, alpha, case):
    source = make_cassette(alpha)
    if case == 'late-record':
        alpha[4]['record']['id'] = 'invalid'
        rewrite(source, alpha)
    elif case == 'late-state':
        alpha[5]['value']['bookmarks']['alpha']['replication_key_value'] = '2030-01-01T00:00:00Z'
        rewrite(source, alpha)
    elif case == 'evolution':
        alpha[-1]['schema']['properties']['new'] = {'type': 'integer'}
        rewrite(source, alpha)
    elif case == 'checksum':
        (source / 'messages.jsonl.gz').write_bytes(b'corrupt')
    elif case == 'catalog':
        (source / 'catalog.json').write_text('{"streams":[]}\n')
        rewrite(source)
    elif case == 'counts':
        rewrite(source, counts={'alpha': 20})
    elif case == 'incomplete':
        rewrite(source, complete=False)
    elif case == 'gzip':
        p = source / 'messages.jsonl.gz'
        p.write_bytes(p.read_bytes()[:-2])
        rewrite(source)
    elif case == 'member':
        p = source / 'catalog.json'
        data = p.read_bytes()
        p.unlink()
        (tmp_path / 'catalog').write_bytes(data)
        p.symlink_to(tmp_path / 'catalog')
    elif case == 'provenance':
        rewrite(source, scrub={'invalid': 'shape'})
    before = snapshot(tmp_path)
    result = cli(source, tmp_path / 'dest', 'alpha.secret=mask')
    assert result.returncode == 2 and result.stdout == b'' and result.stderr
    assert b'synthetic-private' not in result.stderr and b'Traceback' not in result.stderr
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('case', ['missing-leaf', 'null-parent', 'missing-parent'])
def test_explicit_record_path_failure_leaves_incomplete(tmp_path, make_cassette, case):
    s = {'type': 'SCHEMA', 'stream': 'alpha', 'key_properties': [], 'schema': {
        'type': 'object', 'properties': {'profile': {'type': ['object', 'null'],
        'properties': {'secret': {'type': 'string'}}}}}}
    value = {'missing-leaf': {'profile': {}}, 'null-parent': {'profile': None},
             'missing-parent': {}}[case]
    source = make_cassette([s, {'type': 'RECORD', 'stream': 'alpha', 'record': value}])
    before = snapshot(source)
    dest = tmp_path / 'dest'
    result = cli(source, dest, 'alpha:profile.secret=mask')
    assert result.returncode == 2 and result.stdout == b''
    assert json.loads((dest / 'manifest.json').read_bytes())['complete'] is False
    with pytest.raises(ReplayError, match='incomplete'):
        with Cassette(dest):
            pass
    assert snapshot(source) == before


def test_optional_legacy_and_inherited_truncation(tmp_path, make_cassette, alpha):
    from datawrench.singer_replay.cassette import scrub
    alpha[0]['schema']['required'].remove('secret')
    alpha[3] = copy.deepcopy(alpha[0])
    alpha[6] = copy.deepcopy(alpha[0])
    del alpha[1]['record']['secret']
    source = make_cassette(alpha, limit=1)
    dest = tmp_path / 'dest'
    scrub(source, dest, rules=['alpha.secret=mask'])
    with Cassette(dest) as c:
        assert c.manifest['truncated'] is True
        assert c.manifest['message_count'] == 2
        assert 'secret' not in list(c.messages())[1]['record']


@pytest.mark.parametrize('cut', [0, 1, 2])
def test_keyed_resume_under_changed_anchor(tmp_path, make_cassette, alpha, cut):
    from datawrench.singer_replay.cassette import scrub
    from datawrench.singer_replay.cli import config_from
    from datawrench.singer_replay.replay import replay, InjectedFailure
    source = make_cassette(alpha)
    dest = tmp_path / 'dest'
    scrub(source, dest, rules=['alpha.secret=mask'])
    first, remaining = [], []
    with Cassette(dest) as c:
        with pytest.raises(InjectedFailure):
            replay(c, config_from({'cassette_path': str(dest), 'time_shift': '2030-01-01T00:00:00Z',
                   'fail_after_records': cut}), first.append)
        replay(c, config_from({'cassette_path': str(dest), 'time_shift': '2040-01-01T00:00:00Z'}),
               remaining.append, state=first[-1]['value'])
    records = [m['record'] for m in first + remaining if m['type'] == 'RECORD']
    assert [r['id'] for r in records] == [1, 2]
    assert all(r['secret'] == '***' for r in records)
    assert remaining[-1]['value']['bookmarks']['alpha']['replication_key_value'] == '2024-01-02T00:00:00Z'


@pytest.mark.parametrize('bound', ['line', 'total', 'manifest-initial', 'manifest-final', 'catalog'])
def test_output_bounds_leave_unreplayable_results(tmp_path, make_cassette, alpha, monkeypatch, bound):
    import datawrench.singer_replay.cassette as mod
    source = make_cassette(alpha)
    if bound.startswith('manifest-'):
        rewrite(source, scrub=[{'stream': 'prior', 'field': '雪' * 1000, 'mode': 'mask'}])
    reference = tmp_path / 'reference'
    mod.scrub(source, reference, rules=['alpha.secret=mask'])
    if bound in ('line', 'total'):
        output = gzip.decompress((reference / 'messages.jsonl.gz').read_bytes())
        size = max(map(len, output.splitlines(keepends=True))) if bound == 'line' else len(output)
        name = 'MAX_LINE' if bound == 'line' else 'MAX_MESSAGES_BYTES'
    else:
        if bound == 'catalog':
            # Make the catalog larger than both manifests, with mask expanding a numeric type.
            for m in alpha:
                if m['type'] == 'SCHEMA':
                    m['schema']['properties'].update({f'f{i}': {'type': 'integer'} for i in range(80)})
                    m['schema']['properties']['secret'] = {'type': 'integer'}
                elif m['type'] == 'RECORD':
                    m['record']['secret'] = 1
            source = make_cassette(alpha)
            reference = tmp_path / 'large-reference'
            mod.scrub(source, reference, rules=['alpha.secret=mask'])
            size = (reference / 'catalog.json').stat().st_size
        elif bound == 'manifest-final':
            size = (reference / 'manifest.json').stat().st_size
        else:
            m = json.loads((reference / 'manifest.json').read_bytes())
            for k in ('truncated', 'source_exit_code', 'counts', 'message_count', 'sha256'):
                del m[k]
            m['complete'] = False
            size = len((encode(m) + '\n').encode())
        name = 'MAX_JSON'
    before = snapshot(source)
    # Exactly the generated limit succeeds, then one byte below fails.
    monkeypatch.setattr(mod, name, size)
    exact = tmp_path / 'exact'
    if bound == 'manifest-initial':
        with pytest.raises(ReplayError, match='metadata exceeds'):
            mod.scrub(source, exact, rules=['alpha.secret=mask'])
        assert (exact / 'messages.jsonl.gz').exists()  # Initial manifest fit exactly.
    else:
        mod.scrub(source, exact, rules=['alpha.secret=mask'])
    monkeypatch.setattr(mod, name, size - 1)
    dest = tmp_path / 'dest'
    with pytest.raises(ReplayError, match='exceed'):
        mod.scrub(source, dest, rules=['alpha.secret=mask'])
    if bound == 'manifest-initial':
        assert list(dest.iterdir()) == []
    else:
        assert json.loads((dest / 'manifest.json').read_bytes())['complete'] is False
    monkeypatch.undo()
    with pytest.raises((ReplayError, OSError)):
        with Cassette(dest):
            pass
    assert snapshot(source) == before


def test_chained_scrub_preserves_inherited_descriptions_without_final_state(tmp_path, make_cassette, alpha):
    from datawrench.singer_replay.cassette import scrub
    # Includes two schemas and ends in a RECORD, deliberately lacking a final STATE.
    source = make_cassette(alpha[:2] + [alpha[0], alpha[4]], rules=scrub_rules(['alpha.old=mask']))
    first, second = tmp_path / 'first', tmp_path / 'second'
    scrub(source, first, rules=['alpha:/secret=mask'])
    scrub(first, second, rules=['alpha.old=drop'])
    with Cassette(first) as a, Cassette(second) as b:
        assert b.manifest['scrub'] == a.manifest['scrub'] + [{'stream': 'alpha', 'field': 'old', 'mode': 'drop'}]
        assert b.manifest['rescrub']['source_sha256'] == a.manifest['sha256']
        assert b.manifest['message_count'] == 4
        assert list(b.messages())[-1]['type'] == 'RECORD'
        assert [m['type'] for m in b.messages()] == ['SCHEMA', 'RECORD', 'SCHEMA', 'RECORD']
