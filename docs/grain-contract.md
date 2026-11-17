# datawrench grain-contract

A model that joins a lookup on a key you believed was unique silently multiplies rows, and a
final `GROUP BY` or a matching total row count can hide it. `datawrench grain-contract` takes
an existing SELECT, the primary keys of its inputs and a short JSON declaration of what each
join should do (`m:1`, preserve the left rows, ...), and compiles them with SQLGlot into
zero-row SQL tests you can keep next to the model, run as dbt singular tests, or execute
against a local DuckDB file. Use it when a model's correctness depends on join grain and you
want that assumption checked on every run, not reviewed once.

## Quick start

```sh
pipx install 'datawrench[sqlglot,ijson,duckdb]'   # duckdb only for `check`

# Emit the tests as JSON on stdout, or as a manifest plus one .sql file per check
datawrench grain-contract compile examples/grain_contract/orders.sql \
  examples/grain_contract/orders.contract.json
datawrench grain-contract compile examples/grain_contract/orders.sql \
  examples/grain_contract/orders.contract.json --out generated

# Build the small demo warehouse, then run every check against it (read-only)
python examples/grain_contract/demo.py            # add --dirty to duplicate a customer key
datawrench grain-contract check examples/grain_contract/orders.sql \
  examples/grain_contract/orders.contract.json --database examples/grain_contract/demo.duckdb
```

`examples/grain_contract/generated/` holds the output of the `compile --out` command above
(with `--dbt-model orders`).

Every emitted SQL query returns zero rows on success. `compile --out DIR` writes a
`manifest.json` and individual `.sql` files suitable for a dbt `tests/` directory; without
`--out` the bundle is printed as JSON. Use `--dbt-model your_model` (requires `--out`) to add
a dbt `depends_on` ref to every test. Input SQL must already have Jinja/`ref`/`source` macros
compiled to real relation names. The manifest includes the extracted join predicates, both
join-key lists, grain transitions, and key labels for interpreting `k0`, `k1`, ... in failure
rows. SQL files omit final semicolons so dbt can wrap them in a subquery. Use `--replace` to
regenerate an existing output directory; unrelated files and symlinks cause a refusal. dbt
test names are prefixed with the model/package.

The compiler never contacts a warehouse. `check` uses DuckDB (extra `duckdb`) in a read-only
transaction with two threads, a 256MB memory budget, external access disabled and a
per-check timeout (`--timeout`, default 30 seconds). `--sample-limit N` (default 10) caps the
failing rows shown per check, `--fail-fast` stops at the first violation to avoid later join
scans, and `--json` prints machine-readable results.

Exit codes: 0 pass, 1 data violation (or stale tests with `from-dbt --check`), 2
configuration, SQL, usage or runtime error.

| Subcommand | Arguments |
| --- | --- |
| `compile SQL CONTRACT` | `--out DIR`, `--dbt-model NAME`, `--replace` |
| `check SQL CONTRACT` | `--database PATH` (required), `--timeout SECONDS`, `--sample-limit N`, `--fail-fast`, `--json` |
| `from-dbt` | `--manifest PATH`, `--model NAME_OR_UNIQUE_ID`, `--contract PATH`, `--out DIR` (all required), `--replace`, `--check` |

## Declaration

```json
{
  "version": 1,
  "dialect": "duckdb",
  "primary_keys": {"orders": ["id"], "customers": ["id"]},
  "scopes": {
    "$root": {
      "grain": ["o.id"],
      "joins": {"c": {"cardinality": "m:1", "preserve": "left"}}
    }
  },
  "output_grain": ["order_id"]
}
```

`primary_keys` names physical tables or named CTEs. Its columns are unqualified. `scopes`
names `$root` or a top-level SELECT CTE. Every join-bearing SELECT must have a declaration,
and its join aliases must exactly match the SQL. `grain` is the input row identity,
qualified by its source alias; it initially refers only to the FROM source. Each join can
override `grain` for its resulting rows. Grain items may also be deterministic scalar SQL
expressions over qualified columns, such as `CAST(o.id AS VARCHAR)` or
`COALESCE(a.id, b.id)` for a FULL JOIN with unmatched rows on either side.
Aggregate/window functions, subqueries, stars, parameters and known volatile functions are
rejected. User-defined functions must be deterministic. The default retains the prior
grain, or uses the right primary key when `preserve` is `right`. `output_grain` names
unqualified model output columns. `dialect` is one of `duckdb` (default), `sqlite`,
`postgres`, `snowflake` and `bigquery`.

