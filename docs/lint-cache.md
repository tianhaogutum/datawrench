# datawrench lint-cache

`datawrench lint-cache` (formerly `sqlfluff-cache`) adds a content-addressed,
per-file cache to repeated SQL linting of whole projects, while keeping
**SQLFluff** (tested with 4.5.0) as the parsing, templating and rule engine. It
suits local runs, pre-commit and CI with persistent workspaces: the first lint
covers every file, and later runs only recompute files whose inputs changed. It
is an independent project, not affiliated with SQLFluff;
it does not modify rules, does not execute SQL and does not provide fixes.

## Install and quick start

Requires Linux and Python 3.11+ (originally verified on Linux / CPython
3.11.15). SQLFluff and Jinja2 come from the `sqlfluff` extra:

```sh
pipx install 'datawrench[sqlfluff]'
datawrench lint-cache --version   # prints the bare datawrench version, e.g. 0.1.0
datawrench lint-cache --help
```

`python -m datawrench lint-cache` is equivalent to `datawrench lint-cache`.
The subcommands are `lint`, `status`, `clear` and `gc`. The first installation
needs the network; tests and lint runs do not.

From a datawrench checkout root:

```sh
datawrench lint-cache lint examples/lint_cache/project --stats
datawrench lint-cache lint examples/lint_cache/project --stats
```

The bundled project contains 40 DuckDB SQL files; both runs are expected to exit
0. stdout lists `PASS` per file; stderr contains, respectively:

```text
datawrench lint-cache: files=40 hits=0 misses=40 bypassed=0 skipped=0
datawrench lint-cache: files=40 hits=40 misses=0 bypassed=0 skipped=0
```

If the directory already has a cache, run `datawrench lint-cache clear` first.

## CLI and output

```sh
datawrench lint-cache lint src/sql models --dialect postgres --stats
datawrench lint-cache lint models --config config/sqlfluff.ini --format json
datawrench lint-cache lint models --format github-annotation
datawrench lint-cache lint models --no-cache --stats
datawrench lint-cache lint models --explain
datawrench lint-cache lint models --format cache-json --explain-dependencies
datawrench lint-cache lint models --format cache-json
datawrench lint-cache status --cache-dir .sqlfluff_cache
datawrench lint-cache status --details
datawrench lint-cache status --details --corrupt-reasons
datawrench lint-cache status --corrupt-counts
datawrench lint-cache status --corrupt-counts --corrupt-bytes
datawrench lint-cache status --fail-on-corrupt
datawrench lint-cache gc --max-age-days 30
datawrench lint-cache gc --max-bytes 10485760 --dry-run
datawrench lint-cache gc --max-bytes 10485760
datawrench lint-cache gc --max-bytes 10485760 --dry-run --summary
datawrench lint-cache gc --dry-run --fail-on-planned --summary
datawrench lint-cache clear --dry-run
datawrench lint-cache clear --dry-run --summary
datawrench lint-cache clear
```

Without paths, `lint` scans the current directory. The default cache directory
is `.sqlfluff_cache` in the current directory (the name is unchanged from the
standalone tool, so existing caches keep working); every subcommand accepts
`--cache-dir DIR`. File discovery calls SQLFluff's own logic directly and
honours `.sqlfluffignore`, configured ignore patterns and `sql_file_exts`;
`noqa` is honoured. Repeated identical paths are deduplicated; stdin, custom
globs beyond shell expansion and remote paths are not supported. Config sources,
parent/child directory inheritance, `--config`, `--dialect` and inline config
follow SQLFluff. As upstream, the templater must be set in the **current working
directory** config or in `--config`; it cannot be switched in a subdirectory.
Subdirectory dialect and rule settings do take effect.

- `human`: per-file status, line/position, rule, description, warning/error.
- `json`: the array structure of SQLFluff `lint --format json`, including
  `filepath`, `violations`, `statistics` and `timings`. Violations fully retain
  code, position, description, name, warning and upstream-provided fixes.
  **On a hit, `timings` are the values from the first computation**, not this
  run's duration; exclude timings when comparing with a direct run. Violations
  and statistics should match.
- `github-annotation`: the same JSON annotation array as SQLFluff's formatter of
  that name; normal violations have level `warning`, rules downgraded to warnings
  have `notice`. This is not the native `::` workflow-command syntax.
- `--stats` writes to stderr so JSON stays clean; `misses` includes `bypassed`,
  and `files = hits + misses`; files over upstream's size limit count as
  `skipped`.
- Exit codes: **0** no failing violations (warnings only also give 0); **1**
  failing lint/PRS/TMP violations; **2** path, config, permission, safety
  boundary or other recognised operational errors (including an unknown or
  misspelled dialect from `--dialect` or a `.sqlfluff` `dialect` setting: stderr
  lists the available dialects and stdout is empty). SQL that cannot be parsed
  is cached as well.

Diagnostics and warnings are printed to stderr prefixed with
`datawrench lint-cache: `.

### Per-file cache explanations

`--explain` writes concise per-file reasons and stats to stderr and can be
combined with any existing formatter; it does not change the SQLFluff
JSON/annotation format on stdout. `--format cache-json` prints only the
explanations and stats on stdout, **without diagnostics, fixes or SQL text**;
the exit code still reflects the actual lint result, not just a cache check.
The API's `Result.explanations` provides the same per-file list.

