# Third-party sources and attribution

datawrench is licensed under the Apache License, Version 2.0 (see [LICENSE](LICENSE) and
[NOTICE](NOTICE)). Its commands were previously separate projects by the same author:
`contract-diff`, `sqlmesh-manifest`, `tapcheck` and `singer-replay` were Apache-2.0 (their
NOTICE files are merged into [NOTICE](NOTICE)); `lint-cache`, `dialect-audit`, `polars-vet`,
`parquet-unify`, `seedtypes` and `duckmate` were MIT (the original permission notice is kept
in [`licenses/MIT-original-tools.txt`](licenses/MIT-original-tools.txt)). Unless a section
below says otherwise, all code, synthetic fixtures, tests and documentation are original
work; they contain no production data, customer data or credentials.

datawrench is an independent interoperability project. It is not affiliated with or
endorsed by dbt Labs (dbt), Tobiko Data / the SQLMesh project, the SQLGlot maintainers,
the SQLFluff project, the DuckDB Foundation, the Polars project, the Apache Software
Foundation (Arrow, Parquet), the dbmate maintainers, Singer, Meltano, Airbyte, OASIS
(SARIF) or any other project named below. Product names only identify interoperable file
formats and tools. This file is attribution; it does not relicense any dependency or
upstream material.

## Dependencies (installed separately, never vendored)

The base install needs only PyYAML. Heavier dependencies are optional extras, installed by
your package manager under their own licenses; they keep their own notices in their
distributions. The datawrench wheel contains only the `datawrench` package (including the
JSON schemas `sqlmesh-manifest` validates against, see below) and these project notices.

