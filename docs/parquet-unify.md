# datawrench parquet-unify

`datawrench parquet-unify` (formerly `parquet-unify`) audits a local folder of
drifting Parquet files, plans a conservative common schema, and generates
**executable DuckDB SQL and Polars Python**. Verification runs both recipes
against an independent Arrow reference and reports what the engines' default
glob reads do. Optional rewriting creates a new, uniform folder.

Independent of Arrow, DuckDB and Polars; see [THIRD_PARTY.md](../THIRD_PARTY.md).

## Install and try

Python 3.11+. This command needs PyArrow, DuckDB and Polars:

```sh
pipx install 'datawrench[parquet,duckdb,polars]'   # or: pip install 'datawrench[parquet,duckdb,polars]'
datawrench parquet-unify --version                 # prints the bare datawrench version, e.g. 0.1.0
```

`python -m datawrench parquet-unify` is equivalent to `datawrench parquet-unify`.
The subcommands are `scan`, `plan`, `recipe`, `verify`, `rewrite` and `validate`.
PyArrow is imported when the command starts (including `--help`); DuckDB and
Polars are imported only by `verify` (and by running generated recipes).

From a datawrench checkout (the generator needs only PyArrow):

```sh
# Destination must not already exist. All sample data is synthetic.
uv run --extra parquet python examples/parquet_unify/make_drift_dataset.py /tmp/pu-drift
datawrench parquet-unify scan /tmp/pu-drift
datawrench parquet-unify plan /tmp/pu-drift --format json
datawrench parquet-unify plan /tmp/pu-drift --explain --format json
datawrench parquet-unify verify /tmp/pu-drift --policy widen
datawrench parquet-unify rewrite /tmp/pu-drift --out /tmp/pu-unified
datawrench parquet-unify validate /tmp/pu-drift --output /tmp/pu-unified --null-column note
```

The standalone project was tested with PyArrow 23.0.1, DuckDB 1.5.6 and Polars
1.44.2 (declared ranges: PyArrow >=21,<24, DuckDB >=1.4,<2, Polars >=1.35,<2).
All commands run offline; no database server, credentials, model downloads or
external workflows are needed.

Example scan output (abridged):

```text
2 files; 2 schema groups; 5 rows
/id: widenable: int32 / int64 -> int64
/amount: widenable: int32 / decimal128(12, 2) -> decimal128(12, 2)
/at: widenable: timestamp[ms] / timestamp[us] -> timestamp[us]
/profile/city: missing_in_some: string -> string
/note: missing_in_some: string -> string
```

`verify` emits JSON with `ok`, the reference's row count/schema/per-column
checksums, each engine's result or error, and each naive read's `equal`,
`changed`, or `error` status. For the supplied five-row fixture both generated
recipes pass; both naive readers fail on drift. The tests also cover a default
DuckDB read that silently drops an extra column, reported as `changed`.

## Commands and inputs

```text
datawrench parquet-unify scan PATH [--hive] [--row-groups] [--format text|json|md] [--exit-code]
datawrench parquet-unify plan PATH [--policy widen|strict|stringify-conflicts] [--explain] [--format text|json|md]
datawrench parquet-unify recipe PATH --engine duckdb|polars [--policy ...]
datawrench parquet-unify verify PATH [--policy ...] [--format text|json|md]
datawrench parquet-unify rewrite PATH --out NEW_DIR [--policy ...] [--format text|json|md]
datawrench parquet-unify validate PATH --output ARTIFACT [--policy ...] [--null-column NAME ...] [--checksum-column NAME ...] [--row-checksum-column NAME ...] [--ordered-row-checksum-column NAME ...] [--row-group-checksum-evidence] [--batch-size 65536] [--max-rows N] [--format text|json|md]
```

Every command accepts `--hive` and `--skip-unreadable`. PATH is one local file
or a directory searched recursively, not a URL or input glob. Every regular
file is attempted, regardless of extension, except standard `_metadata` and
`_common_metadata` sidecars. Corrupt/non-Parquet files, symbolic links and
special files are listed as unreadable. Without `--skip-unreadable` they block
planning and execution; `scan` still prints its complete report. Skipping is
explicit and reported, never silent. Empty input, or skipping every file, fails.

`scan` reads footers only. It groups Arrow schemas by SHA-256 fingerprint and
classifies each column and nested path as `identical`, `missing_in_some`,
`widenable`, or `conflict`; the separate `missing_in_some` flag can coexist with
a widening/conflict. JSON includes each path's file/type mapping, reasons,
row counts and group schemas. Paths use JSON-pointer escaping (`~0`, `~1`),
with `/[]` for each list element level. Metadata and nullability are ignored
in fingerprints; field order, types and timezone labels are retained.

`scan --row-groups` optionally adds `row_groups_by_file` to JSON. Each record is
`{file, rows, row_groups: [{row_group, rows, total_byte_size}]}`. Files sort by
**absolute path string**, independently of the existing schema-group traversal.
`row_group` is the zero-based physical footer index; groups retain footer order,
and their row counts sum to the file's footer total. A valid empty file can have
zero groups (`[]`) or actual zero-row groups; the report preserves that layout.
Unreadable/skipped files and standard metadata sidecars have no layout records.
Hive partition columns do not create physical groups.

For example, write `a.parquet` with `id:int64=[1,2,3]`, `row_group_size=2`, and
an empty `b.parquet` with the same schema. Then run:

```sh
datawrench parquet-unify scan INPUT --row-groups --format json
```

