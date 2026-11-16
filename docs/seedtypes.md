# datawrench seedtypes

`datawrench seedtypes` (formerly `seedtypes`) **pins CSV types only when every
decoded field round-trips exactly.** It scans the entire file, reports the first
rejecting physical line for each candidate type, compares real DuckDB and Polars
default readers, and emits executable pinned readers or dbt seed properties.

For example, `007` stays a string; `1.10` becomes a fixed-scale decimal;
`1.10` mixed with `1.1` stays a string. No floating-point type is inferred.

## Install

Python 3.11+ on Linux. This command needs DuckDB, Polars, SQLGlot, PyYAML and
fsspec (DuckDB uses fsspec to read the in-memory CSV batches seedtypes hands it):

```sh
pipx install 'datawrench[duckdb,polars,sqlglot]'   # or: pip install 'datawrench[duckdb,polars,sqlglot]'
datawrench seedtypes --version                     # prints the bare datawrench version, e.g. 0.1.0
datawrench seedtypes --help
```

`python -m datawrench seedtypes` is equivalent to `datawrench seedtypes`.
The original standalone tool was tested with DuckDB 1.4.2, Polars 1.35.2,
SQLGlot 28.0.0, PyYAML 6.0.3 and fsspec 2025.10.0. Generated recipes, adapter type
spellings and default-reader comparisons reflect the installed library versions;
they are tested versions, not a claim of compatibility with every release.

The subcommands are `infer`, `compare`, `emit`, `validate-types` and `check`.
Errors are printed as `datawrench seedtypes: <message>` and exit 2. Command
examples below run from the datawrench checkout root, where the synthetic inputs
live under `examples/seedtypes/`.

## Use

```sh
datawrench seedtypes infer examples/seedtypes/seeds --format json
datawrench seedtypes infer examples/seedtypes/seeds/zip_codes.csv --show-examples
datawrench seedtypes infer examples/seedtypes/seeds/ledger.csv --null-token NULL --null-token ''
datawrench seedtypes compare examples/seedtypes/seeds/zip_codes.csv
datawrench seedtypes emit examples/seedtypes/seeds/zip_codes.csv --target dbt --adapter snowflake
datawrench seedtypes emit examples/seedtypes/seeds/ledger.csv --target duckdb > ledger.sql
datawrench seedtypes emit examples/seedtypes/seeds/ledger.csv --target polars > load_ledger.py
datawrench seedtypes emit examples/seedtypes/seeds --target json
datawrench seedtypes check examples/seedtypes/dbt_project
```

The final command deliberately exits **1**: the example project's zip column
has no explicit type and Polars defaults strip its leading zeros. To fix a real
project, review emitted YAML, save it under a configured seed directory, then
rerun `check`. For existing dbt seed tables, dbt documents that changes to
`column_types` require `dbt seed --full-refresh`.

The Snowflake output for the zip example is:

```yaml
version: 2
seeds:
- name: zip_codes
  config:
    delimiter: ','
    column_types:
      zip: varchar
      city: varchar
```

All commands write only to stdout/stderr. Redirections above are explicit shell
writes. seedtypes never executes an emitted recipe, invokes dbt, changes a
warehouse or evaluates project macros. The Polars snippet defines typed `df`
and a string `rendered` projection for checking decoded-field equality. Decimal
and timestamp fields are read with string overrides then converted strictly,
because Polars' direct CSV timestamp parsing does not accept every supported
canonical spelling. SQL/Python output contains an escaped absolute local path;
regenerate after moving or changing a file. Polars output accepts one file;
other targets accept files and recursively scanned directories.

### Explicit separators

`infer`, `compare`, `emit` and `validate-types` accept `--delimiter comma|tab|semicolon`.
The default remains `comma`. Only UTF-8 (optional BOM) is accepted; neither
contents nor file extensions select a separator, and no encoding conversion
is performed. Files may end in `.csv` or `.tsv`; directory scans include both
extensions and apply the single selected delimiter to every file. Use separate
commands for directories containing different dialects.

```sh
datawrench seedtypes infer examples/seedtypes/delimited/ledger.tsv --delimiter tab --format json
datawrench seedtypes emit examples/seedtypes/delimited/ledger.tsv --delimiter tab --target polars > load_tsv.py
datawrench seedtypes compare examples/seedtypes/delimited/ledger-semicolon.csv --delimiter semicolon
datawrench seedtypes emit examples/seedtypes/delimited/ledger-semicolon.csv --delimiter semicolon --target duckdb > load_semicolon.sql
```