| Package | License | Used by | Kind |
| --- | --- | --- | --- |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | `dialect-audit`, `seedtypes` | base |
| [SQLGlot](https://github.com/tobymao/sqlglot) | MIT | `contract-diff`, `dialect-audit`, `seedtypes`; `sqlmesh-manifest` via SQLMesh | extra `sqlglot` (`>=30.8,<31`) |
| [DuckDB](https://github.com/duckdb/duckdb) | MIT | `duckmate`, `dialect-audit`, `parquet-unify`, `seedtypes` | extra `duckdb` (`>=1.4.2,<2`) |
| [fsspec](https://github.com/fsspec/filesystem_spec) | BSD-3-Clause | DuckDB file-like reads in `seedtypes` | extra `duckdb` |
| [Polars](https://github.com/pola-rs/polars) | MIT | `parquet-unify`, `seedtypes`; `polars-vet` tests | extra `polars` (`>=1.35.2,<2`) |
| [PyArrow](https://github.com/apache/arrow) | Apache-2.0 (wheel bundles further notices) | `parquet-unify` | extra `parquet` (`>=21,<24`) |
| [SQLFluff](https://github.com/sqlfluff/sqlfluff) | MIT | `lint-cache` | extra `sqlfluff` (`>=4.5,<4.6`) |
| [Jinja2](https://github.com/pallets/jinja) | BSD-3-Clause | `lint-cache` | extra `sqlfluff` |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | MIT | `tapcheck`, `sqlmesh-manifest`; `dialect-audit` SARIF tests | extras `singer`, `sqlmesh` |
| [rfc3339-validator](https://github.com/naimetti/rfc3339-validator) | MIT | `tapcheck` `date-time` checks | extra `singer` |
| [SQLMesh](https://github.com/SQLMesh/sqlmesh) | Apache-2.0 | `sqlmesh-manifest` | extra `sqlmesh` (`>=0.236.4,<0.237`) |
| [dbt-artifacts-parser](https://github.com/yu-iskw/dbt-artifacts-parser) | Apache-2.0 | `sqlmesh-manifest` | extra `sqlmesh` |
| [ruamel.yaml](https://pypi.org/project/ruamel.yaml/) | MIT | `sqlmesh-manifest` | extra `sqlmesh` |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | `sqlmesh-manifest` | extra `sqlmesh` |
| [flake8](https://github.com/PyCQA/flake8) | MIT | `polars-vet` as a flake8 plugin | extra `flake8` |
| [pytest](https://github.com/pytest-dev/pytest) | MIT | test runner | test |
| [pandas](https://github.com/pandas-dev/pandas) | BSD-3-Clause | `polars-vet` PLV007 benchmark replay | test |
| [rfc3986-validator](https://github.com/naimetti/rfc3986-validator) | MIT | `uri` format checks in `dialect-audit` SARIF tests | test |
| [Hatchling](https://github.com/pypa/hatch) | MIT | build backend | build |
| [hatch-fancy-pypi-readme](https://github.com/hynek/hatch-fancy-pypi-readme) | MIT | PyPI README link rewriting | build |

Python and its standard library are provided by the user's Python installation under the
[PSF License Agreement and bundled notices](https://docs.python.org/3/license.html).

### License texts in `licenses/`

[`licenses/`](licenses) keeps the license and notice files the standalone projects recorded
for the dependency versions they were tested with (copied unchanged from the installed
distributions or upstream repositories). They document those dependencies; apart from the
dbt manifest schema license, the files are not shipped inside the wheel, and none of these
packages is vendored.

| Path | Contents |
| --- | --- |
| [`licenses/MIT-original-tools.txt`](licenses/MIT-original-tools.txt) | Original MIT notice of the six formerly MIT-licensed commands |
| [`licenses/contract-diff/`](licenses/contract-diff) | SQLGlot MIT notice |
| [`licenses/sqlmesh-manifest/`](licenses/sqlmesh-manifest) | dbt Labs schemas repository license (Apache-2.0) for the vendored manifest v12 schema |
| [`licenses/lint-cache/`](licenses/lint-cache) | SQLFluff MIT notice |
| [`licenses/dialect-audit/`](licenses/dialect-audit) | SQLGlot, DuckDB, PyYAML notices; OASIS SARIF schema notice |
| [`licenses/seedtypes/`](licenses/seedtypes) | DuckDB, Polars, SQLGlot, PyYAML, fsspec notices |
| [`licenses/duckmate/`](licenses/duckmate) | DuckDB notice |

## contract-diff (formerly dbt-contract-diff)

Originally released under Apache-2.0. The comparison code, policy format, tests and all
fixtures (hand-written manifests in `tests/contract_diff/fixtures/`, the synthetic dbt
project in `tests/contract_diff/fixture_project/`, and `examples/contract_diff/`) are
original work. It reads local dbt JSON artifacts and uses SQLGlot as a library; no dbt or
SQLGlot source is vendored or patched.

| Component | Role | License |
| --- | --- | --- |
| [SQLGlot](https://github.com/tobymao/sqlglot) | Runtime dependency (extra `sqlglot`; originally pinned 30.31.0; the type-parsing code it relies on is unchanged in 30.8.x): type tokenizer, dialect parsers and AST | MIT; notice in [`licenses/contract-diff/SQLGLOT-MIT.txt`](licenses/contract-diff/SQLGLOT-MIT.txt) |
| [dbt](https://github.com/dbt-labs/dbt) | Manifest format; independent emulation of the prefix-normalisation baseline in `tests/contract_diff/compare_baseline.py` | Apache-2.0; no upstream code copied |
| [dbt-core](https://github.com/dbt-labs/dbt-core) 1.12.6 + [dbt-duckdb](https://github.com/duckdb/dbt-duckdb) 1.11.1 + [DuckDB](https://github.com/duckdb/duckdb) 1.4.4 | Optional, manual regeneration of `tests/contract_diff/fixtures/real` only; not installed by any extra | Apache-2.0 / Apache-2.0 / MIT; nothing redistributed |

`tests/contract_diff/fixtures/real` holds model-only extracts of real `dbt parse` output
(manifest v12) from the checked-in synthetic project, with telemetry disabled and no
warehouse access. Volatile timestamps and UUIDs are normalized, `created_at` is omitted,
user/environment metadata is cleared, and dependency macros and unrelated sections are
excluded. No production data, credentials or upstream macro source is included.

Formats referenced: dbt manifest JSON (v12 and the consumed model/contract/constraint
fields). The report (`schema_version` 1) and policy (`version` 1) formats are defined by
this project. dbt is a trademark of dbt Labs, Inc.

## sqlmesh-manifest (formerly sqlmesh-dbt-manifest)

Originally released under Apache-2.0. The exporter, validators, diff code, tests and
synthetic examples (`examples/sqlmesh_manifest/`, `tests/sqlmesh_manifest/fixtures/`) are
original work. No SQLMesh or parser source was copied.

Redistributed schema (shipped in the wheel):

- File: `src/datawrench/sqlmesh_manifest/manifest-v12.json` (unmodified).
- Source: https://schemas.getdbt.com/dbt/manifest/v12.json (upstream repository
  https://github.com/dbt-labs/schemas.getdbt.com), published by dbt Labs, Inc. under
  Apache-2.0. Retrieved 2026-11-11; SHA-256
  `f29ac66b0ea66b46575da0a5da66b2716f06f25295044234304e16631773ea4c`.
- Upstream license reproduced in
  [`licenses/sqlmesh-manifest/dbt-schemas-LICENSE`](licenses/sqlmesh-manifest/dbt-schemas-LICENSE).
  The schema is vendored so validation is offline; no schema URL is fetched.

| Component | Role | License |
| --- | --- | --- |
| [SQLMesh](https://github.com/SQLMesh/sqlmesh) | Runtime (extra `sqlmesh`; tested 0.236.4): Context, models, audits and DAG | Apache-2.0 |
| [dbt-artifacts-parser](https://github.com/yu-iskw/dbt-artifacts-parser) | Runtime (extra `sqlmesh`; tested 0.15.0): independent v1–v12 manifest parser | Apache-2.0 |
| [SQLGlot](https://github.com/tobymao/sqlglot) | Runtime (via SQLMesh `~=30.8.0`; tested 30.8.0): SQL ASTs and identifiers | MIT |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | Runtime (tested 4.26.0): v12 and diff/result schema validation | MIT |
| [ruamel.yaml](https://pypi.org/project/ruamel.yaml/) | Runtime (tested 0.19.1): safe literal YAML config loading | MIT |
| [Pydantic](https://github.com/pydantic/pydantic) | Runtime (tested 2.14.3): parser validation diagnostics | MIT |
| [requests](https://github.com/psf/requests) | Tests only (transitive via SQLMesh): network-blocking assertions | Apache-2.0 |

Formats referenced: dbt manifest v1–v12 JSON. The diff (`diff_version` 1), provenance
report (`report_version` 1) and validation-result formats (version 1) and their packaged
JSON schemas are defined by this project. SQLMesh and dbt are trademarks of their
respective owners.

## lint-cache (formerly sqlfluff-cache)

Originally released under the MIT License. The implementation, the synthetic example
project in `examples/lint_cache/` and the fixtures in `tests/lint_cache/fixtures/` are
original work. SQL parsing, templating, rules, file discovery, ignore handling and
serialization are performed by the installed SQLFluff package; no SQLFluff engine, rule
source or test fixture is vendored. The implementation was written against SQLFluff's
`FluffConfig`, `Linter`, `paths_from_path`, `LintedDir` and CLI JSON / GitHub-annotation
output contracts; SQLFluff's MIT notice is kept for attribution of that interface-derived
work.

| Package | Tested version | License | Kind | Notice |
| --- | --- | --- | --- | --- |
| [SQLFluff](https://github.com/sqlfluff/sqlfluff/tree/4.5.0) | 4.5.0 | MIT | runtime (extra `sqlfluff`) | [`licenses/lint-cache/sqlfluff-LICENSE`](licenses/lint-cache/sqlfluff-LICENSE) |
| [Jinja2](https://github.com/pallets/jinja) | 3.1.6 | BSD-3-Clause | runtime (extra `sqlfluff`; inspects Jinja ASTs for `random`) | — |
| [pytest](https://github.com/pytest-dev/pytest) | 9.0.2 | MIT | test | — |

SQLFluff's own dependencies in the tested environment (installed, not vendored):
MarkupSafe (BSD-3-Clause), chardet (0BSD), click (BSD-3-Clause), colorama (BSD-3-Clause),
diff-cover (Apache-2.0), packaging (Apache-2.0 OR BSD-2-Clause), pathspec (MPL-2.0),
platformdirs (MIT), pluggy (MIT), Pygments (BSD-2-Clause), PyYAML (MIT), regex
(Apache-2.0 AND CNRI-Python), tblib (BSD-2-Clause), tqdm (MPL-2.0 AND MIT).

dbt is named only to describe templater compatibility.

## dialect-audit

Originally released under the MIT License. The implementation and the synthetic SQL/YAML
fixtures in `examples/dialect_audit/` and `tests/dialect_audit/fixtures/` are original
work. No SQLGlot or DuckDB implementation or test code is copied.

| Package | Tested version | License | Kind | Notice |
| --- | --- | --- | --- | --- |
| [SQLGlot](https://github.com/tobymao/sqlglot) | 28.0.0 (datawrench installs `>=30.8,<31`) | MIT | runtime (extra `sqlglot`) | [`licenses/dialect-audit/sqlglot-LICENSE`](licenses/dialect-audit/sqlglot-LICENSE) |
| [DuckDB](https://github.com/duckdb/duckdb) | 1.4.1 (datawrench installs `>=1.4.2,<2`) | MIT | runtime (extra `duckdb`) | [`licenses/dialect-audit/duckdb-LICENSE`](licenses/dialect-audit/duckdb-LICENSE) |
| [PyYAML](https://github.com/yaml/pyyaml) | 6.0.3 | MIT | runtime | [`licenses/dialect-audit/pyyaml-LICENSE`](licenses/dialect-audit/pyyaml-LICENSE) |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | 4.25.1 | MIT | test (SARIF validation) | — |
| [rfc3986-validator](https://github.com/naimetti/rfc3986-validator) | 0.1.1 | MIT | test (`uri` format checks) | — |

The notices were copied verbatim from the installed distributions; those distributions keep
their additional bundled notices (including DuckDB's unused experimental Spark interface).

**Formats referenced:** SARIF 2.1.0 (OASIS). The tests keep an unmodified copy of the
official OASIS SARIF 2.1.0 plus Errata 01 JSON schema at
`tests/dialect_audit/schemas/sarif-schema-2.1.0.json`, governed by the OASIS copyright
notice in `tests/dialect_audit/schemas/OASIS-NOTICE.txt` (also at
[`licenses/dialect-audit/OASIS-SARIF-NOTICE.txt`](licenses/dialect-audit/OASIS-SARIF-NOTICE.txt));
provenance and SHA-256 are in `tests/dialect_audit/schemas/README.md`. It is not shipped in
the wheel. DuckDB EXPLAIN and security-model documentation and SQLGlot's public
documentation were consulted as references only. Snowflake and other dialect names
identify SQL dialects only.

## polars-vet

Originally released under the MIT License. The analyzer, rules, source-edit engine, CLI,
flake8 plugin, replayable benchmarks (`examples/polars_vet/benchmarks/`) and synthetic
fixtures (`examples/polars_vet/`, `tests/polars_vet/fixtures/`) are original work. No
upstream source code, issue comment text or data corpus is copied. All CSV rows and
benchmark frames are synthetic.

**Runtime:** no third-party packages; Python standard library `ast` and `tokenize` only.
Checked source is parsed, never imported or executed.

| Package | License | Use | Kind |
| --- | --- | --- | --- |
| [flake8](https://github.com/PyCQA/flake8) | MIT | Optional host for the `PLV` `flake8.extension` entry point (tested 7.3.0); no flake8 code incorporated | extra `flake8` |
| [Polars](https://github.com/pola-rs/polars) | MIT | Real before/after fix tests and benchmark replays (tested 1.35.2) | test |
| [pandas](https://github.com/pandas-dev/pandas) | BSD-3-Clause | PLV007 benchmark replay only; that test is skipped without it (tested 2.3.3) | test |
| [pytest](https://github.com/pytest-dev/pytest) | MIT | Test runner | test |

**Design sources (no copied text or code):** [Ruff issue #7721](https://github.com/astral-sh/ruff/issues/7721)
(user need; Ruff is MIT, no Ruff implementation incorporated); Polars documentation for
[read_csv](https://docs.pola.rs/api/python/stable/reference/api/polars.read_csv.html),
[scan_csv](https://docs.pola.rs/api/python/stable/reference/api/polars.scan_csv.html) and
[DataFrame.top_k](https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.top_k.html),
consulted 2026-11-11. SARIF 2.1.0 output follows the OASIS SARIF format; no schema text is
copied. Polars, Ruff and flake8 are projects of their respective owners.

## parquet-unify

Originally released under the MIT License. The implementation, the generator
`examples/parquet_unify/make_drift_dataset.py` and the synthetic binary fixtures
`tests/parquet_unify/fixtures/drift/*.parquet` (generated by that script with PyArrow
23.0.1) are original work. No upstream implementation source is copied or vendored.

| Component | Role | License |
| --- | --- | --- |
| [Apache Arrow / PyArrow](https://github.com/apache/arrow) | Runtime: Parquet footers, Arrow types, safe casts, row-group rewrite; imported at command start (extra `parquet`; supported >=21,<24, tested 23.0.1) | Apache-2.0 (wheel bundles further notices) |
| [DuckDB](https://github.com/duckdb/duckdb) | Runtime for `verify`: executes generated SQL and naive reads (extra `duckdb`; tested 1.5.6) | MIT |
| [Polars](https://github.com/pola-rs/polars) | Runtime for `verify`: executes generated LazyFrame recipes and naive reads (extra `polars`; tested 1.44.2) | MIT |
| [pytest](https://github.com/pytest-dev/pytest) | Test runner | MIT |

PyArrow, DuckDB and Polars binary wheels contain native components with their own notices;
the LICENSE/NOTICE files inside those installed distributions are authoritative. Problem
reports motivating the integration (context only, not claims about current status):
pola-rs/polars#23252, pola-rs/polars#27281, duckdb/duckdb#13245, duckdb/duckdb#13976.

Formats: Apache Parquet (rewrite writes format version 2.6, zstd). The checksum domains
`parquet-unify-row-v1` / `parquet-unify-ordered-row-v1` and JSON reports are defined by this
project. Apache Arrow and Apache Parquet are trademarks of the Apache Software Foundation.

## seedtypes

Originally released under the MIT License. The command's code and the synthetic fixtures in
`examples/seedtypes/` are original work. It is an independent integration, not an official
DuckDB, dbt, Polars or SQLGlot project; no upstream implementation source was copied.

| Component | Role | License | Notice |
| --- | --- | --- | --- |
| [DuckDB](https://github.com/duckdb/duckdb) | Runtime: VARCHAR CSV reader, TRY_CAST round trips, default-reader comparison (tested with 1.4.2) | MIT | [`licenses/seedtypes/duckdb.txt`](licenses/seedtypes/duckdb.txt) |
| [Polars](https://github.com/pola-rs/polars) / polars-runtime-32 | Runtime: default-reader comparison and generated reader recipe (tested with 1.35.2) | MIT | [`licenses/seedtypes/polars.txt`](licenses/seedtypes/polars.txt) |
| [SQLGlot](https://github.com/tobymao/sqlglot) | Runtime: data type AST rendering for six dbt adapters (tested with 28.0.0) | MIT | [`licenses/seedtypes/sqlglot.txt`](licenses/seedtypes/sqlglot.txt) |
| [PyYAML](https://github.com/yaml/pyyaml) | Runtime: safe static YAML input and properties output (tested with 6.0.3) | MIT | [`licenses/seedtypes/PyYAML.txt`](licenses/seedtypes/PyYAML.txt) |
| [fsspec](https://github.com/fsspec/filesystem_spec) | Runtime: required by DuckDB's Python client to read in-memory file-like CSV batches (tested with 2025.10.0) | BSD-3-Clause | [`licenses/seedtypes/fsspec.txt`](licenses/seedtypes/fsspec.txt) |
| [dbt-core](https://github.com/dbt-labs/dbt-core) | Format reference only (`column_types`, `delimiter` seed configs); not a dependency, no code copied | Apache-2.0 | — |

Notices are reproduced verbatim from the corresponding wheels' LICENSE files. No
third-party datasets are bundled. Formats referenced: dbt seed properties YAML (version 2),
RFC 4180 CSV. Reference docs: https://duckdb.org/docs/stable/data/csv/overview,
https://docs.pola.rs/api/python/stable/reference/api/polars.read_csv.html,
https://docs.getdbt.com/reference/resource-configs/column_types,
https://docs.snowflake.com/en/sql-reference/data-types-datetime,
https://cloud.google.com/bigquery/docs/reference/standard-sql/data-types. Snowflake,
BigQuery, Redshift, Databricks and PostgreSQL are trademarks of their respective owners.

## duckmate

Originally released under the MIT License. The command's code and the synthetic fixture in
`examples/duckmate/` are original work. dbmate's migration markers, numeric versions,
`schema_migrations` table shape and CLI vocabulary were independently reimplemented; no
dbmate code was copied.

| Component | Role | License | Notice |
| --- | --- | --- | --- |
| [DuckDB](https://github.com/duckdb/duckdb) | Runtime: SQL statement parser, execution and catalog APIs (tested with 1.4.2) | MIT | [`licenses/duckmate/duckdb.txt`](licenses/duckmate/duckdb.txt) |
| [dbmate](https://github.com/amacneil/dbmate) | Format/vocabulary reference only (`-- migrate:up` / `-- migrate:down`, `schema_migrations`); not installed or vendored | MIT | — |

duckmate does not import DuckDB's experimental Spark API or redistribute a modified DuckDB
binary. Motivating context: [dbmate PR #580](https://github.com/amacneil/dbmate/pull/580)
(historical; no code used). duckmate's own formats (schema snapshot `v1` header,
`_duckmate_checksums` table, JSON `schema_version` 1) are defined by this project.

## tapcheck

Originally released under Apache-2.0 (its NOTICE is merged into [NOTICE](NOTICE)). Code,
synthetic fixtures (`tests/tapcheck/fixtures/`) and examples (`examples/tapcheck/`) are
original work. No upstream application code is copied or vendored.

| Component | Role | License |
| --- | --- | --- |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | Runtime (`singer` extra; tested 4.26.0, needs >=4.18), plus `referencing`, `jsonschema-specifications`, `attrs`, `rpds-py` transitively; also the benchmark baseline in tests | MIT |
| [rfc3339-validator](https://github.com/naimetti/rfc3339-validator) | Runtime (`singer` extra; 0.1.4): provides jsonschema's `date-time` format checker (pulls `six`) | MIT |
| [fastjsonschema](https://github.com/horejsek/python-fastjsonschema) | Considered, not used or included | BSD-3-Clause |

Formats referenced (facts only, no prose or code copied): [Singer specification](https://github.com/singer-io/getting-started/blob/master/docs/SPEC.md)
(no repository license found; not assumed), [singer-tools](https://github.com/singer-io/singer-tools)
and [issue #19](https://github.com/singer-io/singer-tools/issues/19) (Apache-2.0; regression
fixture independently synthesized), [Meltano SDK BATCH/ACTIVATE_VERSION conventions](https://sdk.meltano.com/en/latest/batch.html)
(Apache-2.0; not a dependency), [Airbyte protocol](https://github.com/airbytehq/airbyte-protocol)
(MIT; no Airbyte/CDK code). The validation report (`report_version` 1), quality report
(`report_kind: "data_quality"`, v1), `tapcheck:ab:` stream-name encoding and
`__tapcheck_airbyte_state__` envelope are defined by this project
(`docs/tapcheck/*.schema.json`).

## singer-replay

Originally released under Apache-2.0 (its NOTICE is merged into [NOTICE](NOTICE)). Code,
the synthetic `tests/singer_replay/fixtures/shop.jsonl` / `fake_tap.py` and
`examples/singer_replay/` are original work. The `singer-replay tap` mode was formerly the
separate `tap-replay` script. No runtime dependencies beyond the Python standard library.

| Component | Role | License |
| --- | --- | --- |
| [target-jsonl](https://pypi.org/project/target-jsonl/0.1.4/) 0.1.4 | Optional loader for one integration test, installed separately by the user and never bundled; its archive has no LICENSE and declares `UNKNOWN`: **no license is assumed or granted**. Its environment pulls singer-python 5.8.0 (Apache-2.0), jsonschema 2.6.0 (MIT), backoff, ciso8601, simplejson, pytz, python-dateutil, adjust-precision-for-schema, tzdata | Unknown (see note) |
| [Meltano](https://github.com/meltano/meltano) | `examples/singer_replay/meltano.yml` follows its custom-plugin conventions; not imported or copied | MIT |
| [Meltano Singer SDK](https://github.com/meltano/sdk) | CLI/catalog-metadata conventions consulted; not imported or copied | Apache-2.0 |

Formats referenced: [Singer specification 0.3.0](https://github.com/singer-io/getting-started/blob/master/docs/SPEC.md)
(no repository license found; nothing redistributed); problem provenance
[meltano/sdk#30](https://github.com/meltano/sdk/issues/30). The cassette format
(`manifest.json` `format_version` 1, `messages.jsonl.gz`, `catalog.json`) and its
trim/rescrub provenance are defined by this project.