The new inventory lists `a.parquet` with `rows: 3` and physical groups 0/1 with
rows 2/1; `b.parquet` has `rows: 0` and its writer's actual empty layout. Byte
sizes are copied exactly from each group's footer `total_byte_size` (uncompressed
column data sizes), without decoding data pages or reopening files. They are
layout evidence, **not an RSS or safe-memory estimate**. Inventory/report size
grows with the number of files and groups; this option imposes no resource cap.
Text appends a `Physical row groups` section with the same indented inventory
JSON and byte-size caveat; Markdown appends that section as fenced JSON.
Without the flag, scan output bytes and exit codes are unchanged. The flag is
available only on `scan`; it does not alter schema/column/row/drift results.

Every column entry also includes `path_parts`: literal field-name strings,
with JSON `null` for a list element. Use these typed components as the unique
field address. For example, a list element at `/payload/[]` has
`["payload", null]`, while a literal struct field named `[]` has
`["payload", "[]"]`. They remain separate entries even when files disagree
about the parent's shape; text/Markdown scan output adds `parts=...` when
display paths collide. `/`, `~`, dots, wildcards and regex-shaped field names
are always literal names.

### Explain a plan, including rejected plans

`plan --explain` adds `ok`, `error`, all per-field `columns`, and
`order_mismatches` to the usual policy/schema output. Each column lists its
original type per absolute source file (`files`, with JSON `null` for a missing
field), its lossless common type (`unified`), or a rejection `reason`. It includes
nested struct fields and list elements. Entries sort by escaped path, then typed components to break
ties; file mappings follow sorted input paths. Output schema order still
follows the first occurrence of each field.

For example, given these two synthetic input schemas:

```text
a.parquet: profile: struct<amount: decimal128(9, 1)>, risk: int64
b.parquet: profile: struct<amount: decimal128(9, 5), city: string>, risk: double
```

`datawrench parquet-unify plan DIR --explain --format json` reports:

```text
/profile/amount: a.parquet=decimal128(9, 1), b.parquet=decimal128(9, 5)
                 unified=decimal128(13, 5)
/profile/city:   a.parquet=null (missing), b.parquet=string; unified=string
/risk:          a.parquet=int64, b.parquet=double
                 unified=null; reason="no lossless common type: int64, double"
```

This is an abridged view of the JSON fields. The complete explanation remains
on stdout even on failure, with `ok: false`, `schema: null`, an `error`, exit
code **1**, and a conflict summary on stderr. All fields are explained, not
just the first conflict. No partial schema is executable. Invalid/unreadable
input still uses exit 2 and the existing `--skip-unreadable` rules.

`columns` always describes **lossless widening**, independently of the selected
policy. `schema` describes the actual policy result: `strict` can reject a
lossless bound, and explicitly choosing `stringify-conflicts` can produce a
string for an otherwise conflicting supported leaf. An int64/double conflict
still fails under either `widen` or `stringify-conflicts`. Text output is JSON;
Markdown wraps that JSON in a code fence. Without `--explain`, plan output and
failure behavior are unchanged. Explanations read footers only.

`order_mismatches` explains field order separately from field types. For example,
`a.parquet: id:int64, note:string` and `b.parquet: note:string, id:int64` have
identical per-field types, but `--policy strict --explain --format json` exits
1 with `ok: false`, `schema: null`, and this record (abridged absolute file path):

```json
{
  "file": "/example/b.parquet",
  "path": "",
  "path_parts": [],
  "reason": "field_order",
  "source_order": ["note", "id"],
  "target_order": ["id", "note"]
}
```

Each record identifies the **containing schema or struct**, not an individual
child. The root schema uses `path: ""`, `path_parts: []`; a struct at `profile`
uses `/profile`, `["profile"]`; a struct inside its list uses `/profile/[]`,
`["profile", null]`. Nested lists and literal names use the same typed address
rules as `columns`. Records sort by escaped path, typed components, then source
file. Each disagreeing file gets its own record.

`target_order` is the union of immediate field names in first-occurrence order
across sorted source files at that container. `source_order` retains the full
original order. Comparison removes target names absent from that source; an
order-preserving subset produces no record. Thus missing fields or changed
types alone do not imply an order mismatch. Missing/null/non-struct parents
contribute no child order. Records remain available even when type conflicts
prevent a target schema; these candidate orders are explanatory, not executable.
The same records appear under `widen` and `stringify-conflicts`, which can accept
order drift. If no order differs, the array is empty. Strict acceptance rules
and the existing per-field type classifications are unchanged.

`--hive` reads directory components `key=value`, URL-decodes both sides and
adds nullable **string** columns. Leading zeros remain intact, dates remain
strings, and `__HIVE_DEFAULT_PARTITION__` means null. Missing partition keys
become null. Duplicate/empty keys and collisions with physical columns fail.
A single-file input has no inferred partition root. Without `--hive`, directory
names do not contribute columns. Neither recipe uses engine Hive inference.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Successful command; `scan` without `--exit-code` is informational |
| 1 | Policy/type conflict, failed cross-engine or artifact validation, or drift with `scan --exit-code` |
| 2 | Invalid/empty input, unreadable files unless explicitly skipped, unsafe output, or I/O failure |

## Reconciliation rules

The default `widen` policy computes a schema-level bound; it does not sample
values to justify a conversion. Missing fields become typed nulls. The first
occurrence determines output column/struct field order (files are sorted).
All output fields are nullable; writer-specific metadata is not preserved.

