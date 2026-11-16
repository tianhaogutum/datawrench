"""Opt-in physical footer layout; schema traversal and default bytes stay intact."""
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datawrench.parquet_unify.catalog import scan
from datawrench.parquet_unify.cli import main, render

NOTE = "Footer total_byte_size is layout evidence, not an RSS or safe-memory estimate."


def table(values, dtype=pa.int64()):
    return pa.table({"id": pa.array(values, dtype)})


def write(path, values, dtype=pa.int64()):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table(values, dtype), path, row_group_size=2)
    return path


def layout(path):
    with pq.ParquetFile(path) as reader:
        footer = reader.metadata
        return {"file": str(path.resolve()), "rows": footer.num_rows,
                "row_groups": [{"row_group": i, "rows": footer.row_group(i).num_rows,
                                "total_byte_size": footer.row_group(i).total_byte_size}
                               for i in range(footer.num_row_groups)]}


def snapshot(root):
    return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def invoke(path, capsys, *args):
    code = main(["scan", str(path), *args])
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, captured.out


def assert_inventory(report, expected):
    assert report["row_groups_by_file"] == sorted(expected, key=lambda r: r["file"])
    assert sum(r["rows"] for r in expected) == report["rows"]
    for record in report["row_groups_by_file"]:
        assert sum(g["rows"] for g in record["row_groups"]) == record["rows"]
        assert [g["row_group"] for g in record["row_groups"]] == list(range(len(record["row_groups"])))
        for group in record["row_groups"]:
            assert all(type(v) is int for v in group.values())


def test_multifile_nested_absolute_string_order_and_footer_only(tmp_path, capsys, monkeypatch):
    root = tmp_path / 'input'
    # Path ordering differs from absolute-string ordering: a/z before a.parquet.
    paths = [write(root / 'a/z.parquet', [1, 2, 3], pa.int32()),
             write(root / 'a.parquet', [4, 5, 6, 7, 8]), write(root / 'empty.parquet', [])]
    expected = [layout(p) for p in paths]
    before = snapshot(root)
    old = scan(root).report()
    assert old['groups'][0]['files'] == [str(paths[0])]
    opened, closed = [], []
    original_init, original_close = pq.ParquetFile.__init__, pq.ParquetFile.close

    def init(reader, path, *args, **kwargs):
        opened.append(str(path))
        original_init(reader, path, *args, **kwargs)

    def close(reader, *args, **kwargs):
        closed.append(reader)
        return original_close(reader, *args, **kwargs)

    def fail(*args, **kwargs):
        raise AssertionError('physical layout must use the existing footer only')

    monkeypatch.setattr(pq.ParquetFile, '__init__', init)
    monkeypatch.setattr(pq.ParquetFile, 'close', close)
    for method in ('read', 'read_row_group', 'read_row_groups', 'iter_batches'):
        monkeypatch.setattr(pq.ParquetFile, method, fail)
    monkeypatch.setattr(pq, 'read_table', fail)
    monkeypatch.setattr(pq, 'read_metadata', fail)
    monkeypatch.chdir(tmp_path)
    code, text = invoke(Path('input'), capsys, '--row-groups', '--format', 'json', '--exit-code')
    assert code == 1  # Opt-in layout must preserve schema drift status.
    report = json.loads(text)
    assert_inventory(report, expected)
    assert [r['file'] for r in report['row_groups_by_file']][:2] == [str(paths[1]), str(paths[0])]
    report.pop('row_groups_by_file')
    assert report == old  # Keep schema-group/column order.
    assert opened == [str(p) for p in sorted(paths)]
    assert len(closed) == len(paths)
    assert snapshot(root) == before