These two inputs and `examples/seedtypes/delimited/ledger.csv` encode the same two
records: `zip` infers VARCHAR, `amount` DECIMAL(3,2), and `note` VARCHAR. The
quoted note contains a comma, semicolon, tab, double quote and newline; the
leading-zero zip rejects BIGINT on original physical line 4. Generated readers
pin the selected separator and preserve the same decoded fields. DuckDB 1.4.2
forbids separators inside its CSV null-token option; for such custom tokens,
the emitted SQL reads strings and applies the null policy before typed casts.

JSON reports include `delimiter` as the actual character (`","`, `"\t"`, or
`";"`). Python `infer`, `compare` and `CSVSource` accept the keyword
`delimiter=','`, `delimiter='\t'` or `delimiter=';'`; other values fail explicitly.
Malformed quoted records and ragged rows report their original record-start
line, including after multiline fields, exit 2 and produce no partial report.
Unsupported CLI separators also exit 2. Static dbt `check` reads per-seed YAML
delimiters; dbt emission pins the selected separator in `config.delimiter`.
Both dbt paths require `.csv` filenames, including tab-separated seeds. For dbt
output from a `.tsv` source, explicitly copy it to a `.csv` filename and select
`--delimiter tab`; other targets accept either extension.

## Exact rule and reports

The source file is read **once** during `infer`. Python's strict CSV parser
tracks physical record start lines; bounded batches are re-encoded in memory
and passed to DuckDB with every input column explicitly `VARCHAR` and sniffing
disabled. DuckDB computes `TRY_CAST(TRY_CAST(value AS T) AS VARCHAR)` for each
remaining candidate. A rejection is permanent; the whole source is still
parsed and validated. This avoids both a sampled schema and an in-memory copy
of the full seed. CSV syntax errors are reported by the source parser with the
original filename and line, before DuckDB receives normalized batches.

Candidates, in preference order:

| Type | Accepted spelling / boundary |
| --- | --- |
| BOOLEAN | `true`, `false`, lowercase only |
| BIGINT | Canonical signed 64-bit integer; no leading `+`, redundant zeros or negative zero |
| DECIMAL(p,s) | Canonical fixed-point text, identical scale throughout the column, precision at most 38; integers beyond BIGINT can use DECIMAL |
| DATE | Valid `YYYY-MM-DD`, years 0001–9999 |
| TIMESTAMP | Valid `YYYY-MM-DD HH:MM:SS[.ffffff]`, no timezone, at most microseconds, no redundant trailing fractional zeros |
| VARCHAR | Universal fallback; also used for header-only and all-null columns |

The cast-back text must match **exactly**, including scale, whitespace and signs.
Some DuckDB decimal types render `.10` instead of `0.10`; the emitted precision
leaves an integer position for the zero. Scale 38 consequently falls back to
VARCHAR: `0.` followed by exactly 38 fractional digits needs precision 39 to
preserve its integer zero, beyond the maximum precision 38. The DECIMAL
rejection code is `decimal_integer_zero`, including negative fractions and an
all-zero fraction. Scale 37 can fit as DECIMAL(38,37). Mixed dates/timestamps,
scientific notation, noncanonical booleans,
invalid dates and values exceeding precision 38 also remain VARCHAR.

Canonical negative zero (`-0`, `-0.0`, `-0.00`, and other all-zero fractions
through scale 37) remains VARCHAR because DECIMAL casting removes its minus
sign. Its rejection code is `decimal_negative_zero`, with a static reason
explaining that sign loss. Positive `0.00` and nonzero `-0.10` remain eligible
for DECIMAL. Designated null tokens are excluded; an earlier rejection keeps
its reason and physical line. At scale 38, negative zero still reports
`decimal_integer_zero`; scale 39 reports `decimal_precision`.

Canonical integer `-0` also rejects BIGINT with code `integer_negative_zero`
and the static reason `casting negative zero to BIGINT removes the minus sign`.
It stays VARCHAR; `--show-examples` shows the actual `-0` → `0` cast-back pair.
Positive `0` and negative nonzero integers within signed 64-bit range remain
BIGINT. Designated `-0` nulls are excluded; earlier leading-zero, range or other
lexical failures keep their first reason and physical line.