| Inputs | Common type / behavior |
|---|---|
| Signed/unsigned integers | Smallest integer containing both ranges; int64 + uint64 becomes decimal(20,0) |
| float32 + float64 | float64 |
| Integers up to 32 bits + float | float64; int64/uint64 + float is a conflict |
| Integer + decimal | Decimal with enough integral digits for the integer's full range |
| Decimal + decimal | Maximum scale + maximum integral digits; precision must be at most 38 |
| Float + decimal | Conflict: no schema-only guarantee of exact representation |
| Date + naive timestamp | Timestamp, finest input unit |
| Timestamps | Finest unit; seconds normalize to milliseconds; aware timezones normalize to UTC |
| Naive + aware timestamp, date + aware timestamp | Conflict |
| string / large_string; binary / large_binary | Equivalent logical string / binary |
| Structs / lists | Recursive field union / element widening; preserve parent nulls and null elements |
| Arrow null | Adopt the other type, or remain null if all inputs are null |

`strict` requires one schema fingerprint, including nested fields and column
order, before canonical carrier normalization. It rejects missing columns and
widening, naming the source files and affected paths.

`stringify-conflicts` first applies the same lossless rules, then converts only
conflicting **string/integer/decimal/boolean** leaves to strings. Existing
strings are unchanged (e.g. `"001"` stays `"001"`). Float, temporal, binary
and complex-shape conflicts still fail: their generic string formatting is
not portable between engines. In particular a double/decimal pair is an
explicit conflict; it is not silently rounded to double. The default example
therefore uses integer/decimal widening and a separate float32/float64 column.
Generate explicit failure fixtures with `--scenario float-decimal`, `timezone`
or `conflict`; use `--scenario hive` for partition examples.

## Runnable recipes and verification

```sh
datawrench parquet-unify recipe /tmp/pu-drift --engine duckdb > /tmp/pu-read.sql
datawrench parquet-unify recipe /tmp/pu-drift --engine polars > /tmp/pu-read.py
python /tmp/pu-read.py   # needs polars
```

SQL can be executed with DuckDB's Python package (no DuckDB CLI required):

```python
from pathlib import Path
import duckdb
with duckdb.connect(config={"threads": 2}) as db:
    table = db.execute(Path("/tmp/pu-read.sql").read_text()).to_arrow_table()  # DuckDB 1.4.x: fetch_arrow_table()
    print(table)
```

Each schema group gets explicit projections, casts and typed nulls before
`UNION ALL BY NAME` / vertical concatenation. Hive groups are additionally split
by partition constants. Structs are rebuilt recursively with parent-null masks;
lists transform their elements. Paths, names and values are escaped literals,
not executable user SQL/Python. Recipes contain absolute snapshot paths; if
files change, regenerate and verify them. They require only the corresponding
engine package, not datawrench itself.

The Python recipe exposes `result`, a LazyFrame. Collect it with
`result.collect(engine="streaming")`, as the script does. The pinned Polars'
automatic/in-memory path can panic on rebuilt nullable structs; the streaming
path is used and tested. DuckDB SQL sets two threads and UTC. Polars scripts
set `POLARS_MAX_THREADS=2` before import and reject an already larger pool.

`verify` runs three independent paths:

1. Arrow reads and safely reconciles each input row group to build a reference.
2. DuckDB executes the generated SQL and reads result batches.
3. Polars executes the generated Python and collects using its streaming engine.

Both engine results must have the planned logical schema, reference row count
and all reference per-column checksums. The report also includes their actual
Arrow schemas. Normalization accepts string/list offset-width differences,
ignores metadata/nullability/list-element names, and allows DuckDB's integer
carrier for all-null columns and UTC-microsecond carrier for aware timestamps.
Safe casts and checksums still enforce the values. Naive reads are separate
recursive `*.parquet` glob reads with engine defaults, without union/cast knobs;
a naive failure does not make an otherwise correct recipe fail verification.
Explicitly skipped files are excluded from recipes/reference, but may still
appear in the naive directory glob.

Checksums hash each typed, canonical scalar with SHA-256, combine both its sum
and squared sum modulo 2^256, then hash those aggregates with the row count.
They ignore row ordering while retaining duplicates, nulls, nested values and
nanosecond timestamp integers. NaN payloads and signed floating zero are
normalized to numerical equality. These are per-column multiset comparisons,
not a proof of row association or a cryptographic file-integrity signature.

Canonical dates retain ISO strings for years 1–9999 (date32 epoch days
`-719162` through `2932896`). Outside that range, their scalar JSON is
`{"date32_days":N}`, where `N` is the exact signed day count since 1970-01-01.
This supports the full int32 date32 domain without Python date conversion,
including dates inside lists/structs, and preserves existing in-range digests.
Null dates still serialize as JSON `null`. This encoding is shared by
`verify`, `rewrite` summaries and selected `validate` checksums; it does not
expand either engine's supported conversion range.

Verification caps Arrow and engine pools at two threads and disables DuckDB
extension autoinstall/autoload. It reports each engine's failure text separately
and still attempts the other engine. It never executes supplied SQL or Python;
only code generated from the supported schema and escaped local paths runs.

## Validate an existing artifact

`validate` checks an existing local Parquet file or recursive directory against
a freshly computed input plan, without running either engine or rewriting files.
It compares **each output file's schema**, the combined footer row count, and
null counts and optional typed value checksums for selected top-level columns. An output union cannot
hide individual files with missing fields or wrong types. Reports include the
resolved input/output file lists, expected/actual counts and schemas, and separate
`checks.schema`, `checks.rows`, and `checks.null_counts` results, plus
`checks.checksums`, `checks.row_checksum` and/or `checks.ordered_row_checksum` when requested. Schema issues
identify the file, escaped path, typed `path_parts`, expected/actual type and
reason. Missing output null-count columns are reported as unknown (`null`),
never filled with synthetic nulls.

```sh
# Save the independent report outside both scanned trees, including on exit 1.
datawrench parquet-unify validate /tmp/pu-drift --output /tmp/pu-unified \
  --null-column note --null-column profile --batch-size 4096 --max-rows 1000000 \
  > /tmp/pu-validation.json
```