For example, with a `.sqlfluff` of `[sqlfluff]`, `dialect = ansi`,
`templater = raw` (one per line), and `a.sql` and `b.sql` both containing
`SELECT 1;` plus a newline, run twice in a row:

```sh
datawrench lint-cache lint a.sql b.sql --format cache-json
datawrench lint-cache lint a.sql b.sql --explain
```

The first JSON is (pretty-printed here):

```json
{
  "files": [
    {"filepath": "a.sql", "action": "linted", "reasons": ["not_cached"]},
    {"filepath": "b.sql", "action": "linted", "reasons": ["not_cached"]}
  ],
  "stats": {"files": 2, "hits": 0, "misses": 2, "bypassed": 0, "skipped": 0}
}
```

The second run's stderr contains `replayed "a.sql": cache_hit`,
`replayed "b.sql": cache_hit` and `files=2 hits=2 misses=0 bypassed=0 skipped=0`.
Hits never invoke template rendering or linting. Changing only `a.sql` to
`SELECT 2;` gives `linted / content_changed` for a, while b stays
`replayed / cache_hit`; adding only `rules = LT01` to the shared config gives
`linted / settings_changed` for both files. Corrupting the cached JSON of the
current key reports `linted / corrupt_entry`, warns and recomputes; the next run
can hit again.

| Reason | Meaning |
|---|---|
| `cache_hit` | Actually replayed from the current exact key; a hit on a historical entry after restoring old inputs is not reported as a re-lint |
| `not_cached` | No current entry and no comparison history for that absolute path and cwd; this does not claim it is the file's first run ever |
| `content_changed` / `settings_changed` | Relative to the most recent successfully cached or replayed inputs, the SQL bytes / effective config digest changed; editing inline config can produce both |
| `sqlfluff_version_changed` | The SQLFluff version changed; its own distribution / built-in plugin versions are not double-counted as dependencies |
| `dependencies_changed` | Python/schema, other installed distributions/plugins, template dependency trees, or the environment digest for trusted libraries/dbt changed |
| `entry_missing` | The comparison history's input digests are identical, but the current result entry is missing |
| `corrupt_entry` | The current exact-key entry is invalid; this definite reason takes priority |
| `history_unavailable` | The current entry is missing and the comparison history is corrupt, so which input changed cannot be inferred reliably; lints as usual and repairs the history |
| `cache_disabled` / `template_cache_bypassed` | `--no-cache` / a template policy bypassed the cache and lint actually ran; comparison history is neither read nor updated |
| `large_file_skipped` | Skipped by SQLFluff's large-file policy; action is `skipped`, not counted in hits/misses |

When several inputs changed, all reasons are listed in the order content,
settings, SQLFluff version, dependencies. Explanations contain only the path,
action and reason codes; never raw SQL, config values, environment variable
names/values or old/new digests. The existing diagnostic formatters and cached
results can still contain SQL fragments; keep them according to the sensitive
data boundary described below. Operational errors still exit 2 with a reason on
stderr and never print incomplete explanation JSON.

`--explain-dependencies` optionally breaks down `dependencies_changed` and
implies `--explain`. Default output is unchanged. When enabled, each file in
cache-json gains a `dependency_changes` array; the API returns the same field
with `lint(..., explain_dependencies=True)`. Subcategories are reported only on
an actual miss whose `reasons` include `dependencies_changed`, listing all
changed items in this order:

| Subcategory | Meaning |
|---|---|
| `template_files` | A declared file tree changed: macros, Jinja include/loader paths, trusted Python libraries, dbt project/profiles (including file names, directories, bytecode) |
| `python_plugins` | Python/schema, or versions/entry points of other installed distributions/plugins, changed; SQLFluff's own version stays a separate reason |
| `environment` | The whole-environment digest for a trusted `library_path` or the dbt opt-in changed; plain raw/Jinja are not invalidated by unrelated environment changes |
| `unavailable` | Legacy history only has the aggregate digest, or the subcategory cannot be determined from the sub-digests; no category is guessed |

For example, with `query.sql` containing `{% include 'part.j2' %}` plus a
newline, Jinja configured with `loader_search_path = templates`, and only
`templates/part.j2` edited:

```sh
datawrench lint-cache lint query.sql --format cache-json --explain-dependencies
```

That file should have `"reasons": ["dependencies_changed"]` and
`"dependency_changes": ["template_files"]`; stderr contains
`linted "query.sql": dependencies_changed [template_files]`. Changing only the
environment of a trusted library gives `["environment"]`. The array is `[]` for
hits (including restored old inputs), first runs, SQL/config/SQLFluff-version-only
changes, corrupt entries, bypasses and skips. The breakdown never changes lint
diagnostics, exit codes or cache keys; combined with `--format json` or
`github-annotation` it only explains on stderr and stdout keeps its format.
Dependency file names, package names, versions, environment variable
names/values and old/new digests are never printed; source file paths are shown
as usual. Categories are conservative input scopes and do not prove which
file/environment variable actually affected rendering.