`cardinality` is `m:1`, `1:m`, `1:1`, or `m:m`. It generates uniqueness checks on the
relevant **actual join keys**, in addition to declared primary-key checks. These uniqueness
checks include unmatched non-NULL input keys. It also groups actual matched rows by their
declared row identity to enforce at-most-one matching on each `1` side. This catches
implicit-cast/collation effects: integer `1` can match both distinct string keys `'1'` and
`'01'`. Rows with a NULL join-key component are excluded from cardinality checks: ordinary
`=` cannot match them. Primary keys and grain keys must be nonnull. `preserve` is explicitly
`left`, `right`, or `none`. Preservation checks compare both total rows and each preserved
grain's multiplicity, so a lost row cannot be offset by a duplicated one. `none` allows
dropped rows, while still asserting the declared resulting grain.

## Use dbt's actual compiled SQL

```sh
dbt compile
datawrench grain-contract from-dbt --manifest target/manifest.json \
  --model order_enriched --contract examples/grain_contract/dbt.contract.json \
  --out tests/grain_order_enriched
dbt test --select order_enriched+
```

The example contract expects an `order_enriched` model with `o` / `c` aliases joining
`stg_orders` and `stg_customers`. Replace these with your model's names. In `primary_keys`,
`ref:stg_orders` resolves the model/seed/snapshot relation from the manifest.
`source:source.project.raw.orders` resolves a source's `unique_id`. Unprefixed keys continue
to name SQL tables or CTEs. Ambiguous dbt names require a unique_id. `from-dbt` exports
package-qualified `depends_on` refs and preserves the manifest version, model checksum, and
resolved names. It reads `compiled_code` (or legacy `compiled_sql`), never raw Jinja, and
tells you to compile first when compiled SQL is absent. The artifact reader uses ijson to
retain only matching nodes/sources, instead of loading the whole manifest. JSON duplicate
keys and malformed UTF-8 are rejected. dbt itself is never run or imported.

Ephemeral refs resolve from dbt's actual `extra_ctes` definitions; SQLGlot parses the
injected names. Nested WITH is allowed inside opaque, join-free input CTEs, including dbt's
standard ephemeral staging format.

Generated tests check their model's and injected inputs' raw source checksums during
`dbt test` and `dbt build`. A source edit produces an actionable stale-contract compilation
error. Pure `dbt compile` remains available for regeneration. Macro/variable changes can
change compiled SQL while leaving that raw checksum unchanged; use this CI sequence to also
detect those changes and contract edits:

```sh
dbt compile
datawrench grain-contract from-dbt --manifest target/manifest.json --model order_enriched \
  --contract examples/grain_contract/dbt.contract.json --out tests/grain_order_enriched --check
dbt test --select order_enriched+
```

`--check` is read-only (0 current, 1 stale, 2 invalid input). Regenerate stale files with
the same `from-dbt` command plus `--replace`, omitting `--check`. The generated
`manifest.json` records the SQLGlot version that produced it (`sqlglot_version`), so
upgrading SQLGlot also reports the tests as stale until they are regenerated. Direct
`compile --dbt-model` exports have only a scheduling dependency; their raw source checksum
is unavailable, so regenerate them before running tests.

The generated output format is unchanged from the standalone `sql-grain-contract` tool
(`"generator": "sql-grain-contract"` in `manifest.json`), so `--replace` and `--check` accept
directories it wrote. dbt tests generated with a checksum guard by the standalone tool carry
its old regeneration hint in their stale-contract message; `--check` reports them as stale
once, and `--replace` rewrites them with the current hint.

The dbt adapter was validated end to end with dbt Core 1.12.6 / dbt-duckdb 1.11.1 on
DuckDB 2.0.0 and with dbt Core 1.10.15 / dbt-duckdb 1.10.0 on DuckDB 1.4.2 against the real
Jaffle Shop models and seeds: every generated test passed, a deliberately duplicated seed row
made 11 of 32 fail, and editing a model made its stale tests error while compilation still
succeeded. Ephemeral staging models and transitively injected sources were exercised the
same way. The manifests in `tests/grain_contract/fixtures/dbt*/` are extracts of those runs.