“Lossless” means equality of decoded CSV field text, not CSV quoting, BOM,
line-ending bytes or output serialization chosen by a warehouse. Quoted `"123"`
and unquoted `123` are the same field. Empty strings are preserved by default;
no token is NULL unless explicitly designated with repeatable `--null-token`.
Designated nulls are excluded from round-trip checks, including quoted tokens.
When the empty string is designated null, emitted Polars readers also treat
unquoted empty fields as null before strict decimal/timestamp conversion.
Custom null policies cannot be represented by dbt properties and cause dbt
emission to fail explicitly.

`infer --format json` and `emit --target json` return an array of file reports:
`path`, `rows`, `delimiter`, `null_tokens`, `notices`, and ordered `columns`. Each column has
`name`, selected `type`, `non_null`, `nulls`, and `rejected`. Each rejection
contains its first physical `line`, a machine-readable `code` and a static
`reason`. For example, BIGINT and DECIMAL reject `007` with `leading_zeros`;
DECIMAL reports `decimal_precision` above precision/scale 38 and
`decimal_mixed_scales` for incompatible scales. A date followed by ordinary
text rejects DATE at the text's line with `date_notation`; an impossible
calendar date reports `invalid_calendar`. VARCHAR means every candidate was
rejected (or there were no non-null values).

Reports omit raw and rendered field evidence by default, including nested
`compare` / `check` JSON and `emit --target json`. **`--show-examples` explicitly
enables sensitive evidence:** at most one `value` / `rendered` pair per rejected
candidate (five pairs per column), plus three mismatch examples per column per
default reader for comparison. Each value is truncated to 160 characters.
Limits apply per column/file, not across a directory of files. Line numbers
remain exact, including multiline quoted fields. Default reader exception text
can quote whole CSV rows, so it is always replaced with an engine/exception-class
summary, even with examples enabled. Paths, headers and explicitly configured
null tokens remain report metadata; this is field-evidence suppression, not
anonymization of metadata. Reader recipes still contain paths and null policies.

The Python APIs use the same default: `infer(path, show_examples=True)`,
`compare(path, show_examples=True)` and
`check_project(path, show_examples=True)` explicitly enable evidence capture.
This changes the 0.1.0 report contract: consumers of `value` / `rendered` must
opt in. Empty files report no columns and a notice; readers/dbt output requires
a header. Header-only files emit explicit string schemas.

For a reproducible explanation fixture beyond the default readers' sample
windows (25,000 rows, decisive physical line 21,000):

```sh
mkdir -p .demo
python examples/seedtypes/make_explanation_fixture.py > .demo/explanations.csv
datawrench seedtypes infer .demo/explanations.csv --format json
```

At line 21,000, `zip` gains a leading zero, `amount` exceeds DECIMAL precision
38, and `event_date` changes from ISO dates to text. All three select VARCHAR;
their decisive rejections are respectively BIGINT/DECIMAL `leading_zeros`,
DECIMAL `decimal_precision`, and DATE `date_notation`. Both emitted readers
preserve these decoded fields. A malformed record still fails with exit 2 and
no partial inference report.

To reproduce the integer-zero boundary at physical line 21,000, including a
preceding multiline record:

```sh
python examples/seedtypes/make_decimal_zero_fixture.py > .demo/decimal_zero.csv
datawrench seedtypes infer .demo/decimal_zero.csv --null-token NULL --format json
```

The fixture has 25,000 data rows. Its first 20,997 fractions are designated
`NULL`; the first non-null fraction is `0.` plus 38 ones. `fraction` selects
VARCHAR and DECIMAL reports `decimal_integer_zero` at line 21,000, with a reason
stating that precision 39 is required. Add `--show-examples` to see DuckDB's
cast-back value, which omits the integer zero. Both emitted readers preserve
the fraction text and designated nulls. Scale above 38 or a nonzero integer
part exceeding total precision 38 still reports `decimal_precision`; an
earlier incompatible scale or lexical rejection keeps its first reason/line.

To reproduce negative-zero sign loss at physical line 21,000 after a multiline
record in another column:

```sh
python examples/seedtypes/make_negative_zero_fixture.py > .demo/negative_zero.csv
datawrench seedtypes infer .demo/negative_zero.csv --format json
datawrench seedtypes infer .demo/negative_zero.csv --format json --show-examples
```

