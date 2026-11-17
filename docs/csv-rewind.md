# datawrench csv-rewind

`datawrench csv-rewind` checks what a proposed DuckDB CSV schema would do to the
**original cell text** before you accept it. Use it when a supplier CSV is about
to be loaded with inferred or hand-picked types (`BIGINT` IDs, `DOUBLE` amounts,
`DATE` columns) and you need to know which cells would change: leading zeros
dropped, long integers rounded, dates or decimals respelled, values turned into
NULL. It reports every changed cell, recommends conservative column types and
writes read SQL that DuckDB has verified to keep the text.

Everything runs locally. DuckDB supplies sniffing, parsing and type conversions;
csv-rewind never parses CSV itself.

## Install

```sh
pipx install 'datawrench[duckdb]'   # or: pip install 'datawrench[duckdb]'
datawrench csv-rewind --version     # prints the bare datawrench version
datawrench csv-rewind --help
```

`python -m datawrench csv-rewind` is equivalent. Without the `duckdb` extra the
command prints the install hint and exits 2.

## Quick start

From a datawrench clone:

```sh
datawrench csv-rewind examples/csv_rewind/survey.csv --header yes \
  --type code=BIGINT --type big=DOUBLE \
  --report report.json --events losses.jsonl --freeze safe-read.sql \
  --write-config accepted.json
```

The sample contains leading-zero supplier codes, integers above 2^53, day-first
dates, decimal trailing zeros and uppercase booleans. Expect exit status **1**:
the report shows `00123 -> 123`, `9007199254740993 -> 9007199254740992.0`,
and date/decimal/boolean normalization. Every column with a changed or failed
cell is recommended as `VARCHAR`. Unchanged columns keep their candidate type.

Run `safe-read.sql` with DuckDB, or use the Python API:

```python
import duckdb
from datawrench.csv_rewind import AuditOptions, audit, load_config

report = audit("examples/csv_rewind/survey.csv", AuditOptions(types={"code": "BIGINT"}, header=True))
with duckdb.connect(config={"threads": 1, "memory_limit": "128MB"}) as connection:
    preserved = connection.execute(report["frozen"]["sql"]).fetchall()
print(preserved[0][0])  # 00123
```

## Options

| Option | Meaning |
| --- | --- |
| `INPUT` | One local CSV/TSV file, optionally compressed (gzip, zstd) |
| `--type COLUMN=TYPE` | Override one inferred type; repeatable; quote `DECIMAL(18,2)` |
| `--types FILE` | JSON object mapping column names to DuckDB types |
| `--read-config FILE` | Audit using a saved frozen JSON recipe |
| `--header {auto,yes,no}` | Confirm or override header detection |
| `--delimiter`, `--quote`, `--escape` | Override the detected dialect (an empty `--quote` disables quoting) |
| `--new-line {lf,crlf,cr}` | Override line-ending inference, useful with multiline fields |
| `--skip N` | Explicitly exclude N initial lines |
| `--date-format`, `--timestamp-format` | DuckDB strptime formats |
| `--encoding` | DuckDB CSV encoding (default `utf-8`) |
| `--null TOKEN` / `--no-nulls` | Candidate null tokens (replace the default empty string) / no null coercion |
| `--sample-size N` | Sniffing rows only (default 20480, `-1` sniffs all); the audit always scans all rows |
| `--max-examples N` | Examples kept in the report (default 20) |
| `--max-cell-chars N` | Truncate displayed evidence (default 512); `--events` keeps full text |
| `--memory-mb N` | DuckDB memory budget (default 128, minimum 32) |
| `--json` | Print the report JSON on stdout |
| `--report FILE` | Write the JSON report atomically |
| `--events FILE` | Stream **all** changed cells as full-text JSONL |
| `--freeze FILE` | Write conservative, verified DuckDB read SQL |
| `--write-config FILE` | Write a reusable frozen JSON recipe |

Exit statuses: **0** every cell round-trips exactly, **1** losses found, **2**
invalid input or incomplete audit, **130** interrupted. Errors are printed as
`datawrench csv-rewind: <message>`.

