# datawrench contract-diff

`datawrench contract-diff` (formerly `dbt-contract-diff`) is an offline CI gate for
compatibility between two **dbt model contracts**. It compares SQLGlot type ASTs,
including nested fields, so a change inside `STRUCT(...)`, `ARRAY(...)`, `MAP(...)`
or `Nullable(...)` cannot hide behind the outer type name. No warehouse credentials
or dbt installation are required.

## Install and run

Python 3.11+; Linux supported. The only runtime dependency is SQLGlot, installed
by the `sqlglot` extra:

```sh
pipx install 'datawrench[sqlglot]'   # or: pip install 'datawrench[sqlglot]'
datawrench contract-diff --version   # prints the bare datawrench version, e.g. 0.1.0
datawrench contract-diff --help
```

From a checkout of this repository:

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/base/manifest.json \
  --head tests/contract_diff/fixtures/head/manifest.json --format text
# Exit 1: the supplied fixture intentionally contains breaking changes.
```

`python -m datawrench contract-diff` is equivalent. The tool reads the two named
local regular files, an optional explicit project policy file, and prints a report.
Input errors are printed as `datawrench contract-diff: input error: <message>` and exit 2.

For example, comparing column `payload` from
`STRUCT(id INTEGER, amount DECIMAL(10,2))` to
`STRUCT(id INTEGER, amount VARCHAR)` emits:

```text
BREAKING model.shop.orders column payload: ... (nested_change); type.amount: DECIMAL(10,2) -> STRING (incompatible)
```

## CLI and output

```text
datawrench contract-diff --base BASE/manifest.json --head HEAD/manifest.json
  [--dialect auto|duckdb|snowflake|bigquery|postgres|clickhouse|databricks|redshift]
  [--format text|json|markdown|github]
  [--fail-on breaking|any|none] [--include-unenforced] [--strict-widening]
  [--policy project-policy.json] [--fail-on-unused-policy] [--model UNIQUE_ID ...]
  [--exclude-model UNIQUE_ID ...] [--fail-on-empty-scope]
```

| Option / result | Meaning |
| --- | --- |
| `--dialect auto` (default) | Use head `metadata.adapter_type`, then base; `spark` maps to `databricks`. Unknown/missing adapter warns and uses DuckDB. Explicit override is available. |
| `--format text` | Human-readable changes with exact nested paths, warnings and notices. |
| `--format json` | Report schema version 1: `changes`, `equivalents`, `warnings`, `notices`, `policy_diagnostics`, `dialect`, `models_compared`, `summary`. Each change has `kind`, `breaking`, `model`, `column`, `before`, `after`, `details`, `version`. |
| `--format markdown` | Escaped table suitable for a PR summary. |
| `--format github` | Escaped GitHub workflow `error` / `notice` / `warning` annotations. Prints only; does not call GitHub. |
| `--fail-on breaking` (default) | Exit 1 when any breaking finding exists. |
| `--fail-on any` | Exit 1 for any finding, including a same-string unparseable type; alias equivalences alone do not fail. |
| `--fail-on none` | Findings do not fail a successful comparison, even when breaking. Input errors still exit 2; the independent `--fail-on-unused-policy` and `--fail-on-empty-scope` gates still apply. |
| `--model UNIQUE_ID` | Compare only these exact case-sensitive model IDs (repeatable). Unknown IDs exit 2 without a report; enforcement filtering still applies. |
| `--exclude-model UNIQUE_ID` | Exclude exact case-sensitive model IDs after selection/enforcement eligibility (repeatable). Unknown IDs exit 2 without a report. |
| `--fail-on-empty-scope` | Opt in to exit 1 when `models_compared == 0`, independently of finding and unused-policy gates. Does not change the report. |
| `--include-unenforced` | Include models with no enforced contract in either artifact. |
| `--strict-widening` | Treat otherwise compatible scalar/nullability widening as breaking. |
| `--policy FILE` | Apply exact known-rule impact overrides from a bounded local JSON file; see below. |
| `--fail-on-unused-policy` | Opt in to exit 1 when this comparison leaves any policy entry unused, independently of `--fail-on`. Does not change the report or discover a policy file. |
| Exit 2 | Invalid JSON/input shape, unreadable/nonregular/oversized file, unsupported constraint format, invalid project policy, or invalid CLI arguments. |

A shell CI gate can use the exit code directly:

```sh
datawrench contract-diff --base artifacts/base/manifest.json \
  --head artifacts/head/manifest.json --format github
```

All report data goes to stdout. Input errors go to stderr. Warnings (including
project/adapter mismatches) appear in the report and do not independently cause
failure. A report with no eligible models explicitly says so and exits 0 by default;
`--fail-on-empty-scope` makes that comparison exit 1.

### Compare selected models

Repeat `--model` to restrict comparison to exact dbt model `unique_id` keys:

```sh
datawrench contract-diff --base artifacts/base/manifest.json --head artifacts/head/manifest.json \
  --model model.shop.orders --model model.shop.customers --format json
