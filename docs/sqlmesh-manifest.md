# datawrench sqlmesh-manifest

`datawrench sqlmesh-manifest` (formerly `sqlmesh-dbt-manifest`) exports a **native
SQLMesh project's local metadata** as a dbt manifest v12 for catalog,
observability, lineage and lint integrations. It loads models with SQLMesh's
Python Context API, translates the logical DAG and audit definitions, and
validates the result using both the official dbt schema and `dbt-artifacts-parser`.

No warehouse connection, plan, apply, model execution or dbt installation is
needed. This is an independent metadata bridge.

## Install and run

Requires Python 3.11+ (originally tested on Linux with Python 3.11; the original
release declared 3.11–3.12 only). The command needs the `sqlmesh` extra, which
installs SQLMesh 0.236.x (and with it SQLGlot 30.8.x), `dbt-artifacts-parser`,
`jsonschema`, `ruamel.yaml` and `pydantic`. Installation requires package
downloads; subsequent tests and exports are offline.

```sh
pipx install 'datawrench[sqlmesh]'   # or: pip install 'datawrench[sqlmesh]'
datawrench sqlmesh-manifest --version # prints the bare datawrench version, e.g. 0.1.0
```

From a checkout of this repository:

```sh
datawrench sqlmesh-manifest export \
  --project examples/sqlmesh_manifest/mini_project --out target/manifest.json --deterministic
datawrench sqlmesh-manifest validate target/manifest.json
datawrench sqlmesh-manifest summary target/manifest.json
```

Expected summary:

```text
models=3 seeds=1 sources=1 tests=2 edges=5
```

The example contains synthetic orders in a real CSV seed, an external customers
table, a staging view, an incremental fact model with `not_null` and
`unique_values` audits, and a full dimension model. Nothing queries these tables.

`python -m datawrench sqlmesh-manifest` is equivalent to `datawrench sqlmesh-manifest`.
The subcommands are `export`, `validate`, `validate-report`, `validate-diff`,
`summary` and `diff`.

## Commands and output

```text
datawrench sqlmesh-manifest export --project DIR --out FILE
    [--gateway G] [--project-name N] [--schema-version 12]
    [--report FILE] [--deterministic] [--trust-project]
datawrench sqlmesh-manifest validate FILE|- [--format text|json]
datawrench sqlmesh-manifest validate-report REPORT|- --manifest MANIFEST [--format text|json]
datawrench sqlmesh-manifest validate-diff FILE|- [--format text|json]
datawrench sqlmesh-manifest summary FILE|-
datawrench sqlmesh-manifest diff BEFORE AFTER [--format text|json] [--out FILE]
    [--include-descriptions]
```

`export` validates before atomically replacing the requested output file. It
creates parent directories as needed; an existing artifact survives a failed
export. Symbolic links in output paths are rejected. Project configuration and
input directories are protected from output writes; use a separate artifact path. File permissions default to private
(0600). Warnings go to stderr; success messages go to stdout.

`--project-name` overrides the SQLMesh `project` setting (otherwise the directory
name is used). Names must match `[A-Za-z_][A-Za-z0-9_]*`. `--gateway` selects gateway
configuration and model defaults; `metadata.adapter_type` comes from its dialect.
Catalog names come from the connection configuration's `get_catalog()` method;
there is no warehouse discovery or remote-state merge. Explicit catalog/schema
qualifications in SQL remain intact.

`--deterministic` fixes generated timestamps to `1970-01-01T00:00:00Z` and the
invocation UUID to zero. All modes render with a fixed epoch reference interval
so loading the same project twice does not introduce date-dependent compiled
SQL. JSON keys and graph edges are sorted. Generated source paths are project-relative; generated metadata excludes
checkout/cache paths and connection credentials. `raw_code` preserves source
literals verbatim, including any paths explicitly written in that source.

`validate` uses `dbt-artifacts-parser` for v1–v12 manifests; v12 is additionally
checked against the vendored official schema. It checks IDs and declared node
dependencies even when graph indexes are null or empty. Supplied parent/child
maps must include declared edges and be reciprocal when both are present.
It prints at most ten structural
errors, with paths and rule names rather than potentially sensitive input values.
It accepts UTF-8 JSON objects up to 64 MiB and rejects duplicate keys and NaN.
It validates structure and linkage, not whether a consumer supports every field.

For standalone validation in CI, use `validate FILE --format json`. Text is
still the default (`--format text` is equivalent). JSON exits **0** (valid) and
**1** (invalid/unreadable/unsupported) emit one object on stdout with empty
stderr. Argument errors, including a missing file argument or invalid/missing
format, retain argparse text on stderr, empty stdout and exit **2**.

Use `validate -` to read saved manifest bytes from stdin in either format:

```sh
cat manifest.json | datawrench sqlmesh-manifest validate -
cat manifest.json | datawrench sqlmesh-manifest validate - --format json
```

