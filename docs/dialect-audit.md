# datawrench dialect-audit

`datawrench dialect-audit` (formerly `dialect-audit`) audits SQL migration
readiness statement by statement. It detects opaque parser fallbacks,
untranslated functions and generator warnings, then checks generated DuckDB
queries against empty schema fixtures. It is intended for warehouse migrations
and teams running compiled warehouse SQL in local DuckDB tests.

## Install and run

Requires Python 3.11+ and Linux (the tested platform). The command needs
SQLGlot, DuckDB and PyYAML, installed through extras:

```sh
pipx install 'datawrench[sqlglot,duckdb]'
datawrench dialect-audit --version   # prints the bare datawrench version, e.g. 0.1.0
datawrench dialect-audit --help
```

From a datawrench checkout root, run the bundled example:

```sh
datawrench dialect-audit run examples/dialect_audit/snowflake \
  --read snowflake --write duckdb --schema examples/dialect_audit/schema.sql
```

The example deliberately exits **1** and reports:

```text
examples/dialect_audit/snowflake/report.sql#1 L2-2 C1-29 OK (binding: bound)
examples/dialect_audit/snowflake/report.sql#2 L4-4 C1-73 OK (binding: bound)
examples/dialect_audit/snowflake/report.sql#3 L6-6 C1-59 OK (binding: bound)
examples/dialect_audit/snowflake/report.sql#4 L8-8 C1-42 ANONYMOUS_FUNCTION, BIND_ERROR (binding: failed)
examples/dialect_audit/snowflake/report.sql#5 L10-10 C1-33 BIND_ERROR (binding: failed)
5 statements, 3 OK, 2 flagged; 2 errors, 1 warnings, 0 skipped files
```

`MY_UDF(amount)` is unknown to SQLGlot and absent from DuckDB. The final query
references `missing_column`, which is absent from the fixture. Reports include
the DuckDB diagnostic and function inventory. Parsing or binding failures do
not prevent checking subsequent statements.

`python -m datawrench dialect-audit` is equivalent to `datawrench dialect-audit`.
No API keys or services are required. Initial dependency installation may use
the network; audits and tests operate offline. Errors are printed as
`datawrench dialect-audit: <message>` and exit 2.

The subcommands are `run`, `baseline` and `diff`.

## CLI and reports

```text
datawrench dialect-audit run PATHS [PATHS ...] --read DIALECT --write DIALECT
  [--schema schema.sql|schema.yml] [--schema-read DIALECT] [--auto-stub]
  [--format text|json|md|junit|sarif] [--fail-on error|warn|never] [--emit-dir DIR]
```

Paths may be individual files or directories recursively containing `.sql`
(case insensitive). Duplicate resolved inputs are audited once. Explicit file
paths can have other extensions. Statement indices are one-based per file;
line ranges include the first and last SQL tokens, excluding leading/trailing
comments and the terminating semicolon. `column_start` and `column_end` locate
those same inclusive endpoints within their respective lines. Lines and columns
are **one-based**. Columns count Unicode code points, not UTF-8 bytes, UTF-16
units or visual width; a tab counts as one character. An initial UTF-8 BOM is
removed before mapping. LF and CRLF line endings are supported without changing
source text. Dialect tokenization keeps quoted semicolons inside their statement.
Unknown dialects list the available names and exit 2.

For example, `batch.sql` containing this single line:

```sql
SELECT 1; SELECT NO_FN(1); SELECT 3;
```

reports spans `L1-1 C1-8`, `L1-1 C11-25`, and `L1-1 C28-35`. The second
statement retains both `ANONYMOUS_FUNCTION` and `BIND_ERROR` with a DuckDB target;
the final statement still binds. For multiline SQL, `Cstart-end` refers to the
column on the first and last line respectively. JSON stores these as
`line_start`, `line_end`, `column_start`, and `column_end`; text, Markdown and
JUnit case names show both ranges. Emitted `report.json` retains the same fields.
A statement-level parse error keeps its token span. If tokenization fails, the
whole-file parse error keeps its existing line range but both columns are
`null` (`columns unavailable` in readable reports); a precise statement span
cannot be recovered. Skipped/comment-only inputs have no statement spans.

Each statement diagnostic adds `location`: either `null` (unknown) or an object
with `line_start`, `line_end`, `column_start`, and `column_end` in the original
file, using the same one-based inclusive code-point convention. A source
`PARSE_ERROR` gets a location only when SQLGlot supplies exactly one structured
error with positive integer line/column endpoints and a nonempty highlight
that exactly matches the source at that endpoint. The parser column is the
**last** highlighted character. Mapping adds the statement's line offset and,
only on its first source line, its column offset. Multiline highlights are
supported. Missing, malformed or mismatched metadata leaves the location `null`; multiple
entries always leave the aggregate primary location `null`. No message scraping
or guessed point locations.
Binder, generator, round-trip and tokenizer diagnostics have `location: null`.
The message remains SQLGlot's slice-relative wording; use the structured
location or the readable diagnostic prefix for original-file coordinates.

For `-- header\n  SELECT 1; SELECT * FROM; SELECT 3;\n`, statement 2
keeps `L2-2 C13-25`, and its `PARSE_ERROR` highlights `FROM` at
`L2-2 C22-25`. Statement 3 still binds. Text, Markdown and JUnit failure details
show `PARSE_ERROR [L2-2 C22-25]`; JSON, JUnit payloads and emitted reports retain
the location object. This locates the parser's highlight, which may be the last
valid token before missing syntax, rather than proving the cause of the error.