The cache automatically maintains a `.inputs-<SHA-256>` comparison history per
absolute path and cwd: it stores only the path, key, four input digests, three
dependency sub-digests and a checksum, never config/environment text. The
sub-digests are saved even when the breakdown output is not enabled, so they can
be compared later. It is bounded JSON, atomically replaced, and does not depend
on scanning all old results; hits with unchanged inputs do not rewrite history.
Old cache keys and the result format remain readable, and history is created the
first time an old entry is hit. History never takes part in hit decisions, and
corruption cannot turn a stale result into a hit. It is not a complete audit
log; after cleanup, concurrent runs or a cwd change there may be no comparison
baseline. A failure to write history only warns and never breaks normal linting
or replay of an old cache; later runs may not be able to explain input
differences. This version reads older histories; the first miss on an older
history reports the breakdown as `unavailable`, and sub-digests are filled in
after a successful write or hit. If you roll back to an older version, it may
treat the new fields as corrupt history and rebuild it; the result cache stays
readable.

For example, the JSON violation object for linting `SELECT  1;` contains:

```json
{"code":"LT01","start_line_no":1,"start_line_pos":7,
 "description":"Expected only single space before numeric literal. Found '  '.",
 "name":"layout.spacing","warning":false}
```

This is an excerpt of the violation fields; actual output also includes ranges,
fixes and file statistics.

## Cache keys and invalidation

The per-file SHA-256 key covers: the file's raw bytes, absolute path, working
directory, the canonical JSON of the effective `FluffConfig` and its config
mapping order (including inline config, excluding the dialect/templater runtime
objects), the cache schema, the Python and SQLFluff versions, SQLFluff plugin
entry points and versions, and the template fingerprint. It also conservatively
includes the versions of **all** installed Python distributions (including
datawrench itself), so upgrading any transitive dependency or datawrench also
invalidates. Content changes invalidate even if the original mtime is restored;
files affected by a new config are recomputed and other files can still hit.
Keys never use the config object's repr, mtime or Python's randomized hash.

Supported templaters:

| Templater | Fingerprint and policy |
|---|---|
| raw | No extra template inputs |
| jinja | Context / inline macros come from the effective config; file names, directories and bytes under `load_macros_from_path`, `exclude_macros_from_path` and `loader_search_path` are hashed recursively |
| python / placeholder | Upstream built-in templaters; context and parameters come from the effective config; no eval |
| Python `library_path` | Recursively hashes the library directory (including bytecode files) and an environment-variable digest; refused by default, requires `--trust-template-code` |
| dbt | Not cached by default, with an explicit notice; running it also requires `--trust-template-code`; `--dbt-project-hash` explicitly enables whole-project hash caching |

Inputs using Jinja's built-in `random` filter, or conservatively detected
forms such as `map('random')`, bypass the cache with a notice, so random
rendering cannot cause false hits. Macro directories and loader dependencies are
checked too; syntax errors are still reported by SQLFluff. The determinism of
arbitrary third-party Python libraries/plugins and dbt database state cannot be
proven by file hashes. Installed SQLFluff plugins are trusted Python code; do not
install plugins for untrusted projects.

`--trust-template-code` explicitly authorises SQLFluff to import libraries /
run dbt. Use it only for code you have reviewed whose results are fully
determined by the declared files and environment. It is not a security sandbox
and does not stop the authorised code from using the network or writing files.
Default Jinja keeps SQLFluff's sandbox; there is no fallback path that evals,
calls a shell or runs dbt workflows automatically. When a library is imported
for the first time and generates `__pycache__`, the dependency tree change can
cause one extra miss on the next run. If a library reads other undeclared files,
the clock, random numbers or external services, use `--no-cache`; do not treat
cached results as an equivalence guarantee.

The dbt opt-in recursively hashes the project from the effective config's
`project_dir` (default: cwd) and requires `dbt_project.yml`; it also hashes
`profiles_dir` (config, `DBT_PROFILES_DIR` or `~/.dbt`) and the digest of the
whole environment. The project excludes only the current cache directory and
`.git`, `.venv`, `node_modules`, `__pycache__` and `.pytest_cache`; target, logs,
seeds and packages are not excluded, so changes to generated files conservatively
invalidate as well. Only digests are stored; profiles/context/environment text
is never written to the cache. Databases, packages/config outside the project and
remote state cannot be proven by this opt-in; this is not a default dbt cache.
The tests verify the dbt fingerprint and bypass policy; they install no adapter
and perform no end-to-end database verification.

## Boundaries and maintenance

- JSON is used, never pickle. Each record has a schema, key, structural checks
  and a content checksum; corrupt/partial/old entries warn and are recomputed.
  Writes use a temporary file + fsync + atomic replacement in the same directory,
  never leaving half a record. Concurrent writes to the same key and cold-cache
  initialisation are tested; there is no background process, and lint runs in a
  single process.
- The cache directory needs the tool's marker; unmarked non-empty directories
  and symlinks in the cache directory path are refused. `clear`/`gc` only delete
  entries in that directory named as 64-hex-digit keys and `.inputs-<SHA-256>`
  comparison histories, preflight all managed names before deleting, never delete
  directories recursively, never follow entry symlinks and never delete unrelated
  files. `status` never creates a cache.
- `status`'s entries/bytes/corrupt and `clear`/`gc`'s removed count only result
  entries; status also reports histories and total bytes (see below). clear also
  empties histories; gc also deletes histories that are corrupt, expired, whose
  source is gone or whose entry was deleted. gc reports history deletions
  separately as `histories_removed`, and the byte budget includes histories (see
  below).