The fixture has 25,000 rows: `amount` is `1.00` except for `-0.00` at the
decisive line. It selects VARCHAR with DECIMAL code `decimal_negative_zero`.
The default report omits field evidence; opt-in shows the actual cast-back
pair `-0.00` → `0.00`. Both emitted readers preserve the original decoded
fields, including the minus sign and multiline note.

The equivalent BIGINT fixture uses canonical `12` rows followed by `-0` at
physical line 21,000, again after a multiline note:

```sh
python examples/seedtypes/make_integer_negative_zero_fixture.py > .demo/integer_negative_zero.csv
datawrench seedtypes infer .demo/integer_negative_zero.csv --format json
datawrench seedtypes infer .demo/integer_negative_zero.csv --format json --show-examples
```

Its 25,000-row integer column selects VARCHAR with BIGINT code
`integer_negative_zero` and DECIMAL code `decimal_negative_zero` at that line.
Default reports omit field evidence; opt-in shows `-0` → `0` for both
candidates. Both emitted readers preserve all decoded fields.

## Validate requested types

Validate your explicit type decisions independently of inference or dbt:

```sh
printf 'zip,amount\n007,1.10\n' > data.csv
printf '%s\n' '{"version":1,"columns":{"zip":"BIGINT","amount":"DECIMAL(3,2)"}}' > types.json
datawrench seedtypes validate-types data.csv --types types.json --format json
```

This exits **1**: `zip` rejects BIGINT on physical line 2 with code
`leading_zeros`; `amount` passes DECIMAL(3,2). Changing the requested `zip`
type to `VARCHAR` exits **0**. `--delimiter comma|tab|semicolon`, repeatable
`--null-token TEXT` and `--show-examples` use the policies described above.
One regular local CSV/TSV file is accepted; directory scans are unsupported.

The types document is strict UTF-8 JSON, at most **65,536 bytes**, with exactly
`version` (integer `1`) and `columns` (a nonempty mapping of at most 256 exact
header names to type strings). A subset of columns is allowed. JSON files must
be regular local `.json` files without symlink ancestors. Duplicate JSON keys,
extra/missing properties, nonstandard JSON constants, unknown headers and
unsupported types are errors. Type strings must be exactly uppercase
`VARCHAR`, `BOOLEAN`, `BIGINT`, `DATE`, `TIMESTAMP` or `DECIMAL(P,S)` with
`1 <= P <= 38` and `0 <= S <= P`; decimal parameters use ordinary integers
without spaces or redundant leading zeros. Arbitrary SQL and type aliases are
rejected before any cast. Schema diagnostics do not echo rejected content.

Validation scans every record through the same bounded `CSVSource` and DuckDB
connection as inference, including records after a column first fails. It casts
to the **exact requested type** and back to text, then applies the canonical
lexical rules above. Inferred DECIMAL eligibility does not certify a narrower
requested precision or a different scale. `decimal_scale` reports a canonical
field with a different scale from the request (including rounding or added
zeros); `decimal_precision` reports a canonical, matching-scale field that does
not fit. Other failures use the existing static rejection codes, including
`leading_zeros`, `integer_negative_zero`, `integer_range`,
`decimal_negative_zero`, `date_notation`, `timestamp_notation`,
`invalid_calendar`, `cast_failed` and `round_trip`. Types with equal precision
and scale may omit the integer zero on cast-back and fail exact equality.

JSON returns one object with `version`, `path`, `rows`, `delimiter`,
`null_tokens`, `valid`, `notices` and `columns`. Requested columns appear in
CSV header order. Each has `name`, `requested_type`, `checked` (all data rows),
`non_null`, `nulls`, `valid` and `rejection` (null or the first physical
record-start `line`, static `code` and `reason`). Counts cover the entire file,
even after rejection. Text output carries the same status/counts and reasons.
No raw or rendered field values appear unless `--show-examples` is selected;
then at most one `value`/`rendered` pair per rejected column is included, each
truncated to 160 characters. Paths, header names and configured null tokens
remain metadata.

An empty file without a header is an input error. Header-only and all-null
requested columns pass vacuously, with explicit notices that the requested
type has no non-null evidence. Empty strings are values unless designated null.
Exit **0** means every requested non-null field round-trips, **1** produces a
complete report of rejected types, and **2** means a schema, option, input or
resource error with no partial stdout report, including malformed trailing CSV.
This is a decoded-field check using pinned DuckDB, not warehouse/dbt execution.
Default dbt `check` still treats explicit types as presence decisions;
`check --validate-column-types` opts into the same exact validation.

