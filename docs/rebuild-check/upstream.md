# rebuild-check: upstream notes for sqlite-utils (draft, not submitted)

## Expose foreign-key timing from the existing schema parser

`src/datawrench/rebuild_check/constraints.py` adds a narrow adapter using
`sqlite_utils.create_table_parser`'s real lexer, item splitter and quote
handling. SQLite's `PRAGMA foreign_key_list` omits deferred/immediate timing.
Consequently identical FK pragma output can mask removal of `DEFERRABLE
INITIALLY DEFERRED` during a rebuild. A seven-case test checks parser output
against actual SQLite INSERT/COMMIT behavior, including `NOT DEFERRABLE
INITIALLY DEFERRED` (which SQLite treats as immediate).

A public `parse_foreign_keys`/timing API could replace this adapter and let
`Table.foreign_keys` and `transform_sql` preserve timing. The private helpers
used are isolated in one module, and the standalone tool was tested with
sqlite-utils 4.2.1. Upstream API changes may require adjusting that module.

Verified against the real 4.2.1 producer: transform an unrelated INTEGER column
to REAL on `c(x INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED, n
INTEGER)`. Its generated SQL omits deferral; the integration test reports only
`foreign_key_timing_changed`. A separate positive integration test confirms this
sqlite-utils release correctly preserves AUTOINCREMENT high-water marks even for
empty tables; that functionality is reused, not rebuilt here.

## Consider an explicit trigger preservation warning

Reproduction: create `accounts` with a balance column and an AFTER UPDATE audit
trigger; call `db['accounts'].transform(types={'balance': float})`. sqlite-utils
4.2.1 retains rows and a partial balance index and reports clean FKs, but the
trigger vanishes. This is already consistent with the documented limitation. A
preflight warning or a plugin hook around rebuild execution could make the
contract clearer without promising automatic trigger rewrites.

datawrench extends sqlite-utils through its existing CLI plugin entry point; it
does not patch or submit changes to upstream. See
`examples/rebuild_check/ledger.sql` and the real-library integration tests in
`tests/rebuild_check/` for a complete local reproducer.