- `gc` deletes entries that are corrupt, whose source file no longer exists, or
  whose write time is older than the given number of days. It is not an LRU and
  does not recompute all current configs; older versions for the same source stay
  until they expire or are evicted for budget. `.entry-*` temporary files left by
  killed processes are ignored and can be deleted manually while no lint is
  running.
- Macro/loader/library dependency trees refuse symlinks and special files; each
  dependency tree is limited to 100000 files and 256 MiB, with an explicit error
  beyond that. Each cache entry is limited to 16 MiB; larger results return normal
  diagnostics with a not-cached notice. SQLFluff's large-file skip policy is kept
  and counted explicitly; skipped files are never cached as clean.
- SQL, config and template dependencies should stay stable during one
  invocation; each dependency tree is hashed only once per invocation. SQL byte
  changes detected while reading/computing raise an error; no transactional
  project snapshot is provided. Malicious concurrent directory replacement,
  cross-machine portable caches and hot-reloading same-version plugin source are
  not supported.
- The cache contains source paths and diagnostics, and diagnostics and fixes can
  contain SQL fragments; protect it like the source files. Checksums are for
  corruption recovery, not signatures; do not accept prebuilt caches from
  untrusted sources.
- No daemon/LSP, fix, parallel workers, stdin, pass-through of all SQLFluff CLI
  options, or third-party templater support; unknown templaters are an explicit
  error. Dry runs are available for clear and gc.
- Cache locking uses `fcntl`, so the command is POSIX-only (Linux is the tested
  platform).

### Clearing preflight

`clear --cache-dir DIR` preflights all managed results and histories with the
same no-follow `lstat` inventory as status/GC before deleting any file. If any
managed name is a real directory, or a directory stat fails with an I/O error, it
exits 2 with a reason on stderr and no JSON on stdout, and deletes none of the
earlier results or histories. For example, with an existing `aaa…aaa.json` and
its history plus a `fff…fff.json/keep` (or `.inputs-fff…fff/keep`) directory,
clear refuses, and the earlier result, history and `keep` all remain.

On success it prints `{"removed": N}`; the API `Cache.clear()` returns the
integer N, still counting only results, while also deleting all managed
histories. It does not read result/history contents or check validity; corrupt
files are deleted too. Managed symlinks (including dangling and directory links)
only have the link itself unlinked; FIFOs are not read; the marker, unrelated
names, `.entry-*` temporary files, ordinary subdirectories and their contents are
left unchanged. A missing or empty unmarked directory returns 0 without creating
the directory or marker.

The preflight is not a transaction: run it while lint/maintenance writers are
stopped. Permission or other errors during the actual unlink after the preflight
can still leave partial deletions; it then exits 2 without success JSON. It is
not guaranteed against malicious concurrent directory replacement, and a preview
does not reserve or lock the later deletion set.

`clear --dry-run` previews all managed names (including valid results/histories)
read-only: it creates no cache, reads no result/history contents and follows no
symlinks. The API `Cache.plan_clear()` returns the same dict; the default clear
JSON and the integer `Cache.clear()` API are unchanged.

| Preview field | Meaning |
|---|---|
| `dry_run` | Always `true` |
| `removed` / `histories_removed` | Actual deletions, both 0 |
| `planned_entries` / `planned_histories` | Number of results / histories planned for deletion |
| `managed_total_bytes` | Sum of the logical `lstat().st_size` of all planned names; symlinks count only themselves |
| `plan` | Sorted by name; each item has `name`, `kind` (`entry` / `history`) and `bytes` |

Unknown/temporary names, the marker and ordinary subdirectory contents are not
in the plan; a missing, empty unmarked or marker-only cache returns an empty plan
and zero counts without writing or deleting any file. Managed real directories,
an invalid cache marker or preflight I/O errors still exit 2 with a reason on
stderr and no partial JSON on stdout. Repeated previews of a stable directory are
identical; the actual clear deletes the same set of names, but the preview does
not guarantee the deletion order. A preview is a snapshot and cannot guarantee
the same plan after concurrent writes; the non-transactional boundary above still
applies.

`clear --dry-run --summary` / `Cache.plan_clear(summary=True)` optionally omits
`plan`, keeping the other six fields of the table above with the same counts and
logical bytes. It prints no managed names, source paths, SQL or raw validation
errors, and reads no result/history contents or link targets. A missing or empty
cache returns zeros for these six fields (`dry_run` is still `true`); it creates
no cache and never marks an empty directory. For example, with 2 results and 1
history planned for deletion and 1024 total logical bytes:

```json
{"dry_run": true, "removed": 0, "histories_removed": 0,
 "planned_entries": 2, "planned_histories": 1, "managed_total_bytes": 1024}
```

`--summary` must be used with `--dry-run`; otherwise the command exits 2 before
preparing the cache, stderr says `--summary requires --dry-run`, and stdout is
empty. The default preview still lists names; the actual clear JSON and integer
API are unchanged. A successful summary contains no names; independent
directory/inventory errors keep their existing stderr text (which can contain
paths), exit 2 and change no files. The summary only narrows the output; the same
complete inventory and plan are still built internally. Logical bytes do not
represent freed disk space; reading the marker can update atime but does not
change contents, inode or mtime. Pause writers when comparing the summary with
the default plan; the preview does not reserve deletions, and an unlink failure
after the preflight of an actual clear can still delete only part of the set,
without success JSON.

### Status and managed bytes