For an aggregate source `PARSE_ERROR` with two or more structured entries,
`related_locations` adds one object per entry, in parser order:
`{"description": "…", "location": {"line_start": 2, "line_end": 2,
"column_start": 34, "column_end": 34}}`. Every highlight is checked independently
with the same endpoint, exact source text and supplied-context checks. A bad
entry keeps its description with `location: null`; a missing/non-string
structured description becomes `null`. A malformed entry is retained with both
fields `null`. Descriptions have ANSI styling removed. Duplicates stay in order,
including duplicate spans and descriptions. Even if exactly one entry verifies,
the aggregate keeps `location: null`. Descriptions never supply coordinates.
Zero/single entries and other diagnostic kinds omit `related_locations` and keep
their existing primary-location contract.

For example:

```sql
-- header
  SELECT 1; SELECT (SELECT * FROM), (SELECT * FROM); SELECT 3;
```

Statement 2 keeps `L2-2 C13-51` and one `PARSE_ERROR` with its unchanged aggregate
message, severity and `location: null`. Its two `related_locations` highlight
closing parentheses at `L2-2 C34-34` and `L2-2 C51-51`, each with the corresponding
SQLGlot description. Text, Markdown and JUnit failure details append
`related #1 [L2-2 C34-34]: …` and `related #2 [L2-2 C51-51]: …` after the aggregate
message. Unknown entries show `[location unavailable]` and, when necessary,
`description unavailable`. JSON, emitted reports and JUnit payloads retain the
structured list. The batch still has three statements, two OK, one flagged,
one error and no warnings; statements 1 and 3 bind.

Primary and related diagnostic locations are additive in report/baseline/diff v1.
Older baselines without either field remain readable. Gaining, moving or changing
this evidence (including related descriptions/order/multiplicity) alone does not
change finding identity, which remains category, severity and aggregate message.
New/resolved findings retain the corresponding current/baseline primary and
related evidence, separately from the enclosing statement location; text diffs
also show the related entries. Stored related lists require at least two exact
`description`/`location` objects, a `PARSE_ERROR` with null/absent primary location,
nullable string descriptions and complete, positive spans inside the statement
when known. Unknown spans remain null. Older CLI releases reject new diagnostic
fields; update the CLI before accepting new snapshots.

| Category | Severity | Meaning |
| --- | --- | --- |
| PARSE_ERROR | error | Source SQL cannot be tokenized/parsed as one statement |
| COMMAND_FALLBACK | error | SQLGlot produced an opaque `Command`, not a structured translation |
| UNSUPPORTED | error | Generator diagnostic or an explicit unsupported audit/binding feature |
| ANONYMOUS_FUNCTION | warn | Function unknown to SQLGlot passes through; may be a valid target function |
| ROUNDTRIP_UNSTABLE | warn | Reparse/regeneration in the target dialect fails or changes generated text |
| BIND_ERROR | error | DuckDB EXPLAIN failed (catalog, column, type, permission, planning, timeout, etc.) |
| UNKNOWN_TABLE | error | Physical table absent from fixture and not inferred by auto-stub |
| OK | — | None of the above checks found a problem |

A statement may have several categories. Counts are per statement/category;
error and warning totals count diagnostics, so they can exceed flagged
statement counts. Function counts reflect occurrences in the **source AST**,
normalized to lowercase SQLGlot function names (for example `timestamp_trunc`);
anonymous functions retain their source name. Both per-file and overall
inventories appear in all reports (JUnit includes the statement payloads).

JSON report version 1 contains `statements` (file, index, line/column ranges, source SQL,
target SQL, categories, diagnostics, functions, auto-stubs, `binding_status`),
`notices`, `function_inventory`, `file_function_inventory`, `file_summaries`,
`binding` and `summary`.
Use `--format json` for integrations. No timestamps are added, so identical
inputs/options have reproducible reports, aside from engine/platform error
wording. JUnit has one test per statement plus one per input notice. Failures
respect `--fail-on`; non-failing input notices become skipped test cases.
JUnit includes `file_summaries` as a JSON suite property and `binding_status`
in each statement payload. These are additive fields in report version 1.

Each statement's `binding_status` records the engine check separately from its
static diagnostics:

| Binding status | Meaning |
| --- | --- |
| `bound` | DuckDB EXPLAIN succeeded; other warnings/errors may still exist |
| `failed` | DuckDB EXPLAIN was attempted and returned an error |
| `not-attempted` | DuckDB EXPLAIN was not reached, e.g. parsing, translation, scope or read-only preflight failed |
| `static-only` | Target is not DuckDB; no engine check is available, even if static checks pass |

`file_summaries` maps each collected input path to statement, OK, flagged,
error and warning counts, `categories`, `binding_statuses` counts, a `skipped`
boolean and `status` (`error`, `warn`, or `ok`). Status reflects the highest
diagnostic severity, independently of `--fail-on`; a later successful statement
cannot clear an earlier finding. Skipped files have zero statements, empty
binding counts and their notice category/severity. File category counts include
input notices; overall `summary.categories` retains its statement-only meaning.
Text and Markdown reports show these file summaries after statement diagnostics.

For a reproducible three-file batch with valid SQL, an unknown function, and
DDL/DML followed by more queries:

```sh
datawrench dialect-audit run tests/dialect_audit/fixtures/batch --read snowflake --write duckdb
```