```

For example, if both enforced models change `id INT` to `VARCHAR`, selecting only
`--model model.shop.orders` reports one orders change and `models_compared: 1`,
exits 1 by default, and omits customers findings and alias equivalences. An
unrelated breaking change cannot fail the selected comparison. `--fail-on any`
still rejects a selected non-breaking finding; `--fail-on none` still ignores
findings. Additions, removals (including their structured details), disabling and
enforcement transitions at selected keys retain their existing behavior.

Selection is literal and case-sensitive: no names, globs, trimming, inferred
renames or version-family expansion. Repeated IDs are deduplicated and sorted
lexicographically, so argument order does not affect output. Each ID must occur
as an active or recognized disabled model key in either parsed manifest. A model
present on only one side is valid; disabled-only keys are known but have no active
comparison. Any unknown ID, even alongside a valid one, exits 2 with stderr and
no report. `--model` alone does not imply `--include-unenforced`: the selected key
must still be enforced in either artifact, unless that option is supplied.

Both complete artifacts and the policy still receive all existing input and
consumed-field validation before comparison is filtered. Malformed unselected
models and inconsistent `latest_version` values within any active model family
still exit 2, even when the selected scope has no enforced contracts. This retains
the existing loader's disabled-entry validation; it does not add full dbt-schema
validation. Type parsing and its warnings, findings, equivalents, rename notices,
counts and finding gates apply only to eligible selected records. Artifact-wide
metadata warnings and automatic dialect selection still use both full manifests.

Version-family findings use only eligible selected active records, once per
`(package_name, name)` family. For example, selecting `model.shop.orders.v2`
reports a `latest_version` change declared on v2 at that exact key; an unselected
v1 cannot supply a report key or a metadata change missing from v2. With multiple
selected versions, the existing last-sorted representative is retained (head
preferred, base fallback). Selection never excuses a selected old version's
removal or breaking change. Policy rules are applied only to selected findings;
rules for unselected models remain unused and can independently trigger
`--fail-on-unused-policy`, including with `--fail-on none`.

JSON schema version 1 adds `selected_models` **only when selection is requested**:
it contains the sorted, deduplicated requested IDs, including unenforced or
disabled-only IDs that contributed no comparison. Text, Markdown and GitHub
reports explain the same scope with an escaped `Selected models (exact unique_ids):`
line; a no-contract notice identifies the selected scope. Omitting both `--model`
and `--exclude-model` preserves full default comparison and report bytes. The Python API accepts
`selected_models=None` (default) or an iterable of exact IDs; an empty iterable
explicitly compares nothing.

### Exclude exact models

Repeat `--exclude-model UNIQUE_ID` to remove exact model keys from the full
comparison or from a `--model` selection:

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/exclusion/base.json \
  --head tests/contract_diff/fixtures/exclusion/head.json \
  --exclude-model model.shop.customers --format json
# Exit 0: only orders.id widens INT -> BIGINT; customers.id narrowing is excluded.
```

Both fixture models are enforced. The result contains one non-breaking orders
finding with its existing structured type detail, `models_compared: 1` and
`excluded_models: ["model.shop.customers"]`. It omits `selected_models` because no
`--model` was requested. Default, `--fail-on breaking`, and `--fail-on none` exit 0;
`--fail-on any` exits 1. Omitting exclusion compares both models and exits 1 by
default for customers' `BIGINT -> INT` narrowing.

Exclusions use the same exact case-sensitive key lookup as selection: active or
recognized disabled model keys from either artifact are known. IDs are sorted
and deduplicated, with no names, globs, trimming or version-family expansion.
Unknown IDs exit 2 on stderr with no report, even if all selected models would
be excluded. Exclusion follows selection and enforcement eligibility; it never
includes an unenforced model. `--include-unenforced` remains independent.

A key may be both selected and excluded. If every eligible key is excluded,
zero models are compared, no findings are emitted, and a notice explains the
empty scope. All finding gates exit 0 in that case unless an independent gate such as
`--fail-on-empty-scope` or `--fail-on-unused-policy` is triggered. `selected_models` retains the
requested IDs; `excluded_models` is added only when exclusion is requested,
including disabled-only or otherwise ineligible IDs. Human formats display an
escaped `Excluded models (exact unique_ids):` scope line. The Python API accepts
`excluded_models=None` by default, or an iterable of exact IDs; an explicit empty
iterable records `excluded_models: []` and requests scoped family accounting
without removing any eligible models.

Excluded records contribute no findings, type-parse warnings, alias equivalents,
rename notices or model counts. With either scope option, latest-family findings
use only the remaining eligible records, preserving the last sorted representative
with head metadata preferred and base fallback. An excluded or unenforced sibling
cannot supply latest metadata or a report key. Both complete artifacts still
receive consumed-field and family consistency validation, even for an empty
scope. Metadata warnings and dialect resolution remain artifact-wide. This keeps
the existing recognized disabled-entry validation, without adding full dbt-schema
validation.

Policy matching remains local to emitted details. A type policy for an excluded
finding stays unused and may independently trigger `--fail-on-unused-policy`,
including with `--fail-on none` and with no eligible models. Lifecycle policy
rules remain rejected. Without exclusion, existing reports and behavior remain
unchanged.

### Fail CI on an empty comparison

Use `--fail-on-empty-scope` when CI must compare at least one eligible model:

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/exclusion/base.json \
  --head tests/contract_diff/fixtures/exclusion/base.json \
  --model model.shop.orders --exclude-model model.shop.orders \
  --fail-on none --fail-on-empty-scope --format json
