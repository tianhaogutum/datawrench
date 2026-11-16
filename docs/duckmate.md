# datawrench duckmate

`datawrench duckmate` (formerly `duckmate`) runs local DuckDB migrations using
dbmate-style SQL files. Apply and roll back migrations, review changes on a
temporary copy, and verify both migration checksums and a reproducible schema
snapshot. No database server, network service or API account.

## Install

Requires Linux and Python 3.11+. The only third-party runtime dependency is DuckDB:

```sh
pipx install 'datawrench[duckdb]'   # or: pip install 'datawrench[duckdb]'
datawrench duckmate --version       # prints the bare datawrench version, e.g. 0.1.0
datawrench duckmate --help
```

`python -m datawrench duckmate` is equivalent to `datawrench duckmate`. The
original standalone tool was tested with DuckDB **1.4.2**. Schema snapshots are
rendered from DuckDB's own catalog SQL, so a different DuckDB version can change
snapshot text (see [Schema snapshots](#schema-snapshots-and-history-compatibility)).
Commands run offline.

The subcommands are `new`, `up`, `migrate`, `rollback`, `status`, `dump`, `load`,
`verify` and `plan`. Errors are printed as `datawrench duckmate: <message>`.

## Try the included fixture

From the datawrench checkout root:

```sh
demo_dir=$(mktemp -d)
cd examples/duckmate/basic
export DATABASE_URL="duckdb:$demo_dir/demo.duckdb"
datawrench duckmate up
datawrench duckmate status
datawrench duckmate verify
datawrench duckmate rollback
datawrench duckmate up
```

The first `up` prints:

```text
Applying: 20260101120000_create_users.sql
Applied: 1
Writing: db/schema.sql
```

`status` reports `[X] 20260101120000_create_users.sql` and
`Applied: 1, Pending: 0`. The fixture creates a users table and one synthetic row
with an `example.invalid` address. `db/schema.sql` contains the schema and migration
history, **not** the users row.

All relative paths are relative to the current working directory. To verify this
fixture from the repository root, use:

```sh
DATABASE_URL="duckdb:$demo_dir/demo.duckdb" datawrench duckmate verify \
  --migrations-dir examples/duckmate/basic/db/migrations \
  --schema-file examples/duckmate/basic/db/schema.sql
```

## Migration format

```sh
datawrench duckmate new create_accounts
```

This creates `db/migrations/<UTC timestamp>_create_accounts.sql`. Edit it:

```sql
-- migrate:up
CREATE TABLE accounts(id INTEGER PRIMARY KEY, label VARCHAR);
INSERT INTO accounts VALUES (1, 'local demo; semicolons are fine');

-- migrate:down
DROP TABLE accounts;
```

Files must be UTF-8 and named `<numeric_version>_<name>.sql`. Versions are ordered
numerically; duplicate numeric versions (including `001` versus `1`) are rejected.
Recorded history versions must be ASCII decimal digits convertible by Python's
`int` under its configured decimal conversion limit (normally **4,300 digits**,
including leading zeros). History beyond that limit is refused with exit **2**
and a history-validation diagnostic; duckmate does not change Python's limit.
Identical recorded rows in legacy tables without a primary key are also refused,
using the existing `duplicate numeric migration versions` diagnostic and exit **2**;
numeric aliases such as `010` / `10` remain duplicates. History validation checks
all rows for numeric syntax, then conversion limits, then numeric uniqueness.
It preserves stored spelling and never repairs metadata or adopts checksums.
`plan --to VERSION --json` reports these invalid histories as `invalid_input`,
with no steps and unresolved current/target versions set to null; text plans
return the diagnostic on stderr. This history
boundary does not restrict long `--to` inputs: target plans compare normalized
decimal strings before conversion, including arbitrarily many leading zeros.
Require exactly one up marker followed by one down marker, each on its own line.
SQL before the up marker is rejected. Marker lines inside SQL bodies are reserved.
DuckDB's own parser splits statements, including quoted semicolons and dollar
strings. The checksum is SHA-256 of the **entire file**, including down SQL,
comments, line endings and whitespace.

Each migration, including its history updates, commits in its own transaction.
A failing statement reports the file, statement number and SQL, rolls back that
migration, and does not record its version. Earlier successful migrations in the
same invocation stay committed. A failed initial migration leaves no user or
metadata tables, although an empty DuckDB file may have been created.

An optional marker suffix `transaction:false` disables the transaction for that
section only. `transaction:true` is also accepted. In nontransactional mode, a
failure leaves earlier statements committed, with the version unrecorded (or
still recorded for a failed down section). The error explicitly says to repair
the partial changes before retrying. Explicit SQL BEGIN/COMMIT/ROLLBACK statements
are rejected; transaction boundaries belong to duckmate.

## Commands and options

Options may precede or follow the command.

| Command | Behavior |
| --- | --- |
| `new NAME` | Create a timestamped migration template; no database URL required |
| `up`, `migrate` | Apply all pending files; aliases in this tool |
| `rollback [--count N]` | Roll back the N highest applied numeric versions; default 1 |
| `status` | Show applied, pending and missing files; never create a database |
| `dump` | Atomically write a deterministic schema-only snapshot |
| `load` | Build a database from the snapshot; target must not already exist |
| `verify` | Fail on missing/edited migrations, missing checksums or schema differences |
| `plan [--count N]` | Apply pending SQL on a temporary copy and print a unified schema diff; `--count N` plans rollback |
| `plan --to VERSION` | List ordered up/down steps to a version, without parsing or executing migration SQL |

| Option | Default / meaning |
| --- | --- |
| `--url`, `-u` | `DATABASE_URL`; required except for `new` |
| `--migrations-dir` | `./db/migrations` |
| `--schema-file` | `./db/schema.sql` |
| `--migrations-table` | `schema_migrations`; simple identifier in `main`, case-insensitive |
| `--no-dump-schema` | Skip the automatic snapshot after up/migrate/rollback |
| `--strict` | Reject pending versions lower than or equal to the highest applied version; include those problems in JSON status |
| `--dry-run` | Same copy-based execution as plan, for up/migrate/rollback/plan only |
| `--json` | Versioned JSON report for `status` or inventory-only `plan --to VERSION` |
| `--to VERSION` | Inventory-only target plan, for `plan` only; `0` means empty history |

Permissive mode applies out-of-order files in numeric order. Rollback uses numeric
version order, not application time. `--strict` does not reject gaps in applied
history; it only rejects pending out-of-order migrations. `--count` must be positive
for rollback and nonnegative for plan. Unsupported combinations such as
`load --dry-run` fail before any SQL execution.

Exit codes: **0** success, **1** SQL failure, drift, lock contention or I/O error,
**2** invalid input or an explicitly unsupported feature.

## Target-version plans

`plan --to VERSION` generates an ordered migration schedule from file inventory
and recorded history. For example, in `/work` with `001_accounts.sql` applied and
`002_seed.sql` / `010_index.sql` pending:

```sh
datawrench duckmate --url duckdb:local.duckdb plan --to 10
```

```text
Migration plan (no SQL executed)
Current version: 001
Target version: 010
Direction: up
1. up 002 /work/db/migrations/002_seed.sql
2. up 010 /work/db/migrations/010_index.sql
Steps: 2
SQL execution: not checked; a generated plan does not guarantee executable SQL.
```

After applying those files, `plan --to 1` lists down sections for `010` then `002`,
and `plan --to 0` additionally lists `001`. Versions compare numerically;
`10`, `010` and `00010` select the same target while output retains the file's
version spelling. The current version is the highest applied numeric version,
or `0` for an empty/nonexistent database. Nonzero targets must match an existing
file; version `0` is reserved for the empty baseline in this planning mode.
A directory containing a version-zero migration is explicitly rejected.

Up plans select pending files at or below the target in ascending numeric order.
This includes earlier gaps in permissive mode, even when the target equals the
current version. `--strict` rejects those out-of-order steps. Down plans select
only applied files above the target in descending order; a nonzero down target
must already be applied. They leave earlier gaps alone. A plan with no steps
prints `Direction: none` and `Steps: 0` and exits **0**.

Unknown/gap targets (e.g. `3`), targets above the available range (e.g. `11`),
negative/non-numeric targets and unapplied rollback targets exit **2**, explaining
the refusal on stderr with no partial plan on stdout. `--to` cannot be combined
with `--count` (including zero), `--dry-run`, or another command. Edited/missing
applied files, orphan checksums, WAL, lock and I/O problems exit **1**. Legacy
history without checksums retains the same compatibility as ordinary migrations.

This mode hashes files and reads metadata on a disposable database copy. It does
not validate file UTF-8/markers, parse or execute migration SQL, or reconstruct
the schema. A successful schedule means versions can be selected, **not that
the SQL will succeed**. The existing `plan` without `--to` and `plan --count N`
execute SQL on a copy to check execution and show a schema diff; they do not
accept a target version. Even a successful copy execution cannot guarantee
success against a later or concurrently changed database.

Both planning modes leave database, migration and snapshot bytes unchanged, and
an absent database stays absent. They require a checkpointed source with no WAL
and no external writer, may create the sibling advisory lock, and need space
for a full copy. Replay the offline synthetic target fixtures with:

```sh
uv run --extra test pytest tests/duckmate -q -k target_plan
```

### JSON target plans

Add `--json` to capture the inventory schedule for automation:

```sh
datawrench duckmate --url duckdb:local.duckdb plan --to 10 --json > plan.json
```

For the applied-`001` / pending-`002`,`010` example above, stdout is one JSON
document (exit **0**, empty stderr; hashes here are illustrative):

```json
{
  "schema_version": 1,
  "command": "plan",
  "database": "/work/local.duckdb",
  "migrations_directory": "/work/db/migrations",
  "migrations_table": "schema_migrations",
  "strict": false,
  "requested_target": "10",
  "current_version": "001",
  "target_version": "010",
  "direction": "up",
  "steps": [
    {"position": 1, "direction": "up", "version": "002", "path": "/work/db/migrations/002_seed.sql",
     "checksum": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "recorded_checksum": null},
    {"position": 2, "direction": "up", "version": "010", "path": "/work/db/migrations/010_index.sql",
     "checksum": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "recorded_checksum": null}
  ],
  "sql_execution": "not_checked",
  "ok": true,
  "exit_code": 0,
  "problems": []
}
```

Steps have consecutive one-based positions, string versions retaining the file's
spelling, and absolute paths. Up/down ordering and gap handling match text plans.
Each step also includes `checksum` and `recorded_checksum`, additive fields in
schema version 1. `checksum` is the lowercase 64-character SHA-256 of the
**entire raw migration file**: both SQL sections, markers, comments and line
endings, even invalid UTF-8 bytes. It is the same file digest for up and down
steps. `recorded_checksum` is the stored digest for that applied version in the
selected history table; it is null for pending steps and applied legacy versions
without recorded checksums. Null does **not** mean verified, and planning never
records or adopts checksums. A normal down step has equal current and recorded
checksums; editing an applied file still produces `checksum_drift` with no steps.
Editing only a pending file changes its reported checksum without validating its
SQL. These hashes identify the bytes inventoried during this call; the CLI does
not consume saved JSON plans or guarantee those bytes remain unchanged later.

A no-op has `direction: "none"`, `steps: []`, `ok: true` and exit **0**.
`requested_target` preserves the input; `target_version` uses the matched file's
version or `"0"` for the baseline. `sql_execution: "not_checked"` always means
migration SQL was neither parsed nor executed; the report does not certify SQL
validity or live-schema equivalence. Consumers should allow additional fields.

Once arguments parse as `plan --to VERSION --json`, refusals also produce one
complete JSON document on stdout with empty stderr. They have `ok: false`,
`direction: null`, `steps: []`, and `exit_code` matching the process exit code.
There is no partial executable schedule. Each problem has `code`, `message`,
`version` and `path`; the latter two are null when no particular migration is
identified. Context fields (`database`, `migrations_table`, `current_version`,
`target_version`) are null until validated/read/resolved. Messages are diagnostic
text; automation should use codes:

Oversized numeric recorded history uses `invalid_input`/exit **2**, with a message
such as `schema_migrations: recorded migration versions exceed the supported
numeric conversion limit`. Its unresolved `current_version` and `target_version`
and problem `version`/`path` stay null. History validation precedes target range,
reserved-zero and strict scheduling checks. Text target plans report the same
diagnostic on stderr with empty stdout and exit **2**.

Duplicate checksum rows for the selected history table are also refused, even
when their checksums agree, with `<table>: duplicate checksum rows in
_duckmate_checksums`. This protects externally supplied metadata missing its
composite primary key: insertion order never chooses checksum evidence. JSON
target plans return `invalid_input`/exit **2**, empty stderr and no steps; current
and target versions and problem version/path remain null. Text target plans
return the diagnostic on stderr with empty stdout and exit **2**. Recorded-history
syntax, conversion-limit and duplicate-version checks run first; checksum row
duplication is checked before drift, missing/orphan history, zero, target and
strict scheduling checks. Rows for other history tables remain scoped out.
Unique checksum rows work with or without a primary key; duckmate neither repairs
constraints nor records/adopts checksums during planning.

NULL checksum version keys for the selected history table are refused with
`<table>: checksum versions must not be NULL in _duckmate_checksums`. This uses
the same `invalid_input`/exit **2**, complete JSON envelope, null unresolved
context and empty text stdout contract above. Recorded-history validation runs
first, then NULL checksum keys, then non-text keys, then duplicate checksum keys, before drift or
scheduling checks. NULL alone, mixed NULL/string orphan keys and duplicate NULL
keys all refuse regardless of insertion order. Foreign-table NULL keys remain
scoped out; ordinary string orphan checksums still report `orphan_checksum`/exit
**1**. Planning never decodes or executes the migration SQL or changes metadata.

Non-text checksum version keys in selected external metadata (for example BLOB,
INTEGER, LIST or STRUCT values) are refused with `<table>: checksum versions must
be text in _duckmate_checksums`. JSON target plans return the same complete
`invalid_input`/exit **2** envelope with empty stderr, no steps and null unresolved
context; text target plans return the diagnostic on stderr, empty stdout and exit
**2**. Original selected key values are checked before duplicate hashing, dict
conversion, sorting or JSON serialization. No casting or decoding is attempted,
even for BLOB bytes spelling an applied version. Foreign-table keys remain scoped
out; ordinary text keys retain their exact spelling and work with or without a
primary key. This does not enforce the whole external metadata schema or repair
it.

Selected checksum **values** must also be text. Non-text values, including BLOB
bytes spelling the correct SHA-256 hex string and NULL values in external tables
without a NOT NULL constraint, refuse with `<table>: checksum values must be text
in _duckmate_checksums`. Recorded-history validation and all checksum-key checks
(NULL, non-text, duplicates) run first; value validation then precedes drift,
missing/orphan history and scheduling checks. JSON target plans use the same
complete `invalid_input`/exit **2** envelope above, with no steps and null
unresolved context; text target plans return the diagnostic on stderr and empty
stdout. `status --json` returns **2**, empty stdout and the diagnostic on stderr,
following its existing input-error contract; it does not emit a failure JSON
document. Foreign-table values remain scoped out. No value casting or decoding,
checksum adoption, metadata repair or migration SQL execution is attempted.
Ordinary text checksums retain their exact spelling and existing matching,
drift and orphan behavior, with or without a primary key.

Schema dumps export **all** rows of `_duckmate_checksums`, including rows for
other history tables. Every exported checksum version key must be non-NULL text.
NULL keys refuse with `_duckmate_checksums: checksum versions must not be NULL in
schema dumps`; BLOBs (even bytes spelling `010`), numbers and collections refuse
with `_duckmate_checksums: checksum versions must be text in schema dumps`.
Both refusals use exit **2**, empty stdout and a diagnostic on stderr. Catalog
reproducibility and selected recorded-history checks run first, then all exported
NULL keys, then non-text keys, then checksum values. Dump retains duplicate text
checksum rows in external tables without a primary key; it does not apply the
selected status/target-plan duplicate-key rule to exports.

Every exported checksum value must be text; BLOBs (even
ASCII bytes of a correct digest), numbers, collections and NULL values refuse
with `_duckmate_checksums: checksum values must be text in schema dumps`, exit
**2**, empty stdout and a diagnostic on stderr. Existing catalog-reproducibility
and selected recorded-history checks run first. Export values are checked before
checksum SQL literals are rendered or the output is opened: an existing export
stays unchanged and an absent export stays absent. Dump does not decode migration
files, cast/decode checksum keys or values, omit malformed rows, adopt checksums or repair
metadata. Text values retain their exact spelling and escaping, with or without
a composite primary key. Status and target-plan validation remain scoped to the
selected history table, with the key/value precedence described above.

| Problem code | Exit | Meaning |
| --- | --- | --- |
| `invalid_target` | 2 | Negative or non-numeric target |
| `target_out_of_range` | 2 | Target above the available versions |
| `target_not_found` | 2 | Nonzero target is a gap with no migration file |
| `target_not_applied` | 2 | Nonzero rollback target is not in applied history |
| `reserved_version_zero` | 2 | Inventory contains a version-zero migration; its string version and absolute file path identify the offending entry |
| `incompatible_options` | 2 | Target plan combined with `--count` or `--dry-run` |
| `out_of_order` | 2 | Strict upgrade refusal; one problem per selected blocker |
| `checksum_drift`, `missing_file`, `orphan_checksum` | 1 | History preflight failed; all detected history problems are included |
| `wal_present`, `lock_busy` | 1 | Source has a WAL or another duckmate holds its lock |
| `migration_directory_not_found` | 2 | Migration path is missing or not a directory; absolute directory path, null version |
| `invalid_migration_filename` | 2 | A `.sql` filename is invalid; absolute file path, null version |
| `duplicate_migration_version` | 2 | Duplicate numeric version (including leading-zero aliases); absolute file path and its string version |
| `invalid_input` | 2 | Other input validation, including invalid database/snapshot paths or table names |
| `io_error` | 1 | Filesystem failure; a migration-byte read failure includes its string version and absolute path, directory-listing and other filesystem failures have null migration context |
| `database_error`, `plan_error` | 1 | DuckDB or other planning failure |

For applied `010` and pending `002`/`020`, `plan --to 20 --json` lists up steps
`002` then `020`. Adding `--strict` exits **2** with an `out_of_order` problem
for version `002` and its absolute path, `current_version: "010"`,
`target_version: "020"` and no steps. Target `3` exits **2** with
`target_not_found`; target `0` lists down step `010` only. Legacy histories
without checksums retain text-plan compatibility and are not refused solely for
missing checksums. History preflight runs before target lookup/strict checks;
history failures take precedence over them.

Inventory failures are reported before reading database history or resolving the
target, so `current_version` and `target_version` remain null. The first invalid
entry in lexicographic path order is reported, with no partial steps. For duplicate
versions such as `010_one.sql` and `10_two.sql`, the problem identifies the second
path, `10_two.sql`, with `version: "10"`; creation order does not matter. Invalid
filenames have a null version even if they begin with digits. These are filename
checks only; file contents are not decoded or parsed. Text diagnostics and exit
codes are unchanged.

For a file such as `000_zero.sql`, `reserved_version_zero` retains `version:
"000"` and its absolute path, with exit **2** and no steps. This check runs after
history preflight and before target resolution: `current_version` is the highest
recorded version (or `"0"` for absent/empty history), and `target_version` remains
null. Checksum/history failures still take precedence. Duplicate zero aliases,
such as `000_zero.sql` and `0_other.sql`, fail earlier during inventory with
`duplicate_migration_version`, identifying the second lexical entry. Malformed
bytes in a zero-version file are not decoded or executed. The human diagnostic
remains unchanged.

If a validly named entry cannot be read (for example, a directory named
`010_unreadable.sql`), the refusal keeps code `io_error` and exit **1**, with
`version: "010"` and its absolute `path`. The message keeps the original OS
diagnostic. Earlier readable files do not produce partial steps; inventory
failures still leave `current_version` and `target_version` null. Unrelated I/O
errors, such as a database-copy failure, keep null migration version/path.
Missing files during a read and permission errors follow the same read-failure
contract. Migration-directory listing errors (including errors partway through
listing) also fail with `io_error`, exit **1**, the original OS diagnostic and no
steps. Their migration `version`/`path` and current/target versions remain null:
the inventory is unknown, not empty. Enumeration finishes before lexical entry
validation or byte reads, and before lock/history checks. A genuinely empty
directory still produces a successful baseline plan. All commands sharing the
migration inventory fail on listing errors; text diagnostics use the same OS
message. Text plan, execution and status diagnostics retain their original
messages and exit codes.

Malformed CLI syntax (such as missing option values), `plan --json` without
`--to`, and `--json` on unsupported commands retain stderr usage/input errors
and exit **2** without JSON. JSON status keeps its separate contract below.
JSON target plans retain the same copy, WAL, advisory-lock and unchanged-file
boundaries as text target plans. Replay their regression fixtures with:

```sh
uv run --extra test pytest tests/duckmate -q -k json_target_plan
```

## CI migration status

After applying the included fixture, capture a report from the repository root:

```sh
DATABASE_URL="duckdb:$demo_dir/demo.duckdb" datawrench duckmate status --json \
  --migrations-dir examples/duckmate/basic/db/migrations > "$demo_dir/status.json"
python -c 'import json, sys; print(json.load(open(sys.argv[1]))["summary"])' "$demo_dir/status.json"
```

The summary is `{'applied': 1, 'changed': 0, 'missing': 0, 'out_of_order': 0,
'pending': 0, 'unverified': 0}`. Pending files alone succeed in permissive mode.
If an applied file is edited or deleted, the command still writes the complete JSON document to stdout and
returns **1**, so save the report even when a CI step fails. `up`/`migrate` already
refuse either condition before executing pending SQL; their diagnostics now
include the version and absolute path (or expected filename pattern).

The report has `schema_version: 1`, absolute `database` and `migrations_directory`
paths, the normalized `migrations_table`, `strict` (the requested policy), `ok`,
`summary`, `migrations` and `problems`. Entries are ordered by numeric version and keep versions as strings
(including leading zeroes). Each entry has `version`, `path`, `state`, `applied`,
`checksum` (current whole-file SHA-256), `recorded_checksum` (stored SHA-256) and
`out_of_order` (a boolean, described below). These fields extend schema version 1;
consumers should allow additional fields.

| State | Meaning |
| --- | --- |
| `pending` | File exists; version is absent from database history |
| `applied` | Recorded version and file checksum match |
| `changed` | Applied file differs from its recorded checksum |
| `missing` | Applied version has no file; `path` and `checksum` are null |
| `unverified` | Applied file exists but has no recorded checksum (legacy history) |

For a missing version, `expected_path` is an absolute pattern such as
`/workspace/db/migrations/001_*.sql`. History stores versions, not original file
names, so the report cannot recover the deleted file's exact name. The summary's
`applied` count includes changed, missing and unverified applied versions; the
other state counts describe individual states, while `out_of_order` overlaps
`pending`. `problems` contains all detected issues
with `code`, `version`, `path` and `message`; codes are `checksum_drift`,
`missing_file`, `missing_checksum`, `orphan_checksum` and, with `--strict`,
`out_of_order`. An orphan checksum (metadata without an applied version) makes
`ok` false even if every file is
pending or applied. Checksums for other history tables are excluded.

`out_of_order` is true only for a pending version numerically less than or equal
to the highest applied version. `summary.out_of_order` counts those files in both
policies; they remain in the `pending` state/count. Already-applied versions,
gaps with no pending file, newer pending versions and empty history are not
blockers. The highest applied version comes from history even if its file is
changed or missing. In permissive mode this ordering information alone does not
add problems or make `ok` false. `status --json --strict` adds an `out_of_order`
problem per blocker, including its string version, absolute path and the highest
applied version in the message. It emits the complete report and exits **1**,
including any checksum/history problems. This follows JSON status's CI failure
contract; the execution command `up --strict` retains exit **2** for ordering
refusals. Text status retains its existing behavior, including with `--strict`.

For a temporary applied-`010` / pending-`002` fixture (no pending SQL is executed):

```sh
work=$(mktemp -d)
mkdir "$work/migrations"
cat > "$work/migrations/010_accounts.sql" <<'SQL'
-- migrate:up
CREATE TABLE accounts(id INTEGER);
-- migrate:down
DROP TABLE accounts;
SQL
datawrench duckmate --url "duckdb:$work/local.duckdb" \
  --migrations-dir "$work/migrations" up --no-dump-schema
cat > "$work/migrations/002_seed.sql" <<'SQL'
-- migrate:up
INSERT INTO accounts VALUES (2);
-- migrate:down
DELETE FROM accounts WHERE id = 2;
SQL
datawrench duckmate --url "duckdb:$work/local.duckdb" \
  --migrations-dir "$work/migrations" status --json --strict
```

The last command exits **1** with `strict: true`, `ok: false`, summary
`applied: 1`, `pending: 1`, `out_of_order: 1` (other counts zero), and a problem
with `code: "out_of_order"`, `version: "002"` and the seed file's absolute path.
Without `--strict` it exits **0**, keeps the same counts and entry flags, and
reports `strict: false`, `ok: true`, `problems: []`. The accounts table remains
empty, history stays at `010`, and database/migration/snapshot bytes stay unchanged
by either status command. SQL syntax/marker validation is not required for this
inventory report, including for out-of-order files.

`status --json` only inventories and hashes files and reads database metadata.
It never parses or executes migration SQL, creates metadata, or writes a schema
snapshot. Even broken markers or invalid UTF-8 in an edited file can be reported
as drift. It does **not** validate whether pending SQL will succeed or compare the
live schema; use `plan` and `verify` for those checks. Missing checksums return 1
in the JSON report, while upgrades retain existing legacy-history compatibility.
The original text `status` output and exit behavior are retained.

Like other read-only commands, the target database must already exist. Invalid
filenames/duplicate numeric versions, unavailable databases and lock/I/O errors
are command failures: diagnostics go to stderr, with no JSON report. `--json`
on commands other than `status` / `plan --to`, and `status --json --dry-run`, fail with exit 2. The sibling
advisory lock file can be created, but migrations and database bytes are unchanged
for the checkpointed local fixtures. Close other DuckDB writers first.

Replay the temporary-database upgrade/edit/delete and boundary fixtures with:

```sh
uv run --extra test pytest tests/duckmate -q -k 'json_'
```

## Schema snapshots and history compatibility

`schema_migrations(version VARCHAR PRIMARY KEY)` follows dbmate's table shape.
Checksums live separately in `_duckmate_checksums`, keyed by history table and
version. Migration SQL cannot refer to the configured history table or identifiers
starting with `_duckmate`; those names are reserved. Existing dbmate-style history
can be read and rolled back, but `verify` fails explicitly for versions with no
checksum. duckmate does not silently adopt legacy checksums or bless edited files.

The dump includes schemas, sequences, enum/alias/struct types, tables and their
constraints, scalar/table macros, views, indexes, object/column comments, and
inserts for migration history and checksums. It excludes user data. Definitions
come from DuckDB's catalogs; dependency ordering is validated by recreating the
schema in a fresh, restricted in-memory connection. Output is stable across
repeated dumps and dump → load → dump for supported objects. Definition changes
appear in `verify`'s unified diff. Catalog SQL normalizes formatting; the snapshot
is not a copy of the original migration text.

`load` constructs a temporary file beside the target, validates its schema, and
publishes it without overwriting an existing file. Failed loads remove temporary
files. Schema writes use atomic replacement; the output path cannot be the
database, its WAL/lock, or a file under the migrations directory.

Automatic dumping happens **after migrations commit**. If a schema cannot be
dumped or the output cannot be written, the command fails and explicitly reports
that migrations have committed. Repair the object/output and run `dump`; do not
assume the migration was rolled back because snapshot creation failed.

## Local SQL boundary and dry runs

URLs accept `duckdb:relative.duckdb`, `duckdb:/absolute/path.duckdb` and
`duckdb:///absolute/path.duckdb`. URL path percent-encoding is supported. Authorities,
query strings, fragments, Windows/UNC paths, database symlinks, `:memory:`, remote
schemes and embedded remote paths are rejected. Parent directories must already
exist. Parent aliases are resolved to obtain a common lock path.

Every connection disables external access, extension auto-install/loading,
community/unsigned extensions, persistent secrets and unredacted secret access,
then locks the configuration. Only CREATE, ALTER, DROP,
INSERT, UPDATE, DELETE, SELECT and COMMENT SQL classes are accepted. ATTACH,
DETACH, COPY, IMPORT/EXPORT, INSTALL/LOAD, CALL and configuration/transaction
statements are rejected before any section executes. Secrets and temporary
objects are rejected. File/HTTP readers, including those inside SELECT or DDL,
are also blocked by the engine. No shell, subprocess workflow or user script is
executed. SQL failures include the statement text, so keep credentials out of
migration files.

`plan` without `--to` and `--dry-run` **execute local SQL on a disposable copy**, including DML;
they do not just print SQL. They never open the original through DuckDB, write
its bytes, write schema.sql, or call external workflow steps. An existing WAL
causes a refusal: close other writers/checkpoint first. An absent target stays
absent. The diff includes schema and migration metadata, but not row-level data
changes; a plan is not a data-diff tool.

All database commands take an exclusive nonblocking Linux `flock` on the sibling
`<database>.duckmate.lock`. This small sidecar remains on disk to avoid inode races;
even read-only commands and plans can create it. It is not a modification of the
database file. Locks coordinate duckmate processes; other DuckDB clients must be
closed during copying. DuckDB additionally enforces its own file locking for real
connections.

## First-version limits

- Local files and Linux only; no MotherDuck, remote/production databases, attached
  databases, extensions, ORM generation or Go dbmate driver integration.
- Positional scalar and table macros are supported. Macro defaults, overloads and
  typed arguments, catalog tags, temporary objects and definitions that need the
  original **database name** cannot be reproduced and produce explicit errors.
  Use schema-qualified references such as `app.users`, rather than
  `my_database.app.users`.
- Sequences are schema-only: live cursor and original START position are data
  state. Snapshots reset START to MINVALUE for positive increments, MAXVALUE for
  negative increments; no row/sequence continuation backup is promised. This keeps
  ordinary inserts from appearing as schema drift.
- DuckDB's catalog expands named type references in table DDL. Standalone type
  definitions and structural column types are retained; original alias dependency
  identity is not a backup guarantee. Keep the original migrations for full
  historical reconstruction.
- SQL is trusted local migration code within the restricted engine. One thread,
  256 MB engine memory and no temporary-disk spilling are configured, but there
  is no CPU timeout. Plans copy the whole database and need corresponding local
  disk space. Large analytics datasets are outside the tested envelope.
- `verify` compares the current snapshot file and checksum metadata, not a
  separately replayed migration history. Deliberately editing both baselines is
  outside its threat model. No user data checksums or automatic repair.

## Tests

From a datawrench checkout:

```sh
uv run --extra test pytest tests/duckmate -q
```

Tests are synthetic, offline, single-process except for one short-lived CLI
process testing actual lock contention. They cover real dbmate-format fixture
execution, transaction and rollback failures, transaction:false, parser boundaries,
complex dependency ordering and dump/load round trips, checksums/schema drift,
CI JSON status including edited/deleted files, malformed file bytes and strict
out-of-order blockers with permissive-policy and empty/gapped-history boundaries,
target-version schedules (up/down/no-op, per-step whole-file and recorded checksum
evidence including pending edits and legacy nulls, range/gap/strict refusals, invalid SQL
without execution and unchanged database/migration/snapshot bytes),
strict/permissive ordering, byte-identical dry runs, remote/file-access rejection,
atomic loads and path/metadata protections.

## License

Part of datawrench, licensed under Apache-2.0 ([LICENSE](../LICENSE), [NOTICE](../NOTICE)). This command was first released as a standalone MIT-licensed project; that notice is kept in [`licenses/MIT-original-tools.txt`](../licenses/MIT-original-tools.txt). This is an independent implementation, not an upstream dbmate or DuckDB
project. No dbmate source was copied. See [LICENSE](../LICENSE),
[THIRD_PARTY.md](../THIRD_PARTY.md) and [`licenses/duckmate/`](../licenses/duckmate/)
for sources and dependency notices.