Only the exact argument `-` selects stdin; `./-` or an absolute path reads a
literal file named `-`. Stdin uses the same bounded binary JSON reader and
version/schema/parser/graph validation as files, including supported v1–v12
manifests. It reads at most 64 MiB plus one byte and rejects input over 64 MiB.
Empty, malformed, non-object, duplicate-key, non-finite, oversized and unreadable
streams exit **1** with the existing text diagnostics or JSON `read_json` rule;
later validation failures retain their existing stages and null version/counts.
Valid input exits **0** with the same success text or version/counts JSON.
Argument errors still exit **2** before reading stdin. Failed stdin never falls
back to a file named `-`. Validation does not close or write to stdin, load a
project, fetch schemas or write an artifact. Reads wait for EOF or the size
bound, without a timeout or incremental JSON processing. `diff` retains file-only arguments;
summary and report stdin are described below.

The packaged [manifest result schema](../src/datawrench/sqlmesh_manifest/manifest-validation-result-v1.json)
uses `manifest_validation_version=1`, separate from the provenance report
validation schema and from the input dbt manifest version. A valid mini-project
export returns:

```json
{"manifest_validation_version":1,"valid":true,"manifest_version":12,"counts":{"resources":7,"models":3,"seeds":1,"sources":1,"tests":2,"edges":5},"diagnostics":[]}
```

`manifest_version` is an integer from 1 through 12 after validation succeeds;
older supported manifests use the same existing parser and graph checks. Counts
include enabled resources across nodes, sources, macros, docs, exposures, metrics,
groups, saved queries, semantic models and unit tests. `resources` includes all
these types, while the four named counters count their respective resource types.
Disabled resources, selectors and graph indexes are excluded. Like `summary`,
`edges` counts supplied `parent_map` entries; an absent/null parent map gives zero
even when declared dependencies or a child map are present.

Failure has no partial version/counts and exactly one fixed diagnostic:

```json
{"manifest_validation_version":1,"valid":false,"manifest_version":null,"counts":null,"diagnostics":[{"path":"$","rule":"graph_validation"}]}
```

Rules follow validation order: `read_json` (unreadable, oversized, malformed,
non-object, duplicate-key or non-finite JSON), `manifest_version` (missing,
non-string or unsupported schema URL), `schema_validation` (official v12 schema),
`parser_validation` (supported version rejected by the independent parser), then
`graph_validation` (IDs, dependencies or graph indexes). An unexpected failure
uses `internal_error`. Only `manifest_version` uses the fixed path
`$.metadata.dbt_schema_version`; every other rule uses `$`. Output is bounded to
one diagnostic, independent of how many errors the input contains. JSON includes
no input values, arbitrary dictionary keys, file paths or exception details.
The schema fixes all envelope/count/diagnostic fields and allowed rule/path pairs;
a change to that contract requires a new result version. Validation reads only
the local input; it never loads SQLMesh projects, fetches schemas or writes an
artifact. Text keeps its existing bounded structural error messages.

`summary` validates first, then prints resource counts and the supplied
`parent_map` edge count. An absent/null parent map counts zero edges, even if
declared dependencies or a child map are present. Models, seeds, sources and
tests always appear in that order; additional resource types follow in sorted
type order (`analysis` becomes `analyses`, others append `s`), with edges last.
Disabled resources and selectors are excluded. Documentation in `docs` counts
as `docs`, including legacy entries without `resource_type`; input is not
modified. Versions whose schema requires that field still reject its omission.

Use `summary -` for saved manifest bytes on stdin:

```sh
cat manifest.json | datawrench sqlmesh-manifest summary -
# models=3 seeds=1 sources=1 tests=2 edges=5
```

Only the exact summary file argument `-` selects stdin; `./-` and absolute paths
read literal dash files. The existing binary reader accepts at most 64 MiB and
reads at most 64 MiB plus one byte, then uses the same supported v1–v12 validator
and count formatter as files. Valid input exits **0** with one deterministic
text line and empty stderr. Invalid, unsupported, malformed, duplicate-key,
non-finite, oversized or unreadable input exits **1**, with no partial summary
and existing diagnostics on stderr. Argument errors exit **2** before stdin
reads; summary has no `--format` option. There is no file fallback on stdin
failure, project loading, schema fetching or artifact writing. Stdin is neither
closed nor written; reads wait for EOF or the size bound without a timeout or
incremental parsing. The byte bound does not bound expanded object memory.

Legacy documentation without `resource_type` now summarizes successfully in
both file and stdin modes. For the retained v1 fixture:

```sh
datawrench sqlmesh-manifest summary tests/sqlmesh_manifest/fixtures/manifest_validation/v1.json
# models=0 seeds=0 sources=0 tests=0 docs=1 edges=0
```

Nonempty documentation is tested across supported versions, including legacy
v1–v7 entries without a type and v8–v12 entries with `resource_type="doc"`.
Mixed analysis/documentation/macro labels, disabled/selectors exclusions and
exported v12 counts are covered; this is not exhaustive coverage of historical
dbt producers.

For export/validate/summary, exit codes: `0` success; `2` export/configuration/argument error (including missing
project or unsupported schema); `1` invalid/unreadable input for validate/summary.
Upstream exceptions are reported by exception type to avoid exposing secrets.

## Offline semantic difference report

