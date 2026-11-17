# failurepack: upstream notes (not submitted)

No issue, PR, message, or patch was sent anywhere. These notes are reviewable
follow-up material for the existing tool this adapter extends.

## dfshrink 0.1.2: repeated oracle evaluations

The installed `_PredicateTracker` docstring says candidates are not repeated
and uses that as the reason not to memoize. This small public-API example
actually makes 65 predicate calls, 34 of them repeats, while retaining `[0, 31]`:

```python
from collections import Counter
import polars as pl
from dfshrink import shrink_rows

calls = []


def fails(frame):
    rows = tuple(frame["row"].to_list())
    calls.append(rows)
    return 0 in rows and 31 in rows


result = shrink_rows(pl.DataFrame({"row": range(32)}), fails)
print(result.predicate_calls)
print(sum(n - 1 for n in Counter(calls).values()))
```

The duplicate calls matter when a predicate reads files and runs a validator.
A narrowly scoped upstream improvement could correct the docstring and offer
an optional bounded cache. Keeping whole row-index tuples can consume large
amounts of memory; failurepack instead stores bounded SHA-256 keys and links
cache hits to their original repeated-validation evidence. Unexpected oracle
errors are not cached. A fresh final verification still bypasses the cache.

The existing public predicate seam already makes this possible externally;
failurepack does not fork or replace the upstream algorithm.

## Pandera adapter documentation: selected failures

The broad adapter's "any schema error" behavior is correct for its documented
contract. It can nevertheless retain a negative amount while deleting the
duplicate-ID anomaly a caller intended to report. This is a recipe/documentation
opportunity, not a claim that the adapter is broken.

`tests/failurepack/test_failure_identity.py` includes realistic compound-uniqueness and
MultiIndex examples. A selected-failure recipe should use schema context,
column/index-level identity, check index, reason code, and the relevant unique
column group. Observed bad values and row numbers should stay outside the rule
identity. [prior-art.md](prior-art.md) records the verified remaining file/bundle gap.

Pandera 0.36.0 also drops all-null columns when reshaping uniqueness failure
cases. A tuple unique on `(a, b)` can therefore report only `a` when the failing
`b` values are all null. `test_compound_uniqueness_keeps_nullable_column_context`
reproduces this. A structured unique-column tuple on `SchemaError` would avoid
parsing its existing tuple description with `ast.literal_eval`; the adapter
currently fails closed if that pinned-version description is unrecognized.
