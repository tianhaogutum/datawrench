"""Opt-in physical group inventories, independent of aggregate/file validity."""
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.catalog import plan, scan
from datawrench.parquet_unify.cli import main
from datawrench.parquet_unify.data import OrderedRowSummary, normalize_table
from datawrench.parquet_unify.validation import validate_artifact
from .test_ordered_row_checksum_evidence import golden, table


def write(path, data, size=2, *, zero_groups=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if zero_groups:
        with pq.ParquetWriter(path, data.schema): pass
    else:
        pq.write_table(data, path, row_group_size=size)
    return path


def digest(data):
    summary = OrderedRowSummary(data.schema)
    summary.add(data)
    return summary.digest()


def groups(path, data, size=2, *, invalid=False):
    with pq.ParquetFile(path) as reader:
        counts = [reader.metadata.row_group(i).num_rows for i in range(reader.num_row_groups)]
    offset, records = 0, []
    for i, count in enumerate(counts):
        records.append({'file': str(path), 'row_group': i, 'rows': count,
                        'checksum': None if invalid else digest(data.slice(offset, count))})
        offset += count
    return records


def run(source, output, capsys, names=('id', 'note'), *extra, evidence=True):
    args = [arg for name in names for arg in ('--ordered-row-checksum-column', name)]
    code = main(['validate', str(source), '--output', str(output), *args, *extra,
                 *(['--row-group-checksum-evidence'] if evidence else [])])
    captured = capsys.readouterr()
    return code, captured


@pytest.mark.parametrize('case', ['matching', 'reordered', 'split', 'empty', 'zero_groups'])
@pytest.mark.parametrize('batch', [1, 3, 64])
def test_exact_group_contract(tmp_path, capsys, case, batch):
    data = table([1, 2, 3, 4], ['a', 'b', 'c', 'd'])
    actual = table([1, 2, 4, 3], ['a', 'b', 'd', 'c']) if case == 'reordered' else data
    if case in ['empty', 'zero_groups']: data = actual = data.slice(0, 0)
    source, output = tmp_path / 'input', tmp_path / 'output'
    # Path string ordering differs from Path component ordering.
    paths = [source / 'a.parquet', source / 'a/z.parquet', output / 'a.parquet', output / 'a/z.parquet']
    for p, t in zip(paths, [data, data, actual, actual]):
        write(p, t, 3 if case == 'split' and p.is_relative_to(output) else 2,
              zero_groups=case == 'zero_groups')
    for root in [source, output]: (root / '_metadata').write_text('ignored')
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
              for p in tmp_path.rglob('*') if p.is_file()}
    options = ('--batch-size', str(batch), '--checksum-column', 'id', '--null-column', 'note',
               '--row-checksum-column', 'id', '--row-checksum-column', 'note')
    code, captured = run(source, output, capsys, ['id', 'note', 'id'], *options, evidence=False)
    baseline = json.loads(captured.out)
    code2, captured = run(source, output, capsys, ['id', 'note', 'id'], *options)
    assert code == code2 == (1 if case == 'reordered' else 0) and not captured.err
    report = json.loads(captured.out); check = report['checks']['ordered_row_checksum']
    expected = [r for p in paths[:2] for r in groups(p, data)]
    actual_records = [r for p in paths[2:] for r in groups(p, actual)]
    assert check.pop('expected_by_row_group') == expected
    assert check.pop('actual_by_row_group') == actual_records
    assert report == baseline  # only the opt-in arrays change, including scope/reasons/other metrics
    assert check['expected'] == golden([[r['id'], r['note']] for r in data.to_pylist()] * 2)
    assert check['actual'] == golden([[r['id'], r['note']] for r in actual.to_pylist()] * 2)
    if case == 'reordered':
        assert expected[0]['checksum'] == actual_records[0]['checksum']
        assert expected[1]['checksum'] != actual_records[1]['checksum']
    assert before == {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in before}


