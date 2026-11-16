"""Every rule has >=3 independent positive and >=2 negative source fixtures."""
import pytest

from datawrench.polars_vet import analyze

HEADER = 'import polars as pl\nfrom polars import col as c, read_csv as rc, concat as cat\ndf = pl.DataFrame({"a": [1, 2]})\nlf = pl.scan_csv("x.csv")\n'
CASES = {
    "PLV001": ([
        'pl.read_csv("x.csv").lazy()', 'rc("x.csv").lazy()', 'pl.read_parquet("x.parquet").lazy()',
    ], ['pl.scan_csv("x.csv")', 'pd.read_csv("x.csv").lazy()']),
    "PLV002": ([
        'c("a").map_elements(lambda x: x + 1)',
        'df["a"].map_elements(function=lambda x: 2 * x)',
        'pl.col("a").apply(lambda s: s.upper())',
    ], ['c("a").map_elements(lambda x: custom(x))', 'other.map_elements(lambda x: x + 1)']),
    "PLV003": ([
        'for row in df.iter_rows():\n    print(row)',
        '[r for r in df.rows()]', '{i: r for i in range(1) for r in df.to_dicts()}',
    ], ['df.rows()', 'for r in other.iter_rows():\n    pass']),
    "PLV004": ([
        'for i in range(3):\n    df = df.with_columns(pl.lit(i).alias("x"))',
        'while ready:\n    lf = lf.with_columns(c("a") + 1)',
        '[df.with_columns(c("a") + i) for i in range(2)]',
    ], ['df.with_columns(c("a") + 1)', 'for i in range(3):\n    other.with_columns(i)']),
    "PLV005": ([
        'for i in range(3):\n    lf.collect()', 'lf.collect().lazy()',
        '[lf.collect() for i in range(2)]',
    ], ['lf.collect()', 'for i in range(3):\n    other.collect()']),
    "PLV006": ([
        'lf.collect().select("a")', 'lf.collect().filter(c("a") > 1)',
        'out = pl.scan_parquet("x").filter(c("a") > 1).collect()\nout.select("a")',
    ], ['lf.select("a").collect()', 'pl.DataFrame({"a": [1]}).select("a")']),
    "PLV007": ([
        'pl.from_pandas(df.to_pandas())', 'p = df.to_pandas()\npl.from_pandas(p)',
        'pl.DataFrame(df.select("a").to_pandas())',
    ], ['pl.from_pandas(pd.DataFrame({"a": [1]}))', 'df.to_pandas()']),
    "PLV008": ([
        'df[df["a"] > 1]', 'df[(df["a"] > 1) & (df["a"] < 4)]',
        'd = pl.read_csv("x")\nd[d["a"] == 2]',
    ], ['df.filter(c("a") > 1)', 'pd[pd["a"] > 1]']),
    "PLV009": ([
        'df.sort("a").head(3)', 'lf.sort("a", descending=True).head(10)',
        'df["a"].sort().head(2)',
    ], ['df.top_k(3, by="a")', 'other.sort("a").head(3)']),
    "PLV010": ([
        'for i in range(3):\n    df = pl.concat([df, df])',
        'while ready:\n    cat([df, df])', '[pl.concat([df, df]) for i in range(3)]',
    ], ['pl.concat([df, df])', 'for i in range(3):\n    other.concat([df, df])']),
    "PLV011": ([
        'pl.read_csv("x").select("a")', 'rc("x").filter(c("a") > 1)',
        'pl.read_parquet("x").select("a")',
    ], ['pl.scan_csv("x").select("a")', 'pd.read_csv("x").select("a")']),
}


@pytest.mark.parametrize('code,source', [(code, s) for code, (good, _) in CASES.items() for s in good])
def test_positive(code, source):
    assert code in {d.code for d in analyze(HEADER + source)}


@pytest.mark.parametrize('code,source', [(code, s) for code, (_, bad) in CASES.items() for s in bad])
def test_negative(code, source):
    assert code not in {d.code for d in analyze(HEADER + source)}