For the supplied rewritten fixture this passes with 5 rows, `note: 4` nulls
(including the missing input column) and `profile: 1` null parent. A smaller
explicit example is `a.parquet: id:int32=[1,2], note=["kept",null]` and
`b.parquet: id:int64=[3]`: the expected artifact has `id:int64=[1,2,3]`,
`note:string=["kept",null,null]`. Deleting the last row reports rows 2 versus 3;
writing `id` as int32 reports a schema issue at `/id`; filling one `note` null
reports nulls 1 versus 2. Each valid-but-mismatched artifact emits a complete
report with `ok: false` and exits **1**. Correct artifacts exit **0**.
Invalid paths, unreadable artifact files, unknown selected column names and
exceeded resource limits exit **2** with an error on stderr.

`checks.rows` always includes `expected_by_file` and `actual_by_file` arrays
alongside `ok`, `expected` and `actual`. Each record is `{file, rows}` from the
already scanned Parquet footer, sorted by absolute file-path string. For input
`/input/a.parquet: id:int64=[1,2]`, `/input/b.parquet: id:int64=[3]` and artifact
`/artifact/a.parquet: id:int64=[1,2]`, `/artifact/z.parquet: id:int64=[]`:

```json
{
  "ok": false, "expected": 3, "actual": 2,
  "expected_by_file": [
    {"file": "/input/a.parquet", "rows": 2},
    {"file": "/input/b.parquet", "rows": 1}
  ],
  "actual_by_file": [
    {"file": "/artifact/a.parquet", "rows": 2},
    {"file": "/artifact/z.parquet", "rows": 0}
  ]
}
```

Each array sums to its side's aggregate and includes readable empty files;
all-empty datasets can pass with totals of zero. The arrays describe independent
file contributions, without pairing files or attributing individual missing values.
They use exactly the readable file scope in `input.files`/`output.files`, retaining
those lists' existing catalog traversal order while sorting the new arrays by path
string. Skipped unreadable input files and standard sidecars do not contribute;
input warnings/unreadable records and output unreadable errors remain unchanged.
Hive input rows count once per physical file. Single-file and relative roots
produce absolute file paths too. These are additive report keys, including with
selected metrics; consumers comparing whole row-check objects must allow them.
The evidence adds no file opens or decoding; its size grows with file count.

`--null-column` is repeatable and uses literal, case-sensitive names, including
dots, slashes, `*` and regex-shaped text; repeated names are deduplicated. Struct
and list columns count null **parents** only, not null children/elements or empty
lists. Missing input fields and null/missing Hive keys contribute planned nulls.
Each `checks.null_counts.columns.NAME` contains
`{ok, expected, actual, unavailable, expected_by_file, actual_by_file}`.
The two per-file arrays contain `{file, nulls}` records sorted independently by
absolute file-path string, without file pairing. For input `a.parquet` with
`id:int64=[1,2], note:string=["kept",null]` and `b.parquet` with `id:int64=[3]`,
and artifact `a.parquet` with `note:string=["kept","filled"]`, `b.parquet` with
`note:string=[null]` (same ids), the selected `note` check is:

```json
{
  "ok": false, "expected": 2, "actual": 1, "unavailable": [],
  "expected_by_file": [
    {"file": "/input/a.parquet", "nulls": 1},
    {"file": "/input/b.parquet", "nulls": 1}
  ],
  "actual_by_file": [
    {"file": "/artifact/a.parquet", "nulls": 0},
    {"file": "/artifact/b.parquet", "nulls": 1}
  ]
}
```

Run `validate INPUT --output ARTIFACT --null-column note`; this still exits **1**.
Numeric contributions sum to the unchanged aggregate when available. Every
readable scoped file contributes, including empty files: a present empty field
has `nulls: 0`, while a missing output field has `nulls: null` even if empty.
Healthy files keep their own numeric counts when another file makes the aggregate
unknown. Missing input fields and null/missing Hive keys contribute the file's
row count; Hive string constants contribute zero. Skipped inputs and sidecars
are excluded. Single-file and relative roots still produce absolute paths.
These additive keys reuse the existing selected-metric pass without extra file
opens or decoding. Evidence grows with selected columns and readable files;
report bytes and sorting memory remain unbounded. Strict whole-object consumers
must allow the new keys; reports without null selections remain unchanged.

An artifact field missing in any file, including an empty file, keeps its actual
count `null` and fails the check. Its `unavailable` list contains exact top-level
`missing_field` schema issues plus the literal `column` name:
`{column, file, path, path_parts, reason, expected, actual}`. For example,
`id:int64=[1], profile:struct<x:int64>=[null]` with one correct artifact file and
an empty file missing `profile` reports `profile` unavailable at `["profile"]`,
while selected `id` still has count 0 and `unavailable: []` (overall exit 1).
Lists deduplicate identical records and sort by absolute file, escaped path,
ASCII JSON typed `path_parts`, then reason; selected columns retain first-request
order. Available counts have `[]`, even when their values differ. Nested missing,
type, duplicate or order defects, top-level type changes, root order and
unselected defects remain schema failures but do not prevent measuring a
present column's parent validity. Column/row checksum eligibility and reasons
remain independent; nested defects can block a digest while its parent null
count remains measurable. Without null selections, the null-count column map
stays empty and no null-count reason records are added.
`--hive` applies only to the input; the artifact must contain materialized
partition columns. `--skip-unreadable` can explicitly exclude input files, with
a warning and report entries; artifact files are never skipped. Standard
`_metadata`/`_common_metadata` sidecars are ignored on both sides. No URLs, input
globs, engine inference or undeclared directory siblings are read.