# Exit 1: models_compared is 0; the existing report and scope notice are printed.
```

Without the flag this command exits 0. The gate checks `models_compared == 0`,
so it also catches full empty artifacts, unenforced-only scope, and selected
recognized disabled-only keys. Selection, enforcement eligibility and exclusions
keep their existing exact semantics. `--include-unenforced` prevents the gate
when it makes an active model eligible; it does not activate disabled models or
undo exclusions. A compared unchanged model passes the empty-scope gate despite
having no findings. Additions, removals and other comparisons still count as
compared models and keep their existing finding gates.

The flag is independent of `--fail-on breaking|any|none` and
`--fail-on-unused-policy`: any requested gate can fail the comparison. Reports
in all four formats, scope fields, notices, warnings and schema version remain
unchanged. The flag does not bypass global input validation: invalid manifests,
policies and unknown selected/excluded IDs still exit 2 with stderr and no report.
It verifies comparison coverage, without validating SQL, data or warehouse state.

### Leaf explanations for CI and reviewers

Type, constraint, column, enforcement and model addition/removal/disabling findings include a rule,
the reason it applies, and an actionable suggestion in every output format.
Reproduce the local synthetic
STRUCT (ClickHouse named Tuple) / ARRAY / Nullable decimal fixture:

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/nested-base.json \
  --head tests/contract_diff/fixtures/explanations/nested-head.json --format json
# Exit 1: Nullable(Decimal(10,2)) becomes Decimal(8,2) at payload.items.element.amount.
```

The one column finding contains two atomic details: nullability removal at
`type.items.element.amount.nullable`, then decimal narrowing at
`type.items.element.amount`. The latter has this JSON excerpt:

```json
{
  "path": "type.items.element.amount",
  "path_segments": [
    {"kind": "field", "name": "items"},
    {"kind": "array_element"},
    {"kind": "field", "name": "amount"}
  ],
  "before_type": "DECIMAL(10,2)",
  "after_type": "DECIMAL(8,2)",
  "classification": "narrowing",
  "breaking": true,
  "rule": "decimal_capacity",
  "certainty": "known",
  "reason": "Decimal integer digits 8 -> 6; fractional digits 2 -> 2. Both capacities must be retained or increased for widening; this change is narrowing.",
  "remediation": "Retain the original integer and fractional capacity, or publish a new model version after validating value ranges and rounding requirements."
}
```

These are additive fields within schema version 1; existing fields, finding
counts, classifications and exit policies remain unchanged. CI consumers should
ignore unknown object fields and use the following structured fields rather than
parsing human messages:

| Detail field | Stable meaning |
| --- | --- |
| `path_segments` | Ordered path relative to the finding's exact `model` and `column` keys. `field` has the exact canonical `name` (including quotes when quoted); `array_element`, `map_key`, `map_value`, and `nullability` are selectors. An empty list means the type root, the whole column for `column_removed` / `column_added`, the effective model contract root for `enforcement_removed` / `enforcement_added`, or the whole model for `model_added` / `model_removed` / `model_disabled` / `model_identity_changed`. This distinguishes quoted names containing dots from nested fields and field names from container selectors. |
| `before_type`, `after_type` | Canonical types for each atomic type step; null means an absent field. A nullability step changes only nullability, then subsequent details compare the underlying types. Legacy nullability `before` / `after` retain the strings `True` / `False`. For unparseable types and whole-column removals/additions, these fields retain the raw input instead of inventing canonical types. |
| `rule` | Policy identifier from the table below; applies to this detail, including overrides that make widening breaking. |
| `certainty` | `known` means classified under the declared metadata policy, not proven warehouse cast safety. `unknown` means a type could not be parsed. |
| `reason`, `remediation` | Human explanation and suggested next action under the default rule; wording may evolve. |
| `policy` | Present only on a matched override: `source` file path, JSON `pointer`, configured `severity`, and original `default_breaking`. The detail’s `breaking` is the effective impact. |

| Rule identifiers | Policy applied |
| --- | --- |
| `decimal_capacity`, `integer_range`, `string_capacity` | Compare decimal integer/fractional capacity, full integer range, or declared string length. |
| `scalar_incompatible` | No supported compatibility rule for this type/parameter change. |
| `nested_type_invariant`, `strict_widening` | Nested retyping or strict mode overrides otherwise allowed widening. |
| `nullability_added`, `nullability_removed` | Adding Nullable widens; removing it narrows. |
| `struct_field_added`, `struct_field_removed`, `struct_field_order` | Named field addition, removal, or observable order change. Reordering points at the containing STRUCT because the change concerns multiple fields. |
| `unparseable_type` | Unknown compatibility; raw equality never proves safety. |
| `constraint_guarantee_removed`, `constraint_guarantee_added` | Exact constraint removal/addition under the metadata policy. |
| `column_removed` | Loss of the entire column name and contract; always breaking, outside project policy overrides. |
| `column_added` | Addition of an entire column; non-breaking under the metadata policy, outside project policy overrides. This does not validate its type, SQL or data. |
| `enforcement_removed` | Loss of the model contract enforcement declaration; always breaking, outside project policy overrides. SQL, data and engine enforcement behavior are not validated. |
| `enforcement_added` | Addition of the model contract enforcement declaration; non-breaking, outside project policy overrides. SQL, data and engine enforcement behavior are not validated. |
| `model_added` | Addition of the declared model contract to the active manifest; non-breaking, outside project policy overrides. No warehouse creation, SQL/data validation or engine guarantees are inferred. |
| `model_removed` | Loss of the declared model contract from the active manifest, with no matching disabled entry; always breaking, outside project policy overrides. No warehouse deletion or SQL/data validation is inferred. |
| `model_disabled` | Loss of the active declared model contract with a recognized disabled key; always breaking, outside project policy overrides. Disabling metadata does not establish warehouse deletion or validate SQL/data. |
| `model_identity_changed` | Effective package/name family or version changed under the same unique_id; always breaking, outside project policy overrides. No migration exemption or warehouse change is inferred. |
| `latest_version_changed` | Declared latest-version metadata changed for an effective package/name family; non-breaking, outside project policy overrides. No consumer migration or warehouse behavior is inferred. |