@pytest.mark.parametrize('kind', ['missing', 'type', 'nested', 'order', 'duplicate'])
@pytest.mark.parametrize('empty', [False, True])
def test_invalid_groups_and_healthy_files_keep_exact_reasons(tmp_path, capsys, kind, empty):
    fields = [('x', pa.int64()), ('y', pa.string())]
    data = table([1, 2, 3], ['a', 'b', 'c']).append_column('p/~', pa.array([None] * 3, pa.list_(pa.struct(fields))))
    source = write(tmp_path / 'input', data)
    if kind == 'missing': bad = data.drop(['p/~'])
    elif kind == 'type': bad = data.set_column(2, 'p/~', pa.array(['bad'] * 3))
    else:
        changed = {'nested': [fields[0], ('z', pa.int64())], 'order': fields[::-1],
                   'duplicate': [fields[0], *fields]}[kind]
        bad = data.set_column(2, 'p/~', pa.array([None] * 3, pa.list_(pa.struct(changed))))
    if empty: bad = bad.slice(0, 0)
    output = tmp_path / 'output'
    bad_paths = [write(output / n, bad) for n in ['0', 'z']]
    good = write(output / 'a', data)
    zero = write(output / 'b', data.slice(0, 0))
    no_groups = write(output / 'c', bad.slice(0, 0), zero_groups=True)
    names = ['p/~', 'id']
    options = ('--null-column', 'p/~', '--checksum-column', 'id', '--row-checksum-column', 'id')
    old_code, old = run(source, output, capsys, names, *options, evidence=False)
    code, captured = run(source, output, capsys, names, *options)
    assert code == old_code == 1 and not captured.err
    report = json.loads(captured.out); check = report['checks']['ordered_row_checksum']
    selected = data.select(names)
    assert check.pop('expected_by_row_group') == groups(source, selected)
    records = [r for p in bad_paths for r in groups(p, selected, invalid=True)]
    records += groups(good, selected) + groups(zero, selected.slice(0, 0))
    assert check.pop('actual_by_row_group') == sorted(records, key=lambda r: (r['file'], r['row_group']))
    assert report == json.loads(old.out)
    assert check['actual'] is None
    assert {r['file'] for r in check['unavailable']} == set(map(str, [*bad_paths, no_groups]))
    assert report['checks']['checksums']['columns']['id']['actual'] is not None
    assert report['checks']['row_checksum']['actual'] is not None


@pytest.mark.parametrize('policy', ['widen', 'stringify-conflicts'])
@pytest.mark.parametrize('names', [['*', 'a.b', 'k', ''], ['k', '', 'a.b', '*']])
def test_planned_types_hive_missing_and_literal_tuple_order(tmp_path, capsys, policy, names):
    source, output = tmp_path / 'input', tmp_path / 'output'
    dtype = pa.int64() if policy == 'widen' else pa.string()
    a = pa.table({'a.b': pa.array([1, None, 2], pa.int32()), '*': ['a', 'b', 'c'], '': ['x', '', None]})
    b = pa.table({'a.b': pa.array([3] if policy == 'widen' else ['003'], dtype)})
    files = [write(source / 'k=001' / 'data', a), write(source / 'k=__HIVE_DEFAULT_PARTITION__' / 'data', b),
             write(source / 'no-key' / 'data', b)]
    schema = pa.schema([('a.b', dtype), ('*', pa.string()), ('', pa.string()), ('k', pa.string())])
    parts = [normalize_table(a.append_column('k', pa.array(['001'] * 3)), schema), normalize_table(b, schema), normalize_table(b, schema)]
    materialized = pa.concat_tables(parts)
    out = write(output / 'k=not-inferred' / 'data', materialized)
    code, captured = run(source, output, capsys, [*names, names[0]], '--policy', policy, '--hive', '--batch-size', '1')
    assert code == 0 and not captured.err
    report = json.loads(captured.out); check = report['checks']['ordered_row_checksum']
    assert report['scope']['ordered_row_checksum_columns'] == names
    assert check['expected_by_row_group'] == [r for p, t in zip(files, parts) for r in groups(p, t.select(names))]
    assert check['actual_by_row_group'] == groups(out, materialized.select(names))
    assert check['expected'] == check['actual'] == digest(materialized.select(names))


