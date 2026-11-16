from pathlib import Path

import polars as pl
from polars.testing import assert_frame_equal
import pytest

from datawrench.polars_vet import analyze, fix_source


@pytest.mark.parametrize('reader,extension', [('read_csv', 'csv'), ('read_parquet', 'parquet'), ('read_ipc', 'ipc'), ('read_ndjson', 'ndjson')])
def test_fix_idempotent_and_equivalent(tmp_path, reader, extension):
    df = pl.read_csv(Path(__file__).parent / 'fixtures/people.csv')
    path = tmp_path / ('sample.' + extension)
    getattr(df, 'write_' + extension)(path)
    source = f'import polars as pl\nresult = pl.{reader}({str(path)!r}).lazy().select("id", "score").collect()\n'
    fixed = fix_source(source, analyze(source))
    assert f'pl.scan_{extension}' in fixed
    assert fix_source(fixed, analyze(fixed)) == fixed
    before, after = {}, {}
    # Execute only our own controlled synthetic fixture, never user input.
    exec(source, before)
    exec(fixed, after)
    assert_frame_equal(before['result'], after['result'])


@pytest.mark.parametrize('source', [
    'pl.read_csv(path).lazy()', 'pl.read_csv("*.csv").lazy()',
    'pl.read_csv("https://example.com/x.csv").lazy()',
    'pl.read_csv("x.csv", columns=["a"]).lazy()',
    'pl.read_csv("x.csv", use_pyarrow=True).lazy()',
    'pl.read_csv("x.csv", **options).lazy()',
    'pl.read_csv(handle).lazy()', 'pl.read_csv(b"data").lazy()',
    'pl.read_csv("x.csv").lazy(unexpected=True)',
    'pl.read_csv("x.csv", separator=sep).lazy()',
    '(pl.read_csv("x.csv")  # keep\n).lazy()',
    'pl.read_csv("x.csv").lazy( # keep\n)',
    'pl.read_csv("x.csv", storage_options={"token": "secret"}).lazy()',
    'pl.read_csv("~/x.csv").lazy()',
])
def test_unsafe_read_not_fixed(source):
    source = 'import polars as pl\n' + source
    diagnostic, = analyze(source)
    assert diagnostic.code == 'PLV001'
    assert not diagnostic.edits
    assert fix_source(source, [diagnostic]) == source


def test_alias_fix_needs_bound_scanner():
    source = 'from polars import read_csv as r\nq = r("x.csv").lazy()'
    assert not analyze(source)[0].edits
    source = 'from polars import scan_csv as s\n' + source
    assert 'q = s("x.csv")' in fix_source(source, analyze(source))


def test_fix_preserves_comments_unicode_and_crlf():
    source = 'import polars as p\r\nname = "é"; q = p.read_csv(\r\n    "x.csv",  # reader comment\r\n    separator=";",\r\n).lazy()  # trailing\r\n'
    fixed = fix_source(source, analyze(source))
    assert fixed == source.replace('p.read_csv', 'p.scan_csv').replace('.lazy()', '')


def test_shared_keywords_and_keyword_source(tmp_path):
    path = tmp_path / 'data.csv'
    path.write_text('a;b\n1;2\n3;4\n')
    source = f'import polars as p\nresult = p.read_csv(source={str(path)!r}, separator=";", n_rows=1).lazy().collect()'
    fixed = fix_source(source, analyze(source))
    before, after = {}, {}
    exec(source, before)
    exec(fixed, after)
    assert source != fixed
    assert_frame_equal(before['result'], after['result'])


@pytest.mark.parametrize('deletion', ['del scan', 'del (scan, other)', 'del [scan, other]'])
def test_deleted_scanner_is_not_used_for_fix(tmp_path, deletion):
    path = tmp_path / 'data.csv'
    path.write_text('x\n7\n')
    source = (
        'from polars import read_csv as read, scan_csv as scan\nother = None\n'
        f'{deletion}\nresult = read({str(path)!r}).lazy().collect()\n'
    )
    diagnostic, = analyze(source)
    assert diagnostic.code == 'PLV001'
    assert not diagnostic.edits
    fixed = fix_source(source, [diagnostic])
    assert fixed == source
    before, after = {}, {}
    exec(source, before)
    exec(fixed, after)
    assert_frame_equal(before['result'], after['result'])
    assert after['result'].to_dict(as_series=False) == {'x': [7]}


@pytest.mark.parametrize('survivor,replacement', [
    ('import polars as p', 'p.scan_csv'),
    ('from polars import scan_csv as live', 'live'),
])
def test_deleted_scanner_falls_back_to_live_binding(tmp_path, survivor, replacement):
    path = tmp_path / 'data.csv'
    path.write_text('x\n7\n')
    source = (
        'from polars import read_csv as read, scan_csv as gone\n'
        f'{survivor}\ndel gone\nresult = read({str(path)!r}).lazy().collect()\n'
    )
    fixed = fix_source(source, analyze(source))
    assert f'result = {replacement}(' in fixed
    before, after = {}, {}
    exec(source, before)
    exec(fixed, after)
    assert_frame_equal(before['result'], after['result'])