For constraints, `before` / `after` contain the exact normalized guarantee object
or null. The path is `constraints`, relative to the finding's column (or model
when `column` is null). Its single `constraint` path segment has a `signature`:
the JSON serialization of that guarantee with sorted keys, excluding metadata
such as constraint names and warning flags. This identifies expressions and
ordered FK mappings without relying on unstable array indices. The CLI does not
parse SQL check expressions to infer nested field references or SQL equivalence.
Run the `constraints-base.json` / `constraints-head.json` fixture in the same
directory to see a column check expression change and model unique removal
(exit 1, two breaking findings and one non-breaking addition).

The `opaque-equal`, `opaque-changed`, and `malformed` fixture pairs demonstrate
failure behavior. Each reports `unparseable_type`, `certainty: "unknown"`, warnings,
and an empty type path. Equal opaque strings exit 0 by default but remain findings;
changed or missing strings exit 1. `--fail-on any` rejects all uncertain comparisons.
Malformed manifest JSON still exits 2. Suggestions to publish a new model version
require keeping the original contract available; changing an existing model's
version or deleting the original remains breaking.

Whole-column removal has one detail with `path: "column"`, `path_segments: []`,
`classification: "column_removed"` and `rule: "column_removed"`. The location is
the pair of exact `model` / `column` keys on the finding: a column named
`payload.items` is one literal column, separate from field `items` inside
column `payload`. No dot splitting, case folding or synthetic nested removals
are applied. Reproduce this boundary with:

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/column-removed-base.json \
  --head tests/contract_diff/fixtures/explanations/column-removed-head.json --format json
# Exit 1: payload.items is removed; payload and its nested items field remain.
```

The detail's `before` and `before_type` preserve the exact raw `data_type`, here
`STRUCT("a.b" INTEGER, amounts ARRAY(DECIMAL(10,2)))`; `after` and `after_type`
are null. Missing/null old types remain null and empty strings remain empty.
`certainty: "known"` concerns the lost column, even when its old type is opaque
or malformed; it does not assert type parsing or cast safety, and removal alone
does not add parse warnings. Removed column constraints are covered by this one
finding rather than counted again. The reason and remediation explain the lost
consumer reference and how to retain the old contract during version migration.
Probable renames still break. All output formats show the removed type and action;
`--fail-on none` changes only the exit status.

Whole-column addition also has one detail at `path: "column"`,
`path_segments: []`, with `classification` and `rule` both `column_added`.
The finding retains exact model/column keys, including literal dots, quotes and
case. Its `before` / `before_type` are null; `after` / `after_type` preserve the
raw declared `data_type`, including whitespace. Missing/null types stay null
and empty strings stay empty. For example:

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/column-added-base.json \
  --head tests/contract_diff/fixtures/explanations/column-added-head.json --format json
# Exit 0: id INT remains; literal payload.items STRUCT(amount DECIMAL(10,2)) is added.
# Adding --fail-on any exits 1; --fail-on breaking or none exits 0 with the same report.
```

`certainty: "known"` identifies the structural addition only. It does not claim
that the type is valid, that producing SQL runs, or that data satisfies the
contract. Opaque, malformed and missing added types retain their existing parse
warnings; the addition remains one non-breaking finding. Its remediation asks
users to validate the declared type, SQL and data in their warehouse before
relying on the column. New-column constraints are covered by the same finding;
unchanged columns gain no addition detail, new models retain their model-level
finding, and probable renames still include a breaking removal. All formats show
the raw type and guidance.

Enforcement removal has one model-level detail with `path: "contract"`,
`path_segments: []`, and `classification` / `rule` both `enforcement_removed`.
The finding uses the exact model key and `column: null`. The detail's `before`
is boolean `true` and `after` is boolean `false`; it has no type fields.
Existing top-level `before` / `after` remain null. Reproduce it with:

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/enforcement-removed-base.json \
  --head tests/contract_diff/fixtures/explanations/enforcement-removed-head.json --format json
# Exit 1: model.shop.orders retains id INT but turns enforcement off.
# Default/breaking/any exit 1; none exits 0 with the same report.
```

The effective flag uses either node-level or config contract declarations;
missing enforcement defaults to false and conflicting explicit values remain
input errors. Moving a declaration between those locations without changing
its value creates no removal. `certainty: "known"` describes the declaration
loss only: SQL, data and warehouse enforcement behavior are not validated.
The guidance asks users to restore the declaration or preserve the original
contract while versioning and migrating consumers, with separate validation.
Model additions retain their existing summary behavior; model
removal and disabling have their own details below. Enforcement overrides remain rejected for every
severity. A valid type/constraint policy can remain unused when only enforcement
changes; `--fail-on-unused-policy` still gates that independently of `--fail-on none`.

Enforcement addition has the same model contract root and exact model key,
with `column: null`, `path_segments: []`, and `classification` / `rule` both
`enforcement_added`. Its detail records boolean `before: false`, `after: true`
and `breaking: false`; top-level `before` / `after` remain null, with no type
fields added. For example:

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/enforcement-added-base.json \
  --head tests/contract_diff/fixtures/explanations/enforcement-added-head.json --format json
# Exit 0: model.shop.orders retains id INT and declares enforcement.
# Default/breaking/none exit 0; any exits 1 with the same report.
```

The effective flag follows the same node/config and missing-false rules as
removal; unchanged enforcement gains no addition finding. `certainty: "known"`
identifies the added declaration only. The reason explicitly excludes SQL,
data and warehouse enforcement validation; remediation asks users to validate
these separately before relying on the declaration. Enforcement additions
remain outside policy overrides for both severities. An unused type/constraint
selector still triggers `--fail-on-unused-policy` independently of other gates.
All formats show the booleans and guidance.