`diff` compares two existing **dbt manifest v12** files without loading a project,
executing code or contacting a warehouse. Both inputs pass the same official
schema, independent parser and graph validation as `validate` before comparison.
It reports model and source additions/removals, changes to surviving models' declared
`depends_on.nodes`, and column additions/removals/metadata edits on surviving
models and sources. Opt in to top-level description changes with
`--include-descriptions`. A source schema change is reported even when all downstream
model metadata stays unchanged. JSON and text
render one shared result, ordered by node ID and column key.

For example, delete the staging model and redirect the final model to its source
in a temporary copy of the offline fixture:

```sh
work=$(mktemp -d)
cp -R examples/sqlmesh_manifest/lineage_project "$work/project"
datawrench sqlmesh-manifest export --project "$work/project" --out "$work/before.json"
sed -i 's/FROM analytics.stage/FROM raw.input/' "$work/project/models/final.sql"
rm "$work/project/models/stage.sql"
datawrench sqlmesh-manifest export --project "$work/project" --out "$work/after.json"
datawrench sqlmesh-manifest diff "$work/before.json" "$work/after.json"
# Exit 1: differences found. To save the same result as JSON:
datawrench sqlmesh-manifest diff "$work/before.json" "$work/after.json" \
  --format json --out "$work/diff.json"
```

Expected text (exit 1):

```text
Semantic changes: 2
dependencies_changed "model.lineage_example.final" (before="models/final.sql"; after="models/final.sql")
  - "model.lineage_example.stage" ("models/stage.sql")
  + "source.lineage_example.raw.input" ("external_models/input.yaml")
model_removed "model.lineage_example.stage" (before="models/stage.sql"; after=null)
```

Unchanged exports, including ordinary exports with different timestamps and
invocation IDs, print `No semantic changes.` (exit 0). Top-level metadata,
checksums, code, graph-index ordering and JSON key order do not affect this
scoped comparison. Dependency and column-tag lists are treated as sets; known
optional ColumnInfo defaults are normalized. Other metadata arrays retain their
order. Columns match by their exact dictionary key, without type or identifier
case folding. Column reports retain metadata values, including user-defined
`meta`, tags, constraints and extra fields.

JSON report version 1 contains `has_changes`, counts by change kind in `summary`,
and `changes`. Every change has `node_id`, `before_file` and `after_file`;
dependency changes include added/removed IDs and their respective source paths.
Column changes include the column key and normalized `before`/`after` metadata.
Sources use the same `column_added`, `column_removed` and `column_changed` kinds
and summary counters as models; `node_id` identifies the source. Sources match
by exact ID, including sources with no downstream models. `source_added` and
`source_removed` each carry the source `node_id` and respective YAML provenance:
the absent side's file is null. Only sources present in both inputs have their
columns compared; a whole-source addition/removal emits one lifecycle event.
Implicit sources use null file paths.
An implicit dependency has `file=null` because its manifest path is synthetic.

Source lifecycle events extend diff version 1 additively: existing kinds, fields
and counters keep their meanings; `summary` now always includes `source_added`
and `source_removed`, including zero counts. Consumers must allow new event kinds
and summary keys; clients with exhaustive v1 allowlists need updating.

### Opt-in model and source descriptions

`diff BEFORE AFTER --include-descriptions` also compares top-level `description`
on surviving models and sources enabled on both sides. Resources in `disabled`
and resources with `config.enabled=false` on either side are excluded from this
additional comparison; omitted `enabled` means true. The validated v12 model/source
contract allows an omitted description with default `""`; omission and explicit
empty string are equivalent. Null and non-string descriptions are invalid inputs,
even without the option. Strings compare exactly, including whitespace, line
endings, Unicode and empty values; nothing is trimmed or normalized.

For a surviving `model.shop.orders` at `models/orders.sql` changing `Orders.` to
`Orders with returns.`, JSON contains one event and exits 1:

```json
{"kind":"description_changed","node_id":"model.shop.orders","before_file":"models/orders.sql","after_file":"models/orders.sql","before":"Orders.","after":"Orders with returns."}
```

The report retains `diff_version=1` and adds `summary.description_changed=1`.
With the option, the counter is always present, including zero when unchanged.
Without the option, this counter is absent and existing default report shapes,
bytes and exit codes are preserved; a description-only edit still prints
`No semantic changes.` and exits 0. Added/removed resources retain one lifecycle
event each. Implicit provenance remains null. Column description changes keep
their existing column events; they do not also become resource description events.
`source_description` (the source group description), SQL, config, tags, seed/test
and other resource metadata remain outside this additional comparison.

JSON and text use the same events sorted by exact node ID. Within a surviving
model, dependency changes precede the description event, followed by columns in
exact column-key order. Within a surviving source, the description event precedes
columns. Text escapes description strings as ASCII JSON, including newlines:

```text
Semantic changes: 1
description_changed "model.shop.orders" (before="models/orders.sql"; after="models/orders.sql")
  before: "Orders."
  after: "Orders with returns."
```

This is an additive v1 extension. The packaged schema and `validate-diff` accept
both existing eight-counter and opt-in reports. `description_changed` is an
optional nonnegative integer summary counter. Known description events require
string `before`/`after` and explicit nullable file provenance; they reject column
and dependency payload fields. Unknown future kinds remain allowed with base
provenance. Structural validation does not require the optional counter to be
present alongside an event or check string inequality or counter totals.

