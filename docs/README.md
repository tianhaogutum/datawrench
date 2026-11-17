# datawrench documentation

One page per command. Every command runs offline; `datawrench <command> --help`
lists its options, and the [README](../README.md) shows which extra each one needs.

## Check

- [check](check.md): one read-only pass over a data repository that runs the analyses of the other commands and reports what will break.

## dbt & SQLMesh

- [contract-diff](contract-diff.md): type-aware compatibility gate between two dbt manifest model contracts.
- [sqlmesh-manifest](sqlmesh-manifest.md): export a SQLMesh project as an offline dbt manifest v12; validate and diff it.

## SQL quality

- [lint-cache](lint-cache.md): content-addressed per-file cache for SQLFluff lint results.
- [dialect-audit](dialect-audit.md): per-statement SQL dialect migration report with offline DuckDB binding checks.
- [rebuild-check](rebuild-check.md): rehearse SQLite migrations on a backup and check that table rebuilds kept triggers, indexes, views, keys and constraints ([prior art](rebuild-check/prior-art.md), [upstream notes](rebuild-check/upstream.md)).

## Keys & joins

- [join-witness](join-witness.md): which source records multiply a join and how that changes a sum (TableProof adapter).
- [grain-contract](grain-contract.md): compile SELECT SQL, primary keys and join-grain declarations into zero-row SQL tests, also as dbt singular tests from a compiled manifest.
- [key-audit](key-audit.md): preview Unicode key cleanup on a CSV before distinct join keys collapse into one ([integrations with Miller, csvkit, OpenRefine](key-audit/integrations.md)).

## DataFrames & files

- [polars-vet](polars-vet.md): static performance checks for Polars code ([rules](polars-vet/rules.md)).
- [parquet-unify](parquet-unify.md): audit and reconcile drifting Parquet schemas, verified across DuckDB and Polars.
- [seedtypes](seedtypes.md): lossless CSV/seed type inference with pinned dbt, DuckDB and Polars recipes.
- [duckmate](duckmate.md): dbmate-format DuckDB migrations with schema snapshots and drift verification.
- [csv-rewind](csv-rewind.md): what a proposed DuckDB CSV schema would do to the original cell text, with per-cell evidence and verified frozen read SQL.
- [failurepack](failurepack.md): a minimal replayable CSV bundle for one selected Pandera failure ([prior art](failurepack/prior-art.md), [upstream notes](failurepack/upstream-notes.md)).

## Singer pipelines

- [tapcheck](tapcheck.md): streaming contract and data-quality checks for Singer and Airbyte JSONL.
- [singer-replay](singer-replay.md): record scrubbed Singer cassettes and replay them as a connection-free tap.