Model removal has one model-root detail with `path: "model"`,
`path_segments: []`, and `classification` / `rule` both `model_removed`.
The finding retains the exact model `unique_id`, `column: null`, and existing
null top-level `before` / `after`. The detail's `before` is the exact model key
and `after` is null, identifying declared presence followed by absence; these
are identifiers, not types, and no `before_type` / `after_type` is added.

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/model-removed-base.json \
  --head tests/contract_diff/fixtures/explanations/model-removed-head.json --format json
# Exit 1: base declares enforced model.shop.orders with id INT; head has no model or disabled entry.
# Default/breaking/any exit 1; none exits 0 with the same report.
```

`certainty: "known"` identifies loss of a declared model contract in the manifest
only. It does not infer warehouse deletion, SQL execution, data validation or
engine guarantees. Guidance asks users to restore the original model contract,
or keep it available while publishing a new version and migrating consumers.
Removing an existing version still breaks even when a replacement version is
added; the existing version-bump message remains. Removed columns and constraints
are covered by this one model finding and are not expanded into duplicate losses
or parsed for type warnings.

A matching `disabled` entry or `config.enabled=false` produces `model_disabled`
and receives its own disabled detail below, without a removal detail. Unchanged models, additions and excluded
unenforced removals also receive none. `--include-unenforced` explicitly includes
unenforced removal. Lifecycle overrides remain rejected for both severities.
Valid type/constraint policies cannot consume the model detail; unused entries
still gate independently with `--fail-on-unused-policy`, including under
`--fail-on none`.

Model disabling has one detail at `path: "model"`, `path_segments: []`, with
`classification` and `rule` both `model_disabled`. A matching model entry in
`disabled[unique_id]` or `config.enabled=false` produces the same finding.
The detail's `before` is the exact model key and `after` is null; `column` and
top-level `before` / `after` remain null, with no type fields. For example:

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/model-disabled-base.json \
  --head tests/contract_diff/fixtures/explanations/model-disabled-head.json \
  --model model.shop.orders --format json
# Exit 1: one selected breaking model_disabled, with one model-root detail.
# model-disabled-config-head.json demonstrates config.enabled=false instead.
# Default/breaking/any exit 1; none exits 0 with the same report.
```

The reason describes loss of the active declared contract only. `certainty: "known"`
identifies that metadata transition; it does not establish warehouse deletion,
validate SQL/data or promise engine behavior. Guidance asks users to re-enable
and restore the original contract, or preserve it while publishing a new version
and migrating consumers. Exact keys containing dots, quotes and case remain one
model root. Removed columns/constraints are not expanded or parsed for type warnings.
Existing counts, version messages and `model_removed` details are retained.

`--model` scopes these details to eligible selected models; unselected disabled
models contribute no findings. Unenforced models remain excluded unless
`--include-unenforced` is supplied. Disabled-only keys are recognized but have no
active comparison. Without either scope option, the full comparison remains in effect and
JSON omits `selected_models`; disabled findings gain the same additive detail.
Unchanged and added models gain no disabled detail. Lifecycle policy overrides
remain rejected; type/constraint selectors stay unused for a disabled model and
can trigger `--fail-on-unused-policy` independently of `--fail-on none`.

Model addition has one detail at `path: "model"`, `path_segments: []`, with
`classification` and `rule` both `model_added`. The detail's `before` is null
and `after` is the exact model `unique_id`, including literal dots, quotes and
case. `column` is null and no type fields are added. Existing top-level fields
retain their meaning: `before` and `version` are null; `after` holds the head
model's declared version (or null), rather than its identifier.

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/model-added-base.json \
  --head tests/contract_diff/fixtures/explanations/model-added-head.json \
  --model model.shop.orders --format json
# One selected non-breaking model_added with one model-root detail.
# Default/breaking/none exit 0; any exits 1 with the same report.
```

`certainty: "known"` describes an added declaration in the active manifest only.
It does not establish warehouse creation, SQL validity, data satisfaction or
engine guarantees. Guidance asks users to validate the declared contract,
producing SQL, data and warehouse enforcement separately. New columns and
constraints remain covered by this one model finding; existing added-type parse
warnings are retained. Adding a replacement version never excuses removal or
breaking changes to the original version.

A disabled-to-active transition at the same key also reports `model_added`.
`--model` retains exact selection and enforcement filtering: unselected additions
contribute no findings, parse warnings or counts; unenforced additions require
`--include-unenforced`. Without selection the full comparison gains the same
detail, with no `selected_models` field. Lifecycle policy overrides remain
rejected; type/constraint entries for a new model stay unused and can trigger
`--fail-on-unused-policy`, even with `--fail-on none`.

Model identity changes have one detail at `path: "model"`, `path_segments: []`,
with `classification` and `rule` both `model_identity_changed`. The detail's
`before` and `after` objects each contain `unique_id` (the exact model key),
`package_name`, `name`, and the raw `version` value (or null when absent).
Package/name values are the effective family consumed by the existing comparison:
when `package_name` is absent, it uses the second dot-separated key component
(or an empty string for a key without dots). An omitted node `unique_id` still
uses the model key. Literal dotted/quoted keys remain intact at the model root;
this explanation adds no identity validation or key parsing rules.

```sh
# Exit 1: one selected breaking model_identity_changed with one model-root detail.
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/model-identity-base.json \
  --head tests/contract_diff/fixtures/explanations/model-identity-head.json \
  --model model.shop.orders --format json
