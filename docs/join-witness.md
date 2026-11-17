# datawrench join-witness

A join that should be many-to-one quietly turns into many-to-many when a dimension
table picks up a duplicate key, and every `SUM` downstream of it is inflated.
`datawrench join-witness` finds which original source records multiply a join and how
much each one adds to a numeric sum, before you build the wide result. Use it on local
CSV/TSV extracts when a join's row count or a total looks wrong, or as a gate in front of
an existing [TableProof](https://github.com/suguangliang3083-jpg/tableproof) join contract.

It is a small adapter on top of TableProof 0.1.x: TableProof supplies parsing, exact key
counts, cardinality checks, join-size predictions and fingerprints; `join-witness` adds
ranked multiplying keys tied to original records, per-measure sum attribution and
executable DuckDB assertions.

## Quick start

```sh
pipx install 'datawrench[tableproof]'

# The packaged four-order example; exits 1 on purpose (duplicate customer keys).
datawrench join-witness --demo --rank-by left:amount

# Your own files: orders joined to customers, which should be many-to-one.
datawrench join-witness --left examples/join_witness/orders.csv \
  --right examples/join_witness/customers.csv \
  --key customer_id --expect many-to-one --left-measure amount \
  --assertions-output assertions.sql

# An existing TableProof contract.
datawrench join-witness --config examples/join_witness/tableproof.toml \
  --audit orders-to-customers --left-measure amount --show-rows
```

Exit status: 0 passes, 1 violates the data policy, 2 is an input, usage or I/O error.

The demo predicts six left-join rows from four orders. Customer `C001` contributes four
joined rows from two orders and two customer records. The report shows the original
records and explains the amplified `amount` sum (35.00 before the join, 55.00 after,
20.00 added by repetition). It never constructs the source-record cross product. The demo
uses packaged copies of the example files, so it works from any directory.

## Options

| Option | Meaning |
| --- | --- |
| `--left PATH`, `--right PATH` | Local UTF-8 CSV or TSV (`.tsv` means tab-separated) |
| `--key COLUMN` | Key present on both sides; repeat for a composite key |
| `--left-key COLUMN`, `--right-key COLUMN` | Keys with different names; repeat in component order |
| `--expect` | `one-to-one`, `one-to-many`, `many-to-one` or `many-to-many` |
| `--how` | `inner`, `left`, `right` or `full`; defaults to the contract's `join_type`, else `left` |
| `--left-measure COLUMN`, `--right-measure COLUMN` | Numeric column whose sum to trace; repeat |
| `--rank-by` | `rows` (default), `left:COLUMN` or `right:COLUMN` |
| `--null-keys`, `--left-unmatched`, `--right-unmatched` | Policy: `error`, `warn` or `ignore` |
| `--fail-on` | `error` or `warning`; overrides the configured threshold |
| `--top N` | Ranked key groups to show (0..100, default 10) |
| `--sample-rows N` | Original records per side and key (0..20, default 3) |
| `--sample-bytes N` | Total raw record-value JSON budget (0..16777216, default 4 MiB) |
| `--redact` / `--show-rows` | Omit raw key and record values / include them, overriding the config |
| `--format` | `text` (default) or `json` |
| `--output PATH` | Write the report atomically instead of to stdout |
| `--assertions-output PATH` | Write runnable DuckDB SQL assertions |
| `--config PATH`, `--audit NAME` | Use a TableProof v1 TOML contract and select one join |
| `--demo` | Run the packaged example |
| `--print-schema` | Print the bundled JSON report schema and exit |

Repeat `--key` for a composite key. If names differ, use repeated `--left-key` and
`--right-key` in matching order. `--config` cannot be combined with the direct
table, key and policy options.

### Ranking by money, not rows

By default, keys are ranked by extra joined rows above the declared cardinality. To find
a large monetary error hidden among relatively few records, use `--rank-by left:amount`
(or `right:COLUMN`). This automatically includes that measure and ranks by its
**absolute** extra signed value from repetition. The signed contribution remains visible,
including refunds and other negative amounts. A column name may itself contain colons;
everything after `left:` or `right:` is the exact column name. Metric ranking stores one
Decimal per key whose selected measure repeats, in addition to the existing counters.

### TableProof contracts

Existing TableProof contracts work directly, without copying their keys or policies into
another configuration format. `--audit` is optional when there is exactly one configured
join. Relative input paths resolve beside the TOML file, as in TableProof. Its optional
materialized result is validated too, and its declared join type cannot be reinterpreted.
Configured `show_raw_keys = false` also redacts this command's record values; `--show-rows`
opts into raw records and `--redact` forces redaction. Direct mode shows raw records by
default. The configured `fail_on` threshold is honored; `--fail-on error|warning`
overrides it. JSON includes `exit_status` and `fail_on` so a warning threshold is
distinguishable from the upstream audit's verdict.

## What the report means

- `upstream_audit` is the complete TableProof audit. Its verdict determines the exit
  status. Unmatched keys warn; blank keys fail by default (`--null-keys` changes the
  blank-key policy).
- `top_keys` ranks multiplying matched key groups by excess rows above the declared
  cardinality caps, then directional added copies, row product, and exact key.
  `many-to-one` caps right frequency at one; `one-to-many` caps left; `one-to-one` caps
  both. `many-to-many` imposes no cap, but still reports directional multiplication.
  These are diagnostic baselines, not instructions to deduplicate or claims about which
  source row is correct. Each shown key says whether it violates the declared
  relationship; allowed directional repetition can still help trace right-side dimension
  measures through a valid many-to-one join.
- Per-side added copies count repetitions of that side's original records. The two
  counts describe different baselines and must not be added together. Duplicate
  unmatched keys can fail a global relationship without multiplying any matching
  records; consult the upstream duplicate findings as well.
- Measures report the original sum, the sum of retained records counted once, the
  predicted joined sum, the signed value added by repetitions, and the sum dropped by
  unmatched records. Losses and repetitions are separate, so cancellation cannot hide an
  inflation. Numeric sums are exact Decimal strings. Empty measure cells are ignored;
  sums are coalesced to zero, and non-null row counts are explicit.
- Source locators are 1-based data-record numbers plus physical start/end lines (a quoted
  multiline CSV cell can occupy several lines). Each sample's `join_copies` is the number
  of output occurrences of that original record. `--top` and `--sample-rows` bound report
  size; omitted counts remain explicit. `--sample-bytes` adds a total compact-JSON value
  budget; oversized record values are omitted explicitly while their locators remain.
  This budget covers the raw value objects, not the entire report or the upstream
  in-memory key counters. `--redact` removes key and record values, retaining locators,
  hashes and sums.

## Executable assertions

`--assertions-output PATH` writes read-only **DuckDB** SQL that loads the same local files
as exact strings and raises `error(...)` for required duplicate or blank-key constraints.
Run it with DuckDB's CLI, or execute each `sql` field from the JSON report with DuckDB
Python. The assertions enforce global usable-key uniqueness and the error-level blank-key
policy. They do not reproduce TableProof's normalization or coverage warnings. There is
no portable SQL `ASSERT` statement; the dialect is deliberately explicit.

Every assertion first checks the original header, so reordered or renamed columns require
regenerating the SQL. Key grouping uses byte equality even if a caller has enabled a
case-insensitive DuckDB collation. Record terminators, quoting, column types and literal
filename glob escapes are explicit. Generated SQL was executed against DuckDB 2.0.0 and
1.4.3. DuckDB is an optional execution target, not a dependency of the command.

A related DuckDB reader behaviour is worked around: with DuckDB 2.0.0 and 1.4.3, a valid
CRLF CSV whose quoted header cell contains an LF, read with an explicit `columns`
mapping, `header=true` and `auto_detect=false` and no newline option, returned zero rows
instead of two. An empty reader would make duplicate keys look unique, so the generated
SQL always names a validated record terminator (`new_line`). See also
[duckdb/duckdb#25827](https://github.com/duckdb/duckdb/issues/25827) (related sniffing
scenario; a shared cause is not established).

## Formats and limits

- Local regular UTF-8 / UTF-8-BOM CSV and TSV files with UTF-8 paths only. No SQL text,
  database connection, remote URL, spreadsheet, Parquet or arbitrary predicate parsing.
- Exact, case-sensitive strings. No trimming, numeric coercion, case folding,
  deduplication or source modifications. `001` differs from `1`; whitespace is
  significant. Empty key components never match, including each other. This is
  TableProof's SQL-like missing-key convention, not pandas null matching.
- Selected measures must be finite decimal literals, optionally with exponent; no locale
  currency or thousands separators. Maximum 1000 digits and absolute Decimal exponent
  1000; arithmetic precision is 4096 digits.
- TableProof keeps key counters and normalization maps in memory. This command does
  several sequential validation passes, uses O(distinct keys + bounded samples) storage,
  and does not promise bounded RAM for arbitrary cardinality. Use stable input snapshots:
  concurrent source changes cause an error.
- SQL exports require consistent CSV/TSV record terminators within each source file;
  mixed record terminators are rejected. Embedded newlines in quoted fields and LF, CRLF
  or CR record terminators are supported.
- On POSIX, SQL export rejects paths combining literal backslashes with glob characters
  (`*?[]`); DuckDB cannot address the escaped pattern reliably in this combination. Copy
  those inputs to simpler filenames.
- Empty inputs keep TableProof's `unknown` observed relationship and may fail its policy
  even though a uniqueness-only SQL assertion passes.
- The exact counters come from TableProof's module helper `tableproof.audit.read_table`,
  which is not an explicitly public export; datawrench allows `tableproof>=0.1.1,<0.2`.

## Report schema

The bundled JSON report schema (`src/datawrench/join_witness/data/report-v1.schema.json`)
describes the v1 machine contract; `datawrench join-witness --print-schema` prints it
without input files or a schema-validation dependency. The report's `tool.name` stays
`join-witness` and `schema_version` stays `1.0`. Additional fields are allowed for
compatible additions; existing fields keep their meanings. Decimal sums are strings for
exact arithmetic. Very large row counts are JSON integers, so consumers should use a
lossless integer parser.

## Related tools

- **TableProof** already covers uniqueness, declared relationships, unmatched rows,
  normalization hazards, all four join-size predictions and a materialized result's key
  multiset. `join-witness` reuses that audit rather than reimplementing it.
- **joinspy** (R) offers join-size predictions, ranked Cartesian key products and the
  offending duplicate rows; it is a close existing solution for R users.
- **Polars** (`DataFrame.join(validate="m:1")`) and **pandas** (`merge(validate=...)`)
  enforce cardinality on the join itself; use those guards in your code. They do not
  return a provenance plus sum-attribution report.
- **Frictionless Table Schema** declares primary and foreign keys; it does not specify
  quantitative multiplication witnesses.

All work is local; nothing is uploaded.