## Reuse a recipe for the next delivery

```sh
datawrench csv-rewind tests/csv_rewind/fixtures/duckdb-canonical.csv --header yes --write-config accepted.json
datawrench csv-rewind tests/csv_rewind/fixtures/duckdb-canonical.csv --read-config accepted.json --json
```

Replace the second input with the next supplier file. `--read-config` fixes the
dialect, column order and candidate types from the accepted recipe. A formerly
numeric ID that arrives as `00123` is checked as the old numeric type and is
reported with the new raw evidence. Reordered, renamed, missing or extra
columns fail before a report can be produced. With no header, a column's
meaning cannot be verified beyond its position and record width.

CLI type/dialect options explicitly override the recipe. Empty VARCHAR fields
stay text when a recipe is reloaded because the frozen reader forces non-null
preservation. The old source hash is kept as provenance; it is not required
to equal the next delivery's hash. Engine-version changes are noted and every
new frozen import is verified again. The Python equivalent is
`audit("next.csv", load_config("accepted.json"))`.

[`examples/csv_rewind/repeated_delivery.py`](../examples/csv_rewind/repeated_delivery.py)
is a complete two-delivery example covering padded IDs, rounded long integers,
changed date formats, decimal spelling and boolean spelling. It writes
reviewable reports, full evidence, recipes and verified SQL under
`examples/csv_rewind/output/`.

The recipe file is JSON with `"format": "csv-rewind.duckdb"` and `"version": 1`;
the report has `"format_version": 1`. Both formats are unchanged from the
standalone `csv-rewind` tool, so existing recipes keep working.

## Candidate schemas and evidence

Omitted types come from DuckDB's `sniff_csv`. Supply repeatable
`--type COLUMN=TYPE` or `--types schema.json` with
`{"supplier_id": "BIGINT", "amount": "DECIMAL(18,2)"}`. Command-line types
override that file. Unknown names and unsupported types fail. Supported types
are `VARCHAR`, `BOOLEAN`, the DuckDB integer and float types,
`DATE`/`TIME`/`TIMESTAMP`/`TIMESTAMPTZ` and `DECIMAL(precision,scale)`.

The audit always scans **every data row**, even when inference uses a sample.
`data_row` is the 1-based data record after header/skip handling, not a physical
line number: quoted multiline fields are one record. Null tokens default to the
empty string; `--null NA --null NULL` replaces that set, and `--no-nulls` disables
it. Null coercion itself is a loss of original text and is reported.

The JSON report contains counts by column and reason and at most 20 examples.
Examples are limited to 512 characters and include length and hash when
truncated. `--events losses.jsonl` streams **every changed cell with complete
original and rendered strings**, without holding the whole file or full report
in memory. File outputs are staged and replaced only after a complete audit; an
error removes staged files.

## What "lossless" means here

The contract is: decoded CSV field text equals DuckDB's canonical VARCHAR output
after the candidate parse. This intentionally detects presentation changes such
as `1.00 -> 1.0` even when the numeric value is unchanged. It also distinguishes
numeric value changes, failed parses, leading zeros, whitespace, boolean/date
format changes and null conversion. It does not claim IEEE floating-point
binary exactness when a short decimal prints identically.

The raw read forces every column to VARCHAR and `force_not_null`, preserving
empty fields. Frozen SQL fixes the sniffed dialect and types, forces non-null
text preservation, and uses UTC. The generated configuration is then actually
read back through DuckDB's CSV type-conversion path: every rendered cell and
the row count must match the text read before the report can succeed.
DuckDB's `type_pushdown` optimizer is disabled in audit connections where it
exists, so a VARCHAR comparison cast cannot bypass the typed conversion.
The source is fingerprinted before and after the audit; detected changes are
rejected. Use a stable local input and rerun for each supplier delivery.

## Prior art