```

For this version-only `1 -> 2` example, detail values are
`{"unique_id":"model.shop.orders","package_name":"shop","name":"orders","version":1}`
and the same object with `version: 2`. Top-level `before: 1`, `after: 2`,
`version: "1"`, and the existing “breaking change without version bump” message
are retained. Default, `--fail-on breaking`, and `--fail-on any` exit 1;
`--fail-on none` exits 0. Package/name changes at the same key are also breaking,
even when the version is unchanged. One detail covers simultaneous family/version
changes; independent type/constraint and latest-version findings remain separate.

The reason describes declarations only. Preserve the original contract under its
original identity while publishing a separate model version; a changed identity
or a new sibling version gives no migration exemption. `certainty: "known"`
identifies consumed metadata, without inferring warehouse renaming, creation,
deletion, SQL/data validity or an engine guarantee. Exact model selection and
enforcement eligibility still apply; lifecycle policy overrides remain rejected,
and unused-policy diagnostics remain local to each comparison.

Latest-version changes have one detail at `path: "model"`, `path_segments: []`,
with `classification` and `rule` both `latest_version_changed`. Its `before`
and `after` objects contain the effective `package_name`, `name`, and exact
consumed `latest_version` value. Missing or null latest metadata is represented
by null; strings and numbers retain their raw JSON types. Package/name use the
existing family fallback described above. No type fields or sibling paths are
added, including for literal dotted/quoted model keys.

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/latest-version-base.json \
  --head tests/contract_diff/fixtures/explanations/latest-version-head.json \
  --model model.shop.orders.v1 --format json
# One selected non-breaking latest_version_changed with one model-root detail.
# Default/breaking/none exit 0; any exits 1 with the same report.
```

Both records repeat the family's latest metadata: `1` in base, `2` in head.
Selecting v1 reports only that key, with top-level `before: 1`, `after: 2`,
`version: "1"`, and one model compared. Detail values are
`{"package_name":"shop","name":"orders","latest_version":1}` and the same
object with `latest_version: 2`. Selecting both versions retains one family
finding at the last sorted record declaring non-null latest metadata (head
preferred, base fallback); it does not expand a single selection to siblings.
An eligible selected record with no latest metadata cannot inherit a change
from an unselected sibling. Enforcement eligibility, artifact-wide family
validation and existing active-family behavior on identity changes are retained.
Without either scope option, the existing full-family representative remains unchanged.

`certainty: "known"` identifies consumed declarations only. Latest metadata
addition/removal/change is non-breaking under this metadata policy; it does not
establish consumer migration, warehouse behavior, SQL/data validity or engine
enforcement. Guidance asks users to review consumers' version selection and
validate SQL, data and warehouse enforcement separately. Independent identity,
type, constraint and lifecycle findings still apply; changing latest metadata
never excuses a breaking change to an existing contract. Latest/lifecycle policy
overrides remain rejected, and unused-policy diagnostics stay comparison-local.
### Project policy overrides

Policies are opt-in: omitting `--policy`, or using `{"version": 1, "rules": []}`,
retains the conservative defaults. A policy changes the CI impact of an existing
known type/constraint finding; it does not prove cast safety, suppress findings,
change classifications, or alter alias equivalences. There is no automatic file
discovery, inheritance, include, or execution.

The minimal runnable example is [`examples/contract_diff/project-policy.json`](../examples/contract_diff/project-policy.json):

```json
{
  "version": 1,
  "rules": [{
    "model": "model.shop.orders",
    "column": "payload",
    "path_segments": [
      {"kind": "field", "name": "items"},
      {"kind": "array_element"},
      {"kind": "field", "name": "amount"}
    ],
    "rule": "decimal_capacity",
    "severity": "non_breaking"
  }]
}
```

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/nested-base.json \
  --head tests/contract_diff/fixtures/explanations/nested-head.json \
  --policy examples/contract_diff/project-policy.json --format json