### Validate diff JSON offline

The packaged [diff-v1.json schema](../src/datawrench/sqlmesh_manifest/diff-v1.json) defines
this producer's current `diff_version=1` results using JSON Schema Draft 7.
It is separate from the dbt manifest and validation-result schemas. Consumers
can validate a saved CLI result without loading a SQLMesh project:

```sh
datawrench sqlmesh-manifest validate-diff diff.json
```

By default (or with `--format text`), the command exits **0** for a structurally valid report and prints
exactly `Valid semantic diff v1 (structural validation only)` plus a newline to
stdout, with empty stderr. Invalid, unreadable or unsupported reports exit **1**,
with empty stdout and exactly `validation error: Invalid or unsupported semantic diff report`
plus a newline on stderr. The same fixed diagnostic covers unexpected validation
failures; it never includes file paths, metadata, arbitrary keys or exception
details. Argument errors (missing/extra file arguments or unsupported options,
including `--out`, invalid formats or a missing format value) retain argparse usage/error text on stderr,
empty stdout and exit **2**. Validation success has exit 0 even when the report
describes changes; this differs from the `diff` command's change-detection exit 1.

For CI, `validate-diff diff.json --format json` emits one deterministic JSON
object on stdout with empty stderr, exiting **0** for valid reports and **1**
for invalid/unreadable/unsupported reports. Argument errors still use argparse
text on stderr and exit **2**, even when JSON was requested.

Use `validate-diff -` to read saved report bytes from stdin in either format:

```sh
cat diff.json | datawrench sqlmesh-manifest validate-diff -
cat diff.json | datawrench sqlmesh-manifest validate-diff - --format json
```

Only the exact argument `-` selects stdin; use `./-` or an absolute path for a
literal file named `-`. Stdin uses the same binary JSON reader and packaged
validator as files: at most 64 MiB plus one byte is read, and reports exceeding
64 MiB are rejected. Empty, malformed, non-object, duplicate-key, non-finite,
unsupported and unreadable streams use the same fixed failure result and exit
**1**. A valid original, description or additive report exits **0**, including
reports that describe changes. There is no fallback to a file named `-` on
stdin failure. The command does not close or write to stdin, load projects or
original manifests, or fetch schemas. Reads wait for EOF or the size bound;
this is bounded input validation, with no timeout or incremental JSON processing.
`validate` and `validate-report` also support stdin as described in their
respective sections; other commands retain their existing file behavior.

The distinct packaged [diff result schema](../src/datawrench/sqlmesh_manifest/diff-validation-result-v1.json)
uses `diff_validation_version=1`, independently of the input `diff_version` and
the manifest/provenance validation results:

```json
{"diff_validation_version":1,"valid":true,"diagnostics":[]}
```

Every failure, including unexpected validation failures, returns exactly:

```json
{"diff_validation_version":1,"valid":false,"diagnostics":[{"path":"$","rule":"invalid_diff"}]}
```

The single fixed diagnostic follows the text mode's policy: it does not expose
failure stages, input values, arbitrary keys, paths or exception details. Results
have no input-derived counts or metadata; success means structural validity only,
even for reports describing changes or containing future kinds. The result schema
fixes the envelope fields and success/failure diagnostic shapes, rejects extra
fields, and requires a new result version for contract changes. JSON uses sorted
keys, two-space indentation and a trailing newline in both success and failure.

It reuses the bounded JSON artifact reader: at most 64 MiB, a JSON object,
no duplicate keys or non-finite numbers (including exponent overflow such as
`1e999`, now rejected by the shared reader for all artifact commands).
Inputs remain read-only. Only the packaged schema is used; input `$schema` or
`$ref` fields do not select a schema or trigger fetching. No SQLMesh project,
original manifest, network or output artifact is needed. Validation is explicitly
structural, with the additive policy and consistency limits described below.

For direct schema use in Python:

```python
import json
from importlib.resources import files
from pathlib import Path
from jsonschema import Draft7Validator

schema = json.loads(files("datawrench.sqlmesh_manifest").joinpath("diff-v1.json").read_text())
Draft7Validator.check_schema(schema)
Draft7Validator(schema).validate(json.loads(Path("diff.json").read_text()))
```

Only local schema references are used; no schema download or project access is
needed. The envelope requires version, boolean `has_changes`, all eight original
nonnegative integer summary counters and a `changes` array. The description counter
is optional. An unchanged report
requires an empty array and all counters zero; a changed report requires at least
one event. Older v1 outputs without source lifecycle counters need those zero
keys supplied before validation against this current-producer schema.

Every event requires `kind`, `node_id`, `before_file` and `after_file`, including
explicit null provenance. Lifecycle events require a null absent-side file and
reject column/dependency payload fields. Dependency events require `added` and
`removed` reference arrays, at least one nonempty; each reference requires
`node_id` and nullable `file`. Column events require `column` and normalized
`before`/`after` objects: null/object for added, object/null for removed and
object/object for changed. Normalized metadata includes all existing defaults,
config, tags and constraints; user metadata values remain unrestricted JSON.