Schema comparison ignores metadata, nullability, list-element names and
string/binary/list offset widths. Names, field order, numeric types, nested
structure, timestamp units and timezone labels must match the plan. Engine
carriers allowed by `verify` (such as INTEGER for Arrow null) receive no special
exemption here. Every supported policy, including explicit stringification,
uses its planned output schema.

Without selected null or value-checksum columns validation is footer-only. With any selection it decodes
one file at a time using Arrow `iter_batches`, with threading disabled and a
positive row batch size (default 65,536). All physical columns are decoded to
avoid prefix-projection ambiguity for literal dotted names. `--max-rows` is an
optional nonnegative limit on **each side's total footer rows**, checked before
any column decoding. It is not a byte, memory or runtime cap: wide/nested values,
Parquet decoder buffers, directory enumeration and report size remain unbounded.
Checksum normalization and scalar serialization need additional transient buffers.
No final table is materialized and no performance claim is made.

If either side exceeds `--max-rows`, validation exits **2** with empty stdout.
For input `a.parquet: id:int64=[1,2]`, `b.parquet: id:int64=[3]`, a correct
three-row artifact and `--ordered-row-checksum-column id --max-rows 2`, stderr is
(using illustrative absolute paths):

```text
error: input rows 3 exceed max-rows 2; no column data decoded; rows_by_file=[{"file":"/input/a.parquet","rows":2},{"file":"/input/b.parquet","rows":1}]
```

The suffix is compact JSON with ASCII-escaped strings, `file` then `rows` keys,
and records sorted by absolute file-path string. It includes every readable
zero-row file; its rows sum to the refusing side's total. Output overruns list
only output contributions; if both sides exceed the limit, input wins. Existing
unknown-column and unreadable-output errors take precedence. Skipped unreadable
input files and standard sidecars are excluded; the existing skip warning still
precedes the error. This evidence reuses scanned footers without extra file opens
or decoding, applies with or without selected metrics and in every format, and
grows with file count. Successful and mismatched reports are unchanged.

Add repeatable `--checksum-column NAME` to compare values without running either
engine. Names are literal, case-sensitive top-level names, with duplicates
removed in first-request order, just like `--null-column`. For example:

```sh
datawrench parquet-unify validate /tmp/pu-drift --output /tmp/pu-unified \
  --checksum-column id --checksum-column note --null-column note --batch-size 4096
```

For the three-row `id`/`note` example above, both selected checksums match.
Changing `id` from `[1,2,3]` to `[1,2,4]` preserves schema, rows and null counts,
but exits **1** with `checks.checksums.ok: false` and
`checks.checksums.columns.id.ok: false`; `note` still matches. Selecting only
`note` on that changed artifact passes, since `id` values were not selected.
Unknown names, including a differently cased name or a nested field address,
exit **2** before decoding any data.

The report adds `expected.checksums` and `actual.checksums`, mapping names to
64-character SHA-256 hex digests, and `checks.checksums.columns`, mapping names
to `{ok, expected, actual, unavailable, expected_by_file, actual_by_file}`.
`scope.values_checked: true` means **only** the
listed `scope.checksum_columns`, `scope.row_checksum_columns` and/or
`scope.ordered_row_checksum_columns` were checked. Without any checksum selection,
these checksum sections are absent and `values_checked` stays `false`.
A selected artifact column missing or schema-incompatible in **any** file has
an unknown (`null`) actual checksum and fails its check, even if all its expected
values are null or the incompatible file is empty. Schema diagnostics remain
available; checksums never cast away artifact schema errors.

Each selected column's `expected_by_file` and `actual_by_file` arrays contain
`{file, checksum}` records, independently sorted by absolute path **string**.
`file` is the absolute readable Parquet path; `checksum` is that file's
64-character digest using the same selected planned type and canonical
`Summary` encoding as the aggregate. For input `a.parquet id:int64=[1,2]`,
`b.parquet id:int64=[3]` and artifact `a.parquet id:int64=[1,2]`,
`b.parquet id:int64=[4]`, `--checksum-column id` still exits **1** with the
same aggregate digests. The two `a` file digests match; the `b` digests differ.
These are independent file inventories: validation does not pair paths, add
digests or require matching file layouts. Only aggregate digests decide the
checksum check, so repartitioning the same values can still pass.

A valid empty file has the existing zero-row `Summary` digest. A missing or
incompatible selected output field has `checksum: null`, even in an empty
file; the aggregate stays `null` with its existing `unavailable` reasons.
Healthy files retain their own digests, including files after an invalid file.
Input missing fields contribute planned typed nulls and Hive keys contribute
strings/nulls; output Hive directories are never inferred. Skipped unreadable
inputs and ignored sidecars have no records. No `--checksum-column` selection
adds no per-column checksum section or evidence work.

File digests accumulate during the shared bounded metric pass, with no second
file open or decoding pass. A healthy file still needs that pass when an earlier
invalid file has already made its aggregate unavailable. File summaries reuse
the canonical encoder; aggregate and file summaries each encode their values.
Evidence size grows with selected columns times readable files and requires
sorting; `--batch-size` and `--max-rows` do not cap report size, bytes or RSS.
The extra keys are additive; consumers that reject extra keys must allow them.

Each selected `checks.checksums.columns.NAME.unavailable` lists the exact
schema issues preventing that column's digest, using
`{column, file, path, path_parts, reason, expected, actual}` records. `column`
is the literal selected name; all remaining fields copy `checks.schema.issues`.
The list is empty for an available digest, including a value mismatch. For
example, input `id:int64=[1], profile:struct<x:int64>=[null]` and an artifact
directory containing one correct file plus an empty file missing `profile`
exits **1** with `actual.checksums.profile: null` and a `missing_field` record
at `path_parts: ["profile"]`. Selecting `id` too leaves its digest available
with an empty list. An empty file with `profile:struct<y:int64>` instead reports
missing `profile/x` and unexpected `profile/y`.