# Exit 1: decimal narrowing is overridden, but the separate nullability removal breaks.
```

The decimal detail retains `classification: "narrowing"`, its types, reason and
certainty, with `breaking: false` and:

```json
"policy": {
  "source": "examples/contract_diff/project-policy.json",
  "pointer": "/rules/0",
  "severity": "non_breaking",
  "default_breaking": true
}
```

Text, Markdown and GitHub output also display this source and entry. Each rule
requires all five keys shown above. Only `breaking` and `non_breaking` are valid
severities; `breaking` can escalate an allowed change. Model `unique_id`, column,
complete `path_segments`, and `rule` must all match exactly. Names are literal:
no globbing, prefixes, case folding or descendant matches. Copy paths from a
JSON report, including quotes in quoted field names. `[]` selects only the type
root. `nullability` must be the last selector. A field named `element` differs
from `array_element`; a quoted field `"a.b"` differs from nested fields `a` / `b`.

Known type/constraint rule identifiers in the table above are supported, except
`unparseable_type`, which is **always rejected in a policy**, even when currently
unmatched. `column_removed`, `column_added`, `enforcement_removed`, `enforcement_added`,
`model_removed` and all other model lifecycle rules are also rejected:
their explanations do not expand policy scope. Unknown compatibility cannot be
configured as safe. For a constraint
rule, use exactly one `constraint` selector with the normalized `signature`
string copied from a report; `column: null` selects model constraints only.
Model lifecycle, enforcement, and whole-column addition/removal are outside the
policy's scope. Unmatched entries do not change impact. A policy for `integer_range`
does not override `strict_widening` or `nested_type_invariant`; select the actual
reported rule explicitly to override those defaults.

A finding is breaking if **any** atomic detail remains breaking after policy.
Summaries, version-bump messages and `--fail-on breaking` use that effective
impact. `--fail-on any` still fails on downgraded findings; `--fail-on none` still
rejects invalid input. Unknown rules/keys, missing selectors, malformed JSON,
duplicate object keys, non-finite constants, duplicate/conflicting entries and
invalid severities exit 2 on stderr with no report. Validation happens even when
no contracts or policy entries match. Policy files must be regular UTF-8 JSON,
at most 1 MiB, with at most 1,000 entries and 65 selectors per path. They cannot
load other files or configure parsing, dialects, or input limits.

#### Unused policy diagnostics

Every unused entry produces one `policy_diagnostics` object in policy array
order, including when no enforced contracts are compared. Each has
`code: "unused_policy_entry"`, `source`, JSON `pointer`, the exact configured
`model`, `column`, `path_segments`, `rule`, `severity`, and a human `message`.
The message also appears in `warnings` and contributes to `summary.warnings`.
Text, Markdown and GitHub formats display it as a warning with the policy file,
entry pointer and selectors. With no policy, an empty policy, or all entries
matched, `policy_diagnostics` is `[]`.

An entry is used only when it matches a known type/constraint detail emitted
in **this comparison**, even if its severity equals the default. An unused entry
may have stale selectors, but unchanged/alias-equivalent columns and excluded
unenforced models also leave entries unused. Model/column lifecycle changes and
unknown types cannot consume type/constraint entries. The diagnostic does not
guess which selector is wrong or broaden matching to another path. Reusing a
policy in the Python API starts fresh accounting for each comparison.

By default, warnings alone do not change `--fail-on breaking`, `any` or `none`:
`any` counts findings, not diagnostics. To reproduce a matching decimal override alongside
three unused entries (stale model, stale column, and a root path that does not
match the nested decimal detail):

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/nested-base.json \
  --head tests/contract_diff/fixtures/explanations/nested-head.json \
  --policy examples/contract_diff/unused-policy.json --format json
# Exit 1: nullability still breaks; three warnings point to /rules/1, /rules/2, /rules/3.
```

Using `nested-base.json` for both inputs instead produces no findings, four
unused-entry warnings, and exit 0 under every fail-on mode by default. Add
`--fail-on-unused-policy` to opt in to exit 1 for those diagnostics:

```sh
datawrench contract-diff \
  --base tests/contract_diff/fixtures/explanations/nested-base.json \
  --head tests/contract_diff/fixtures/explanations/nested-base.json \
  --policy examples/contract_diff/unused-policy.json --fail-on none --fail-on-unused-policy --format json
# Exit 1: no findings, four unused entries; omit --fail-on-unused-policy for exit 0.
```

This gate is independent of `--fail-on breaking|any|none` and changes only the
exit status, preserving findings, severities, diagnostics and output in every
format. Absent/empty/all-matched policies do not trigger it; other warnings and
notices do not trigger it. Unused entries still trigger it when no contracts are
found or models are excluded. Invalid manifests or policies still exit 2.
Unused is comparison-local and does **not** prove that an entry is stale: use this
gate only when every configured entry is expected to match in the chosen pair.


## Compatibility policy

Models are matched by exact dbt `unique_id`, and columns by exact manifest key.
A model is in scope when its contract is enforced in **either** artifact; this
also detects enforcement being removed. Node-level `contract.enforced` and
`config.contract.enforced` are supported; contradictory values are rejected.
Non-model resources are ignored. `disabled` entries and `config.enabled=false`
are recognized.

| Change | Policy |
| --- | --- |
| Model removed/disabled; enforcement turned off | Breaking |
| Enforcement turned on | Non-breaking declaration addition |
| Column removed / added | Breaking / non-breaking |
| Unique same-type removed+added column pair | Probable rename notice; removal still breaks |
| `int` / `integer`; unbounded `string` / `varchar` / `text`; `float` / `float64` / `double` | Identical under this contract policy; raw differences recorded in `equivalents` |
| Integer range grows (`INT` → `BIGINT`); decimal capacity grows; string length grows | Widening, non-breaking unless strict |
| Integer/string/decimal capacity shrinks | Narrowing, breaking |
| Decimal scale grows but integer capacity shrinks | Incompatible, breaking |
| Other changed scalar types/parameters | Conservatively incompatible, breaking |
| Named STRUCT/Tuple field added | Non-breaking |
| Named field removed, retyped, or surviving fields reordered | Breaking (even nested numeric widening) |
| ARRAY element or MAP key/value retyped | Breaking; recurse to leaf paths |
| STRUCT field added within ARRAY/MAP | Non-breaking |
| `Nullable(T)` added / removed | Widening / narrowing; nested changes are conservatively breaking |
| `LowCardinality(T)` added / removed | Encoding-only equivalence; compare underlying `T`, retaining nullability |
| Constraint removed / added | Breaking / non-breaking |

Supported constraints at both column and model scope: `not_null`, `primary_key`,
`unique`, `foreign_key`, `check`. Guarantees are compared by type, expression,
ordered columns, reference target and ordered target columns. Names and adapter
warning switches are metadata. Changing an expression or FK mapping is a removal
plus an addition. SQL expressions are never evaluated or rewritten; even literal
whitespace differences remain significant. Unsupported constraint kinds or
semantic attributes produce exit 2.

An existing version with any breaking change gets the explicit error message
**“breaking change without version bump”**. A new version alongside an unchanged
old version is allowed. Removing or modifying that old version still breaks;
changing the `version` under the same `unique_id` is also breaking.
`latest_version` metadata changes are reported once per `(package_name, name)`
family, including removal of that metadata. There is no automatic migration or
assumption that a version bump excuses deleting an existing contract.