## Semantics and limits

Join checks use written FROM/JOIN prefixes **before WHERE, GROUP BY, DISTINCT, window
filtering, ORDER BY, or LIMIT**. Thus a final filter or aggregation cannot hide an exploding
join. The final output-grain check executes the complete SELECT, including its filters and
aggregation. Scope grain describes the join input/intermediate rows, not the scope's
aggregated projection. Checks concern the logical, written join order; the database
optimizer remains free to execute the SQL in another physical order.

Supported: explicit INNER/LEFT/RIGHT/FULL joins whose ON is a conjunction of qualified
`column = column` predicates, including composite keys, self joins, and named top-level
CTEs. No USING/NATURAL/CROSS/SEMI/ASOF/non-equi joins, unqualified join keys, derived-table
joins, recursive WITH, top-level CTE column alias lists, or joins hidden in scalar
subqueries/set operations. Contracted join scopes must be top-level and cannot own local
WITH definitions. Unsupported join shapes fail compilation. Column existence/type validity
belongs to the database; SQLGlot parsing alone cannot establish it. Input SQL is trusted
code.

Generation supports DuckDB, SQLite, Postgres, Snowflake and BigQuery. Only DuckDB local
execution is provided; other dialects require their own engines. Checks rescan join
prefixes and may be expensive on large models. Apply an appropriate upstream input window
via a CTE when validating incremental data. The checks validate the given deterministic
SELECT, rather than a previously materialized table. Recompile tests whenever the model SQL
or contract changes.

The compiler reuses SQLGlot's `eliminate_ctes` rule to prune unused context from each
assertion, after validating every join in the original query. This removes redundant
definitions, not the required scans of relevant join prefixes. Aliases of one input
relation share a primary-key property within a scope, so that full-table PK scan is emitted
once. Join-key and match-count checks remain specific to each join.

SQLGlot: datawrench installs `sqlglot>=30.8,<31` (the `sqlglot` extra shared with
`contract-diff`, `dialect-audit` and `seedtypes`). The compiler was built on SQLGlot 30.31.0;
the AST classes, argument names (`from_`, `with_`) and optimizer rules it uses
(`eliminate_ctes`, `normalize_identifiers`) are the same in 30.8, and the example compiles
to identical SQL on both.

## Prior art

The underlying assertions already exist elsewhere; `grain-contract` connects them to the
individual join prefixes of an existing SELECT:

- **SQLGlot** provides the parser, AST and SQL generator that are reused; it has no user
  grain declarations or per-join runtime assertions.
- **dbt-utils** `equal_rowcount`, `unique_combination_of_columns` and
  `cardinality_equality` cover the individual checks, but do not extract intermediate join
  relations from SELECT SQL. A total row count alone can hide a lost row offset by a
  duplicated one. Generated tests follow dbt's zero-failing-rows convention.
- **dbt constraints and singular tests** do not compile join-step intentions from SELECT
  SQL; `from-dbt` consumes manifests and `extra_ctes` and exports dependency-aware,
  checksum-guarded singular tests.
- **Polars** `join(validate="m:1")` has very close runtime cardinality semantics for
  DataFrame joins (an INNER JOIN still accepts losing an unmatched left row), not for
  existing warehouse SQL.
- **sqlproctor** statically verifies semantic join paths and structural aggregate fan-out;
  it does not emit data assertions per join or detect dirty dimension keys at runtime.
- **SQLMesh** model grain and audits identify rows and run on actual model data; its docs do
  not describe compiling every explicit join into prefix-preservation audits.

See [THIRD_PARTY.md](../THIRD_PARTY.md#grain-contract) for versions, licenses and fixture
provenance.

## Python API

```python
from datawrench.grain_contract import ContractError, compile_contract

bundle = compile_contract(sql_text, contract_dict)   # raises ContractError
for check in bundle.checks:
    print(check.id, check.description)
    print(check.sql)
```

`datawrench.grain_contract.runner.run_bundle(bundle, "warehouse.duckdb")` executes a DuckDB
bundle read-only; `datawrench.grain_contract.dbt.read_manifest` / `from_manifest` resolve a
model from a compiled dbt manifest.