This exits **1**: eight statements, four OK, four flagged, five errors and one
warning. Binding counts are four `bound`, two `failed` and two `not-attempted`.
`01_valid.sql` is OK; `02_unknown.sql` and `03_writes.sql` retain ERROR status.
The CREATE/INSERT are rejected; the following SELECT still reports the table
as missing, and the final `SELECT 3` binds successfully. The unknown-function
statement retains both `ANONYMOUS_FUNCTION` and `BIND_ERROR` diagnostics.
With `--write postgres`, all eight are `static-only`; only the unknown-function
warning remains, so the default exit is **0**, or **1** with `--fail-on warn`.

Exit codes:

- **0**: no findings meeting the threshold.
- **1**: findings meet `--fail-on error` (default), or `warn` (errors + warnings).
  `never` always suppresses finding-based failures.
- **2**: CLI, dialect, schema-load, path or output configuration error; unaffected
  by `--fail-on never`.

Non-UTF-8, binary (NUL-containing), unreadable or oversized files are reported
as `INPUT_SKIPPED` errors. Jinja markers `{{`, `{%`, `{#` anywhere in a file
produce a `JINJA_SKIPPED` warning: point the tool at compiled SQL. This is a
conservative textual check, including markers inside SQL strings/comments.
Empty/comment-only files produce an `EMPTY_INPUT` warning. Inputs are limited
to 2 MiB each, including schema fixtures. SQL symlinks and special files are
rejected. A tokenization error reports the whole file as one `PARSE_ERROR`;
statement-level parse errors recover at the next tokenized semicolon.

```sh
datawrench dialect-audit run examples/dialect_audit/snowflake --read snowflake --write duckdb \
  --schema examples/dialect_audit/schema.yml --format json > report.json
datawrench dialect-audit run examples/dialect_audit/snowflake --read snowflake --write duckdb \
  --schema examples/dialect_audit/schema.sql --format junit > junit.xml
datawrench dialect-audit run examples/dialect_audit/snowflake --read snowflake --write postgres \
  --format md --fail-on warn
```

Only `--write duckdb` performs binding. Other targets explicitly report
`binding: not-available`; their `OK` means static checks only. `--schema` and
`--auto-stub` are errors for other targets.

## Local SARIF export

```sh
datawrench dialect-audit run batch.sql --read snowflake --write duckdb --format sarif > audit.sarif
```

`run --format sarif` writes one SARIF **2.1.0** JSON log with one run for
local editor or CI ingestion; it uploads nothing. `run --baseline` is unsupported
and exits **2**, including with SARIF. `diff` still accepts only text/JSON;
`diff --format sarif` exits **2** until diff SARIF has its own contract.
Default text output, other formats, exact-source text diffs and `--emit-dir`
remain unchanged. Emission still writes generated `.sql` files and native
report-v1 `report.json`, without adding a SARIF artifact.

Each existing statement diagnostic, followed by each input notice, becomes one
result. A clean audit has `results: []`; OK statements do not create results.
Each statement diagnostic has `result.properties.statementIndex`: the existing
one-based current-file statement index. All diagnostics from one statement share
it, and indices restart for each file. It counts OK statements too, rather than
numbering results. File notices omit this property (and the result `properties`
object when empty); they receive no null/zero sentinel. A whole-file tokenizer
`PARSE_ERROR` is an existing synthetic statement with index 1 and retains that
index. This additive metadata identifies the enclosing current statement; it is
not a persistent identity across edits and supplies no diagnostic region.
For `SELECT NO_FN(1); SELECT NO_FN(1);` with a Postgres target, the two identical
file-only warnings carry indices 1 and 2 without inventing highlight locations.
`ruleId` is the existing category code (including notice codes); the used rules
are sorted by code in `tool.driver.rules`, and each result has a matching
`ruleIndex`. Error severity maps to `error`, warn to `warning`. Levels do not
change with `--fail-on error|warn|never`, and finding exits remain **1/1/0** for
an audit with errors. Configuration/path/schema/output failures remain **2**,
with stderr and no SARIF output, even under `--fail-on never`.

Locations use absolute, percent-encoded local `file:` URIs for relative or
absolute input paths. `runs[0].artifacts` inventories every audited input, including
clean, empty, skipped and error files: one `{"location":{"uri":"file:…"}}` entry
per native report `file_summaries` key, sorted lexicographically by that stored
path key. Input discovery already deduplicates paths. Each entry contains only
the location, with the same URI escaping as result locations; it adds no file
contents or regions. All-clean inputs still have empty results/rules and their
full inventory. An empty inventory renders as `[]`; an empty input directory
remains a configuration error. For a directory containing clean `a.sql`/`b.sql`,
comment-only `empty.sql` and Jinja `skipped.sql`, all four appear in artifacts
while only the two existing notices appear in results. The summary stays at
4 files, 2 statements, 2 OK, 0 flagged, 0 errors, 2 warnings and 2 skipped files,
with default exit **0**.

Only an existing, verified original diagnostic highlight
becomes the primary `physicalLocation.region`. Unknown locations stay file-only,
including binder/generator/round-trip diagnostics, tokenization failures and
file read errors. A statement's wider token span is never substituted, and
translated SQL offsets or diagnostic wording never supply source coordinates.

`run.columnKind` explicitly declares `unicodeCodePoints`. Lines and columns
are one-based; start coordinates are unchanged, and SARIF's **exclusive**
`endColumn` is the existing inclusive `column_end + 1` on `endLine`.
This applies to multiline highlights, LF/CRLF, tabs and astral Unicode characters;
the existing initial-BOM convention is retained. For
`SELECT 1; SELECT NO_FN(1); SELECT * FROM;`, the unknown function and binder
results stay file-only. The `PARSE_ERROR` highlights `FROM` at start line 1,
column 37, end line 1, exclusive column 41.

