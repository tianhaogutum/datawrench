# datawrench check

`datawrench check` reads a data repository and tells you **what will break
before the warehouse does**: breaking changes to enforced dbt model contracts,
seed columns whose default CSV read changes the text, Parquet files that no
longer share a schema, slow Polars patterns, SQL that will not translate to
another dialect, DuckDB migrations that will not run, joins that multiply rows
beyond their TableProof contract, grain contracts that no longer compile against
their SQL, and Singer tap output that breaks its own contract.

It is the front door to the other datawrench commands. It does not reimplement
their analysis: each check calls the read-only Python API of one tool and turns
the result into one report. It never writes a file, never runs dbt, a model, a
tap or a migration, and never opens a network connection.

```sh
uvx 'datawrench[all]' check .                     # or: pipx install 'datawrench[all]'
datawrench check . --base-manifest prod/manifest.json
datawrench check target/compiled --dialect snowflake:duckdb
datawrench check . --format markdown >> "$GITHUB_STEP_SUMMARY"
datawrench check . --format sarif > datawrench.sarif
```

## Try it on the demo project

[`examples/check/`](../examples/check/) is a small synthetic dbt project with a
seed, two manifests (this branch and "production"), a Polars pipeline, Singer
tap output and dbmate-style DuckDB migrations. Nothing in it came from a real
system.

```sh
cd examples/check
datawrench check --base-manifest prod/manifest.json
```

It reports 8 inputs, 5 errors, 5 warnings and 8 info notices, among them:

```text
dbt & SQLMesh
  target/manifest.json  dbt manifest
    error  contract-diff/breaking          model.shop.orders › coupon_code: column removed
    error  contract-diff/breaking          model.shop.orders › customer_id: bigint -> integer (narrowing)

DataFrames & files
  pipelines/daily_revenue.py  Polars script
    warn   polars-vet/PLV004               with_columns in a loop; batch independent expressions outside the loop  line 9
  seeds/zip_codes.csv
    warn   seedtypes/unpinned-lossy        zip: default Polars CSV read alters values; the seed has no column_types pin for it  line 2

Singer pipelines
  singer/orders.jsonl  Singer/Airbyte message file
    error  tapcheck/TC010                  stream orders: key property missing or null at id  line 3
```

`tests/check/test_cli.py::test_readme_demo_output` pins these lines.

## What is detected

Inputs are recognised by their **content**, not their name, so you can point
`check` at the root of a repository. Dot-directories, `node_modules`, `venv`,
`site-packages`, `__pycache__`, `dbt_packages` and `dbt_modules` are skipped,
symlinks are not followed, and in a `target/` folder only the files directly
inside it (such as `manifest.json`) are read, never `target/compiled` or
`target/run`. Pass `target/compiled` explicitly to audit compiled SQL.

| Input | Recognised by |
| --- | --- |
| dbt project | a folder with `dbt_project.yml`; the CSV seeds inside it belong to it |
| dbt manifest | JSON whose `metadata.dbt_schema_version` is a manifest schema and that has `nodes` |
| SQLMesh project | a folder with `models/` and a `config.yaml`/`config.yml`/`config.py` that mentions `gateways`, `model_defaults` or `sqlmesh` |
| SQL file | `*.sql`, except dbmate-style migrations, SQLMesh `MODEL (...)` / `AUDIT (...)` definitions and duckmate schema snapshots |
| SQLFluff config | `.sqlfluff`, or a `[sqlfluff]` / `[tool.sqlfluff]` section in `setup.cfg`, `tox.ini` or `pyproject.toml` |
| Polars script | `*.py` with `import polars` or `from polars import` |
| CSV file | `*.csv` outside a dbt project |
| Parquet dataset | the `*.parquet` files of one folder; Hive partition folders (`dt=2026-11-01/`) are folded into their parent |
| migrations folder | a folder of `*.sql` files with a `-- migrate:up` marker (dbmate format) |
| Singer/Airbyte message file | `*.jsonl`, `*.ndjson`, `*.singer` (or a `*.json` that is really JSON Lines) whose first line is a Singer or Airbyte message |
| Singer/Airbyte catalog | JSON with a `streams` array of Singer (`tap_stream_id`, `schema`) or Airbyte (`json_schema`) entries |
| singer-replay cassette | a folder with `manifest.json`, `messages.jsonl.gz` and `catalog.json` |
| TableProof join contract | `*.toml` with a `[[joins]]` table and a `relationship =` line (TableProof v1, as read by `join-witness`) |
| grain contract | JSON with `version: 1`, `primary_keys`, `scopes` and `output_grain` and no other top-level fields; its SQL is `NAME.sql` next to `NAME.contract.json` |

A `manifest.json` or `catalog.json` that does not parse is reported as
`parse/invalid-json`; other unparseable JSON is skipped silently. Files you
name explicitly are always reported if they are not a recognised input.

## Checks

`datawrench check --list-checks` prints this list. All checks run by default;
a check with nothing to look at stays silent.