Reasons include nested missing/unexpected/type/order/duplicate defects from
every artifact file. Identical full records are deduplicated and sorted by
absolute file, escaped path, ASCII JSON `path_parts`, then reason within each
column. Root field order and unselected schema defects remain independent
schema failures and contribute no reasons for a healthy selected column.
Per-column and row-tuple selections are independent; both reuse the same
footer evidence without extra schema comparison or decode passes. Reason
lists and their sorting grow with reported defects, without a report byte cap.

Input batches are safely normalized to the selected **planned types**, including
widening, explicit stringification, recursive struct/list unions, typed nulls for
missing fields, and materialized Hive strings/nulls. Output batches use those
same types after schema checks. Checksums reuse the `verify` canonical scalar
algorithm described above: nulls, duplicates, nested child/element values and
nanosecond timestamps contribute; NaNs and signed floating zero normalize.
Null counts and checksums share one batch pass per file when both are selected.
Digests ignore batch boundaries, file layout and row ordering. They compare
**per-column multisets** and cannot prove cross-column row association,
uniqueness or cryptographic file integrity. Swapping `note` values between two
rows can pass even when both `id` and `note` are selected.

To check association among selected columns, add repeatable
`--row-checksum-column NAME`. It selects one **ordered tuple**, using literal,
case-sensitive top-level names in first-request order and ignoring repeats.
This selection is independent of `--checksum-column` and `--null-column`.
Unknown names (including nested field addresses) exit **2** before decoding.

```sh
datawrench parquet-unify validate /tmp/row-input.parquet --output /tmp/row-output.parquet \
  --row-checksum-column id --row-checksum-column note \
  --checksum-column id --checksum-column note --batch-size 4096
```

For input `id:int64=[1,2], note:string=["a","b"]`, an identical artifact or
whole-row reorder to `id=[2,1], note=["b","a"]` exits **0**. An artifact with
`id=[1,2], note=["b","a"]` exits **1**: both per-column checks still pass, but
`checks.row_checksum.ok` is false. Selecting only `id` leaves `note` values
unchecked. Whole-row ordering, repartitioning, row-group and batch boundaries
do not affect the digest; changing duplicate tuple multiplicity does.

When requested, the report adds 64-character hex `expected.row_checksum` and
`actual.row_checksum`, plus
`checks.row_checksum: {ok, expected, actual, unavailable, expected_by_file, actual_by_file}`.
`scope.row_checksum_columns` retains the selection order, and
`scope.row_checksum_semantics` states its limits. A selected artifact field
missing or schema-incompatible in **any** file (including nested defects and
empty files) makes the entire actual row digest unknown (`null`) and fails the
check with exit **1**. Schema diagnostics remain independent. Unselected schema
defects still fail schema validation without invalidating a selected tuple digest.
Without this option, existing default and per-column reports are unchanged.

The two per-file arrays contain `{file, checksum}` records, sorted independently
by absolute file-path string. Each digest uses the selected planned tuple in
first-request column order and the same `RowSummary` encoding as the aggregate.
Valid empty files have the zero-row digest. Missing input fields become typed
nulls and Hive constants participate. A missing or incompatible selected output
field gives that file `checksum: null`, even when empty; healthy files before
and after it retain digests. The aggregate remains `null` with the same reasons.

For input `a.parquet` with `id=[1,2], note=["a","b"]` and `b.parquet` with
`id=[3,4], note=["c","d"]`, an artifact that changes only `b` to
`note=["d","c"]` exits **1** when both tuple fields are selected. The two `a`
file digests agree and the two `b` digests differ, although both per-column
multisets still match. These inventories describe each side independently:
they do not pair files or imply that digests add. Repartitioning can preserve
the aggregate while changing individual file digests. Skipped inputs and
sidecars contribute no records. Consumers rejecting unknown keys must allow
the two additive keys on selected tuple checks.

File summaries accumulate alongside the aggregate in the shared single-threaded
batch pass; no second file open, decode pass or full-table read is added for
them. Healthy files after an invalid file still need an evidence pass, even
though the unavailable aggregate no longer needs their values. File and
aggregate summaries each encode the selected tuples. Evidence memory grows
with readable file count and requires sorting; existing row-only bounds do
not cap report size, bytes, RSS or runtime.

`checks.row_checksum.unavailable` explains selected artifact schema defects as
records of `{column, file, path, path_parts, reason, expected, actual}`.
`column` is the selected literal top-level name; the other fields copy the exact
schema issue, including nested list markers (`null`) and literal names in
`path_parts`. For example, an empty artifact file missing `note` contributes:

```json
{"column":"note","file":"/example/empty.parquet","path":"/note","path_parts":["note"],"reason":"missing_field","expected":"string","actual":null}
```

All selected-field issues are included, even in files after the first bad file:
missing/unexpected nested fields, wrong types, nested order and duplicate fields.
Identical records are deduplicated. Records sort by first-request column order,
absolute file, escaped path, ASCII JSON `path_parts`, then reason. Unselected
issues and root field order are excluded: tuples select fields by name in their
own order. Such schema failures can coexist with a passing row checksum. The
list is empty when the digest is available, including a value mismatch with
`ok: false`; it distinguishes unavailable data from different values. Invalid
or unreadable artifacts still exit **2**, including duplicate top-level names,
without a partial validation report. These reasons reuse footer evidence and
add no decode pass; their list, deduplication and sorting use memory proportional
to reported schema defects, with no report byte cap.