Aggregate parser errors keep a file-only primary location. Their independent
`relatedLocations` remain in parser order with IDs 1, 2, …; verified entries
get regions and unknown entries stay file-only. Duplicates remain distinct.
Descriptions are retained; null/empty descriptions show `description unavailable`.
The exporter never promotes a related highlight to an aggregate primary.

Tool metadata contains the tool name/version. Run properties contain source/target
dialects, binding mode, fail-on policy and seven numeric summary counts. Diagnostic
messages and related descriptions are capped individually at **4,096 Unicode
code points**, with a final `…` when truncated. Other report artifacts retain
full messages. JSON escapes quotes, backslashes, controls and non-ASCII characters.
No extra source/translated SQL snippets, artifact contents, command
lines, environment or timestamps are embedded; existing engine/parser messages
can themselves contain SQL literals. Rendering uses the audit's evidence and
performs no extra input reads. Review logs before sharing them. Absolute URIs
are local-machine paths; no hosted CI upload or particular editor integration
is claimed. The tests validate offline against the retained
[official OASIS schema](../tests/dialect_audit/schemas/README.md).

## Committed baselines and CI diffs

Save an audit snapshot explicitly, review it, and commit the JSON alongside
compiled SQL. The checkout includes `examples/dialect_audit/snowflake.baseline.json`, generated
with the originally pinned dependencies (SQLGlot 28.0.0, DuckDB 1.4.1, PyYAML
6.0.3) and the same fixture as the ordinary run example. Run these commands
from the datawrench checkout root, because baselines store paths relative to
`--root` (default: the current directory):

```sh
# Create a new baseline (exits 0 even when the audit contains findings).
datawrench dialect-audit baseline examples/dialect_audit/snowflake \
  --read snowflake --write duckdb --schema examples/dialect_audit/schema.sql \
  --output audit-baseline.json

# Read-only comparison. With the versions recorded in the committed example,
# it exits 0: no changes. Other SQLGlot/DuckDB/PyYAML/datawrench versions report
# tool_versions_changed (and possibly message drift) and exit 1.
datawrench dialect-audit diff examples/dialect_audit/snowflake \
  --read snowflake --write duckdb --schema examples/dialect_audit/schema.sql \
  --baseline examples/dialect_audit/snowflake.baseline.json

# After reviewing a diff, explicitly accept the current snapshot.
datawrench dialect-audit baseline examples/dialect_audit/snowflake \
  --read snowflake --write duckdb --schema examples/dialect_audit/schema.sql \
  --output audit-baseline.json --update
```

`baseline` creates a file exclusively; an existing valid baseline requires
`--update`. Replacements use an atomic rename. SQL inputs, schema fixtures,
symlink outputs and unrelated/invalid existing files cannot be overwritten.
Remove an invalid baseline explicitly before recreating it. `run` and `diff`
never update any baseline. Put the baseline outside scanned SQL inputs, and use
`.json` as its extension. Configuration or I/O errors exit **2** and do not
accept a snapshot. Baselines are limited to 64 MiB of UTF-8 JSON.

`diff` exits **0** for no changes, **1** for any drift (including resolved
findings), and **2** for invalid/missing baselines or audit configuration/input
errors. It supports `--format text` (default) or `--format json`. This drift
policy is separate from `run --fail-on`: existing acknowledged errors do not
cause a diff failure, but remain in the baseline. Use an ordinary `run` as an
additional CI step if all current errors must fail the build.

For example, start with `sql/query.sql` containing `SELECT 1;` and save a
baseline. Change it to `SELECT MISSING_AUDIT_FN(1);`. The diff exits 1 and shows
`NEW sql/query.sql#1 L1-1 C1-26 ANONYMOUS_FUNCTION`, a new `BIND_ERROR`,
`SQL_CHANGED` with both generated SQL strings, and `BINDING_CHANGED` from
`bound` to `failed`. After accepting that state, restoring `SELECT 1;` shows
both findings as `RESOLVED`. An unchanged input/version produces no differences,
even when it has existing warnings or failed bindings.

Baseline format version 1 records project-relative file names, statement
indices and line/column ranges, source/target SQL, diagnostics, functions, inferred
stubs, binding statuses, skipped-input notices, audit options, schema SHA-256,
and the actual tool versions under `tool_versions` (keys `dialect-audit`, which
now records the datawrench version, `sqlglot`, `duckdb` and `PyYAML`). Diff format version
1 separates `new_findings`, `resolved_findings`, `sql_changes`,
`binding_changes`, `source_changes`, `analysis_changes` (functions/stubs), file
and statement additions/removals, `statement_order_changes`,
`configuration_changes`, and `version_changes`.
`reasons` provides corresponding machine-readable reason names; a tool upgrade
always has its own `tool_versions_changed` reason, including when target SQL
also changes. Finding identity includes category, severity and diagnostic text;
a changed message appears as a resolved finding plus a new one. Engine/platform
wording changes can therefore produce drift. No timestamps are stored.

Both commands use `--root` (default: current directory) to normalize input and
schema paths. All must be inside that root. Invocation paths still resolve
relative to the current directory; `--root` does not change directories. This
lets identical checkouts at different absolute paths compare equally. Input
argument ordering and absolute/relative spelling do not affect the snapshot.
Within each relative file, statements are paired by **exact source SQL** first,
using the stored token-span text as a fingerprint (no case, whitespace or comment
normalization). Duplicate sources pair in ascending index order, once per
occurrence. Remaining statements pair at equal one-based indices (the existing
fallback), then a conservative **anchor-gap** rule can pair a shifted edit.
Matching never crosses files and needs no new baseline fields.