`status --cache-dir DIR` prints one JSON document on stdout; the API
`Cache.status()` returns the same fields:

| Field | Meaning |
|---|---|
| `entries` / `bytes` / `corrupt` | Number of result entries, their logical bytes, and results that are structurally/checksum invalid or unreadable; original meaning retained |
| `histories` / `history_bytes` / `history_corrupt` | Number of comparison histories, their logical bytes, and histories that are structurally/checksum invalid or unreadable |
| `managed_total_bytes` | `bytes + history_bytes`; equal to `bytes_before` of `gc --dry-run` on a stable directory |

Counts and bytes include managed names that are corrupt, expired, whose source
is deleted, or whose referenced result is missing; structurally valid
orphaned/expired histories still count in `histories` and `history_bytes`, not
in `history_corrupt`. status never repairs or cleans files and never creates a
cache; a missing, empty unmarked or marker-only directory returns 0 for all
seven fields. It uses the same `lstat().st_size` basis as GC: only results and
histories, not the marker, unrelated files, `.entry-*`, ordinary subdirectories
and their contents, or filesystem allocation overhead.

Symlinks under managed names (including dangling links and links to directories)
and special files count as corrupt; only the link's own size counts, targets are
not followed and FIFOs are not read in a blocking way. Real subdirectories under
managed names are refused, as in GC. Unmarked non-empty directories, cache
path/marker symlinks, invalid markers and I/O errors that prevent completing the
directory inventory exit 2 with a reason on stderr and no partial JSON on stdout.
Read failures of a single entry/history count as the corresponding corrupt.
status is a snapshot, not a transaction; compare it with GC usage while no lint
is writing.

CI can use `status --fail-on-corrupt` to check for structural corruption
without parsing JSON. After a successful check it exits 1 if `corrupt > 0` or
`history_corrupt > 0`, and 0 otherwise; stdout is exactly the same JSON
(including the newline), stderr is empty and no fields are added. Without this
option it still exits 0 even when corruption is found. For example, a single
zero-byte managed FIFO also exits 1; structurally valid orphaned/expired
histories and missing/empty caches still exit 0. The check only judges structure
and readability; it does not prove the cache applies to current inputs.

The option applies only to `status` and can be combined with `--details`,
`--corrupt-reasons`, `--corrupt-counts` and `--corrupt-bytes`; it implies none of
them. Reasons still require details and bytes still require counts; unmet
requirements exit 2 before the cache is prepared. Directory, marker, inventory
or preparation failures still exit 2 with a reason on stderr and empty stdout,
and change no files; these operational errors are never counted as exit-1
corruption. Successful JSON without details contains no managed names, paths,
SQL or raw errors; independent failures keep their existing stderr diagnostics.
The exit decision reuses the two corruption totals of the same status run, with
no extra reads, stats or link traversal. It is a read-only, non-transactional
snapshot taken while writes are idle; it does not guarantee later freshness or
resist concurrent replacement. Reads may update atime but do not change
contents, inode or mtime, and no missing cache is created and no empty directory
marked.

`status --details` or the API `Cache.status(details=True)` adds a `details`
array next to the seven fields above; default output is unchanged. The array is
sorted by managed file name, and each item has exactly:

| Detail field | Meaning |
|---|---|
| `name` | File name of the managed result or history, without directory path |
| `kind` | `entry` or `history` |
| `bytes` | Logical `lstat().st_size` of that name; symlinks count only themselves |
| `corrupt` | Boolean; whether the same bounded read/validation failed, on the same basis as the aggregate corrupt count |

For example, the detail item for a corrupt `aaa…aaa.json` is
`{"name":"aaa…aaa.json","kind":"entry","bytes":1,"corrupt":true}` (name
abbreviated here; it is the full 64-digit key). Per-kind counts, bytes and
`corrupt=true` counts in the array equal the corresponding aggregate fields.
Structurally valid histories whose source/result is deleted or expired still
have `corrupt=false`; this does not prove the cache still applies to current
inputs. A missing/empty cache returns zero aggregates and `details: []` and
creates no files.

Details disclose only cache names, kinds, sizes and corruption flags; never
source paths, SQL, raw inputs or read-error text. Like plain status they read
managed contents for bounded validation, which may update atime, but never
repair, delete or rewrite files, nor change contents, inode or mtime.
Unrelated/temporary names and ordinary subdirectory contents are still ignored;
symlinks/FIFOs are not followed or blocked on. The exit-2/no-partial-JSON
convention for the directory/stat errors above is unchanged. Repeated output on a
stable directory is identical; during concurrent writes details and sizes are
also a non-transactional snapshot, so stop writers before checking.

`status --details --corrupt-reasons` or the API
`Cache.status(details=True, corrupt_reasons=True)` additionally adds
`corrupt_reason` to each item. Without this option the seven-field aggregate and
four-field details are unchanged. `--corrupt-reasons` alone (or the API with
`corrupt_reasons=True` but not `details=True`) is refused before the cache is
inspected: the CLI exits 2 with an error on stderr and empty stdout, and the API
raises `InputError`.

Classification uses the priority below and reuses the same no-follow `lstat`
inventory and bounded validation failure, without reading contents again:

| `corrupt_reason` | Meaning (top to bottom priority) |
|---|---|
| `non_regular` | The managed name is not a regular file, e.g. symlink, dangling link, FIFO; not followed or blocked on |
| `oversized` | A regular result larger than 16 MiB, or a regular history larger than 16 KiB; exactly at the limit is still validated |
| `unreadable` | Any other regular file with an I/O error while opening/reading |
| `invalid_data` | Any other JSON, encoding, nesting depth, schema, shape, key or checksum validation failure |
| `null` | Validation passed; includes structurally valid orphaned/expired histories |

For example, the detail for the corrupt JSON above only gains
`"corrupt_reason":"invalid_data"`. Reasons never include exception types/text,
SQL, source paths or input values; they are not repair advice for corrupt data or
a freshness proof for current inputs. Names, ordering, logical bytes and the
corrupt aggregate basis are unchanged; a missing cache is still not created and
returns empty details. Managed real directories and inventory
enumeration/`lstat` errors still refuse the whole command with exit 2 and no
stdout, and never become detail reasons. Classification is also a
non-transactional snapshot that requires idle writers, and reads may still update
atime; contents, inode and mtime are unchanged, and classification consistency
under concurrent replacement is not guaranteed.

`status --corrupt-counts` independently summarises the four corruption
categories above without listing any managed names. The API is
`Cache.status(corrupt_counts=True)`. Only when enabled, it adds a
`corrupt_counts` field split into `entries` and `histories`, each always
containing all four integer counts (including 0) and no `null` / valid category.
For example, with only one corrupt JSON result and two structurally valid
orphaned/expired histories:

```json
"corrupt_counts": {
  "entries": {"non_regular": 0, "oversized": 0, "unreadable": 0, "invalid_data": 1},
  "histories": {"non_regular": 0, "oversized": 0, "unreadable": 0, "invalid_data": 0}
}
```

The four values of each group sum to `corrupt` / `history_corrupt`
respectively; valid orphaned/expired histories contribute nothing. Counts reuse
the same bounded no-follow validation and category priority, without extra
content reads. Enabled alone, stdout has only the original seven fields plus
`corrupt_counts`, with no details, names, source paths, SQL or exception text.
It can be combined with `--details`, whose items keep their four fields; only
adding `--corrupt-reasons` adds a per-item `corrupt_reason`. Even with counts
enabled, `--corrupt-reasons` still requires `--details`; otherwise the command
exits 2 before inspecting the cache, with a reason on stderr and empty stdout.
All formats without counts are unchanged. A missing/empty cache returns both
groups as all zeros without creating a directory; real managed directories,
inventory or marker errors still refuse the whole command (exit 2, no stdout, no
changes) and are not counted. The logical-bytes, read-only/atime and
non-transactional snapshot limits are the same as for status above; categories
are structural validation results, not data freshness.

`status --corrupt-counts --corrupt-bytes` or the API
`Cache.status(corrupt_counts=True, corrupt_bytes=True)` further adds
`corrupt_bytes`: each of `entries` / `histories` always contains the four
non-negative integer byte sums `non_regular`, `oversized`, `unreadable` and
`invalid_data`, all 0 for missing/empty/all-valid caches. For example, with only
one 1-byte corrupt JSON result:

```json
"corrupt_bytes": {
  "entries": {"non_regular": 0, "oversized": 0, "unreadable": 0, "invalid_data": 1},
  "histories": {"non_regular": 0, "oversized": 0, "unreadable": 0, "invalid_data": 0}
}
```

Each item contributes its inventory `lstat().st_size` only to the corruption
category chosen by the same validation, consistent with summing details with
reasons per category, without extra content reads, target traversal or stat
passes. Valid items (including orphaned/expired histories) contribute 0, so the
category byte sum does not necessarily equal `managed_total_bytes`. Symlinks
count only the link's own length; a FIFO can have a positive count but 0 bytes;
an oversized regular file counts its full logical size. This is not an estimate
of allocated disk blocks or reclaimable space, and excludes the marker,
unrelated/temporary names and ordinary subdirectory contents.

`--corrupt-bytes` requires `--corrupt-counts`; otherwise `InputError` is raised
before the cache is prepared, and the CLI exits 2 with
`--corrupt-bytes requires --corrupt-counts` on stderr and empty stdout. If
`--details` is also missing, the existing `--corrupt-reasons` requirement is
reported first. Only an explicit `--details` lists names; only adding
`--corrupt-reasons` adds reasons to details. Successful output without details has
only the original seven fields, `corrupt_counts` and `corrupt_bytes`, with no
names, source paths, SQL or raw exceptions; all formats without the bytes option
are unchanged. Independent directory/inventory/marker failures still refuse the
whole command: exit 2, no stdout, no changes. Repeated results are deterministic
while writes are idle; this is a read-only non-transactional snapshot that does
not prove freshness or guarantee consistency under concurrent replacement. Reads
may update atime but do not change contents, inode or mtime; a missing/empty
cache creates no files.

### Byte-budget cleanup and dry runs

`gc --max-bytes N` is an optional non-negative integer byte budget; the default
30-day age cleanup still runs first and can be changed with `--max-age-days`.
Corrupt entries, entries whose source is deleted, expired entries and
invalid/orphaned histories are removed first; then result entries are evicted in
**ascending mtime nanoseconds, ties broken by ascending file name** until the
total is at most N. Deleting a result also deletes the histories pointing to it.
Hits do not refresh result mtime, so this is oldest-write-first, not LRU; `N=0`
clears all managed results and histories, and being exactly at the budget evicts
nothing extra.