The additive v1 policy allows extra fields and nonnegative integer summary keys.
Unknown event kinds are accepted with the required base provenance; consumers
must skip kinds they do not understand. Known event shapes remain checked and
cannot bypass checks through this fallback. New fields/kinds may extend v1;
removing required fields or changing their types or meaning requires a new
`diff_version` and schema. The schema checks structure, including typed known
metadata fields, and does not compare counter totals to events, require sorted
or unique events, check dependency-set disjointness, or prove that before/after
metadata differs. It does not validate original manifests or authenticate reports.
JSON Schema treats integral numbers such as `1.0` as integers; strings and
booleans are rejected in integer fields.

The paired `tests/sqlmesh_manifest/fixtures/source_diff/{before,after}` projects demonstrate an
upstream-only schema edit. Their model SQL and declared columns are identical:

```sh
work=$(mktemp -d)
datawrench sqlmesh-manifest export --project tests/sqlmesh_manifest/fixtures/source_diff/before --out "$work/before.json" --deterministic
datawrench sqlmesh-manifest export --project tests/sqlmesh_manifest/fixtures/source_diff/after --out "$work/after.json" --deterministic
datawrench sqlmesh-manifest diff "$work/before.json" "$work/after.json" --format json
```

Exit 1 reports three column events for `source.source_diff.raw.input` at
`external_models/input.yaml`: `id` changes from INT to BIGINT and receives a new
description, `label` is added, and `retired` is removed. Model and dependency
change counts stay zero.

The paired `tests/sqlmesh_manifest/fixtures/source_lifecycle/{before,after}` projects keep all
surviving model metadata unchanged while removing `raw.retired`, adding
`raw.added`, and renaming the declared `raw.legacy` ID to `raw.renamed`. Use the
same export/diff commands with these project paths. Exit 1 reports four events,
sorted by source ID: added `raw.added`, removed `raw.legacy`, added `raw.renamed`,
removed `raw.retired`, with the respective `external_models/<name>.yaml` paths.
All other counters are zero; the sources need no downstream consumers. For example:

```json
{"kind":"source_added","node_id":"source.source_lifecycle.raw.added","before_file":null,"after_file":"external_models/added.yaml"}
```

Text renders the same record as:

```text
source_added "source.source_lifecycle.raw.added" (before=null; after="external_models/added.yaml")
```

Diff exit codes: **0** no changes in this scope; **1** differences found;
**2** invalid/unreadable/unsupported input, unsafe destination or write failure.
Errors produce no partial result and leave an existing output untouched.
`--out` writes atomically with private permissions and cannot alias either input
(including hardlinks, symlinks and relative-path aliases). Without it, the result
goes to stdout. Inputs are always read-only.

This is a focused metadata report, not SQL equivalence or a deployment planner.
SQL/config/materialization changes, model/source-level tags,
seed/test changes and path-only moves are outside its comparison scope.
Model/source-level descriptions are included only with `--include-descriptions`.
IDs define model and source identity: a renamed ID is a removal/addition, even
when its file path stays the same. Added/removed models and sources get one
event each, rather than events for every contained column/edge.
Source paths are provenance for changes, not a matching heuristic. No export
provenance sidecar is required.

## Per-node provenance and translation report

Add `--report FILE` to export a separate JSON report alongside the manifest:

```sh
datawrench sqlmesh-manifest export \
  --project examples/sqlmesh_manifest/lineage_project --out target/manifest.json \
  --report target/lineage.json --deterministic
datawrench sqlmesh-manifest validate target/manifest.json
datawrench sqlmesh-manifest validate target/manifest.json --format json
datawrench sqlmesh-manifest summary target/manifest.json
```

Expected summary: `models=2 seeds=0 sources=1 tests=1 edges=3`. This offline
fixture has the chain `analytics.final → analytics.stage → raw.input` and one
attached not-null audit. For example, the report includes this provenance and
dependency mapping for the final model (excerpt):

```json
{
  "dbt_node_id": "model.lineage_example.final",
  "origin": {
    "file": "models/final.sql",
    "sqlmesh_name": "\"memory\".\"analytics\".\"final\"",
    "sqlmesh_kind": "INCREMENTAL_BY_TIME_RANGE"
  },
  "dependencies": [{
    "dbt_node_id": "model.lineage_example.stage",
    "file": "models/stage.sql",
    "sqlmesh_name": "\"memory\".\"analytics\".\"stage\""
  }]
}
```

Report version 1 has a `resources` entry for every emitted model, seed, source
and audit test, sorted by dbt ID. Each entry has an `origin`, sorted dependency
references, and an `unconverted` list of semantic names and fixed explanations.
Audit origins include the audit name and attached node when applicable; built-in
audits point to the model file declaring them. Implicit sources have `file=null`
and an explicit missing-declaration explanation. `manifest_sha256` binds the
report to the exact serialized manifest bytes.

The report identifies incremental/SCD execution rules, schedules, workflow
statements, storage/session settings, macro implementations, Python execution,
unavailable compiled SQL and audit execution semantics that are not translated.
Project workflow statements and unconverted SQLMesh unit tests appear in
`project_unconverted`. This is an allowlisted explanation of known mapping
limits, not an exhaustive inventory of every SQLMesh option. Unsupported kinds
such as CUSTOM/MANAGED and unloadable projects still fail with exit 2; no partial
report is produced to bypass these errors.