An anchor gap lies strictly between two consecutive exact-source matches in
baseline order. Each anchor's source must occur **once in each snapshot**, and
no other exact match may cross either boundary: every earlier baseline match
must remain earlier in current order, and every later match must remain later.
This also makes the anchors consecutive in current order. Duplicate matches
cannot anchor a gap and are never skipped to form a wider gap. File edges are
not anchors. The gap must contain exactly **one unmatched old and one unmatched
new statement**, counted after exact matching but **before** same-index fallback.
Pair them only if both survive that fallback. Multiple candidates cannot become
eligible merely because fallback consumed some of them. Separate stable gaps
can pair independently, including when a reorder occurs elsewhere in the file.

For example, baseline `SELECT 1; SELECT 2; SELECT 3;` compared with
`SELECT 0; SELECT 1; SELECT 20; SELECT 3;` reports only `SELECT 0` as added at
current #1. It pairs baseline #2 with current #3, reporting source and target SQL
changes from `SELECT 2` to `SELECT 20`, with `baseline_location` retaining
`L1-1 C11-18` and the current location `L1-1 C21-29`. The two unchanged anchors
stay quiet. Reversing the comparison reports the removal and the reverse edit.

The new rule cannot infer shifted edits in unbounded gaps, gaps with multiple
candidates, duplicate anchors, or boundaries crossed by relative reordering.
Such unmatched statements remain explicit additions/removals, unless the
existing same-index fallback pairs them. No SQL similarity, target equivalence,
semantic or fuzzy matching is used. A uniquely bounded singleton is a
conservative positional pairing, not proof that the author edited that statement.

For example, baseline `SELECT 1; SELECT NO_FN(1); SELECT 3;`, then insert
`SELECT 0;` at the start. The diff exits **1** and reports only
`STATEMENT_ADDED ...#1`; unchanged statements have no SQL, finding, binding or
source-change cascade. Their shifted indices and line/column positions are
incidental and ignored. Deleting the inserted statement reports only its removal.
Matching source still compares target SQL, diagnostics, binding, functions and
stubs, so configuration or tool changes can produce real drift at current
locations. Resolved findings keep their baseline locations.

Reordering exact matches produces one file-level `statement_order_changes`
entry and a `statement_order_changed` reason, rendered as
`STATEMENT_ORDER_CHANGED` in text. `baseline_indices` lists matched baseline
statement identities in original order; `current_order` lists those same
baseline indices in current order. Pure insertions/deletions do not change
relative order. Sources whose occurrence counts differ are excluded from this
order check: identical copies have ambiguous identities, so movement involving
those copies cannot be inferred reliably. Added/removed copies still retain
counts and findings; their reported occurrence follows the source-order pairing
policy, which may differ from the position actually edited.

Each order entry also includes `matched_statements`, in current statement order.
Each item contains the exact stored `source_sql`, `baseline_location`, and
`current_location` (file, index, line and column ranges). These are the same
exact-source pairs used by the order check, including eligible unmoved pairs;
sources with differing occurrence counts remain excluded. Both numeric arrays
still refer to baseline identities, while `current_location.index` is the actual
current statement index, which may also shift because of additions/removals.
This is additive diff-v1 context; baseline files need no update.

For baseline `SELECT 1; SELECT 2; SELECT 3;` and current
`SELECT 3; SELECT 1; SELECT 2;`, text retains the numeric heading and adds:

```text
STATEMENT_ORDER_CHANGED query.sql: [1, 2, 3] -> [3, 1, 2]
  matched query.sql#1 L1-1 C1-8 (baseline: query.sql#3 L1-1 C21-28)
    source: "SELECT 3"
  matched query.sql#2 L1-1 C11-18 (baseline: query.sql#1 L1-1 C1-8)
    source: "SELECT 1"
  matched query.sql#3 L1-1 C21-28 (baseline: query.sql#2 L1-1 C11-18)
    source: "SELECT 2"
```

Every matched heading shows both stored locations; missing legacy columns remain
`(columns unavailable)` on that side. The source payload is ASCII JSON-escaped
on one physical line, including multiline SQL, Unicode, quotes and backslashes.
Decoding it recovers the exact matched token-span source, never SQL reconstructed
from numeric identities or generated target SQL. Comparison/rendering uses the
snapshots without rereading source files. No relative reorder means no order
entry or matched context. Reasons, matching priorities and diff exits are unchanged.

Paired edits retain current locations plus `baseline_location` for source,
generated SQL, binding and analysis changes. Their text headings append
` (baseline: FILE#INDEX Lstart-end Cstart-end)` when any stored location field
differs (file, index, lines or columns), including a location move at the same
index. For the shifted edit above, both `SOURCE_CHANGED` and `SQL_CHANGED` show:

```text
SQL_CHANGED query.sql#3 L1-1 C21-29 (baseline: query.sql#2 L1-1 C11-18)
  before: "SELECT 2"
  after: "SELECT 20"
  source before: "SELECT 2"
  source after: "SELECT 20"
```

Equal locations keep the original heading with no suffix. Unknown columns use
`Lstart-end (columns unavailable)` on the corresponding side, including a legacy
baseline; current columns never substitute for missing old columns. Location
metadata alone still does not create drift, so an otherwise unchanged legacy
snapshot produces no paired heading. The existing current heading and before/after
payloads are retained. Statement additions/removals show their current/baseline
location respectively, without a paired suffix; file-level entries stay file-only.
Statement pairing is unchanged.