Each row is normalized to its selected planned types with the same nested,
null, missing-input-field, Hive and scalar rules described above. Its SHA-256
payload is compact, ASCII-escaped JSON
`["parquet-unify-row-v1", SELECTED_SCHEMA, VALUES]`, where `SELECTED_SCHEMA` is
the ordered list of `{name, type}` records from the planned schema report, and
`VALUES` is the ordered list of canonical scalar values. Thus names, selection
order and planned types participate in the digest. Accumulate the row-hash
integers' sum and squared sum modulo 2^256, then SHA-256 the ASCII string
`ROW_COUNT:SUM_HEX64:SQUARED_SUM_HEX64` (lowercase, zero-padded hex). Empty
datasets use count zero and both zero sums. These probabilistic multiset checks
check association only within the selected tuple; they do not prove uniqueness
or cryptographic file integrity. The `parquet-unify-row-v1` and
`parquet-unify-ordered-row-v1` domain strings keep their original spelling under
datawrench, so digests stay comparable with reports from the standalone tool.

To validate a caller-chosen **logical row sequence**, use independent repeatable
`--ordered-row-checksum-column NAME` selections. Names are literal, case-sensitive
top-level fields, deduplicated in first-request tuple order. Unknown names exit
**2** before decoding. Each side independently concatenates readable files sorted
by **absolute file-path string**, then rows in their physical order within each
file. This is a caller-chosen logical ordering, **not a Parquet dataset ordering
guarantee**. Existing catalog file lists retain their traversal order; the ordered
metric uses the explicit string sort (which can differ for nested paths).

```sh
datawrench parquet-unify validate /tmp/ordered-input --output /tmp/reversed.parquet \
  --ordered-row-checksum-column id --ordered-row-checksum-column note \
  --row-checksum-column id --row-checksum-column note
```

For input `a.parquet: [(1,"a"),(2,"b")]`, `b.parquet: [(3,"c")]` with
`id:int64, note:string`, artifact `[(3,"c"),(2,"b"),(1,"a")]` exits **1**:
row counts and unordered tuple checksums pass, but the ordered checksum fails.
Different filenames, file splits, row groups and batch sizes pass when they
preserve the same concatenated sequence. File paths and boundaries are not
hashed. Nulls and duplicate tuples contribute at every position; swapping two
identical tuples naturally leaves the sequence unchanged.

Only when requested, reports add `expected.ordered_row_checksum`,
`actual.ordered_row_checksum` and
`checks.ordered_row_checksum: {ok, expected, actual, unavailable, expected_by_file, actual_by_file}`. Available
digests are complete 64-character lowercase SHA-256 hex strings, even on a
mismatch. `scope.ordered_row_checksum_columns` lists tuple column order;
`scope.ordered_row_checksum_semantics` and `scope.ordered_row_checksum_order`
describe value and scan scope. A selected field missing or incompatible in any
artifact file, including an empty file, makes the entire actual digest `null`
and fails with exit **1**. `unavailable` uses the exact same selected schema
evidence, ordering and deduplication rules as `row_checksum` above; available
digests expose `[]`. Root order and unselected defects fail schema independently.
Input skip/unreadable rules and output unreadable exit **2** remain unchanged.
Omitting the option preserves existing output bytes and adds no data decoding.

The two per-file arrays contain `{file, checksum}` records, independently sorted
by absolute file-path **string**. Each digest describes that file's physical
selected tuple sequence using the same planned types and `OrderedRowSummary`
header/row/count encoding as the aggregate. The path is not hashed. For input
`a.parquet: [(1,"a"),(2,"b")]`, `b.parquet: [(3,"c"),(4,"d")]` and an artifact
with identical `a` but `b: [(4,"d"),(3,"c")]`, selecting `id` and `note` still
exits **1** with the same aggregate ordered mismatch. The two `a` file digests
agree; the `b` digests differ. The unordered tuple checksum still matches.
These are independent inventories: validation does not pair files or compose
digests. Different file splits can preserve the aggregate sequence while
changing file digests; only the aggregate decides the ordered checksum check.

Valid empty files use the existing empty ordered digest, including its selected
schema header and zero count. Missing or incompatible selected output fields
give that file `checksum: null`, even when empty; healthy files before and after
it retain digests, while the aggregate stays `null` with the same reasons.
Planned missing input typed nulls and Hive strings/nulls participate. Output
Hive directories are never inferred; skipped inputs and sidecars have no records.
The two additive keys require consumers that reject unknown keys to allow them.

`--row-group-checksum-evidence` is opt-in and valid only with at least one
`--ordered-row-checksum-column`; otherwise `validate` exits **2** before scanning
the artifact. With it, `checks.ordered_row_checksum` also gains
`expected_by_row_group` and `actual_by_row_group`. Each record is
`{file, row_group, rows, checksum}`: `file` is the absolute path string,
`row_group` the zero-based physical footer index, `rows` the footer row count,
and `checksum` that group's selected ordered digest, or `null` when the file's
selected output fields are invalid. Records sort by path string, then group
index. Zero-group files contribute no records; actual zero-row groups receive
the empty digest. Input groups come from the same footer-ordered read. Each
physical group is read once with the same bounded, single-threaded reader
(`row_groups=[i]`), so no extra file opens or full-table reads occur. Aggregate
and per-file digests, default reports and exit codes are unchanged.

These records localize a difference to one physical group within a file. They
do not pair files or compare groups across files, and digests are not composed.
For `data.parquet` with `id:int64=[1,2,3,4]`, `note:string=[a,b,c,d]` and
`row_group_size=2`, swapping the artifact's last two whole rows exits **1**; the
first group's digests agree and the second differs. Evidence grows with the
number of readable physical groups; there is no byte, RSS or runtime cap.