Report generation uses already-loaded metadata without additional rendering.
The same `--trust-project` boundary applies. The report excludes raw/compiled
code, audit arguments, configuration values and upstream exception text; only
identifiers and project-relative provenance paths are retained. Those identifiers
and filenames are not redacted. The manifest still preserves raw source literals
as described below. Report bytes are stable for deterministic static projects
after relocation; without `--deterministic`, the manifest hash changes with its
timestamp/invocation ID.

Both destinations must be distinct files outside protected project inputs;
symlinks and ancestor/descendant destination pairs are rejected before loading.
Loading, validation or report-generation failures leave existing artifacts
untouched. Each file is replaced atomically with private permissions, but the
two replacements are not a filesystem transaction: an I/O failure on the second
write can leave a new manifest with an older report. Check `manifest_sha256`
before consuming a pair.

### Validate a report/manifest pair

```sh
datawrench sqlmesh-manifest validate-report target/lineage.json \
  --manifest target/manifest.json
# Valid provenance report v1 for supplied manifest
```

`validate-report` reads a saved report and a manifest file offline without
loading a project. The report can also come from stdin:

```sh
cat target/lineage.json | datawrench sqlmesh-manifest validate-report - \
  --manifest target/manifest.json
cat target/lineage.json | datawrench sqlmesh-manifest validate-report - \
  --manifest target/manifest.json --format json
```

Only the exact report argument `-` selects stdin; use `./-` or an absolute path
for a literal report file named `-`. **`--manifest -` remains a literal file**;
it never reads stdin. Report stdin uses the existing bounded binary JSON reader
and the same pair validator: at most 64 MiB plus one byte is read, and reports
over 64 MiB are rejected. Valid matching pairs retain the same text/JSON counts
and exit **0**. Empty, malformed, non-object, duplicate-key, non-finite,
oversized or unreadable stdin retains the report `read_json` diagnostic and exit
**1**; later version, hash and resource/lineage failures keep their existing
stages. There is no fallback to a report file named `-`. Argument errors remain
text on stderr with exit **2**, before reading stdin. Validation does not close
or write to stdin, fetch schemas or write artifacts. Reads wait for EOF or the
size bound, with no timeout or incremental parsing; the bound limits input
bytes, not expanded object memory.

It requires the export provenance format `report_version=1` (an integer) and a
valid dbt manifest **v12**, then checks `manifest_sha256` against the exact manifest
file bytes. Reformatting the manifest or pairing a report with another export
fails even if their metadata is otherwise equivalent. Diff reports are a separate
format and are not accepted here.

Every manifest model, seed, source and test must appear exactly once. Resource
types, project name, origin file/SQLMesh identity/kind/audit and audit attachment
must match the manifest. Dependencies must match the manifest edges exactly,
without duplicates, and each reference must have the matching ID, file and
SQLMesh identity. Implicit-source files must be null. Graph-index keys are
required by v12 but may have null values: declared node dependencies are still
checked; seed edges use the parent map, or the child map if the parent map is
null. With both maps null, seed edges are treated as empty. Resource and
dependency ordering does not affect validity.

Exit **0** prints `Valid provenance report v1 for supplied manifest`; exit **1**
reports invalid, unsupported, unreadable or mismatched input to stderr with no
success output. Argument errors exit **2**. Neither input is modified and no
output artifact is written. Both inputs have a 64 MiB limit; malformed JSON,
duplicate object keys, non-finite constants and invalid report shapes fail.
Diagnostics describe rules without echoing report values. Translation-gap
entries are checked for shape, not recomputed: the original SQLMesh project is
unavailable. This is consistency validation, not an authenticity signature; a
coordinated edit of both files can still form a valid pair.

For CI, add `--format json` (text remains the default). Exit **0** or **1**
emits exactly one JSON object to stdout and no stderr. The packaged
[result schema](../src/datawrench/sqlmesh_manifest/report-validation-result-v1.json) defines
`validation_version=1`, distinct from the input report's version. A valid
mini-project pair produces:

```json
{
  "validation_version": 1,
  "valid": true,
  "report_version": 1,
  "counts": {
    "resources": 7, "models": 3, "seeds": 1, "sources": 1, "tests": 2,
    "dependencies": 5
  },
  "diagnostics": []
}
```

Counts cover the validated report's resources and directed dependency references,
including seed edges from available graph indexes. Failure returns `valid=false`,
`report_version=null`, `counts=null`, and exactly one first-failure diagnostic:

```json
{
  "validation_version": 1,
  "valid": false,
  "report_version": null,
  "counts": null,
  "diagnostics": [
    {"input": "pair", "path": "$.manifest_sha256", "rule": "manifest_sha256"}
  ]
}
```

`input` identifies `report`, `manifest`, or their `pair`; `path` is `$` or a fixed
field path with array indexes. `rule` is a fixed identifier from the schema.
Unreadable, oversized, malformed, duplicate-key, non-finite and non-object JSON
use `read_json` at the relevant input root. Unsupported versions use
`report_version` or `manifest_version`; invalid manifest structure/parser/graph
checks use `manifest_validation` at `$` to avoid exposing arbitrary manifest
keys. Report shape errors use `schema.<keyword>`. Pair checks identify the hash,
coverage, origin or dependency rule. Unexpected exceptions use `internal_error`
at the pair root without exception type or text. Results contain no filenames,
identifiers, input values or partial counts on failure. One diagnostic bounds
output independently of the number of errors; keys are sorted, with no timestamp.
Repeated validation of unchanged inputs produces identical output.