## Compare actual defaults

`compare` first infers lossless types, then executes both installed readers and
compares **every** rendered row against another raw-text pass. Header/selected-separator/quote
layout is fixed, execution uses one thread, and DuckDB's normal sample and
Polars' normal inference length and null/date policies are retained. Custom
null tokens set the expected baseline; default readers are intentionally not
reconfigured to match them. `LOSSY` reports counts and `first_mismatch_line`
without field values; `--show-examples` adds up to three examples per column.
`check` also includes this line in each lossy-column issue. `ERROR` records a default reader refusing valid input; it is never
reported as a successful comparison. Default readers can behave differently
across versions: DuckDB preserves leading zeros seen in its initial sample,
while the pinned Polars reader does not.

Reproduce a late-sample failure without network or external data:

```sh
mkdir -p .demo
python examples/seedtypes/make_late_fixture.py > .demo/late_zip.csv
datawrench seedtypes compare .demo/late_zip.csv
```

This generates 25,000 data rows. The first leading-zero value is at physical
line 21,000. Both pinned defaults load it as an integer and render `2134`;
seedtypes chooses VARCHAR. The tests also exercise a late `1.10`: the pinned
DuckDB default truncates it to `1`, while Polars fails its integer parse.

Comparison uses the real eager Polars default, so **compare and check are
limited to 64 MiB per input file** and can use more RAM than `infer`. The DuckDB
connection has a 128 MB internal memory limit, disables extension auto-loading
and downloads, and disables disk spill. That limit is not a whole-process RAM
cap. `infer` has no file-size limit and retains at most 1,024 rows / 4 MiB of
field bytes per source batch (plus parser/engine buffers).

## Static dbt checks and dialects

`check PROJECT_DIR` reads `dbt_project.yml`, configured `seed-paths` (default
`seeds`), and `.yml` / `.yaml` seed properties underneath them. It recognizes
inherited `+column_types` / `column_types` at root, project, directory and seed
levels, plus seed properties `config.column_types`. Properties take precedence;
column names must match the CSV header exactly. It exits 1 for unconfigured
columns whose default readers lose text. If a default reader fails, every
unconfigured column is conservatively reported as a risk.

`check` also resolves `+delimiter` / `delimiter` at each of those project levels,
then applies a seed's properties `config.delimiter` override. Values are literal
characters: `","`, `";"` or `"\t"` (a YAML double-quoted escape for a tab),
not the CLI names `comma`, `semicolon` or `tab`. Missing values inherit, with
comma as the default. Explicit nulls, other separators, multi-character strings
and non-string values fail with exit 2 and no partial report. Each encountered
setting is validated even if a deeper setting overrides it; when both spellings
are present, both must be supported and the `+` spelling wins. Delimiter and
column-type inheritance are independent. Nested comparison JSON records the
actual separator under `comparison.inference.delimiter`.

Invalid delimiter diagnostics name the originating YAML file and configuration
path, including the offending key. For example, an invalid inherited setting
reports `/project/dbt_project.yml: seeds['example']['nested']['+delimiter']`,
while a properties setting reports `/project/seeds/properties.yaml: seed 'codes',
seeds[1].config['delimiter']` (the list index is zero-based). Both retain the
supported-delimiter guidance without echoing the rejected value or CSV field
data. Root settings are checked even with no seeds; all loaded seed-properties
entries are checked, including unmatched names. Only encountered project
configuration nodes are checked; unused project subtrees are not traversed.

Invalid `column_types` settings use the same YAML provenance format. For example,
`/project/seeds/properties.yaml: seed 'codes', seeds[1].config['column_types']:
column_types must map exact column names to nonempty type strings` identifies
the setting without echoing rejected column keys, type values or CSV fields.
Nulls, non-mappings, non-string column keys and non-string/empty/whitespace-only
type values fail with exit 2 and empty stdout. Malformed configuration nodes
identify the node path and require a mapping. Validation covers the root even
with no CSVs, encountered ancestors even when overridden deeper, and every
loaded property entry including unmatched seeds. Unused project subtrees remain
unchecked. At each node, `+column_types` takes precedence and only that selected
spelling is validated when both are present; column maps merge from root through
seed, then properties. By default, nonempty explicit type strings remain presence decisions,
without warehouse type or cast validation.

Opt into exact validation of effective declarations with:

```sh
datawrench seedtypes check examples/seedtypes/dbt_column_types --format json
datawrench seedtypes check examples/seedtypes/dbt_column_types --validate-column-types --format json
```