## Reproduce the comparison

From a checkout of this repository:

```sh
uv run --extra test pytest tests/contract_diff -q
uv run --extra sqlglot python tests/contract_diff/compare_baseline.py
uv run --extra sqlglot python tests/contract_diff/compare_baseline.py --json
```

The replay compares the same seven changed type pairs against an independent
emulation of the specified lowercase/prefix-before-first-`(` policy. It measures
**detection**, not execution speed:

| Observation in the checked-in fixture | Count |
| --- | ---: |
| Changed type strings on matched columns | 7 |
| Prefix baseline calls unchanged | 5 |
| Breaking changes missed by prefix baseline | 4 |
| Alias-equivalent changes flagged by prefix baseline | 1 |

The four missed breaking changes are decimal narrowing, a STRUCT leaf change,
an ARRAY/STRUCT leaf change, and a MAP value change. The test suite asserts the
counts and exact affected columns. `--base`, `--head`, and `--dialect` let you
replay your own artifacts. Constraints, model lifecycle and column additions or
removals are deliberately outside this baseline experiment. The replay script
independently implements only the specified `lowercase(trim(type.split('(', 1)[0]))`
policy; it is not a full reproduction of a particular dbt release, nor a benchmark
of dbt runtime performance.

### Real dbt artifact fixture

Default tests include CLI comparison of checked-in model-node extracts generated
by **dbt-core 1.12.6 + dbt-duckdb 1.11.1 + DuckDB 1.4.4**, not just hand-written
JSON (`tests/contract_diff/fixtures/real/`). They contain one nested
decimal-to-string breaking change. Default tests run offline without installing
or executing dbt.

Optional regeneration (uses a larger, temporary dependency set):

```sh
uv run --extra sqlglot --with dbt-core==1.12.6 --with dbt-duckdb==1.11.1 --with duckdb==1.4.4 \
  python tests/contract_diff/generate_real_fixture.py
# The script refuses to overwrite tests/contract_diff/.fixture-build; remove it before a repeat.
```

The checked-in synthetic project (`tests/contract_diff/fixture_project/`) uses an
in-memory profile, one thread and no hooks or remote packages. Regeneration
explicitly runs `dbt parse` twice with telemetry disabled; it does not run models
or access production data. Generated fixtures strip unrelated dbt sections/macros
and normalize volatile metadata. Hand-written fixtures can be regenerated with
`python tests/contract_diff/make_fixtures.py`.

## Limits and safety

- This is a conservative contract policy, not proof of warehouse cast safety.
  The requested float alias policy deliberately equates SQLGlot FLOAT/DOUBLE
  (also REAL/Float32); engines may store them with different precision. Disable
  this tool as a sole precision-sensitive gate, or inspect the raw equivalences.
- SQLGlot applies dialect-specific defaults (for example Snowflake numeric
  types). DuckDB declared `VARCHAR(n)` limits are intentionally retained for
  contract comparison even though DuckDB itself ignores those lengths.
- Only documented widening rules are allowed. Cross-family numeric casts,
  timestamps/timezones, ENUM changes and other parameter changes are conservative.
  Exact field/column spelling is retained, including case; no rename inference
  is applied as an exemption. Quoted and unquoted nested identifiers are distinct.
- Named STRUCT/Tuple, ARRAY, MAP and Nullable/LowCardinality are understood.
  User-defined types, anonymous tuples, fixed-size arrays, collations, field
  constraints, BYTE/CHAR parameter modifiers and unsupported AST forms fall back with explicit warnings.
  Fallback trims only outer whitespace: quoted content and case are preserved.
  Equal opaque strings are an **unparseable finding**, not a silent equivalence;
  unequal or missing type strings are breaking. Use `--fail-on any` to require
  fully understood contracts.
- New models/versions have no previous shape to compare and are reported as
  added; their type parse failures are warned without a breaking classification.
  The same applies to newly added columns. This tool does not validate their contract against model SQL or an
  entire dbt JSON schema. Resource fields unrelated to contracts are ignored.
- Inputs must be UTF-8 JSON regular files, at most 64 MiB each. Duplicate keys,
  non-finite JSON constants and invalid consumed model/constraint fields are
  rejected. Type parsing is capped at 8,192 characters and 64 nesting levels;
  over-limit types use the explicit warned fallback. FIFO/device inputs fail
  promptly. Symlinks to regular files may be read; no input paths are written.
- SQL/type strings and constraint expressions are parsed or compared as data.
  The CLI never executes SQL, Jinja, shell scripts, dbt, external workflows or
  network calls, and has no write/dry-run mode. It never reads credential profiles
  or environment variables. Reports contain supplied model/type/constraint data;
  treat report artifacts according to your input's sensitivity.
- Not a replacement for `state:modified` selection, a dbt patch, a warehouse data
  diff, or a full emulation of every dbt release's breaking-change behavior.

## License

Part of datawrench, licensed under Apache-2.0 ([LICENSE](../LICENSE)); the standalone project was also Apache-2.0 and its NOTICE is merged into [NOTICE](../NOTICE). The original `dbt-contract-diff` source, tests and fixtures were Apache-2.0
(all model SQL, YAML and synthetic values were written for the project). See
[LICENSE](../LICENSE) and [THIRD_PARTY.md](../THIRD_PARTY.md); the SQLGlot MIT
notice is in [`../licenses/contract-diff/SQLGLOT-MIT.txt`](../licenses/contract-diff/SQLGLOT-MIT.txt).
Independent of, and not affiliated with or endorsed by, dbt Labs or SQLGlot.