The budget is the sum of `lstat().st_size` of `<64 lowercase hex>.json` results
and `.inputs-<64 lowercase hex>` histories in the tool-marked directory,
**including history overhead**. It excludes the marker, unrelated files,
`.entry-*` temporary files and directory/filesystem allocation blocks, so it is
not a `du`-style cap on the whole directory's disk usage. Symlinks under managed
names are treated as corrupt and only the link itself is deleted; targets are
never read or deleted, and unrecognised names are ignored entirely. A
subdirectory disguised as a managed name exits 2 before any deletion; nothing is
deleted recursively. Unmarked non-empty directories or cache path symlinks are
likewise refused.

`--dry-run` can be used alone for age cleanup or combined with a budget; it is
read-only and creates no cache and writes/deletes no files. On success stdout is
one JSON report:

- `dry_run`, `max_age_days`, `max_bytes` (null if unset) record the request.
- `bytes_before` / `bytes_after` are managed byte totals; in a dry run, after is
  the **planned** value.
- `plan` is sorted by each file's mtime nanoseconds and name; each item has
  `name` (cache file name only), `kind` (entry/history), `bytes` and `reason`.
  Reasons are `corrupt`, `source_missing`, `expired`, `entry_missing`,
  `entry_removed` and `over_budget`; result eviction order follows the rule
  above and histories are cleaned with their results.
- `planned_entries` / `planned_histories` are the numbers of results/histories
  in `plan`, provided for both dry runs and real runs, computed without reading
  cache contents a second time. An empty plan (including missing/empty caches)
  gives 0.
- `removed` / `histories_removed` are the result/history deletions performed,
  both 0 in a dry run; on a successful real run they equal the planned counts.
  Existing fields, plan order and the integer `Cache.gc()` API are unchanged.

For example, with only two unexpired results a and b, a written earlier, and the
budget set to the actual bytes of result b plus its history: a dry run should
plan a as `over_budget` and a's history as `entry_removed`, with after equal to
the budget, `planned_entries=1`, `planned_histories=1`, actual deletions 0, and
both files still present. Running without `--dry-run` keeps the planned counts at
1/1, the actual deletions are 1/1, and only b and its history remain. Repeating
the same cleanup plans nothing, with counts 0/0. Planned counts summarise the
same snapshot and do not reserve later deletions; only managed
names/kinds/bytes/reasons are disclosed, never source paths, SQL or raw inputs.
After cleanup, a is linted again normally and b can hit; diagnostics/exit codes
match `lint --no-cache`.

The optional `gc --summary` (API `Cache.collect(..., summary=True)`) omits
`plan`, keeps the other nine scalar fields and adds `planned_reasons`, printing
no managed names, source paths, SQL or raw validation errors. Without it the
existing JSON is completely unchanged. It can be used alone for age cleanup or
combined freely with `--max-bytes` and `--dry-run`; real runs still delete per
the same per-name plan, without changing the cleanup policy.

`planned_reasons.entries` and `planned_reasons.histories` each contain the
following six fixed keys, valued with the number of planned deletions
(non-negative integers); all zero-valued keys are always kept:

| Key | Meaning in the existing GC plan |
| --- | --- |
| `corrupt` | Bounded structural validation failed, including links/FIFOs/unreadable data |
| `source_missing` | The source no longer exists or is not a file |
| `expired` | Write time is older than the age threshold |
| `entry_missing` | The result the history points to is not in the managed inventory |
| `entry_removed` | The history points to a result deleted in this run, including budget eviction |
| `over_budget` | A result evicted to satisfy the byte budget |

Each planned item counts only under its single existing reason, not under every
possible reason. Result priority is corrupt → source_missing → expired →
over_budget; history priority is corrupt → source_missing → entry_removed →
entry_missing → expired (the budget phase can also add entry_removed).
entry_missing/entry_removed for results and over_budget for histories are
currently always 0. The two dictionaries sum to `planned_entries` /
`planned_histories` respectively. The summary reuses the same deterministic plan
without re-reading contents; it still needs O(number of managed names) memory for
the internal inventory/plan. Missing/empty caches or no evictions give all
reasons 0; no missing cache is created and no empty directory marked. Dry-run
actual deletions are still 0, and successful real runs equal the planned counts;
before/after bytes match the default plan. Managed real subdirectories, inventory
read errors or unsafe cache paths still exit 2 with stderr, no stdout and no
changes, and are not counted as reasons. Reads may update atime; the snapshot,
idle-writer and partial-unlink-failure limits apply as well.

CI can use `gc --dry-run --fail-on-planned` to check whether anything is
pending cleanup, without parsing JSON or deleting files. It exits 1 when a
successful plan has `planned_entries > 0` or `planned_histories > 0`, and 0
otherwise; stdout is byte-for-byte identical to the plan JSON for the same idle
cache without this option (including the newline), stderr is empty and no fields
are added. A history-only plan or a zero-byte FIFO also exits 1; missing, empty,
marker-only or valid no-op plans exit 0 without creating a cache or marker.
Without this option, successful previews/real GC runs still exit 0.