Generated-SQL-change entries also retain `source_sql: {"before": "…",
"after": "…"}` in diff v1, copied from the actual matched baseline/current
records. The existing `before` and `after` fields remain generated target SQL
strings, including `null` when translation is unavailable. Text appends exact
source context after those target lines. For unchanged source
`SELECT 1001::INT AS amount;`, create a Postgres baseline and compare a MySQL
target:

```sh
datawrench dialect-audit baseline query.sql --read snowflake --write postgres --output baseline.json
datawrench dialect-audit diff query.sql --read snowflake --write mysql --baseline baseline.json
```

The diff exits **1** with `configuration_changed, target_sql_changed` and:

```text
SQL_CHANGED query.sql#1 L1-1 C1-26
  before: "SELECT CAST(1001 AS INT) AS amount"
  after: "SELECT CAST(1001 AS SIGNED) AS amount"
  source: "SELECT 1001::INT AS amount"
```

Both targets remain `static-only`. Equal sources get one `source:` line;
different sources paired by an existing edit fallback get `source before:` then
`source after:`. Each payload is an ASCII JSON string on one physical line,
preserving exact stored whitespace, comments, LF/CRLF, Unicode, quotes and
backslashes when decoded. Sources come from both selected records without
rereads or reconstruction from generated casts or numeric indices. Actual
baseline/current indices and spans, including unavailable legacy columns, remain
separate. No generated SQL change means no SQL-change source context. This
additive `sql_changes[].source_sql` field requires no baseline rewrite and changes
no matching, findings, reasons or exit policy.

Binding-change entries additionally retain `source_sql: {"before": "…",
"after": "…"}` in diff v1, copied from the actual matched baseline and current
statement records. The existing `before` and `after` fields remain binding-status
strings. Text appends exact source context after those status lines. For baseline
and current `SELECT 1001 AS amount;`, changing `--write postgres` to
`--write duckdb` exits **1** with `configuration_changed, binding_changed` and:

```text
BINDING_CHANGED query.sql#1 L1-1 C1-21
  before: "static-only"
  after: "bound"
  source: "SELECT 1001 AS amount"
```

Equal sources get one `source:` line. If an existing edit fallback pairs different
sources, text retains both as `source before:` and `source after:` lines, in that
order. Every source payload is an ASCII JSON string on one physical line;
decoding recovers exact stored whitespace, comments, Unicode, quotes and
backslashes. Source comes from the paired records, without input rereads or
reconstruction from indices or generated SQL. Locations retain each side's
actual indices/spans and unavailable legacy columns. No binding change means
no binding source context. This additive diff-v1 field requires no baseline
rewrite and changes no matching, findings, reasons or exit policy.

Analysis-change entries also retain `source_sql: {"before": "…", "after": "…"}`
in diff v1, copied from the actual matched baseline/current records. Existing
`before` and `after` objects still contain only `functions` and `auto_stubs`.
For unchanged `query.sql` containing `SELECT t.amount FROM sales AS t;`, create
an inferred-stub baseline, then compare with an equivalent provided schema:

```sh
datawrench dialect-audit baseline query.sql --read snowflake --write duckdb --auto-stub --output baseline.json
# schema.sql: CREATE TABLE sales (amount DOUBLE);
datawrench dialect-audit diff query.sql --read snowflake --write duckdb --auto-stub --schema schema.sql --baseline baseline.json
```

The diff exits **1** with `configuration_changed, analysis_changed`, retains a
`bound` outcome, and shows:

```text
ANALYSIS_CHANGED query.sql#1 L1-1 C1-31
  before: {"functions": {}, "auto_stubs": [{"columns": ["amount"], "table": "main.sales", "type": "DOUBLE"}]}
  after: {"functions": {}, "auto_stubs": []}
  source: "SELECT t.amount FROM sales AS t"
```

Equal sources get one `source:` line after the analysis payloads. Different
sources paired by an existing fallback get `source before:` then `source after:`.
Each payload is an ASCII JSON string on one physical line; decoding retains the
exact stored token-span whitespace, comments, LF/CRLF, Unicode, quotes and
backslashes. Comparison/rendering uses both stored records without rereads or
reconstruction from functions, stubs, generated SQL or numeric identities. Both
locations keep actual indices/spans and legacy unavailable columns. No analysis
drift means no analysis entry or source context. This additive
`analysis_changes[].source_sql` field requires no baseline rewrite and changes
no matching, findings, reason ordering or exit policy.

Text statement additions/removals put `source:` then `SQL:` on two indented
lines immediately after the location/binding heading. `source:` is the exact
stored token-span `source_sql`, from the current statement for additions and
the baseline statement for removals. `SQL:` retains the generated target SQL,
including `null` when no translation exists. For example, adding
`SELECT 4::INT; SELECT * FROM;` after `SELECT 1; SELECT 2; SELECT 3;` shows:

```text
STATEMENT_ADDED query.sql#4 L1-1 C31-43 (binding: bound)
  source: "SELECT 4::INT"
  SQL: "SELECT CAST(4 AS INT)"
STATEMENT_ADDED query.sql#5 L1-1 C46-58 (binding: not-attempted)
  source: "SELECT * FROM"
  SQL: null
```

The parse finding still follows these statement entries. Reversing the diff
shows `STATEMENT_REMOVED` with the stored baseline source, SQL and location.
Each payload uses JSON escaping on one physical line: quotes, backslashes,
tabs and LF/CR are escaped, and non-ASCII characters use `\u` escapes
(surrogate pairs for astral characters). Decoding the source string recovers
its exact stored whitespace, comments and Unicode; it is not reconstructed
from generated SQL. Paired changes keep their existing before/after payloads.