def test_single_decode_per_physical_group_and_no_extra_opens(tmp_path, capsys, monkeypatch):
    data = table([1, 2, 3, 4, 5], ['a', 'b', 'c', 'd', 'e'])
    source, output = tmp_path / 'input', tmp_path / 'output'
    inp = write(source / 'data', data)
    bad = write(output / '0', data.drop(['note']).slice(0, 0))
    out = write(output / 'a', data)
    (source / 'bad').write_text('unreadable'); (source / '_metadata').write_text('ignored')
    (output / '_common_metadata').write_text('ignored')
    opened, passes = [], []
    original_init, original_batches = pq.ParquetFile.__init__, pq.ParquetFile.iter_batches
    def init(reader, path, *args, **kwargs):
        reader.test_path = str(path); opened.append(str(path)); original_init(reader, path, *args, **kwargs)
    def batches(reader, *args, **kwargs):
        assert kwargs['batch_size'] == 1 and kwargs['use_threads'] is False
        assert len(kwargs['row_groups']) == 1
        passes.append((reader.test_path, kwargs['row_groups'][0]))
        for batch in original_batches(reader, *args, **kwargs):
            assert batch.num_rows <= 1
            yield batch
    def fail(*args, **kwargs): raise AssertionError('full-table/group read forbidden')
    monkeypatch.setattr(pq.ParquetFile, '__init__', init)
    monkeypatch.setattr(pq.ParquetFile, 'iter_batches', batches)
    for name in ['read', 'read_row_group', 'read_row_groups']: monkeypatch.setattr(pq.ParquetFile, name, fail)
    monkeypatch.setattr(pq, 'read_table', fail)
    code, captured = run(source, output, capsys, ['id', 'note'], '--skip-unreadable', '--checksum-column', 'id',
                         '--null-column', 'id', '--row-checksum-column', 'id', '--batch-size', '1')
    assert code == 1 and 'warning:' in captured.err
    # Footer once per readable/skipped candidate (4: input bad+data, output 0+a), then metrics once
    # per needed file (3). Identical to the pre-flag HEAD count, so the flag adds no opens.
    assert len(opened) == 7 and opened.count(str(bad)) == 2
    assert passes == [(str(inp), i) for i in range(3)] + [(str(bad), 0)] + [(str(out), i) for i in range(3)]
    check = json.loads(captured.out)['checks']['ordered_row_checksum']
    assert check['actual_by_row_group'][0] == {'file': str(bad), 'row_group': 0, 'rows': 0, 'checksum': None}
    assert all(r['checksum'] is not None for r in check['actual_by_row_group'][1:])


@pytest.mark.parametrize('case', ['no_ordered', 'unknown', 'unreadable', 'input_budget', 'output_budget', 'batch', 'max_rows'])
def test_guards_precede_decoding(tmp_path, capsys, monkeypatch, case):
    data = table([1, 2], ['a', 'b'])
    source, output = write(tmp_path / 'input', data), write(tmp_path / 'output', data)
    names, options = ['id'], []
    expected = {'no_ordered': 'requires --ordered-row-checksum-column', 'unknown': 'unknown top-level ordered row checksum columns',
                'unreadable': 'output unreadable', 'input_budget': 'input rows 2 exceed',
                'output_budget': 'output rows 2 exceed', 'batch': 'batch-size must be positive',
                'max_rows': 'max-rows must be nonnegative'}[case]
    if case == 'no_ordered': names, options = [], ['--row-checksum-column', 'id']
    elif case == 'unknown': names += ['ID']; output.write_text('invalid')
    elif case == 'unreadable': output.write_text('invalid')
    elif case in ['input_budget', 'output_budget']:
        options = ['--max-rows', '0']
        if case == 'output_budget': write(source, data.slice(0, 0))
    elif case == 'batch': options = ['--batch-size', '0']
    else: options = ['--max-rows', '-1']
    def fail(*args, **kwargs): raise AssertionError('must not decode')
    monkeypatch.setattr(pq.ParquetFile, 'iter_batches', fail)
    code, captured = run(source, output, capsys, names, *options)
    assert code == 2 and not captured.out and expected in captured.err


@pytest.mark.parametrize('kind', ['root_order', 'unselected'])
def test_unselected_defects_do_not_block_group_digest(tmp_path, capsys, kind):
    data = table([1, 2], ['a', 'b'])
    source = write(tmp_path / 'input', data)
    output = write(tmp_path / 'output', data.select(['note', 'id']) if kind == 'root_order' else data.drop(['note']))
    code, captured = run(source, output, capsys, ['id'])
    assert code == 1 and not captured.err
    check = json.loads(captured.out)['checks']['ordered_row_checksum']
    assert check['ok'] and check['unavailable'] == []
    assert check['actual_by_row_group'] == groups(output, data.select(['id']))


def test_library_catalog_needs_no_opt_in_footer_scan(tmp_path):
    data = table([1, 2, 3], ['a', 'b', 'c'])
    source, output = write(tmp_path / 'input', data), write(tmp_path / 'output', data)
    catalog = scan(source)
    assert catalog.entries[0].row_groups is None
    report = validate_artifact(catalog, plan(catalog), output, ordered_row_checksum_columns=['id'], row_group_checksum_evidence=True)
    assert report['checks']['ordered_row_checksum']['expected_by_row_group'] == groups(source, data.select(['id']))
