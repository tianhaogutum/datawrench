# datawrench polars-vet

`datawrench polars-vet` (formerly `polars-vet`) is an offline, dependency-free Python linter that catches 11 Polars performance and API anti-patterns before they reach production. It resolves Polars import aliases and follows simple local frame/expression assignments. It includes a CLI, a flake8 extension, conservative source fixes, and real, replayable Polars comparisons.

```python
import polars as pl
q = pl.read_csv("people.csv").lazy()            # PLV001: scan_csv
names = pl.col("name").map_elements(lambda x: x.upper())  # PLV002: str.to_uppercase
```

## Install and run

Python 3.11+ on Linux. The linter itself has no runtime dependencies beyond the standard library, and no Polars installation is needed to lint.

```sh
pipx install datawrench               # or: pip install datawrench
datawrench polars-vet --version       # prints the bare datawrench version, e.g. 0.1.0
datawrench polars-vet check your_package/
# Returns 1, with path:line:column CODE message diagnostics.

# From a datawrench checkout:
datawrench polars-vet check examples/polars_vet/
```

`python -m datawrench polars-vet` is equivalent to `datawrench polars-vet`. The only subcommand is `check`.

`PATHS` accepts explicit files and recursively scanned directories. Directory scans include `.py` files and skip `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, and `.pytest_cache`. Explicit regular files can have any extension. Overlapping paths are deduplicated. Source is parsed, **never imported or executed**.

```sh
datawrench polars-vet check src/ --select PLV001,PLV002 --ignore PLV002
datawrench polars-vet check src/ --format json
datawrench polars-vet check src/ --format sarif > results.sarif
datawrench polars-vet check src/ --format github
datawrench polars-vet check examples/polars_vet/fixable_copy.py --fix
datawrench polars-vet check src/ --exit-zero
```

Exit codes: **0** for no remaining selected diagnostics, **1** for diagnostics/source errors, **2** for invalid CLI usage (including unknown codes). `--exit-zero` masks diagnostics, not usage errors. `--select`/`--ignore` accept exact comma-separated codes from the table below, not prefixes. `PLV000` reports syntax, file, encoding, size, suppression, or safe-rewrite failures and is always active. One broken file does not stop other files from being checked. No user source text is included in error messages.

JSON contains `version` (the datawrench version), `diagnostics`, and `fixedFiles`. Each diagnostic has `path`, 1-based `line`/`column`, exclusive `end_line`/`end_column`, `code`, `message`, and `fixable`. SARIF uses version 2.1.0 with `tool.driver.name` kept as `polars-vet` for continuity with existing code-scanning results; GitHub output escapes workflow-command characters. Columns count Unicode characters. Output order is deterministic.

Use `# noqa: PLV001` or `# noqa: PLV001, PLV002` on the diagnostic's **starting line**; bare `# noqa` suppresses all rules on that line. A string containing `# noqa` is not a directive. There are no file-level suppression/configuration directives in this version.

## Reasoned line suppressions

For a reviewable exception, use an exact rule list and a nonempty reason:

```python
from polars import read_csv as read, col as c
q = read("data.csv").lazy(); expr = c("x").map_elements(lambda x: x + 1)  # polars-vet: ignore[PLV001] -- eager validation is intentional
```

This suppresses PLV001 on that line, including reader aliases. PLV002 on the same line and PLV001 on other lines still report. Multiple codes use `ignore[PLV001, PLV002]`. The comment must start with `# polars-vet:` on the diagnostic's **starting line**, even for multiline expressions; a comment-only line never applies to the following line. Strings are not directives. The spelling and codes are case-sensitive. The directive keeps its original `polars-vet` spelling under datawrench, so existing comments keep working.

Unknown codes (including PLV000), duplicate codes, missing reasons, and malformed directives report PLV000 at the comment and suppress nothing. Error messages do not echo the comment or reason. This strict validation applies to `polars-vet` directives; legacy `noqa` keeps its existing compatibility behavior.

```sh
datawrench polars-vet check src/ --report-unused-suppressions
datawrench polars-vet check src/ --select PLV001 --report-unused-suppressions
```

Unused reporting is opt-in and emits one PLV000 per unused code, considering only rules enabled by `--select`/`--ignore`. A partially used list reports only its unused codes. Suppressions never create fixes: suppressed PLV001 expressions remain unchanged under `--fix`, and comments and reasons are preserved. Other unsuppressed, selected fixes still apply. The analyzer API exposes `report_unused_suppressions=True` and `selected={"PLV001", ...}` with the same behavior.

To audit intentional exceptions without changing source:

```sh
datawrench polars-vet check src/ --format json --show-suppressions
```

`--show-suppressions` requires `--format json` (other formats, including the default text format, return 2). It adds a `suppressions` array with one entry per exact code in each **valid reasoned directive**, ordered by path, line, and code. For the example above, it includes:

```json
{"path": "src/job.py", "line": 2, "code": "PLV001", "reason": "eager validation is intentional", "status": "used"}
```

Paths use the same spelling as diagnostics; lines are 1-based. Reasons retain internal text and Unicode, with outer whitespace trimmed. `used` means at least one selected diagnostic was suppressed on that starting line; `unused` means none was found; `disabled` means the code was excluded by `--select`/`--ignore`; `unknown` means syntax validation or analysis could not finish for that file. A disabled code stays disabled even in a broken file. Usage follows the existing heuristic analyzer, not runtime execution or semantic proof. Multiple suppressed findings for the same code/line produce one entry.

Strings, legacy `noqa` (which has no required reason), and malformed/unknown/duplicate-code directives are excluded. Invalid directives still report PLV000 and suppress nothing. Read/discovery failures still report PLV000; unreadable files contribute no entries. Tokenizable valid directives in syntax-invalid files can appear with unknown usage; tokenization failures can leave a partial inventory; consult the per-path completeness metadata below.

The same flag also adds per-path completeness metadata, including files with **zero** valid directives:

```json
{"suppressionAudit": {"complete": false, "paths": [{"path": "missing.py", "status": "discovery-error"}, {"path": "src/empty.py", "status": "complete"}]}}
```

`paths` is sorted by displayed path and deduplicated by absolute normalized path, using the same path spelling as diagnostics/inventory for checked files. It includes every discovered file and every failed discovery path (which can be a directory); successfully traversed directories have no separate row. An empty successfully scanned directory has `paths: []` and `complete: true`. Deliberately skipped directories/symlink directories are outside the scan scope. The overall `complete` is true exactly when every row has status `complete`. Each row contains only `path` and `status`, with no source, reason, or exception text. Statuses mean:

| Status | Meaning |
| --- | --- |
| `complete` | Tokenization, Python syntax validation, directive validation and analysis finished. Selected findings or optional unused-suppression diagnostics do not make the audit incomplete. |
| `invalid-directive` | Analysis finished, but at least one malformed/unknown/duplicate-code reasoned directive was rejected. Valid directives in the same file keep their analyzed usage. |
| `syntax-error` | Tokenization or Python syntax validation failed; collected valid directives have `unknown` selected usage. |
| `analysis-error` | AST analysis or a requested safe rewrite could not finish; retained valid directives have `unknown` selected usage. |
| `read-error` | Source could not be safely read/decoded, including unsupported encoding, size or symlink paths; no directives can be verified. |
| `discovery-error` | An input path could not be discovered or a directory could not be traversed. |

If more than one failure affects the same path, the single status uses this precedence: `discovery-error`, `read-error`, `analysis-error`, `syntax-error`, `invalid-directive`, `complete`. For duplicate failure spellings the lexically first spelling is used. Disabled directive codes remain `disabled` on failures. Malformed directives never appear in the inventory or suppress findings; failures keep PLV000 diagnostics. The metadata is descriptive and does not change exit policy, including `--exit-zero`. With `--fix` it describes the final reanalysis; baseline checks describe current source without storing audit metadata in snapshots. Neither `suppressions` nor `suppressionAudit` appears without `--show-suppressions`.

The inventory alone does not change diagnostics, exit codes, source, or baselines. `--report-unused-suppressions` separately enables failure for unused selected codes. With `--fix`, the inventory describes the reanalyzed, rewritten source; with `--baseline`, it describes current source, including suppressions that make prior diagnostics resolved. Reasons are deliberately disclosed only by this opt-in JSON field, never stored in a baseline. Without the flag the existing output schema is unchanged. The Python API `analyze_with_suppressions(...)` accepts the same options as `analyze(...)` and returns an `AnalysisResult` with `diagnostics`, `suppressions`, and `suppression_status` (`complete`, `invalid-directive`, `syntax-error`, or `analysis-error`); `analyze(...)` still returns only diagnostics. Inventory output is not a flake8 feature.

## CI diagnostic baseline

Record accepted findings explicitly, then fail CI only for new findings or checks that cannot be verified:

```sh
# Run at the repository root; the baseline directory anchors all relative paths.
datawrench polars-vet check src/ --baseline polars-vet-baseline.json --update-baseline
# Commit the JSON baseline after review. Ordinary checks never update it.
datawrench polars-vet check src/ --baseline polars-vet-baseline.json
datawrench polars-vet check src/ --baseline polars-vet-baseline.json --format json
```

For example, record `q = pl.read_csv("old.csv").lazy()` and a PLV002 callback. Moving unrelated code or reformatting the reader leaves PLV001 **existing**; replacing the callback with a native expression makes PLV002 **resolved**; adding `other = pl.read_parquet("new.parquet").lazy()` produces a **new** PLV001. Existing and resolved findings return **0**. New, **uncertain**, or PLV000 **error** findings return **1**. `--exit-zero` can mask check failures, as with ordinary checks. It cannot turn a failed baseline update into success.