New and resolved **statement findings** also carry an additive diff-v1
`source_sql` string: the exact enclosing current record for `new_findings`,
and the exact stored baseline record for `resolved_findings`. Text appends
`  source: "…"` after the existing diagnostic message and any related details.
Each source payload is one physical ASCII JSON line with the escaping described
above. File-level notices omit this field and source line entirely.

For a finding-only example, keep `query.sql` unchanged as
`SELECT t.amount + 1 FROM sales AS t;`. Save a baseline with `schema.sql`
containing `CREATE TABLE sales (other DOUBLE);`, then replace the schema with
`CREATE TABLE sales (amount VARCHAR);`:

```sh
datawrench dialect-audit baseline query.sql --read snowflake --write duckdb --schema schema.sql --output baseline.json
# Change schema.sql as described above, leaving query.sql unchanged.
datawrench dialect-audit diff query.sql --read snowflake --write duckdb --schema schema.sql --baseline baseline.json
```

The baseline exits **0**. The diff exits **1**, retains
`configuration_changed, new_findings, resolved_findings` and a `failed` binding
outcome. Both `NEW query.sql#1 L1-1 C1-35 BIND_ERROR` and
`RESOLVED query.sql#1 L1-1 C1-35 BIND_ERROR` retain their respective messages
and null primary diagnostic locations, then each adds:

```text
  source: "SELECT t.amount + 1 FROM sales AS t"
```

There is no statement, order, source, generated SQL, binding or analysis change
in this example. Source context is copied from each actual enclosing record,
even for changed/shifted sources paired by an existing fallback or unpaired
added/removed statements. It is never recovered from generated EXPLAIN snippets
in engine messages, target SQL, numeric identities or reread files. Unchanged
findings produce no finding entry or source line. Finding identity, duplicate
counts/order, diagnostic evidence, matching, reasons and exits are unchanged;
existing baselines need no rewrite.

New findings keep current locations;
resolved findings keep baseline statement and diagnostic locations. Same-index
source text or line-range changes are still reported even if target
SQL is identical. Column-only moves also produce `source_changed` when both
snapshots have known columns. All statement
diff locations carry both column fields, including old locations for resolved
findings. File-level notices have `index`, `line_start`, `line_end`,
`column_start` and `column_end` set to `null` in diffs.

Column fields are an additive extension to report/baseline/diff format v1. Older
v1 baselines without columns remain readable and comparable: missing columns
mean unknown, and gaining location metadata alone does not cause drift. Diffs
against them still compare source SQL and lines, but cannot detect column-only
moves or recover columns for resolved findings. Explicitly run `baseline
--update` to record columns. The loader rejects partial, nonpositive, ill-typed
or same-line reversed column pairs; unknown endpoints must both be `null`.
Older releases cannot read the new baseline fields, so update the CLI before
accepting a new snapshot.

A matching baseline **does not verify compatibility**: non-DuckDB targets keep
`static-only`, rejected queries keep `not-attempted`, failed EXPLAIN checks keep
`failed`, and successful binding with auto-stubs retains the inferred,
unverified DOUBLE types. JSON diffs include `current_binding_statuses`; text
prints the same counts. `bound` retains the limits described below. Review
baseline SQL and diagnostics for sensitive literals before committing them.

A minimal GitHub Actions example (after checkout and setup of Python 3.11),
using the checked-in example baseline:

```yaml
steps:
  - run: pipx install 'datawrench[sqlglot,duckdb]'
  - name: Compare SQL audit with the reviewed baseline
    run: |
      datawrench dialect-audit diff examples/dialect_audit/snowflake \
        --read snowflake --write duckdb --schema examples/dialect_audit/schema.sql \
        --baseline examples/dialect_audit/snowflake.baseline.json --format json > audit-diff.json
```

The comparison step fails on exit 1 or 2. For a real project, replace the SQL,
schema and baseline paths with your committed inputs. Baseline updates belong
in an explicit review change, never in the comparison job.

## Schema fixtures and auto-stubs

DDL fixtures default to the source dialect. Use `--schema-read` when they use a
different SQLGlot dialect. For example `CREATE TABLE sales.orders (id INT64)`
can be read with `--schema-read bigquery` and converted to DuckDB types.

Only `CREATE TABLE name (column type, ...)` is accepted. The tool extracts and
validates column types, quotes identifiers, and builds its own empty DuckDB
tables. It does **not** execute fixture SQL directly. CTAS, INSERT, defaults,
constraints, table properties, replacement/IF NOT EXISTS, views and UDTs are
rejected with schema path and statement context. Supported types include
ordinary integers, floating point, DECIMAL, VARCHAR/TEXT, binary, date/time,
INTERVAL, UUID, JSON and arrays; nested STRUCT/MAP, ENUM and other exotic types
are rejected. No data is loaded.

The YAML equivalent is a direct mapping, with SQL identifier syntax for
qualified/quoted table names and literal column names:

```yaml
sales.orders:
  id: INTEGER
  amount: DECIMAL(12, 2)
  created_at: TIMESTAMP
'"table with spaces"':
  value: VARCHAR
```

YAML uses a restricted safe loader: aliases, custom objects, duplicate keys,
non-string keys/types and empty schemas are rejected. Identifiers follow
DuckDB's ASCII case-insensitive rules. Three-part `catalog.schema.table` names
are explicitly unsupported; use `schema.table` fixtures.