The default command exits **0**. The opt-in command exits **1**: inherited
`zip: BIGINT` rejects `007` on physical line **4** in `codes.csv`, while
`DECIMAL(3,2)` preserves `1.10`. The tab-separated `text_codes.csv` inherits
its directory delimiter and overrides `zip` to `VARCHAR` in properties; that
seed passes. Overriding `codes` to `VARCHAR` in properties makes both pass.

Opt-in accepts exactly the canonical uppercase grammar documented for
`validate-types`: `VARCHAR`, `BOOLEAN`, `BIGINT`, `DATE`, `TIMESTAMP` and
`DECIMAL(P,S)`. Whitespace, lowercase spellings, adapter aliases such as `INT`,
parameterized VARCHAR and SQL expressions are configuration errors. This
includes adapter-rendered/lowercase types produced by `emit --target dbt`;
those remain supported by default presence-only checks. No declared expression
is executed. The validator consumes the effective mapping in memory, without
temporary type documents. It uses pinned DuckDB casts and canonical lexical
rules, and does not certify warehouse casts or execute dbt.

All loaded YAML documents are parsed before optional type validation. Every
selected setting in encountered ancestors, including declarations later
overridden, root settings without CSVs, and unmatched properties entries must
use supported types. The existing selected `+column_types` precedence and
unused project subtree boundary still apply. All seed configurations are
resolved before any CSV/reader work. Invalid declarations identify their YAML
file and configuration path with static guidance, without rejected content.

Opt-in JSON adds `column_type_validation` to each seed, with the same result
schema as `validate-types`. Each explicitly typed column also has `source`
(absolute YAML filename) and `config_path` for its **winning** declaration.
The first rejection has its physical record-start line, static code and reason;
counts cover the entire file. Top-level `column_type_failures` lists rejected
explicit columns separately from existing `issues` (untyped default risks).
Text output includes both counts, per-column results, notices and provenance.
Without the flag, neither new JSON key is emitted and existing reports/exit
decisions remain unchanged.

Default null policy is still no designated nulls: empty strings are values.
With the flag, repeatable `--null-token TEXT` applies to validation and the
comparison baseline; it is rejected without the flag. Header-only and entirely
designated-null columns pass with no-evidence notices. An empty explicit map
has no typed failures; untyped default risks still count. A headerless empty
seed is allowed only when no explicit columns are requested. Configured literal
comma/tab/semicolon separators apply to every scan. The existing 64 MiB check
limit still applies. No raw/rendered fields appear unless `--show-examples` is
selected, then evidence uses the existing bounded limits. Exit **0** means no
untyped risks and no typed failures; **1** means a complete report with either;
**2** means invalid configuration/input/resource/usage with empty stdout,
including malformed trailing CSV after a rejection.

Python callers can use `check_project(path, validate_column_types=True,
null_tokens=['NULL'])`; the same opt-in, canonical types and privacy rules apply.

