# datawrench polars-vet rule guide

All snippets assume `import polars as pl` and locally bound Polars frames `df` or `lf`. A diagnostic is advice, not proof that a rewrite preserves every dtype or application side effect. Only PLV001 has automatic fixes.

## PLV001 — eager read then lazy

Bad:
```python
q = pl.read_csv("people.csv").lazy()
```
Good:
```python
q = pl.scan_csv("people.csv")
```

Also detects Parquet, IPC and NDJSON readers. A scanner enables projection/predicate pushdown. Literal local path fixes use a narrow shared argument whitelist; see [the command guide](../polars-vet.md#conservative-fixes). Dynamic paths and file objects are diagnosed without fixes. Lazy evaluation defers IO and errors; chunk layout can change. Imported aliases work, but fixing a reader-only import requires a bound scan alias or module alias.

## PLV002 — vectorizable element lambda

Bad:
```python
name = pl.col("name").map_elements(lambda s: s.upper())
a = pl.col("a").map_elements(lambda x: x + 1)
```
Good:
```python
name = pl.col("name").str.to_uppercase()
a = pl.col("a") + 1
```

Detects one-argument arithmetic against a numeric constant and zero-argument `upper`, `lower`, `strip` calls on that argument. Also recognizes the historical `apply` spelling and Series receivers. Native string strip is `.str.strip_chars()`. Complex lambdas are intentionally not flagged. Review dtypes, Python big-integer/overflow behavior, nulls, division errors, and Unicode differences. No lambda is executed or automatically replaced.

## PLV003 — row iteration

Bad:
```python
result = sum(row[0] for row in df.iter_rows())
```
Good:
```python
result = df.select(pl.col("a").sum()).item()
```

Detects direct `for`, async-for, and comprehension iteration over `iter_rows()`, `rows()`, or `to_dicts()` on a known eager frame. Merely creating rows without syntactic consumption is not flagged. Side-effecting loops may be intentional; null summation and selected column order must be reviewed. One unshadowed builtin `enumerate(df.iter_rows())` wrapper is also recognized, including optional positional or keyword `start`; the same applies to `rows()` and `to_dicts()`. The finding stays on the inner row call, with no automatic fix. Imported/reassigned/shadowed `enumerate`, starred arguments, arbitrary/nested wrappers and dynamic row-iterator provenance are not inferred. Simple saved row-iterable name assignments and pass-through aliases are tracked, with findings only on consumed names in loops/comprehensions (directly or in one supported builtin `enumerate`/`zip`/`iter`). One direct unshadowed builtin `zip` with exactly two or three explicit positional arguments and either no keywords or one literal-boolean `strict=True`/`strict=False` keyword reports each proven row name/call argument at its own span; no recursive wrapper inference or zip-result tracking. One direct unshadowed builtin `iter` with exactly one explicit positional argument and no keywords reports the proven inner eager row call or saved name. Two-argument callable/sentinel iteration, starred arguments, unsupported keywords or nonliteral zip `strict` values, shadowed/imported/aliased builtins, nested wrappers and saved wrapper results are not inferred. A single flat two-distinct-name tuple target paired with exactly two tuple RHS elements also retains each proven eager row-call/saved-name element independently, capturing proofs before binding either target (including swaps). Other unpacking shapes and non-row kinds are not inferred. Assignment alone stays silent; existing invalidation applies. See [the command guide](../polars-vet.md#saved-row-iterables-plv003) for the exact binding and suppression contract.

Direct `yield from rows` or `yield from df.rows()` delegation also consumes a proven saved row name or eager row call (`iter_rows`, `rows`, `to_dicts`), including flat tuple-origin names. One nonfixable PLV003 spans only the delegated value. Existing proof/invalidation and inner-start-line suppression apply. Plain `yield`, creation alone, wrappers (including `iter`/`enumerate`/`zip`), unknown/lazy/helper/dynamic sources and non-name/non-row-call values are not inferred at this consumer. Nested syntax is still visited normally; source and generators are never executed.

## PLV004 — repeated with_columns

Bad:
```python
for i in range(8):
    df = df.with_columns((pl.col("a") + i).alias(f"new{i}"))
```
Good:
```python
df = df.with_columns([(pl.col("a") + i).alias(f"new{i}") for i in range(8)])
```

Detects syntactically repeated eager/lazy `with_columns` calls. Batching only preserves behavior for independent expressions with distinct output names. A column depending on the previous iteration cannot simply be batched. Nested function bodies reset loop context; comprehensions create loop context.

## PLV005 — repeated/redundant collection

Bad:
```python
frames = []
for query in queries:
    frames.append(lf.filter(pl.col("a") == query).collect())
q = lf.collect().lazy()
```
Good:
```python
frames = pl.concat([lf.filter(pl.col("a") == query) for query in queries]).collect()
q = lf
```

Detects known lazy `collect()` inside loops (including while tests) and `collect().lazy()` chains. Batching can change error timing, memory usage and side effects; collect once only when compatible. A single collection outside a loop is valid. Unknown function parameters/iterable element types are not inferred.

## PLV006 — projection/predicate after scan collection

Bad:
```python
q = pl.scan_csv("people.csv").collect().filter(pl.col("score") > 10)
```
Good:
```python
q = pl.scan_csv("people.csv").filter(pl.col("score") > 10).collect()
```

Recognizes `select`/`filter` on tracked collected scan results, including simple intermediate names. Moving operations before collection enables pushdown; it can also skip parsing/errors in discarded columns, so the diagnostic has no fix. In-memory DataFrame selection is not flagged. Scan provenance is retained only through recognized single-frame methods, not arbitrary joins or helper functions.

## PLV007 — pandas round trip

Bad:
```python
result = pl.from_pandas(df.to_pandas())
```
Good:
```python
result = df.clone()
```

Recognizes `pl.from_pandas` and `pl.DataFrame` with a positional pandas value originating from a tracked `to_pandas()`. Intermediate plain assignments are followed. A one-way export or ordinary pandas-to-Polars conversion is valid. Actual pandas work, extension dtypes, object columns, indices, and null conversion need human review; this rule never removes that work.

## PLV008 — boolean frame indexing

Bad:
```python
result = df[df["a"] > 1]
```
Good:
```python
result = df.filter(pl.col("a") > 1)
```

Requires a tracked eager frame and a comparison/boolean expression referencing a string column subscript of the same frame. Polars does not support pandas-style row boolean masks through `__getitem__`; depending on shape/version this can raise or be interpreted as column selection. The benchmark checks rejection for a frame whose row/column counts differ. Ordinary column access, Polars filter, unknown/pandas frames are not flagged.

## PLV009 — full sort for small result

Bad:
```python
result = df.sort("a", descending=True).head(25)
```
Good (non-null unique key):
```python
result = df.top_k(25, by="a").sort("a", descending=True)
```

For ascending order consider `bottom_k`. Also recognizes lazy frames and Series/Expr sorting. [Polars documents](https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.top_k.html) that top-k results are unordered and prefer non-null values. Preserve explicit result ordering; review ties, stable ordering, null placement, multi-key direction and whether `k` is small enough to help. Never automatically fixes. Native lazy optimization may already improve some plans.

## PLV010 — concat in a loop

Bad:
```python
for part in parts:
    result = pl.concat([result, part])
```
Good:
```python
result = pl.concat([initial, *parts])
```

Detects calls bound to Polars `concat` in loops/comprehensions, including an imported alias. Repeated metadata/chunk accumulation can be costly. Preserve order and concat mode; collecting a generator may consume more memory. A concat whose result is used independently each iteration can be intentional. This is a syntactic loop warning, not a claim about computational complexity in every Polars release.

## PLV011 — eager read then selection

Bad:
```python
result = pl.read_parquet("people.parquet").select("name")
```
Good:
```python
result = pl.scan_parquet("people.parquet").select("name").collect()
```

Detects immediate `select`/`filter` on the same four reader families as PLV001. Projection and predicate pushdown can reduce decoding and memory. Reader/scan arguments and error timing can differ, so this rule has no fix. Nonadjacent read/selection assignments are not flagged by this rule. Optimizing malformed input can change which parsing errors surface; review that tradeoff.