Argument errors, including a missing `--manifest` or invalid `--format`, retain
exit **2**, empty stdout and argparse text on stderr, even in JSON mode. For example:

```sh
datawrench sqlmesh-manifest validate-report target/lineage.json \
  --manifest target/manifest.json --format json
```

## Mapping contract

| SQLMesh | dbt manifest v12 |
|---|---|
| SQL model / explicitly trusted Python model | `nodes`, `resource_type=model` |
| FULL / VIEW / EMBEDDED | `config.materialized=table / view / ephemeral` |
| INCREMENTAL_* / SCD_TYPE_2* | `incremental`; original kind and kind definition in `meta` |
| SEED | `nodes`, `resource_type=seed`, CSV SHA-256 checksum and relative `meta.seed_file` |
| EXTERNAL | `sources`, original table `identifier`, database and schema |
| Undeclared DAG dependency | Implicit source with `meta.implicit=true` and a warning |
| Columns / column descriptions | `columns.*.data_type` / `description` |
| Tags, owner, cron, grain | `tags`, `meta`; original SQLMesh FQN in `meta.sqlmesh_name` |
| Model dependencies | `depends_on.nodes`, `parent_map`, reciprocal `child_map` |
| Built-in not_null / unique_values / accepted_values | dbt generic test metadata `not_null / unique / accepted_values` |
| Other / overridden audits and standalone audits | Singular tests with compiled SQL and dependencies |

For example, `model.mini_project.fct_orders` has materialization `incremental`
and depends on `model.mini_project.stg_orders`. Each audit test depends on the
audited model and any other relations in its rendered query. Generic tests use
`attached_node`; singular tests retain attachment in `meta.attached_node` because
the official singular-test schema has no such field. Blocking and skipped audit
flags map to severity and enabled state. Audit `condition` maps to `config.where`.

Simple, unique table names become `model.<project>.<name>` (or `seed.…`). Duplicate
leaf names across schemas and names needing normalization receive an FQN hash
suffix; original relation names remain in `alias` and metadata. Sources use
`source.<project>.<schema>.<table>`, with a catalog-derived suffix for non-default
catalogs. A collision after normalization is an error rather than an overwrite.
Adding a same-named model can therefore change the previously unique model ID.

Multi-column `not_null` and `unique_values` audits expand into one generic test
per column. Accepted-values metadata is generic for a literal column and
homogeneous string or numeric values; expressions and mixed values stay singular.
Custom audits overriding built-in names stay singular too. Rendered audit SQL
retains SQLMesh semantics, including its handling of NULLs; generic metadata does
not promise identical dbt test execution semantics (in particular `unique`).
These artifacts are intended for metadata consumers, not `dbt run` or `dbt test`.

`raw_code` contains original SQLMesh model/Python source; test `raw_code` contains
the audit definition/query. `compiled_code` contains rendered logical SQL, not
physical snapshot table names. SQLMesh can load some runtime-dependent models
with declared columns/dependencies but no offline query: those get
`compiled_code=null` and a warning. Python entry points are never executed by the
exporter; their compiled code is null. Sources have no code fields, and the dbt
v12 seed schema does not permit `compiled_code` or `depends_on.nodes`; seed graph
parents are represented in the top-level graph maps.

## Input trust and supported boundaries

By default, use **literal YAML config and static SQL**. Python config/models,
macros, signals, materializations and linter files require `--trust-project`.
Jinja SQL and SQL macro functions also require that flag. Plain SQLMesh variables
such as `@this_model` are allowed; SQL is parsed/rendered, never submitted for
execution. YAML is loaded with the safe loader. Templated YAML, custom loaders,
dbt project loaders, default SEED kinds, executable external-model YAML fields,
symlink inputs and seed paths outside the project root are rejected.

```sh
# Only for code you have inspected and trust:
datawrench sqlmesh-manifest export --project ./trusted_native_project \
  --out target/manifest.json --trust-project
```

