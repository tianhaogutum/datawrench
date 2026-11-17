# datawrench rebuild-check

`datawrench rebuild-check` (formerly `sqlite-rebuild-check`, the
`sqlite-utils-rebuild-check` plugin) rehearses SQL migrations on a disposable
backup of a SQLite database and checks that rebuilding a table did not silently
drop its triggers, indexes, views, foreign keys or constraints. SQLite's own
procedure for most schema changes is "create a new table, copy, drop, rename",
and every step can succeed with all rows intact while an audit trigger or a
partial index quietly disappears. Run it before you apply a migration to a real
SQLite file, whether the SQL came from sqlite-utils `transform`, Alembic batch
mode, Django, or your own hand-written script.

It is a check, not another migration generator: it runs the SQL you already
have and compares the before and after state.

## Install

Requires Python 3.11+ linked to SQLite 3.37 or later. The check needs
[sqlite-utils](https://sqlite-utils.datasette.io/) (its CREATE TABLE parser) and
[sqlparse](https://github.com/andialbrecht/sqlparse) (token-level definition
comparison), both in the `sqlite` extra:

```sh
pipx install 'datawrench[sqlite]'     # or: pip install 'datawrench[sqlite]'
datawrench rebuild-check --version    # prints the bare datawrench version, e.g. 0.1.0
datawrench rebuild-check --help
```

`python -m datawrench rebuild-check` is equivalent. Without the extra the
command prints a one-line install hint and exits 2. The standalone tool was
tested with sqlite-utils **4.2.1**; the foreign-key timing adapter uses private
sqlite-utils parser helpers (see [upstream notes](rebuild-check/upstream.md)).

## Quick start

From the datawrench checkout root:

```sh
work=$(mktemp -d)
python examples/rebuild_check/create_ledger.py "$work/ledger.db"

datawrench rebuild-check "$work/ledger.db" examples/rebuild_check/broken-rebuild.sql --temp-dir "$work"
# Exit 1: missing account_audit trigger and balance_idx index.

datawrench rebuild-check "$work/ledger.db" examples/rebuild_check/good-rebuild.sql --temp-dir "$work" --format json
# Exit 0. Full before/after schema and validation results are printed.
```

Text output of the broken rebuild:

```text
FAILED: 2 finding(s)
ERROR object_missing index:balance_idx: An existing schema object was removed
ERROR object_missing trigger:account_audit: An existing schema object was removed
1 definition change(s); use --format json for before/after details.
```

## Usage

```text
datawrench rebuild-check [options] DATABASE MIGRATIONS [MIGRATIONS ...]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--format {text,json}` | `text` | `json` writes one JSON document (`schema_version: 1`) to stdout |
| `--timeout SECONDS` | `30.0` | Total deadline (> 0), including the backup |
| `--foreign-keys {on,off}` | `on` | Initial `PRAGMA foreign_keys` of the migration connection; the SQL may change it |
| `--count-rows` / `--no-count-rows` | on | Count rows of ordinary tables before and after; `--no-count-rows` skips only the count scans |
| `--max-violations N` | `100` | Bound on foreign-key violation samples (N ≥ 1); truncation is explicit |
| `--policy FILE` | | JSON allowlist with exact findings and reasons (see [Intentional changes](#intentional-changes)) |
| `--temp-dir DIR` | system temp | Parent directory for the disposable backup |
| `--connection-plugins` | off | Run installed sqlite-utils `prepare_connection` hooks on the replica |

SQL files run in argument order. A UTF-8 BOM and CRLF are accepted. Use
`--foreign-keys off` only when that matches your migration runner; the default
is on, and a script may explicitly change it. An open transaction at the end of
the last file is an incomplete migration, not an automatic commit.

Exit and status contract: `0 / passed`, `1 / failed` (completed with unaccepted
findings), `2 / error` (input, backup, migration or validation did not
complete; also usage errors). SQL error locations in the JSON report include a
file and starting line.

## What gets checked

- SQLite's online backup API copies the database, including committed WAL
  contents. The source connection is read-only; no migration statement runs
  against it, and the temporary copy is removed on success, failure or
  interruption.
- Before/after SQL definitions for tables, indexes, views and triggers, plus
  table metadata. Missing objects fail. Changes to an existing index, view or
  trigger fail unless explicitly accepted. Ordinary table definition changes are
  reported as expected migration differences.
- Removed or changed foreign-key contracts, including composite keys,
  update/delete actions and deferred enforcement timing; lost UNIQUE, CHECK and
  primary-key constraints and STRICT / WITHOUT ROWID flags. AUTOINCREMENT
  removal and decreased sequence high-water marks are also checked.
- Whole-database foreign-key validation, including child tables outside the
  rebuilt table. SQLite compiles DML probes with enforcement on, catching
  missing parents or invalid parent keys even for empty child tables.
- Surviving views and trigger-bearing tables are prepared with `EXPLAIN`.
  Inserts, updates and deletes are **compiled, never executed**. This catches
  unresolved table and column references without firing a trigger.
- Before/after `integrity_check` and ordinary table row counts. FK and integrity
  validation still read data with `--no-count-rows`. Shadow and virtual tables
  are not counted.

All surviving foreign-key, integrity or reference failures fail the result,
including preexisting ones. The JSON marks matching baseline errors
`preexisting: true`; baseline results remain available when a migration fixes
them.

## Intentional changes

Create a policy file with exact finding codes, object identities and review
reasons:

```json
{"allow": [{"code": "object_missing", "object": "trigger:account_audit", "reason": "Auditing moved to the application in this migration"}]}
```

Then pass `--policy policy.json`. Accepted findings stay visible in the report
(marked `ACCEPTED` in text output; in JSON, `"accepted": true` plus the policy
entry's reason). Duplicate, unknown, empty or unused entries fail instead of becoming
silent blanket exceptions. Policies cannot accept migration errors, foreign-key
violations, broken references or integrity failures. Inspect the JSON before
accepting a dependency-definition change. Policy input is limited to 1 MiB of
characters; an unreadable policy is reported as a `policy_error` finding (exit 2).

## Python API

```python
from datawrench.rebuild_check import check_migration

report = check_migration("copy.db", ["001.sql", "002.sql"], timeout=60)
assert report["exit_code"] == 0, report["findings"]
```

If your schema needs application functions or collations, pass
`prepare_connection=setup` to register them on the **replica** before
inspection. The preparer is called once with `query_only` enabled to avoid
accidental changes to the baseline. It must not leave a transaction open. For
installed sqlite-utils connection plugins, `--connection-plugins` reuses the
host's `prepare_connection` hooks. Hooks are trusted local Python code; the
SQLite deadline does not preempt arbitrary Python hook code. No hooks run on the
original connection.

## As a sqlite-utils plugin

datawrench registers the same check as a sqlite-utils plugin command through
the `sqlite_utils` entry point. sqlite-utils loads it only when datawrench with
the `sqlite` extra is installed in the same environment; nothing in
`datawrench` itself imports the plugin, and the plugin entry point never makes
sqlite-utils a requirement of the base install.

```sh
sqlite-utils install 'datawrench[sqlite]'
sqlite-utils rebuild-check ledger.db migration.sql --format json
```

`sqlite-utils rebuild-check` takes the same arguments and options and prints
the same output as `datawrench rebuild-check`.

## Limits

This checks preservation and resolvable references, not arbitrary behavioral
equivalence. Equal row counts do not prove equal row contents. A trigger can
compile yet implement the wrong business rule; add application-specific tests.
SQLite accepts unknown columns in `UPDATE OF` trigger headers; the check
separately validates that header using sqlparse tokens. Application functions,
collations and extensions require an explicit trusted preparer or connection
plugin; encryption is unsupported. FTS and shadow objects are inventoried but
FTS content consistency is not verified.

SQL must be trusted. As a practical boundary, migration connections reject
ATTACH/DETACH, VACUUM (including INTO), extension loading and writes to
`writable_schema` or directory pragmas. This is not a hardened hostile-SQL
sandbox. Native SQLite locking may create normal WAL/shared-memory sidecars even
for read-only source access. No live source-file byte copy or `immutable=1`
shortcut is used.

Definition comparison uses sqlparse tokens to ignore comments, whitespace and
unquoted ASCII case. It conservatively preserves quote spelling and string
literals; it is not semantic SQL equivalence. sqlite-utils' CREATE TABLE parser
can have narrower coverage than SQLite, and any parse limitation is surfaced.
Statements stream through SQLite's own completion scanner with an 8 MiB
character limit per statement; no unbounded database rows are loaded.

The check does not enforce every aspect of changed column definitions (for
example collation, conflict resolution or default-expression equivalence).
Their complete before/after definitions remain in the JSON for review.
Statements with extreme numbers of semicolons inside string literals may hit
the CPU deadline.

## See also

- [Prior art and design choices](rebuild-check/prior-art.md)
- [Upstream notes for sqlite-utils](rebuild-check/upstream.md)
- Fixture provenance: `tests/rebuild_check/fixtures/README.md`