The version 1 JSON baseline stores relative POSIX paths, exact rule codes, SHA-256 identities for the named function/class scope and diagnostic AST/statement, and original diagnostic spans. It stores no source expressions, data paths, comments, or reasons. Line numbers are display metadata, not matching keys. Line shifts, comments, quote/whitespace changes and reordering uniquely named functions preserve matches. Editing another statement in the same file does not invalidate the whole file. File renames produce resolved/new pairs unless explicitly mapped as below. Scope/variable renames or changes to the diagnostic's own statement still produce resolved/new pairs; there is no fuzzy rename matching. A resolved result means the selected analyzer no longer reports it, including when a suppression was added; it does not prove a semantic performance improvement.

Identical repeated diagnostic statements within the same named scope cannot be reliably distinguished. Checks label them **uncertain** with an explanation and fail rather than hiding them. Unreadable/invalid sources or traversal failures keep affected prior findings uncertain instead of declaring them resolved. A missing file inside a successfully scanned directory can be resolved. Unknown identity formats, malformed/oversized baselines, changed input scope or rule policy, and unsafe paths return **2**. Match the saved scan paths (normalized and order-independent), effective `--select`/`--ignore` codes, and `--report-unused-suppressions` setting, or request a refresh explicitly. This prevents a partial scan or narrower rule selection from reporting unexamined findings as fixed.

`--update-baseline` requires `--baseline FILE` and creates or atomically replaces only that JSON file after a complete, unambiguous check. Errors or uncertain identities prevent any update and return 1. Refreshing can explicitly change the scope/policy or replace a malformed baseline; review the diff because it accepts all currently reported findings. Baselines must end in `.json`, be at most 2 MiB, and have all checked paths under their directory; use the repository root for portable checkouts. The existing regular-file and symlink restrictions apply. Concurrent destination changes are refused. Baseline mode rejects `--fix`; fix source in a separate command and compare again. Baselines are opt-in for the standalone CLI, not the flake8 plugin.

For an intentional **file rename**, use a repeatable exact path map:

```sh
mv src/job.py src/pipeline.py
datawrench polars-vet check src/ --baseline polars-vet-baseline.json --baseline-path-map src/job.py=src/pipeline.py --format json
# After reviewing the comparison, persist current paths in the baseline:
datawrench polars-vet check src/ --baseline polars-vet-baseline.json --baseline-path-map src/job.py=src/pipeline.py --update-baseline
```

Each `OLD=NEW` uses nonempty, canonical relative POSIX **file** paths anchored to the baseline directory, regardless of the current working directory. No absolute paths, `.`/`..` components, redundant separators, or `=` within filenames are accepted. Maps are exact, simultaneous, and one-to-one; they do not match directory prefixes or infer renames. OLD must have saved findings and no longer exist; NEW must be a regular, nonsymlink file discovered by this check. Duplicate sources or destinations, self-maps, chains/cycles, and destinations with any saved findings are rejected as ambiguous/colliding. Symlink paths/ancestors are rejected on either side.