This checks the cleanup plan under the selected age/budget policy; structurally
valid items that are expired, whose source is deleted, whose result is missing
or that are over budget can also trigger exit 1, whereas
`status --fail-on-corrupt` only judges structural corruption. The option applies
only to GC, requires `--dry-run`, can be combined with `--summary`,
`--reason-bytes`, age and budget, and implies no other option. Without
`--dry-run` it exits 2 before preparing the cache, with empty stdout and
`--fail-on-planned requires --dry-run` on stderr, changing no files; bytes still
require the summary. Preparation, marker, managed-real-directory or inventory
I/O failures still exit 2 with no stdout/changes and are not treated as exit-1
policy results.

The exit decision reuses the two planned counts returned by a single collect,
without extra reads, stats or link traversal; successful summary JSON still
contains no names, source paths, SQL or raw validation errors. stderr for
independent operational failures can still contain path/I/O text. Stop writers
before comparing plans and avoid age-threshold boundaries; this is a read-only
non-transactional snapshot that does not lock the later deletion set, prove the
current inputs apply, or resist malicious concurrent replacement. The existing
bounded validation and source existence checks are unchanged; reads may update
atime but do not change contents, inode or mtime.

The optional `gc --summary --reason-bytes` (API `Cache.collect(...,
summary=True, reason_bytes=True)`) additionally adds `planned_reason_bytes`; the
fields of `--summary` alone are unchanged. `planned_reason_bytes.entries` and
`.histories` each keep all six keys of the table above, valued with the logical
bytes planned for deletion per reason (non-negative integers, including all 0).
For example, for a missing/empty cache the added part is:

```json
{"planned_reason_bytes":{"entries":{"corrupt":0,"source_missing":0,"expired":0,"entry_missing":0,"entry_removed":0,"over_budget":0},"histories":{"corrupt":0,"source_missing":0,"expired":0,"entry_missing":0,"entry_removed":0,"over_budget":0}}}
```

It can be combined with `--dry-run`, age and budget; `--reason-bytes` without
`--summary` is refused before the cache is prepared/read, exiting 2 with
`--reason-bytes requires --summary` on stderr, no stdout and no changes. The byte
totals of the two dictionaries equal `bytes_before - bytes_after`, as planned
values in both dry runs and successful real runs. Each item keeps the same
priority reason and the original inventory's no-follow lstat size, without extra
content reads, target traversal or stat scans. Symlinks count the link itself, and
FIFOs can count 0 bytes even when that reason's count is non-zero; the marker,
unknown/temporary/nested data and disk block overhead are excluded, so this is
not the disk space freed. Successful output still contains no managed names,
source paths, SQL or raw validation errors; it is still a snapshot needing idle
writers, reads may update atime, and preflight/partial unlink failures keep the
no-success-JSON boundary above.

The API `Cache.collect(max_age_days=30, max_bytes=None, dry_run=False,
summary=False, reason_bytes=False)` returns the same report; the existing
`Cache.gc(..., max_bytes=None)` still returns the number of deleted results.
Invalid budgets or ages raise before anything is deleted. Cleanup is not a
transaction and does not run automatically after each lint: run it while no lint
is writing. Concurrent writes, replaced files or two commands straddling the
expiry time change the plan/actual usage. Permission or other errors during the
actual deletion exit 2, possibly after partial deletion, without a success
report; fix the permissions and plan again.

The Python API lives in `datawrench.lint_cache` (`datawrench.lint_cache.engine.lint`,
`datawrench.lint_cache.cache.Cache`).

## Tests

From a datawrench checkout:

```sh
uv run --extra test pytest tests/lint_cache -q
datawrench lint-cache lint examples/lint_cache/project --stats
datawrench lint-cache lint examples/lint_cache/project --stats
```

Tests run offline and single-process. Some tests compare against the `sqlfluff`
console script of the same installation, so it must be on `PATH`. They cover
real ANSI/PostgreSQL SQL fixtures, Jinja macros and includes, Python/placeholder
templaters, ignore/noqa, warnings, PRS/TMP, config/content/version/plugin
invalidation, corruption recovery, CLI formatter equivalence, maintenance,
per-file explanations (consecutive hits, single-file content/config/version/
dependency/corruption changes, bypass/skip, history recovery and no secret
leakage), the optional dependency breakdown (include files, Python/schema/package/
plugin changes, trusted library environment, multiple changes, legacy history
upgrade), the total byte budget (history included, zero/exact boundaries,
same-time ordering, expired/corrupt first, dry-run matching the real plan,
post-cleanup equivalence with no-cache diagnostics), status accounting
(separate result/history counts, total bytes consistent with GC, mixed
corruption/orphaned histories/unrelated files, read-only and missing
directories), symlink/directory boundaries and atomic writes. Two extra small
threads are used only for the cache write race test.

The standalone project shipped a synthetic benchmark script (direct vs. cold vs.
warm runs, with no artificial delays or speed thresholds); it is not part of
datawrench. A cold cache has hashing and write costs, so small files or single
runs do not necessarily benefit.

Part of datawrench, licensed under Apache-2.0 ([LICENSE](../LICENSE), [NOTICE](../NOTICE)). This command was first released as a standalone MIT-licensed project; that notice is kept in [`licenses/MIT-original-tools.txt`](../licenses/MIT-original-tools.txt). See [THIRD_PARTY.md](../THIRD_PARTY.md) for upstream sources and licenses, and
[licenses/lint-cache/](../licenses/lint-cache/) for the SQLFluff license text.