**`--trust-project` permits arbitrary Python imports and macro/Jinja execution.**
It is not a sandbox. Trusted project code can perform its own I/O, imports and
side effects, and Python imports may create bytecode caches. Run untrusted code
in your own OS/container sandbox instead of opting in. The exporter itself
blocks SQLMesh adapters/state, disables analytics, uses a fresh temporary cache
(including SQLMesh's local telemetry file), ignores `.env`, personal config and
SQLMESH configuration environment overrides, and uses one loading worker. It
never invokes hooks as workflow steps, Python model entry points, plan or apply.

Keep model SQL/Python free of literal credentials before sharing the artifact:
`raw_code` deliberately preserves source. Configuration credentials and
unrenderable exception text are not included in the artifact or CLI diagnostics.

First-version limits:

- Native projects only, one project root, one selected gateway; no remote-state
  or multi-repository model merge and no warehouse-discovered schemas/catalogs.
  SQLMesh may require optional driver packages just to validate some gateway
  configurations; install the appropriate SQLMesh extra yourself when necessary.
- The project must be loadable by pinned SQLMesh. Invalid definitions or macros
  that fail during Context loading cause exit 2; only queries that SQLMesh can
  load without offline rendering receive a null compiled query.
- FULL, VIEW, EMBEDDED, SEED, INCREMENTAL_* and SCD_TYPE_2* are supported. CUSTOM,
  MANAGED and unknown kinds fail explicitly. Native SQLMesh metrics fail
  explicitly because they cannot be translated to dbt's semantic metrics.
- SQLMesh unit-test definitions produce a warning and are not converted to dbt
  unit tests. Macro implementations, schedules, hooks, physical snapshots and
  optional dbt semantic resources are not exported. The manifest's macro table
  is empty; compiled SQL and DAG references contain the useful metadata.
- No `catalog.json`, `run_results.json`, SQLMesh-to-dbt project conversion or
  warehouse execution. No promise of compatibility with consumers requiring
  dbt-generated macros, run metadata or a complete executable dbt project.
- Determinism covers static projects and deterministic trusted code. A macro
  reading the clock, environment or network can defeat it. Concurrent mutation
  of input/output paths is outside the supported filesystem boundary.

## Verify

From a checkout of this repository:

```sh
uv run --extra test --extra sqlmesh pytest tests/sqlmesh_manifest -q
datawrench sqlmesh-manifest export --project examples/sqlmesh_manifest/mini_project \
  --out target/manifest.json --deterministic
datawrench sqlmesh-manifest validate target/manifest.json
datawrench sqlmesh-manifest summary target/manifest.json
```

Without the `sqlmesh` extra installed, every module in `tests/sqlmesh_manifest/`
is reported as skipped (`pytest.importorskip("sqlmesh")`).

Tests exercise real Context loading, the CSV/SQL/YAML example, independent parser
and official schema validation, exact DAG edges, all supported materialization
families, generic/custom/standalone audits, gateway selection, colliding names,
Python entrypoint non-execution, dynamic query fallback, byte stability and
failure/security boundaries. SQLMesh's upstream Pydantic deprecation warnings
may be printed; they are not hidden by the test configuration.

Lineage tests cover the two-model/external-source fixture, exact origin/dependency
mapping, untranslated semantics, relocation stability, credential exclusion,
trusted Python entrypoint non-execution and report destination protection.
Report-validation tests cover exact-byte hashes, tampered provenance, duplicate,
missing and redirected IDs/edges, audit attachments, implicit sources, seed graph
indexes, stale artifact pairs, invalid JSON/shapes and offline read-only behavior.
JSON results additionally cover schema compliance, deterministic counts and safe
bounded diagnostics, malformed/unsupported/mismatched inputs, unexpected errors,
argument exits and default/explicit text compatibility.
Standalone manifest JSON tests cover the separate result schema, retained v1
input, fixed counts with null/empty graph maps, each validation stage, secret
dictionary keys and exception details, read-only/offline behavior and text exits.
Diff tests cover independent non-deterministic exports, real model deletion and
dependency redirection, paired external YAML schema edits with unchanged models,
model/source column metadata edits, optional graph maps/defaults,
source lifecycle and ID renames with unreferenced YAML declarations,
shared JSON/text results, offline operation and invalid-input/output protection.
Diff-schema tests validate real CLI results and reject malformed envelopes,
versions, provenance, lifecycle payloads, dependency references and normalized
column records, while preserving the documented additive compatibility policy.
Validate-diff tests cover fixed text/exit contracts, all current and future kinds,
structural consistency limits, bounded private diagnostics, JSON reading failures,
argument errors and offline read-only operation. Shared-reader tests also reject
positive/negative exponent overflow while preserving finite-number parsing.

Description-diff tests cover exact model/source strings (empty, multiline and
Unicode), omitted defaults, disabled resources, lifecycle and column exclusions,
stable event order, explicit null provenance, read-only/offline behavior and
malformed known description payloads.

Upstream contracts: [SQLMesh Context](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/context.html),
[dbt manifest docs](https://docs.getdbt.com/reference/artifacts/manifest-json),
[manifest v12 schema](https://schemas.getdbt.com/dbt/manifest/v12.json).

## License

Part of datawrench, licensed under Apache-2.0 ([LICENSE](../LICENSE)); the standalone project was also Apache-2.0 and its NOTICE is merged into [NOTICE](../NOTICE). The original `sqlmesh-dbt-manifest` source, tests and examples were Apache-2.0; see
[LICENSE](../LICENSE). The subpackage ships the unmodified dbt manifest v12 JSON
schema (`src/datawrench/sqlmesh_manifest/manifest-v12.json`) from dbt Labs, Inc.,
under the Apache License 2.0
([`../licenses/sqlmesh-manifest/dbt-schemas-LICENSE`](../licenses/sqlmesh-manifest/dbt-schemas-LICENSE)).
Source attribution and the schema checksum are in [THIRD_PARTY.md](../THIRD_PARTY.md).
This is an independent project; it is not an official SQLMesh or dbt product.