`--auto-stub` creates a missing table **only** when qualified column references
supply its columns. For `SELECT o.amount FROM sales.orders o`, it creates
`sales.orders(amount DOUBLE)` in a transaction for that statement. CTEs and
derived aliases are scope-resolved; known schema tables are never expanded.
Stubs are rolled back before the next statement. Unqualified `amount`, `*`
and `o.*` alone cannot supply stub columns. All assumptions appear in
`auto_stubs` and text/Markdown reports; an `OK` with stubs is conditional on
those unverified DOUBLE types. Provide a real fixture for reliable type checks.

## Binding and output boundaries

Queries are passed to **EXPLAIN**, never EXPLAIN ANALYZE or a direct query run.
Only read-only query ASTs are checked; DDL, DML, COPY, ATTACH, PRAGMA, SELECT INTO,
and locking clauses produce explicit unsupported/fallback findings. They are
not executed. Audited DDL does not change the fixture for later statements.
SQLGlot generation/round-trip checks remain available before this restriction.

Each run uses a fresh in-memory DuckDB connection with external access,
persistent secrets, Python replacement scans, extension installation/loading,
and disk spilling disabled. Configuration is locked; no warehouse connection,
credential loading or external workflow is involved. The binder uses one
DuckDB thread, a 128 MB engine memory limit, expression-depth limit 200, and a
3-second interrupt timer per EXPLAIN. Planning/optimization can evaluate
constant expressions: these are engine capability limits, **not an OS sandbox
or a hard process-memory/CPU guarantee**. This local CLI is not intended as a
multi-tenant endpoint for hostile SQL. See DuckDB's
[security model](https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview).

`--emit-dir DIR` writes generated statements to flat hashed filenames plus a
`report.json` mapping them back to source files/indices. A matching output
filename or symlink is an error; use a fresh output directory for each run.
The root cannot be a symlink. Exclusive, directory-relative file creation
prevents generated paths and output symlinks from escaping the selected
directory. Outputs may include flagged translations: **inspect diagnostics
before running emitted SQL**. Opaque commands and parse failures have no
emitted SQL. Reports contain source SQL and engine messages; treat their
contents as sensitive if your SQL contains sensitive literals.

## Reproducible comparison and tests

From a datawrench checkout:

```sh
uv run --extra test pytest tests/dialect_audit -q
datawrench dialect-audit run examples/dialect_audit/snowflake --read snowflake --write duckdb \
  --schema examples/dialect_audit/schema.sql --format md; test $? -eq 1
uv run python tests/dialect_audit/compare_plain_transpile.py
uv run python tests/dialect_audit/compare_plain_transpile.py --json
```

The replay uses eight original SQL fixtures and the same source/target dialects
for both approaches. Plain `sqlglot.transpile` uses defaults, counting **both
exceptions and logged warnings**, including Command fallback warnings. This
avoids hiding warnings to inflate the difference. Audit counts include the
empty-schema DuckDB check. The script checks fixture expectations and fails
on version/corpus drift. With the originally pinned versions (SQLGlot 28.0.0,
DuckDB 1.4.1):

| Category | Plain signals | Audit findings |
| --- | ---: | ---: |
| ANONYMOUS_FUNCTION | 0 | 1 |
| BIND_ERROR | 0 | 3 |
| COMMAND_FALLBACK | 1 | 1 |
| PARSE_ERROR | 1 | 1 |
| ROUNDTRIP_UNSTABLE | 0 | 1 |
| UNKNOWN_TABLE | 0 | 1 |
| UNSUPPORTED | 1 | 1 |

Plain flags **3/8 statements**; audit flags **7/8**, with one clean control.
These are detection counts on a deliberately problematic corpus, not a claim
about real-world prevalence, runtime performance or semantic correctness.
The round-trip fixture is a valid date-add whose interval quoting changes:
round-trip instability can be cosmetic and is deliberately only a warning.
Tests also cover real CLI exit codes, multi-statement lines, YAML/DDL mapping,
aliases/CTEs, output formats, offline restrictions and output-path protections.

## Scope and upstream relationship

This is a batch audit layer around SQLGlot and DuckDB, not a SQL engine or a
semantic equivalence prover. It does not render dbt/Jinja, implement UDFs,
rewrite findings automatically, run warehouse commands, or verify non-DuckDB
engines. Procedural scripts, custom delimiters, macros and cross-statement
session state are outside this version. Empty-table EXPLAIN cannot detect
runtime/data-dependent failures, prove results match, or validate permissions
on real warehouses. Unknown functions can be valid native/UDF calls; SQLGlot's
known-function set is not a target-engine function catalog.

Versions were originally pinned to the tested baseline (SQLGlot 28.0.0,
DuckDB 1.4.1, PyYAML 6.0.3), rather than a claim to use the newest releases.
Updating them requires rerunning the category corpus and comparison, and
regenerating any committed baselines.
SQLGlot's lenient parser can still accept invalid SQL; DuckDB binding adds a
second check for its query subset. `OK` always means the available checks
passed, not that migration is proved safe.

Part of datawrench, licensed under Apache-2.0 ([LICENSE](../LICENSE), [NOTICE](../NOTICE)). This command was first released as a standalone MIT-licensed project; that notice is kept in [`licenses/MIT-original-tools.txt`](../licenses/MIT-original-tools.txt). Dependency notices: [THIRD_PARTY.md](../THIRD_PARTY.md) and
[licenses/dialect-audit/](../licenses/dialect-audit/).