The [dbt delimiter config](https://docs.getdbt.com/reference/resource-configs/delimiter)
requires dbt 1.7+. Generated properties always include the literal delimiter,
even for comma input, so an inherited project default cannot change the recipe.
`check` scans only `.csv` seeds, regardless of their selected separator; it does
not infer a delimiter or include `.tsv` files. Emitting dbt properties for a
`.tsv` source fails explicitly. Other reader targets retain `.csv`/`.tsv` support.

```sh
datawrench seedtypes check examples/seedtypes/dbt_delimiters --format json
datawrench seedtypes emit examples/seedtypes/dbt_delimiters/seeds/root_codes.csv --delimiter semicolon --target dbt
```

The first command deliberately exits **1**, with four unpinned `zip` risks on
physical line 4. The fixture combines a root semicolon default, an inherited tab
directory, a comma seed override and a semicolon property override. Each file
has a quoted separator/quote/newline field. Emitting all four seeds separately
with their selected delimiters (or using the Python API for mixed delimiters)
and replacing `seeds/properties.yml` with their combined generated properties
makes `check` exit 0. Directory `emit` still applies one CLI delimiter to all
inputs, so use it only for directories with a single dialect.

This is an **inference-risk lint using DuckDB/Polars**, not an emulation of dbt's
agate reader or a guarantee that an installed dbt adapter applies its config.
By default an explicit type is an intentional presence decision. Optional exact
validation checks decoded fields locally; warehouse casts are not certified. Jinja, YAML aliases, duplicate
keys, external seed paths, overlapping seed paths, unknown column names and
duplicate seed names fail with exit 2; no templates are executed. No profiles,
credentials, packages or compiled manifests are read.

Missing, empty or non-string project `name` values report the absolute
`dbt_project.yml` filename followed by `a static project name is required`.
A `seed-paths` value that is not a list of strings reports that filename followed
by `seed-paths must be a list of local relative directories`. For example,
`/project/dbt_project.yml: a static project name is required` identifies the
origin even when the project argument is relative. Both shape errors exit 2
with empty stdout and static guidance, without rejected keys/values, YAML
snippets or CSV evidence, including with `--show-examples` and
`--validate-column-types`, with or without CSVs. Project YAML loading still
precedes name validation, then path-shape validation, before properties or CSV
processing. Accepted names and paths are unchanged; non-ASCII strings remain
supported. Path containment and filesystem checks follow shape validation.

Rejected seed directories identify the absolute `dbt_project.yml` and zero-based
list entry, for example `/project/dbt_project.yml: seed-paths[1]: expected a directory`.
The other guidance is `escapes the project directory` for absolute paths, parent
components or resolved paths outside the project, and `symlinks are unsupported`
for symlinks within the project, including ancestors. Containment is checked
before symlinks, so external symlinks report the escape error. These errors exit 2
with empty stdout and never echo the rejected path, symlink target, YAML snippets
or CSV fields, including with `--show-examples` and `--validate-column-types`.
The default `seeds` entry uses index 0. Missing directories are still skipped;
valid non-ASCII relative directories remain supported. Path acceptance and
validation order are unchanged.

Duplicate CSV seed names report all conflicting absolute filenames grouped by
their case-sensitive stem, for example:
`duplicate dbt seed names across seed paths are unsupported: 'codes': '/project/a/codes.csv', '/project/b/codes.csv'`.
Groups are sorted by stem and filenames are sorted within each group, regardless
of configured root or file creation order. Unique seeds are omitted; non-ASCII
filenames are preserved and control characters are escaped using Python string
representations. This covers collisions across configured roots and nested
directories, including more than two files or multiple conflicting stems.
Both check modes exit 2 with empty stdout, including with `--show-examples`;
filenames are visible metadata, but CSV fields and YAML snippets are never read
or included by this diagnostic. Discovery and file-path validation still precede
duplicate detection, which precedes properties loading, type validation and CSV
parsing. Name acceptance is unchanged: `Same.csv` and `same.csv` are distinct,
while `same.csv` and `same.CSV` collide. Static dbt checks still ignore `.tsv` files.

Duplicate or disallowed non-scalar YAML keys report the absolute originating
filename followed by `YAML keys must be unique scalars`; aliases report that
filename followed by `YAML aliases are not supported`. This applies to
`dbt_project.yml` and every loaded `.yml` / `.yaml` properties file, including
unmatched entries and projects without CSVs. For example,
`/project/seeds/properties.yaml: YAML keys must be unique scalars` identifies
the file without echoing rejected keys, values, anchor names or CSV fields.
Both errors exit 2 with empty stdout, even with `--show-examples`. Jinja is
rejected before YAML loading; alias detection during composition precedes
duplicate-key construction. These diagnostics do not add YAML line numbers.

Malformed syntax, unknown tags, duplicate anchors and nonprintable characters
report the absolute filename and
`invalid or excessively nested YAML`, followed by one-based `at line N, column N`
when the parser supplies a problem position. For example,
`/project/dbt_project.yml: invalid or excessively nested YAML at line 3, column 39`
locates a syntax error without printing its source line. Unmarked errors (such
as excessive nesting) omit coordinates. Raw parser messages, source snippets,
rejected keys/values, tags and anchor names are suppressed, even with
`--show-examples`. These errors exit 2 with empty stdout for project and loaded
properties YAML, including unmatched entries and projects without CSVs.

Invalid implicitly typed dates/timestamps and rejected explicit scalar
conversions (such as `!!int`, `!!float`, `!!bool` or `!!timestamp`) use the same
safe invalid-YAML diagnostic, with the scalar's one-based start line/column.
For example, `private_settings: {date: 2026-99-99}` exits 2 without a Python
traceback or the rejected value. Valid scalar conversions and quoted strings
retain their YAML meanings. Normalization is limited to known conversion
failures in the built-in scalar constructors; unrelated application errors
are not treated as malformed YAML.

Invalid UTF-8 bytes, including truncated multibyte sequences, report the absolute
originating filename and `expected UTF-8 encoded YAML (optional BOM)`. For example,
`/project/seeds/properties.yaml: expected UTF-8 encoded YAML (optional BOM)`
exits 2 with empty stdout, without decoder byte values/offsets, source snippets,
keys, tags, values or CSV evidence, even with `--show-examples`. This applies to
`dbt_project.yml` and all loaded `.yml` / `.yaml` properties, including unmatched
entries and projects without CSVs, in both default and `--validate-column-types`
modes. Whole-file decoding still precedes Jinja detection and YAML parsing;
no line/column is inferred from a decoder byte offset. Valid UTF-8, an optional
UTF-8 BOM and non-ASCII text retain their existing meanings.

`emit --target dbt --adapter` supports `duckdb`, `postgres`, `snowflake`,
`bigquery`, `redshift`, `databricks`. SQLGlot renders type ASTs; BigQuery uses
BIGNUMERIC when NUMERIC's scale/integer capacity is insufficient, and Snowflake
uses documented `datetime` for timezone-free timestamps. All six mappings are
tested offline. Warehouses and `dbt seed` are not run; adapter/session rendering
and historical-calendar differences remain outside the lexical guarantee.

## Limits and exit codes

Input is local UTF-8 (optional BOM), explicitly comma-, tab- or semicolon-separated
CSV/TSV with a header, standard
double quotes and doubled-quote escaping. Embedded quoted newlines and CRLF
are supported. Blank physical records, ragged rows, unterminated quotes, NULs,
empty/duplicate/multiline headers (duplicates are case insensitive), non-UTF-8 and compressed/remote inputs
are errors. Symlinks and glob metacharacters in input paths are rejected so an
emitted DuckDB reader cannot expand an unintended set of files. There is no
CSV dialect sniffing, encoding conversion, fuzzy date parsing, remote access,
floating-point inference, compression, warehouse loading or source rewriting.

Resource bounds fail explicitly: 256 columns, 1,048,576 characters per field,
4 MiB decoded UTF-8 field bytes per row/header, 9 MB source CSV record, 64 null
tokens of at most 1,024 characters, 2 MiB per YAML document. Files must remain
stable during a command; stat changes abort the scan. Ordinary file operations
are not hardened against an adversary concurrently replacing filesystem paths.

| Command | 0 | 1 | 2 |
| --- | --- | --- | --- |
| infer / emit | Report/recipe produced | — | Input, configuration, resource or usage error |
| compare | Complete comparison (including LOSSY) or no data | A default reader failed | Input/usage/resource error |
| validate-types | All requested types round-trip (or no non-null evidence) | Requested type rejected | Input, schema, resource or usage error |
| check | No unpinned risks or opt-in typed failures | Unpinned loss/failed-reader risk or opt-in requested type rejection | Unsupported/invalid project or input |

## Verification and provenance

From a datawrench checkout:

```sh
uv run --extra test pytest tests/seedtypes -q
datawrench seedtypes infer examples/seedtypes/seeds --format json
datawrench seedtypes compare examples/seedtypes/seeds/zip_codes.csv
datawrench seedtypes check examples/seedtypes/dbt_project; test $? -eq 1
```

The offline suite executes emitted DuckDB SQL and Polars snippets, checking
all decoded fields against their source, covers all adapter mappings, validates
check exit codes and configuration boundaries, and generates data beyond both
engines' default samples. Delimiter tests compare all three encodings and execute
both emitted readers, including quoted separators/newlines and separator-containing
null tokens. Static dbt tests cover inherited delimiters, property precedence,
mixed projects, invalid configuration, physical error lines and generated YAML
for all six adapters. No warehouse, credentials or browser is required.

## License

Part of datawrench, licensed under Apache-2.0 ([LICENSE](../LICENSE), [NOTICE](../NOTICE)). This command was first released as a standalone MIT-licensed project; that notice is kept in [`licenses/MIT-original-tools.txt`](../licenses/MIT-original-tools.txt).
Dependency notices and sources: [THIRD_PARTY.md](../THIRD_PARTY.md) and
[`licenses/seedtypes/`](../licenses/seedtypes/). seedtypes is an independent
integration, not an official DuckDB, dbt, Polars or SQLGlot project.