One file-local ordered summary accumulates beside the aggregate during the
shared bounded, single-threaded batch pass. No second file open, decoding pass
or full-table read is added. Healthy files after an invalid file still need one
evidence pass, even when the aggregate is unavailable. File and aggregate
summaries each encode tuples. Evidence grows with readable file count and
requires sorting; existing row bounds do not cap report size, bytes, RSS or
runtime. No ordered selection adds no ordered evidence work.

The separate wire domain uses SHA-256 over a stream of frames. Each frame is
one ASCII tag byte, the payload's byte length as unsigned 64-bit big-endian,
then payload bytes. The first `H` frame contains compact ASCII-escaped JSON
`["parquet-unify-ordered-row-v1", SELECTED_SCHEMA]`; every row contributes an
`R` frame containing compact ASCII-escaped JSON `VALUES`; the final `C` frame
contains the total row count as ASCII decimal. Schema and values use the same
planned-type normalization and canonical scalar encoder as unordered tuples.
Selection order, names and types are thus included even for empty data. There
are no batch, row-group or file frames. This is a selected-value comparison,
not a file-integrity signature or uniqueness proof; unselected values remain
unchecked. NaNs and signed zero retain their canonical numerical equivalence.

Null counts, per-column, unordered and ordered row checksums share one decoding pass per
needed file. The row accumulator retains only count and two fixed-width sums,
and the ordered accumulator retains only count and SHA-256 state,
with no sorted rows, collected row hashes or full result table. Their transient
memory includes the decoded batch of **all physical columns**, normalized
selected arrays, and one canonical tuple/serialized payload. Batch-size and
max-rows bound **rows only**; a single large string, binary or nested value,
schema, or decoder buffer can still consume large memory. There is no byte/RSS
or runtime cap, including for row-checksum-only validation.

Unselected values remain unchecked, and footer-only validation leaves data
pages unread. Validation does not establish safe-cast feasibility for unselected
input values. Inputs and artifacts must stay stable during the command; files
are not locked. Only stdout/stderr are written by validation; redirect reports
outside the scanned data and never over an input or artifact file.

## Rewriting and boundaries

`rewrite` decodes and writes each source row group into a staging directory,
then checks a single output fingerprint and matching data checksums before
publishing a **new** output directory. All input files (including already
conforming ones) are re-encoded with Zstandard into synthetic `part-00000.parquet`
names, using Parquet 2.6 for nanosecond preservation. Hive columns are
materialized; output is flat and does not need `--hive`. No source paths or
partition values become output paths. No in-place mutation or overwriting is
allowed; the output parent must exist and the output must be outside the input
directory. Ordinary failures clean staging and partial publication. A process
kill during publication can leave a partial new directory; publication is not
a transactional table format.

First-version limits are explicit:

- Local plain Parquet only; no remote stores, Delta/Iceberg/Hudi or schema registry.
  Keep the input stable while scanning/verifying/rewriting; files are not locked.
- Decimal precision 1–38 with `0 <= scale <= precision`. Maps, fixed-size lists,
  dictionaries/extensions, durations, times and other unlisted Arrow types fail
  planning. Oversized casts/offsets/temporal ranges fail rather than truncate.
- DuckDB cannot preserve timezone-aware nanosecond timestamps. Plan/rewrite and
  the Polars recipe support them, but DuckDB recipe/verification explicitly fail.
- DuckDB recipes require nonempty, case-insensitively unique field names without
  NULs, and no empty structs. Recipe paths containing glob metacharacters
  (`*?[]`) are rejected to prevent DuckDB expanding unintended files.
- Verification materializes the final Polars result (and naive reads) in memory.
  Scan is footer-only; rewrite/reference use at most one decoded source row
  group at a time. This is not a benchmarked large-dataset streaming verifier.
- No data quality rules, value-dependent narrowing, sorting, row-order guarantee,
  preservation of source compression/metadata or concurrent writer coordination.

## Development and validation

From a datawrench checkout:

```sh
uv run --extra test pytest tests/parquet_unify -q
```

The tests need PyArrow, DuckDB and Polars installed.

Tests use real, small Parquet files and one process: table-driven lattice and
failure cases, a [committed fixture](../tests/parquet_unify/fixtures/README.md), generated Hive
and conflict fixtures, independently executable recipes, naive data loss,
per-engine errors, checksum equivalence after rewrite, null/list/struct masks,
integer and decimal limits, timestamps (including ns/tz), malformed input,
CLI exit codes and escaped paths/identifiers. No test requires network access.
Plan-explanation regressions also cover complete rejected plans, strict and
explicit stringify policies, decimal scale bounds, missing nested fields,
stable reports, and literal `[]` fields beside list element addresses. Field-order
regressions cover root/nested/list structs, order-only strict rejection, mixed
order/type/missing-field drift, empty names and literal address collisions;
type or order-preserving membership changes produce no false order records.
Artifact-validation regressions cover correct rewrites and deliberate row/type/
null corruption, every file in mixed-schema outputs, literal names, Hive nulls,
empty files, bounded batches, pre-decode row budgets and unchanged data files.
Checksum regressions cover selected value corruption, planned normalization,
missing/Hive fields, literal names, nested/null values, duplicate sensitivity,
nanosecond precision, NaN/zero equality, offset carriers, unknown selections,
incompatible output columns and the explicit row-association limitation.

## License

Part of datawrench, licensed under Apache-2.0 ([LICENSE](../LICENSE), [NOTICE](../NOTICE)). This command was first released as a standalone MIT-licensed project; that notice is kept in [`licenses/MIT-original-tools.txt`](../licenses/MIT-original-tools.txt). Dependency notices and sources: [THIRD_PARTY.md](../THIRD_PARTY.md).