DuckDB already preserves leading-zero IDs during inference and supplies the
frozen read recipe. csvkit `-I --blanks` already offers text-preserving import.
qsv and csv_plus have useful safe-inference guards. Frictionless validates Table
Schemas. csv-rewind adds a check for a **proposed DuckDB schema** with per-cell
evidence; it is a thin adapter, not a CSV parser or a new type inferrer.

| Tool (version inspected 2026-11-11) | What it already does | What csv-rewind adds |
| --- | --- | --- |
| [DuckDB 2.0.0](https://duckdb.org/docs/2.0/data/csv/auto_detection) | `sniff_csv` returns dialect, types, date formats and an executable prompt; `all_varchar` plus `force_not_null` keep empty fields and whitespace | A per-cell reversibility report and downgrading of lossy columns, built on those engine facilities |
| [csvkit 2.2.0](https://csvkit.readthedocs.io/en/latest/tricks.html) | `csvjson -I --blanks` keeps all text; default JSON export changed `00123` to `123.0` | Audit of a selected DuckDB schema with original/rendered evidence |
| [qsv 28.0.0](https://github.com/dathere/qsv) | Full-file `stats` inference, `--zero-padded-numeric`, schema export | Exact cell changes for an externally proposed DuckDB type |
| [Frictionless 5.21.0](https://framework.frictionlessdata.io/docs/framework/schema.html) | Table Schema validation, field read/write conversions | Uses DuckDB's own dialect/type decisions instead of a second engine |
| [csv_plus 1.13.0](https://github.com/almasumdev/csv_plus) | Safe Dart decoder with leading-zero/long-ID guards | Audits a schema used by a different importer |

## Limits and known engine issues

- Only scalar text, boolean, numeric, decimal and temporal types are audited.
  CSV quoting, BOMs and line-ending bytes are outside the decoded-cell contract.
- DuckDB skips physically blank records. Header and dialect inference can be
  ambiguous; confirm the header and inspect the reported parameters. For a
  known RFC-style producer, pass `--quote '"'`: an unclosed quote may otherwise
  cause the sniffer to choose a quote-free dialect.
- Comments are disabled and inferred line skipping is rejected unless
  explicitly requested with `--skip`. Header-only files are rejected.
- This is a DuckDB import check, not an emulation of Excel, pandas or other readers.
- Local filenames are escaped against DuckDB glob expansion as well as SQL
  syntax: a literal `supplier[1]*.csv` never selects another file.
- The importer defaults to one DuckDB thread, a 128 MB engine budget and
  64-row Python batches. DuckDB's default `max_line_size` (2,000,000 bytes)
  applies. Schema/config JSON is limited to 4 MiB and duplicate keys are rejected.
- Input files are not locked, and output replacement is atomic per file rather
  than a transaction spanning all requested outputs.

Engine behaviours worked around (observed with DuckDB 2.0.0; the standalone
tool was tested on DuckDB 1.4.3, 1.5.6 and 2.0.0; datawrench's `duckdb` extra
installs the 1.x series):

- **Explicit CRLF + `strict_mode` returns zero rows.** The reader keeps the fixed
  newline and relies on DuckDB's documented strict default instead of passing
  `strict_mode` explicitly. Malformed quoting and extra/missing fields after
  thousands of valid records are still rejected.
- **`sniff_csv`'s `Prompt` does not quote apostrophes in column names.** The
  audit builds SQL from the structured sniff result and quotes literals and
  identifiers separately.
- **Valid UTF-16 supplementary characters (such as emoji) are rejected** and
  UTF-16BE input can be misread. The audit fails with a transcoding recipe and
  never replaces characters:

  ```sh
  iconv -f UTF-16 -t UTF-8 vendor.csv > vendor-utf8.csv
  datawrench csv-rewind vendor-utf8.csv --header yes
  ```

  For headerless UTF-16/CR files whose fully specified dialect defeats both
  sniffers, Python's standard CSV reader bootstraps only the record width;
  the report says type inference was unavailable and unspecified columns stay
  text.
- **`type_pushdown` can move a VARCHAR comparison cast into the reader**, so a
  naive check would compare the original text with itself. The optimizer is
  feature-detected and disabled in audit connections and verification cursors.