Mapping changes only the prior path used for matching. Unchanged unique findings become **existing** at NEW; edited findings remain **new**/**resolved**, repeated identities remain **uncertain**, and source failures at NEW keep prior findings **uncertain**. Resolved/unverified prior entries display NEW with their saved spans. For example, renaming the two-finding file above preserves both as existing (exit 0); changing its callback and adding another reader still gives one existing, one resolved, and one new finding (exit 1). Ordinary comparisons never change source or baseline bytes, so repeat the map until an explicit update saves current paths. Updates remain subject to the complete, unambiguous-check requirement.

`--baseline-path-map` requires an existing valid `--baseline FILE`, even with `--update-baseline`. A map cannot bypass schema, rule-policy, or scan-scope validation: only saved explicit **file** scan targets are remapped; directory targets must still match exactly. Changed directory scan roots require a separate explicit refresh. Invalid maps return **2**, including with `--exit-zero` or `--update-baseline`, without writing source/baseline. After a successful update, omit the old map; its old path is no longer in the snapshot. The version 1 baseline snapshot schema and non-JSON renderers stay the same.

With `--format json`, a valid mapping request adds `baseline.pathMaps` to the comparison metadata:

```json
{"pathMaps": [{"oldPath": "src/job.py", "newPath": "src/pipeline.py"}]}
```

Each row contains only `oldPath` and `newPath`, using the validated canonical POSIX paths relative to the baseline directory (even when invoked from another working directory). Multiple independent renames produce one row per mapping, sorted by `(oldPath, newPath)` regardless of flag order. The field records exact path mappings applied to the comparison, including mappings whose findings are edited, resolved, uncertain or errored; it does not attest that findings are unchanged. All mappings must pass syntax, saved-entry, policy, collision and filesystem validation before any JSON is emitted; an invalid request returns 2 with no partial metadata and no source/baseline writes.

Ordinary comparisons remain read-only. A successful `--update-baseline` response includes the mappings used for that comparison and `updated: true`, but saves only the current snapshot paths, never `pathMaps` or map history. A blocked update still includes the validated mappings with `updated: false` and retains the saved baseline. Omit maps after a successful update. Without `--baseline-path-map`, `pathMaps` is absent (not an empty array), including during baseline creation/update; without `--baseline`, the JSON schema is unchanged. Classifications, counts, exit policy and the snapshot version/schema are unchanged.

To include the effective scan scope and rule policy in a CI audit, opt in separately:

```sh
datawrench polars-vet check src/ --baseline baseline.json --format json --show-baseline-policy --select PLV002,PLV001 --report-unused-suppressions
```

`--show-baseline-policy` requires both `--baseline FILE` and `--format json`. It adds only `baseline.policy`, with the same policy schema already used by version 1 snapshots:

```json
{"policy": {"targets": [{"path": "src", "kind": "directory"}], "selected": ["PLV001", "PLV002"], "reportUnusedSuppressions": true}}
```

`targets` contains the normalized, deduplicated **current input targets**, sorted by `(path, kind)`, with `kind` equal to `file` or `directory`. Paths use POSIX spelling relative to the baseline directory regardless of the working directory; `.` denotes that directory itself. These are requested scan targets, not a list of discovered files or proof of successful inspection. `selected` is the sorted effective rule list after `--select`/`--ignore` (PLV000 is always active separately), and `reportUnusedSuppressions` is the effective boolean. With exact file maps, policy targets show the current NEW file paths, while `pathMaps` records the OLD/NEW correspondence; original saved scope/rule validation still applies.

The field is descriptive for read-only comparisons, successful updates/creation, and blocked updates (`updated: false`). It does not bypass schema, scope, rule, mapping, collision, source-error or uncertainty checks, and does not change classifications, counts or exit codes. Invalid flag combinations return 2 before source/baseline writes, even with `--fix`, `--update-baseline` or `--exit-zero`; invalid comparisons emit no partial JSON. Explicit refresh without maps can still change saved policy under the existing update rules. Ordinary comparison preserves all source/baseline bytes. Updates keep the existing version 1 snapshot schema, storing only its existing policy/entries, with no new audit fields. Without `--show-baseline-policy`, `baseline.policy` is absent and all existing output schemas stay unchanged. No policy metadata is added to non-JSON output.

For a per-rule CI summary, add `--show-baseline-rule-counts`:

```sh
datawrench polars-vet check src/ --baseline baseline.json --format json --show-baseline-policy --show-baseline-rule-counts
```

`--show-baseline-rule-counts` requires `--baseline FILE` and `--format json`. It adds only `baseline.ruleCounts`, an array sorted by exact `code`. Each row has exactly `code`, `existing`, `new`, `resolved`, `uncertain`, and `error`; counts are nonnegative integers, including zeros:

```json
{"ruleCounts": [{"code": "PLV001", "existing": 1, "new": 0, "resolved": 0, "uncertain": 0, "error": 0}]}
```

Include one row for every code present in the classified diagnostics, including prior resolved/uncertain findings and PLV000 errors. Omit codes with no classified rows even if selected; an empty comparison produces `[]`. For each status, summing per-rule counts equals `baseline.counts`. These counts describe the emitted comparison, including during creation or a successful/blocked update, before any accepted findings become existing on the next check. They compose with `policy`, `pathMaps`, and suppression audits independently.

Without this flag, `ruleCounts` is absent. Snapshot version/schema, other output formats, classifications, aggregate counts, exit codes and update guards are unchanged. Ordinary checks preserve source/baseline bytes. Invalid combinations or invalid comparisons return 2 with no partial JSON or source/baseline writes, even with `--exit-zero` or `--update-baseline`; missing baseline also rejects before `--fix`. Rule counts cannot bypass saved scope/rule policy, map validation, collisions, source errors or uncertainty.

For CI drill-down by file, add `--show-baseline-path-counts`:

```sh
datawrench polars-vet check src/ --baseline baseline.json --format json --show-baseline-path-counts --show-baseline-rule-counts --show-baseline-policy
```

`--show-baseline-path-counts` requires `--baseline FILE` and `--format json`. It adds only `baseline.pathCounts`, an array sorted by canonical baseline-relative POSIX `path`. Every row has exactly `path`, `existing`, `new`, `resolved`, `uncertain`, and `error`, with nonnegative integer counts including zeros:

```json
{"pathCounts": [{"path": "src/job.py", "existing": 2, "new": 0, "resolved": 0, "uncertain": 0, "error": 0}]}
```

Group the same classified diagnostic rows as `baseline.counts`, across all rules, including PLV000 errors and prior resolved/uncertain findings even when their files no longer exist or could not be checked. Paths are normalized relative to the baseline directory regardless of input spelling or working directory; `.` denotes that directory itself when it has a diagnostic. Validated exact maps place prior findings under NEW paths. A path with no classified rows is omitted even if scanned; no classified rows produces `[]`. Each status summed across paths equals its aggregate count. Creation and successful/blocked updates describe the emitted comparison before accepted findings become existing on the next check.

This field composes independently with `policy`, `pathMaps`, `ruleCounts` and suppression audits. Without the flag, `pathCounts` is absent and output stays unchanged. Snapshot v1 schema, diagnostic path display, classifications, aggregate counts, exit behavior and all update guards remain unchanged. Ordinary comparisons preserve source/baseline bytes. Invalid flag combinations reject with exit 2 before writes (including with `--fix`, `--update-baseline` or `--exit-zero`); invalid comparisons emit no partial JSON. The flag cannot bypass saved scope/rule policy, map/collision validation, source errors or uncertainty. Successful explicit updates persist only the existing snapshot policy/entries, never audit counts.

All four output formats include classifications. Baseline JSON adds `baselineStatus` (and `baselineReason` for uncertainty) to each diagnostic, including prior resolved/unverified entries at their saved locations, plus a `baseline` object with `path`, `updated`, and counts for `existing`, `new`, `resolved`, `uncertain`, and `error`. Text/GitHub include status labels; existing/resolved GitHub annotations are notices. SARIF 2.1.0 uses `baselineState` values `unchanged`, `new`, and `absent`, with `properties.baselineStatus`/`baselineReason` for the full classification. Without `--baseline`, existing output and exit behavior are unchanged.

## Rules

| Code | Detects | Automatic fix |
| --- | --- | --- |
| PLV001 | `read_csv/parquet/ipc/ndjson(...).lazy()` | Restricted local literal paths |
| PLV002 | Arithmetic or upper/lower/strip element lambdas | Suggestion only |
| PLV003 | Row iteration in `for`/comprehensions (directly or in one builtin `enumerate`/`zip`/`iter`), or direct `yield from` delegation | Suggestion only |
| PLV004 | `with_columns` in a loop | Suggestion only |
| PLV005 | `collect` in a loop, or `collect().lazy()` | Suggestion only |
| PLV006 | `select`/`filter` after collection of a tracked scan | Suggestion only |
| PLV007 | `to_pandas` passed back to `from_pandas`/`DataFrame` | Suggestion only |
| PLV008 | Frame indexed by its own boolean series expression | Suggestion only |
| PLV009 | `sort().head()` | Suggestion only |
| PLV010 | Polars `concat` in a loop | Suggestion only |
| PLV011 | Eager file read immediately followed by `select`/`filter` | Suggestion only |

[Full rule descriptions and bad/good examples](polars-vet/rules.md) explain semantic caveats. Suggestions require review: loop iterations can depend on earlier columns, Python and native arithmetic can differ on overflow/nulls, and `top_k` has different ordering, tie, and null behavior. PLV008 identifies a potentially invalid API pattern, rather than promising a speedup.

### Enumerated row iteration (PLV003)

```python
import polars as pl
df = pl.read_csv("x.csv")
for i, row in enumerate(df.iter_rows(), start=1):
    print(i, row)
rows = [row for i, row in enumerate(df.rows(), 1)]
```

Each iteration reports one PLV003, `Python iteration over materialized rows; prefer expressions`, on the inner `df.iter_rows()` / `df.rows()` expression, using the existing Unicode code-point spans. This is a suggestion only, with no automatic fix. Analysis never opens `x.csv` or executes the source.

The same applies to `to_dicts()` and all comprehension forms, retaining existing Polars import aliases and simple frame assignments. Exactly one direct call to the unshadowed builtin name `enumerate` is recognized, with one explicit positional iterable and an optional second positional argument or `start=` keyword (including expressions). Starred arguments, duplicate/unknown keywords, nested wrappers, arbitrary calls, callable aliases and untracked receivers are not inferred. A user-defined, imported, reassigned, parameter or local `enumerate` prevents unwrapping; branch/loop/try invalidation and unknown star imports remain conservative. Comprehension targets shadow inside their own scope, after evaluation of the first iterable, and do not leak outward. Suppressions apply to the inner row call's starting line; selection, baseline identities/classification and per-path counts use the existing diagnostic pipeline.

### Saved row iterables (PLV003)

```python
import polars as pl
df = pl.read_csv("x.csv")
rows = df.iter_rows()           # no finding on creation
copy: object = rows
for row in copy:                # PLV003 spans only `copy`
    print(row)
indexed = [row for i, row in enumerate(rows, start=1)]  # spans only `rows`
```

Simple name assignments (including annotated, chained, and pass-through name aliases) retain provenance from tracked eager `iter_rows()`, `rows()`, and `to_dicts()` calls. Existing import/frame aliases and supported eager transformations apply. Each direct loop/comprehension consumption, or consumption inside one supported builtin `enumerate` or `zip`, reports exactly one existing PLV003 on the consumed **name**, with 1-based Unicode code-point columns and an exclusive end. Repeated syntactic uses each report; iterator exhaustion is not inferred. Creation and alias assignment alone do not report, and no fix is offered.

A single flat two-name tuple assignment also retains row provenance independently for each corresponding tuple RHS element:

```python
rows, labels = df.iter_rows(), ["a"]    # no finding on unpacking
for row in rows:                       # PLV003 on `rows`
    print(row)
pairs = [p for p in zip(rows, labels)]  # PLV003 on `rows`
rows, copy = copy, rows                # proofs captured before either name is rebound
```

This form requires exactly one assignment target: a two-element `ast.Tuple` of distinct names, and a two-element `ast.Tuple` RHS (parentheses are optional). Each evaluated RHS element independently reuses the existing eager row-call or saved-name proof; an unknown sibling does not block a proven source. RHS elements are visited in order and their proofs captured before target binding, preserving swaps and later sibling rebinding. Only row provenance is retained here; frame/module/other kinds are not propagated. Lists as the target or outer RHS, stars, nested unpacking, unequal/other arities, chained destructuring targets, attributes/subscripts and duplicate target names remain unsupported and clear written names using existing invalidation. A nested RHS value, helper/lazy/dynamic source or saved wrapper does not acquire row proof; a separately proven sibling can still be retained. Assignment alone stays silent, and all existing consumption and diagnostic behavior above applies.

Assignment, parameter/local binding, import, delete, branch/loop merges and try invalidation use the existing conservative environment. A saved value survives later rebinding of its source frame or another alias. Unknown/lazy sources, other unpacking targets, attributes/subscripts, helper returns, callable aliases, saved wrapper results, `list` and arbitrary/nested wrappers are not inferred. Existing assignment expressions can retain provenance for later name uses; a walrus expression used as the iterable is not recognized. No interprocedural or dynamic provenance is guessed. Suppressions apply only to the consumed name's starting line, including multiline `enumerate`; a directive on creation or the outer wrapper line does not suppress another line. Selection, suggestion text, baseline fingerprints/classification, per-rule/per-path counts and snapshot schema use the existing pipeline.

### Zipped row iteration (PLV003)

```python
import polars as pl
df = pl.read_csv("x.csv")
rows = df.iter_rows()                         # no finding on creation
for row, label in zip(rows, labels, strict=True):  # PLV003 on `rows`
    print(row, label)
pairs = [pair for pair in zip(df.rows(), df.to_dicts(), strict=False)]  # one per row call
for row, label, weight in zip(rows, labels, weights):  # PLV003 on `rows`
    pass
triples = [p for p in zip(labels, df.rows(), rows, strict=True)]  # two inner findings
```

Exactly one direct call to the unshadowed builtin name `zip` is supported in loops and all comprehension forms, with **exactly two or three explicit positional arguments**, and either **no keywords** or **exactly one `strict=True` or `strict=False` keyword**. The `strict` AST value must be a literal boolean; its expression is never executed. Each argument independently reuses the eager row-call or saved-name provenance above; each proven source reports once at its own existing name/call span, never at the outer zip. Two occurrences of the same saved name report at their separate spans. An untracked sibling does not prevent reporting a proven source. This remains the existing nonfixable PLV003 suggestion; creation or saving a zip result alone does not report.

Imported, reassigned, user-defined, parameter or locally bound `zip`, callable aliases and attribute calls such as `builtins.zip` are not inferred. Existing binding, scope and branch/loop/try invalidation applies. Zero, one or four-plus arguments, starred arguments, unknown/duplicate keywords, `**kwargs`, nonliteral `strict` values (including names/calls, numbers and strings), saved zip results, and provenance hidden in nested `zip`/`enumerate`/arbitrary wrappers remain unsupported. No runtime builtin monkey-patching, iterator exhaustion or argument type validation is inferred. Suppressions apply only to each inner source's starting line; selection, Unicode columns, baseline fingerprints/statuses, per-path/per-rule counts and read-only comparison behavior use the existing pipeline.


### Builtin iter row consumption (PLV003)

```python
import polars as pl
df = pl.read_csv("people.csv")
saved_rows = df.iter_rows()                   # no finding on creation
for row in iter(df.iter_rows()):              # PLV003 on `df.iter_rows()`
    print(row)
result = [row for row in iter(saved_rows)]    # PLV003 on `saved_rows`
```

Exactly one direct call to the unshadowed builtin name `iter` is supported in loops and all comprehension forms, with **exactly one explicit positional argument and no keywords**. The argument must be a proven eager `iter_rows`/`rows`/`to_dicts` call or saved row name as above. Each consumption emits one existing nonfixable PLV003 at the inner name/call's original Unicode code-point span, never at the outer `iter`. Creation or saving an `iter` result alone stays silent; saved wrapper results are not tracked.

Imported, aliased, attribute, reassigned, user-defined, parameter or locally bound `iter` is not inferred. Existing binding, scope and branch/loop/try invalidation applies. Two-argument callable/sentinel iteration, zero/multiple arguments, starred arguments, all keywords, nested wrappers (including combinations with `enumerate`/`zip`) and unknown/lazy sources remain unsupported. Suppressions apply only to the inner source's starting line, so an outer-wrapper or creation-line directive cannot suppress a later line. Selection, message, no-fix policy, baseline identities/classification, per-path/per-rule counts and read-only comparisons use the existing pipeline. Runtime builtin monkey-patching and iterator exhaustion are not inferred.

### Direct generator delegation (PLV003)

```python
import polars as pl
df = pl.read_csv("x.csv")
rows, labels = df.iter_rows(), ["a"]  # creation stays silent

def generate():
    yield from rows                  # PLV003 on `rows`
    yield from df.to_dicts()          # PLV003 on `df.to_dicts()`
```

A direct `yield from` value that is a proven saved-row name or eager `iter_rows()`/`rows()`/`to_dicts()` call reports one existing PLV003 on that value, with Unicode code-point columns and no automatic fix. Existing import/frame aliases, saved aliases, flat tuple origins, scope shadowing and branch/loop/try invalidation apply. The generator is analyzed statically; neither it nor its data paths are executed/opened. Repeated syntactic uses each report without inferring iterator exhaustion.

Plain `yield rows` / `yield df.rows()` and creation alone stay silent. No wrapper is unwrapped here, including `yield from iter(rows)`, `enumerate(rows)` or `zip(rows, labels)`; unknown/lazy/helper/dynamic sources, saved wrappers, attribute/subscript values, conditional expressions and walrus values are not inferred. Nested syntax is still analyzed normally (for example, a comprehension inside the delegated value can report its own row consumption). Suppressions apply to the **value's starting line**, including multiline calls, rather than the outer `yield from` line. Selection, flake8, baseline fingerprints/schema/classification/counts and read-only comparisons use the existing pipeline.

## Conservative fixes

`--fix` applies selected, unsuppressed PLV001 fixes and reports diagnostics **after** rewriting. It does not fix the other rules. It preserves encoding, BOM, line endings, surrounding comments, and permissions; writes use a temporary sibling file and atomic replacement. Changed files are reanalyzed. Existing hard links to a file retain the old contents.

Fixes require a nonempty local **literal string** source, no wildcard/URL/tilde path, no starred arguments or `**kwargs`, an empty `.lazy()` call, and an available scan alias or Polars module binding. Deleted aliases cannot supply a fix; another surviving scanner/module binding is used if available, otherwise PLV001 remains nonfixable. Variables, streams, bytes, `columns`, `use_pyarrow`, credentials/storage options and other unrecognized arguments are **reported but never rewritten**. Only these constant-valued keyword arguments are accepted:

- CSV: `has_header`, `separator`, `quote_char`, `comment_prefix`, `skip_rows`, `n_rows`, `try_parse_dates`.
- Parquet: `n_rows`, `use_statistics`, `parallel`.
- IPC: `n_rows`.
- NDJSON: `n_rows`, `infer_schema_length`.

Evaluation becomes lazy, so IO/errors occur at collection time; internal chunk layout can differ. Fixes preserve tabular values in the tested formats, not execution timing or memory layout. A wrapper parenthesis or comment that cannot be preserved causes the fix to be declined. The linter never opens data paths in checked code.

## flake8

```sh
pip install 'datawrench[flake8]'   # into the environment that runs flake8
python -m flake8 --isolated --select PLV your_package/
# From a datawrench checkout:
uv run --extra flake8 flake8 --isolated --select PLV examples/polars_vet/
```

The `PLV` entry point (`datawrench.polars_vet.plugin:PolarsVetChecker`) is installed with the `datawrench` package; the `flake8` extra only installs flake8 itself. Rule codes (`PLV000`..`PLV011`) are unchanged from the standalone project. Flake8 handles its own selection, syntax errors, and noqa (`--disable-noqa` works). Reasoned `polars-vet` directives are handled by the analyzer and still apply with flake8's `--disable-noqa`; optional unused reporting is available through the standalone CLI/API only. It does not apply fixes. The standalone CLI's path/write boundaries apply to the CLI; flake8 owns file discovery and loading when used as a plugin.

## Verification and experiments

Tests are offline, single-process, and use synthetic redistributable fixtures. Real Polars tests validate CSV, Parquet, IPC and NDJSON before/after fixes, CLI failure paths, import/alias/shadowing behavior, every rule (at least three positive and two negative fixtures each), JSON/SARIF, suppression inventory and incomplete-analysis behavior, baseline matching/failure behavior, and the installed flake8 entry point. The tests need Polars, flake8 and pandas (all in the `test` extra); without pandas the benchmark replay test is skipped.

From a datawrench checkout:

```sh
POLARS_MAX_THREADS=2 uv run --extra test pytest tests/polars_vet -q
datawrench polars-vet check examples/polars_vet/; test $? -eq 1
# This intentionally changes the example; keep a copy if you want to repeat the fix demo.
datawrench polars-vet check --fix examples/polars_vet/fixable_copy.py && datawrench polars-vet check --select PLV001 examples/polars_vet/fixable_copy.py
POLARS_MAX_THREADS=2 uv run --extra test python examples/polars_vet/benchmarks/bench_all.py
POLARS_MAX_THREADS=2 uv run --extra test python examples/polars_vet/benchmarks/plv002.py --rows 30000 --repeats 5
POLARS_MAX_THREADS=2 uv run --extra test python examples/polars_vet/benchmarks/bench_all.py --json
```

[Benchmark method and recorded run](../examples/polars_vet/benchmarks/README.md). Each measurable rule has a runnable `examples/polars_vet/benchmarks/plvNNN.py` script; all real comparisons share fixture generation in `common.py`. Every pair first asserts actual frame equality, then measures alternating bad/good runs with a monotonic clock. No minimum speedup is required. PLV008 validates the API error and filter result without timing an invalid operation.

## Scope and limitations

This is a heuristic source analyzer, not a type checker. Supported forms include `import polars as pl`, `from polars import col as c`, imported reader aliases, simple assignments, local imports, recognized frame transformations, loops/comprehensions, and conservative branch merges. Parameters without a local Polars binding are unknown, even with type annotations. Reassigned aliases, parameters, loop targets, pattern/exception/context-manager bindings invalidate inference. Ambiguous origins and unsupported return types are deliberately missed.

There is no interprocedural analysis, full control-flow graph, schema/dtype inference, general wrapper recognition, star-import inference, cross-module alias resolution, or support for dynamic imports, `getattr`, runtime monkey-patching via external calls, or arbitrary custom accessors. Branch/try/loop analysis is approximate; closures and aliases mutated later can produce false positives or negatives. Recognized Polars expressions may still be invalid for a particular dtype. PLV006 follows collected scan assignments but only across recognized transformations; PLV007 detects direct/pass-through round trips, not arbitrary pandas work. One unshadowed builtin `enumerate`, two- or three-positional `zip` (no keyword or one literal-boolean `strict=`), or one-positional/no-keyword `iter` wrapper is recognized for row iteration; nested wrappers, callable aliases and dynamic row-iterator provenance are not inferred. The deprecated `apply` spelling is detected but never rewritten. Dataframe formatting and deprecation migration are out of scope.

The CLI rejects symlink input files or symlink ancestors and never follows symlink directories. Nonregular files and sources over 2 MiB are rejected. Invalid/too-deep syntax is reported. It checks for source changes before replacing a file. It is not designed for directories being maliciously renamed concurrently; run fixes in a trusted checkout. No subprocesses, network requests, templates, expressions, data file IO or workflows are invoked by the linter. There is no daemon, paid API, or telemetry.

## Upstream relationship and provenance

Independent tool, unaffiliated with Polars, Ruff or flake8. The motivation is [Ruff issue #7721](https://github.com/astral-sh/ruff/issues/7721); this is a Python CLI/flake8 plugin, not a Ruff plugin. Polars' [read_csv documentation](https://docs.pola.rs/api/python/stable/reference/api/polars.read_csv.html) explicitly warns about eager reads followed by `.lazy()`; [scan_csv](https://docs.pola.rs/api/python/stable/reference/api/polars.scan_csv.html) supports optimizer pushdown. API behavior and tests here target **Polars 1.35.2**, not all historical or future releases.

[THIRD_PARTY.md](../THIRD_PARTY.md) records dependencies and sources. Part of datawrench, licensed under Apache-2.0 ([LICENSE](../LICENSE), [NOTICE](../NOTICE)). This command was first released as a standalone MIT-licensed project; that notice is kept in [`licenses/MIT-original-tools.txt`](../licenses/MIT-original-tools.txt).
