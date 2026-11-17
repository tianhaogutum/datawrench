# rebuild-check: prior art and chosen integration

Survey date: 2026-11-11, when `datawrench rebuild-check` was still the
standalone `sqlite-utils-rebuild-check` plugin. Public documentation and
installed source were read before implementation. No user database was sent
anywhere. Generated SQL and measurements from that survey are not shipped with
datawrench; the tests regenerate the relevant SQL with the real libraries.

## sqlite-utils 4.2.1 — run locally

https://sqlite-utils.datasette.io/en/stable/cli.html#transforming-tables
https://sqlite-utils.datasette.io/en/stable/plugins.html
https://github.com/simonw/sqlite-utils

`transform` already generates and executes table rebuilds, preserves
ordinary/partial indexes for compatible transformations, and performs
foreign-key validation. Its documented plugin API adds CLI commands. We
installed 4.2.1, inspected `Table.transform_sql`, generated the transform SQL,
and ran `transform(types={"balance": float})` on the example ledger
(`examples/rebuild_check/ledger.sql`). Accounts/entries counts remained 2/1 and
`foreign_key_check` was empty, but `account_audit` disappeared. This is the
concrete remaining gap: checking arbitrary migration SQL against the
pre-migration dependency contract. We extend this CLI instead of creating a
migration generator. The actual host `prepare_connection` hook is also tested
with a custom collation and CHECK function.

## SQLite 3.53.1 — run locally

https://www.sqlite.org/lang_altertable.html#making_other_kinds_of_table_schema_changes
https://www.sqlite.org/foreignkeys.html
https://www.sqlite.org/backup.html
https://www.sqlite.org/sqldiff.html

SQLite itself provides the backup API, SQL parser, schema catalog, foreign-key
metadata/checks and EXPLAIN compiler. We use those mechanisms instead of
inventing a SQLite parser or copying live database files. The official rebuild
procedure requires reconstructing dependencies. `sqldiff` documentation
explicitly excludes trigger/view differences by default and says it is not
designed for migrations. **sqldiff was not installed or run**; its documented
limitations were verified from the official page. Python linked SQLite is
3.53.1; system CLI may differ.

## Alembic 1.20.1 / SQLAlchemy 2.1.8 — run locally

https://alembic.sqlalchemy.org/en/latest/batch.html

Alembic provides reflection and batch move-and-copy migrations, including
indexes and many constraints. It does not promise automatic trigger
preservation. We generated batch SQL with its real offline
`Operations.batch_alter_table` API and reflected metadata. Executing it against
the ledger failed at rename with `error in view balances: no such table:
main.accounts`. This strengthens the case for checking actual execution and
surviving references, rather than regenerating migrations. Alembic is an
optional test dependency only.

## Atlas — docs only, not run

https://atlasgo.io/versioned/lint
https://atlasgo.io/lint/analyzers
https://atlasgo.io/features

Atlas has a broader migration linting/testing system, SQLite support, and
constraint-drop/destructive-change analyzers. The current documentation lists
SQLite-specific LT101 and feature tiers. We did not install a binary or claim
comprehensive equivalence. A local sqlite-utils adapter is a much smaller
integration for an existing SQLite file plus arbitrary SQL, with no Atlas
project, login, or desired-schema model. Version: public docs as retrieved on
2026-11-11; binary version unverified.

## sqlite-schema-diff — docs only, not run

https://github.com/mizuchilabs/sqlite-schema-diff
Survey HEAD: `07792882f41368268b987ea4ca7bf91f7f2fada8` (`git ls-remote`).

Its README covers tables, indexes, views, triggers, schema diff, rebuild
generation, backup and pre-commit FK checking. This already solves schema-first
migration generation; rebuilding those features would duplicate it. The
remaining gap is testing independently authored SQL against an existing
populated database and flagging dependencies that vanished even without a
complete target schema. No claim is made about untested runtime behavior.

## mdg-labs/sqlite-migrate — docs only, not run

https://github.com/mdg-labs/sqlite-migrate
Survey HEAD: `14bd1df24dcf88254efbb901d1f875d47c30a11a` (`git ls-remote`).

Its README describes STRICT-schema migration generation, journal replay against
scratch databases, actual apply with FK/integrity checks, and structural
verification against the journal. Those workflows cover substantial parts of
the idea when adopted as the migration system. This check instead accepts
existing non-STRICT databases and SQL from any producer, and compares
preservation with the before state. No Go installation/build was needed for the
narrower sqlite-utils integration.

## migration-validator — docs only, not run

https://github.com/CheesyChicken/migration-validator
Survey HEAD: `d4fe7cd05c7d6e1e3ced64dfc4a5c65d665e850d` (`git ls-remote`).

Compares two databases, schemas, row checksums and samples. Its README
explicitly excludes foreign keys, triggers, views and CHECK constraints. Reusing
it would require expanding its connector and supplying a migration execution
stage. The sqlite-utils CLI plugin plus native SQLite inspection is the narrower
integration for this exact gap.

## Later integration and parser probes — run locally

**Django 5.2.19** — https://docs.djangoproject.com/en/5.2/topics/migrations/#sqlite .
Generated real SQLite schema-editor field-type-change SQL (see
`tests/rebuild_check/fixtures/django-5.2-rebuild.sql`). This is another
established rebuild producer, not something to reimplement. Its model migration
drops a database-level audit trigger outside model metadata; the check finds
that loss.

**SQLGlot 30.31.0** — https://github.com/tobymao/sqlglot . Installed and ran the
SQLite DDL parser as a possible alternative for deferred-FK inspection. It
parses explicit-parent deferral, but rejects implicit-parent NOT DEFERRABLE and
typed UNIQUE ON CONFLICT IGNORE statements that real SQLite accepts. It is not a
dependency of `rebuild-check`. The narrower adapter reuses the host's
SQLite-specific lexer instead.

## Decision

Build an **extension of sqlite-utils**, with a small reusable Python check
function. Reuse Click/pluggy command registration, SQLite
backup/metadata/compiler, and an established SQL tokenizer only for
conservative textual definition comparison. Do not generate migrations or claim
full behavioral equivalence. Preserve before/after SQL for human review, detect
missing objects and constraint metadata, check all cross-table FKs, prepare
surviving views/trigger statements, and distinguish intentional differences
through exact reviewed policy entries. In datawrench the same function is also
exposed as the argparse subcommand `datawrench rebuild-check`.