@pytest.mark.parametrize('case', ['multiple', 'single', 'empty_group', 'zero_groups', 'mixed_zero_groups'])
def test_real_footer_groups_including_empty_groups(tmp_path, capsys, case):
    path = tmp_path / 'file.parquet'
    if case == 'zero_groups':
        with pq.ParquetWriter(path, table([]).schema):
            pass
    elif case == 'mixed_zero_groups':
        with pq.ParquetWriter(path, table([]).schema) as writer:
            for values in ([], [1, 2], [], [3], []):
                writer.write_table(table(values))
    else:
        write(path, {'multiple': [1, 2, 3], 'single': [1], 'empty_group': []}[case])
    expected = layout(path)
    expected_rows = {'multiple': [2, 1], 'single': [1], 'empty_group': [0],
                     'zero_groups': [], 'mixed_zero_groups': [0, 2, 0, 1, 0]}[case]
    assert [g['rows'] for g in expected['row_groups']] == expected_rows
    code, text = invoke(path, capsys, '--row-groups', '--format', 'json', '--exit-code')
    assert code == 0
    assert_inventory(json.loads(text), [expected])


@pytest.mark.parametrize('fmt', ['json', 'text', 'md'])
def test_frozen_opt_in_rendering_and_default_bytes(tmp_path, capsys, fmt):
    path = write(tmp_path / 'odd|"\n北.parquet', [1, 2, 3])
    report = scan(path).report()
    baseline = render(report, fmt) + '\n'
    code, default = invoke(path, capsys, '--format', fmt)
    assert (code, default) == (0, baseline)
    expected = [layout(path)]
    code, actual = invoke(path, capsys, '--format', fmt, '--row-groups')
    assert code == 0
    if fmt == 'json':
        assert actual == json.dumps({**report, 'row_groups_by_file': expected}, indent=2, ensure_ascii=True) + '\n'
    else:
        inventory = json.dumps(expected, indent=2, ensure_ascii=True)
        if fmt == 'md':
            suffix = '\n\n### Physical row groups\n\n' + NOTE + '\n\n```json\n' + inventory + '\n```\n'
        else:
            suffix = '\n\nPhysical row groups\n' + NOTE + '\n' + inventory + '\n'
        assert actual == baseline.rstrip('\n') + suffix


@pytest.mark.parametrize('skip', [False, True])
def test_hive_skip_and_sidecar_scope(tmp_path, capsys, skip):
    root = tmp_path / 'input'
    paths = [write(root / 'k=001' / 'extensionless', [1, 2, 3]),
             write(root / 'k=__HIVE_DEFAULT_PARTITION__' / 'empty.parquet', [])]
    for sidecar in ('_metadata', '_common_metadata'):
        (root / sidecar).write_text('ignored even when not Parquet')
    bad = root / 'broken.parquet'
    bad.write_bytes(b'not parquet')
    # Valid physical file with invalid Hive key must also stay out of inventory.
    collision = root / 'id=bad' / 'collision.parquet'
    write(collision, [9])
    expected = [layout(p) for p in paths]
    before = snapshot(root)
    flags = ['--hive', '--format', 'json', '--exit-code'] + (['--skip-unreadable'] if skip else [])
    old_code, old_text = invoke(root, capsys, *flags)
    code, text = invoke(root, capsys, *flags, '--row-groups')
    report = json.loads(text)
    assert code == old_code == (0 if skip else 2)
    assert_inventory(report, expected)
    assert {r['file'] for r in report['unreadable']} == {str(bad), str(collision)}
    report.pop('row_groups_by_file')
    assert report == json.loads(old_text)
    assert [c['path'] for c in report['columns']] == ['/id', '/k']
    assert snapshot(root) == before


def test_only_unreadable_has_empty_inventory(tmp_path, capsys):
    (tmp_path / 'broken').write_bytes(b'bad')
    code, text = invoke(tmp_path, capsys, '--row-groups', '--format', 'json')
    assert code == 2
    report = json.loads(text)
    assert report['row_groups_by_file'] == [] and report['files'] == 0
    assert main(['scan', str(tmp_path), '--row-groups', '--skip-unreadable']) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'no readable' in captured.err


@pytest.mark.parametrize('case', ['missing', 'directory'])
def test_invalid_input_stays_exit_two_without_report(tmp_path, capsys, case):
    path = tmp_path / 'missing' if case == 'missing' else tmp_path
    assert main(['scan', str(path), '--row-groups']) == 2
    captured = capsys.readouterr()
    assert captured.out == '' and 'error:' in captured.err


def test_flag_is_scan_only(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(['plan', str(tmp_path), '--row-groups'])
    assert exc.value.code == 2
    assert 'unrecognized arguments: --row-groups' in capsys.readouterr().err