| Area | Check | Uses | Reports | Extra |
| --- | --- | --- | --- | --- |
| dbt & SQLMesh | `contract-diff` | `compare_manifests` | with `--base-manifest`: `breaking` (removed columns or models, narrowed or incompatible types, dropped constraints, enforcement turned off), `non-breaking`, `warning`, `summary`; without it `needs-base` for manifests that have enforced contracts | `sqlglot` |
| | `sqlmesh-manifest` | `read_manifest`, `validate_manifest` | for SQLMesh projects: `invalid-manifest` / `valid` for the exported manifest inside the project, `not-exported` with the export command when there is none | `sqlmesh` |
| SQL quality | `lint-cache` | `lint(..., no_cache=True)` | SQLFluff violations per file and rule code (rule id `lint-cache/LT01` etc., first line, count), `PRS` / `TMP` parse and templating errors; `needs-config` without a SQLFluff config, `templater-not-run` for the dbt templater or a Python `library_path` | `sqlfluff` |
| | `dialect-audit` | `run_audit` | with `--dialect FROM:TO`: `parse-error`, `command-fallback`, `unsupported`, `anonymous-function`, `roundtrip-unstable` per statement, `bind-error` / `unknown-table` with `--sql-schema`, `jinja-skipped`, `summary`; without it `needs-dialect` | `sqlglot,duckdb` |
| Keys & joins | `join-witness` | TableProof `load_config`, `analyze(..., redact=True)` (no samples) | TableProof finding codes per join (`TP_RELATIONSHIP_VIOLATION`, `TP_MANY_TO_MANY_EXPANSION`, `TP_LEFT_UNMATCHED`, ...; TableProof's `error`/`warn`/`info`), `summary` with predicted join rows and the rows above the contract caps; `invalid-contract`, `not-checked`. Reads the CSV/TSV files the contract names; reports counts only, never key values | `tableproof` |
| | `grain-contract` | `compile_contract` (SQLGlot only, nothing executed) | `invalid-contract` when the declarations do not compile against the paired SELECT (undeclared or unsupported joins, bad keys or grain), `compiled`; `dbt-contract` for contracts with `ref:`/`source:` keys (they need `from-dbt` and a manifest), `no-sql` without a paired SQL file | `sqlglot` |
| DataFrames & files | `polars-vet` | `analyze` | `PLV001`–`PLV011` (see [polars-vet rules](polars-vet/rules.md)), `PLV000` for files it cannot fully analyse | |
| | `parquet-unify` | `scan`, `Catalog.report` (footers only) | `type-conflict` no lossless common type, `widenable`, `missing-column`, `unreadable` footers, `schema-groups` / `consistent` | `parquet` |
| | `seedtypes` | `check_project`, `compare` | `unpinned-lossy` a seed column the default DuckDB or Polars read changes and `column_types` does not pin, `reader-failed`, `summary`; for CSV files outside dbt, `lossy-default` (info) | `duckdb,polars` |
| | `duckmate` | `migrations`, `statements` (DuckDB's parser, nothing executed) | `invalid-migration`, `parse-error`, `unsupported-statement`, `empty-down`, `summary`; `not-duckdb` when nothing shows the folder is DuckDB's | `duckdb` |
| Singer pipelines | `tapcheck` | `Validator.run` | tapcheck codes per line: `TC010` missing/null primary key, `TC006` schema violation, `TC005` undeclared stream, `TC009` contract change, `TC008` no final STATE (warning), ...; `summary`; catalogs: registration problems or `catalog` | `singer` |
| | `singer-replay` | `verify` | `invalid-cassette` (checksums, catalog or counts do not match), `verified` | |
| Inputs | `parse` | (built in) | inputs that cannot be read or parsed | |

Rule ids are `check/rule`, for example `contract-diff/breaking` or
`polars-vet/PLV004`; the JSON and SARIF output use the same ids.

### Options a check needs

- `--base-manifest PATH`: the `manifest.json` of your main branch or production
  run (for example the artifact your deploy job uploads, or the one you pass to
  dbt `--state`). Every other manifest found is compared against it; the base
  itself is never compared. Without it, contract-diff only says which manifests
  have enforced contracts to compare.
- `--dialect FROM:TO`: the dialect migration dialect-audit checks, in SQLGlot's
  names (`snowflake`, `bigquery`, `postgres`, `redshift`, `tsql`, `databricks`,
  `duckdb`, ...). `--dialect :duckdb` takes `FROM` from the dbt manifest's
  `adapter_type` when all manifests agree. Without `--dialect`, dialect-audit
  only says how many SQL files it could audit. Files with Jinja are skipped
  (run `dbt compile` and pass `target/compiled`).
- `--sql-schema FILE`: an empty-table DDL or YAML fixture. With a `duckdb`
  target, dialect-audit then binds each query in an in-memory DuckDB (`EXPLAIN`,
  nothing executed) and reports `bind-error` and `unknown-table`. Without it,
  those binding results are left out, because without tables every query would
  fail to bind.
- lint-cache uses your own SQLFluff config. check passes `no_cache=True`, so no
  `.sqlfluff_cache` is created, and it refuses the dbt templater and Python
  `library_path` (both run project code); run `datawrench lint-cache lint DIR
  --trust-template-code` yourself for those.

### Severities

- **error**: it will break as it stands: a breaking contract change, a Parquet
  column with no lossless common type, a migration section DuckDB cannot parse
  or duckmate refuses, a record without its primary key, a SQL statement that
  does not parse, a cassette that does not verify, a join that breaks its
  TableProof relationship, a grain contract that does not compile.
- **warning**: it will probably bite: a seed column whose default read drops
  leading zeros, slow Polars patterns, Parquet types that differ between files,
  unknown functions in a dialect migration, tap output with no final STATE.
- **info**: worth knowing: summaries, options a check needs, inputs a check does
  not read, CSV files outside dbt whose default read changes values.

`--fail-on` (default `error`) picks the level that makes `check` exit 1.

### Optional dependencies

The base install has no heavy dependencies. A check whose extra is missing
becomes one info finding with the install command, for example
`pipx install 'datawrench[parquet]'`; the other checks still run.
`datawrench[all]` installs every extra except `sqlmesh`, which pins SQLMesh's
own sqlglot version: add it with `pipx install 'datawrench[all,sqlmesh]'`.

## Output

### Text (default)

Findings are grouped by area, then by input, most severe first, and end with a
one-line summary:

```text
8 inputs · 5 errors · 5 warnings · 8 info
```

`-v` adds a fix hint (usually the datawrench command that shows or fixes the
problem) and the location inside the input under each finding, and lists the
inputs without findings. Colour is used on a terminal unless `NO_COLOR` is
set; force it with `--color always|never`. Text from inputs is printed with
control characters escaped.

### Markdown (`--format markdown`)

A summary line, then one table per area with errors and warnings, and the info
notices folded into a `<details>` block, for a PR comment or the GitHub Actions
job summary:

```yaml
- name: Check the data repo
  run: |
    uvx 'datawrench[all]' check . --base-manifest prod/manifest.json --format markdown >> "$GITHUB_STEP_SUMMARY"
    uvx 'datawrench[all]' check . --base-manifest prod/manifest.json
```

Cells are escaped (pipes, HTML, `@mentions`) and each area shows at most 40
rows, so the output stays under GitHub's comment size limit.

### JSON (`--format json`)

```json
{
  "format": "datawrench.check/v1",
  "tool": {"name": "datawrench", "version": "0.1.0"},
  "paths": ["."],
  "checks": ["contract-diff", "sqlmesh-manifest", "lint-cache", "dialect-audit", "polars-vet", "..."],
  "inputs": [{"path": "pipelines/daily_revenue.py", "kind": "python-polars", "label": "Polars script",
              "findings": 3}],
  "findings": [{
    "check": "polars-vet", "rule": "PLV004", "ruleId": "polars-vet/PLV004", "severity": "warning",
    "area": "DataFrames & files",
    "message": "with_columns in a loop; batch independent expressions outside the loop",
    "file": "pipelines/daily_revenue.py", "line": 9, "pointer": null, "subject": null, "hint": "..."
  }],
  "summary": {"inputs": 8, "inspected": 11, "error": 5, "warning": 5, "info": 8},
  "failOn": "error",
  "failed": true
}
```

### SARIF (`--format sarif`)

SARIF 2.1.0 for GitHub code scanning. Findings on a file point at the file and
line; findings about a folder (a dbt project, a Parquet dataset, a migrations
folder) and run-level notices become tool execution notifications.

## Exit status

| Code | Meaning |
| --- | --- |
| 0 | No finding at or above `--fail-on` |
| 1 | At least one finding at or above `--fail-on` |
| 2 | Usage or input error: unknown option or check, missing path, bad `--dialect` |

A check that crashes becomes one `internal` error finding; the other checks
still run. Set `DATAWRENCH_CHECK_DEBUG=1` to get the traceback instead.

## Library use

```python
from datawrench.check import CheckOptions, discover, run_checks, select

discovery = discover(["."])
findings = run_checks(discovery, select(only=["contract-diff", "seedtypes"]),
                      CheckOptions(base_manifest="prod/manifest.json"))
for finding in findings:
    print(finding.severity, finding.rule_id, finding.file, finding.message)
```

## Limits

- `check` reads files; it does not connect to a warehouse. A contract diff
  compares two manifests, seed checks compare the CSV text with what the
  default readers produce locally, and Parquet checks read footers only.
- duckmate migrations are parsed only when a duckmate schema snapshot or a
  `.duckdb` file next to them shows they are DuckDB's: dbmate folders for
  Postgres or MySQL are left alone.
- dialect-audit reports what SQLGlot cannot translate or verify, not proof that
  translated SQL is equivalent.
- Not every command has a check. `csv-rewind` needs a proposed schema,
  `key-audit` needs the key columns and an ordered rule list, `rebuild-check`
  runs a migration on a database backup and `failurepack` runs a chosen Pandera
  failure many times; none of that can be inferred from file content or done
  read-only, so run those commands directly.
- Each tool reads a documented, strict input format and says (as an info or
  warning finding) when it refuses an input instead of guessing.