@pytest.mark.parametrize('source', [
    'import pandas as pl\npl.read_csv("x").lazy()',
    'import polars as pl\npl = other\npl.read_csv("x").lazy()',
    'import polars as pl\ndef f(pl):\n    pl.read_csv("x").lazy()',
    'import polars as pl\ndef f():\n    pl.read_csv("x").lazy()\n    pl = other',
    'from polars import read_csv as rc\nrc = other\nrc("x").lazy()',
    HEADER + 'df = other\nfor r in df.rows():\n    pass',
    HEADER + 'if ready:\n    df = other\ndf.sort("a").head(1)',
    HEADER + '[df.sort("a").head(1) for df in things]',
    HEADER + 'with context() as df:\n    df.sort("a").head(1)',
    HEADER + 'def f():\n    global pl\n    pl.read_csv("x").lazy()',
    HEADER + 'try:\n    df = other\nexcept ValueError as pl:\n    pl.read_csv("x").lazy()\ndf.sort("a").head(1)',
])
def test_no_inference_for_shadowed_or_unknown_names(source):
    assert analyze(source) == []


def test_alias_inside_function_and_scope_restore():
    source = 'import polars as p\ndef f():\n    from polars import read_csv as read\n    d = read("a.csv")\n    return d.sort("a").head(1)\np.read_csv("a.csv").lazy()'
    assert [d.code for d in analyze(source)] == ['PLV009', 'PLV001']


def test_nested_function_does_not_inherit_loop():
    assert analyze(HEADER + 'for i in range(3):\n    def f():\n        return lf.collect()') == []


@pytest.mark.parametrize('comment,expected', [('# noqa: PLV001', []), ('# noqa', []), ('# NOQA: PLV002', ['PLV001'])])
def test_noqa(comment, expected):
    source = 'import polars as pl\npl.read_csv("a.csv").lazy() ' + comment
    assert [d.code for d in analyze(source)] == expected


def test_noqa_in_string_does_not_suppress():
    source = 'import polars as pl\nx = "# noqa"; pl.read_csv("a.csv").lazy()'
    assert len(analyze(source)) == 1


def test_unicode_column():
    source = 'import polars as pl\ntext = "你好"; pl.read_csv("a.csv").lazy()'
    diagnostic, = analyze(source)
    assert diagnostic.column == source.splitlines()[1].index('pl.') + 1


def test_syntax_error_once():
    diagnostic, = analyze('import polars as pl\npl.read_csv("x").lazy(\n')
    assert diagnostic.code == 'PLV000'


@pytest.mark.parametrize('source', [
    HEADER + 'df["a"].to_list().sort().head(3)',
    HEADER + 'df["a"].sum().sort().head(3)',
    HEADER + 'pl.read_csv = other\npl.read_csv("x").lazy()',
    HEADER + 'pl.scan_csv = other\npl.read_csv("x").lazy()',
    HEADER + 'del pl.scan_csv\npl.read_csv("x").lazy()',
    HEADER + 'match obj:\n    case {"frame": df}:\n        df.sort("a").head(2)',
    HEADER + 'match obj:\n    case {"frame": df}:\n        pass\ndf.sort("a").head(2)',
])
def test_scalar_method_and_module_mutation_boundaries(source):
    assert analyze(source) == []


def test_collect_in_while_condition_is_repeated():
    assert [d.code for d in analyze(HEADER + 'while lf.collect().height:\n    pass')] == ['PLV005']


def test_noqa_override_for_plugin():
    source = HEADER + 'pl.read_csv("x").lazy()  # noqa: PLV001'
    assert analyze(source) == []
    assert [d.code for d in analyze(source, honor_noqa=False)] == ['PLV001']


def test_pattern_binding_is_function_local():
    source = 'import polars as pl\ndef f(obj):\n    pl.read_csv("x").lazy()\n    match obj:\n        case {"alias": pl}:\n            pass'
    assert analyze(source) == []


@pytest.mark.parametrize('source', [
    HEADER + 'alias = pl\nalias.scan_csv = other\npl.read_csv("x").lazy()',
    HEADER + 'from unknown import *\npl.read_csv("x").lazy()',
])
def test_star_import_and_module_alias_mutations_invalidate(source):
    assert analyze(source) == []


@pytest.mark.parametrize('source', ['return 1', 'break', 'await something()'])
def test_contextual_syntax_failure_once(source):
    diagnostic, = analyze(source)
    assert diagnostic.code == 'PLV000'
