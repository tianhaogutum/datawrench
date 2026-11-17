# Third-party sources and attribution

datawrench is licensed under the Apache License, Version 2.0 (see [LICENSE](LICENSE) and
[NOTICE](NOTICE)). Its commands were previously separate projects by the same author:
`contract-diff`, `sqlmesh-manifest`, `tapcheck` and `singer-replay` were Apache-2.0 (their
NOTICE files are merged into [NOTICE](NOTICE)); `lint-cache`, `dialect-audit`, `polars-vet`,
`parquet-unify`, `seedtypes` and `duckmate` were MIT (the original permission notice is kept
in [`licenses/MIT-original-tools.txt`](licenses/MIT-original-tools.txt)). `rebuild-check`,
`join-witness`, `grain-contract`, `key-audit`, `csv-rewind` and `failurepack` were also MIT,
each under its own copyright line ("... contributors"); those notices are kept unchanged in
`licenses/<command>/LICENSE`. MIT differs from datawrench's Apache-2.0: the code of those
commands remains available under its original MIT notice as well. Unless a section
below says otherwise, all code, synthetic fixtures, tests and documentation are original
work; they contain no production data, customer data or credentials.

datawrench is an independent interoperability project. It is not affiliated with or
endorsed by dbt Labs (dbt), Tobiko Data / the SQLMesh project, the SQLGlot maintainers,
the SQLFluff project, the DuckDB Foundation, the Polars project, the Apache Software
Foundation (Arrow, Parquet), the dbmate maintainers, Singer, Meltano, Airbyte, OASIS
(SARIF), the sqlite-utils, TableProof, Pandera, dfshrink, ftfy or ijson maintainers or any
other project named below. Product names only identify interoperable file
formats and tools. This file is attribution; it does not relicense any dependency or
upstream material.

## Dependencies (installed separately, never vendored)

The base install needs only PyYAML. Heavier dependencies are optional extras, installed by
your package manager under their own licenses; they keep their own notices in their
distributions. The datawrench wheel contains only the `datawrench` package (including the
JSON schemas `sqlmesh-manifest` validates against, see below, and the join-witness report
schema and demo CSVs) and these project notices.

| Package | License | Used by | Kind |
| --- | --- | --- | --- |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | `dialect-audit`, `seedtypes` | base |
| [SQLGlot](https://github.com/tobymao/sqlglot) | MIT | `contract-diff`, `dialect-audit`, `seedtypes`, `grain-contract`; `sqlmesh-manifest` via SQLMesh | extra `sqlglot` (`>=30.8,<31`) |
| [DuckDB](https://github.com/duckdb/duckdb) | MIT | `duckmate`, `dialect-audit`, `parquet-unify`, `seedtypes`, `csv-rewind`, `grain-contract check`; `join-witness` tests and generated SQL | extra `duckdb` (`>=1.4.3,<2`) |
| [fsspec](https://github.com/fsspec/filesystem_spec) | BSD-3-Clause | DuckDB file-like reads in `seedtypes` | extra `duckdb` |
| [Polars](https://github.com/pola-rs/polars) | MIT | `parquet-unify`, `seedtypes`, `failurepack` (with dfshrink); `polars-vet` and `join-witness` tests | extras `polars`, `pandera` (`>=1.35.2,<2`) |
| [PyArrow](https://github.com/apache/arrow) | Apache-2.0 (wheel bundles further notices) | `parquet-unify` | extra `parquet` (`>=21,<24`) |
| [SQLFluff](https://github.com/sqlfluff/sqlfluff) | MIT | `lint-cache` | extra `sqlfluff` (`>=4.5,<4.6`) |
| [Jinja2](https://github.com/pallets/jinja) | BSD-3-Clause | `lint-cache`; `grain-contract` tests | extra `sqlfluff` |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | MIT | `tapcheck`, `sqlmesh-manifest`; `dialect-audit` SARIF and `join-witness` schema tests | extras `singer`, `sqlmesh` |
| [rfc3339-validator](https://github.com/naimetti/rfc3339-validator) | MIT | `tapcheck` `date-time` checks | extra `singer` |
| [SQLMesh](https://github.com/SQLMesh/sqlmesh) | Apache-2.0 | `sqlmesh-manifest` | extra `sqlmesh` (`>=0.236.4,<0.237`) |
| [dbt-artifacts-parser](https://github.com/yu-iskw/dbt-artifacts-parser) | Apache-2.0 | `sqlmesh-manifest` | extra `sqlmesh` |
| [ruamel.yaml](https://pypi.org/project/ruamel.yaml/) | MIT | `sqlmesh-manifest` | extra `sqlmesh` |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | `sqlmesh-manifest` | extra `sqlmesh` |
| [flake8](https://github.com/PyCQA/flake8) | MIT | `polars-vet` as a flake8 plugin | extra `flake8` |
| [sqlite-utils](https://github.com/simonw/sqlite-utils) | Apache-2.0 | `rebuild-check` (CREATE TABLE parser; also hosts the plugin command) | extra `sqlite` (`>=4.2.1,<4.3`) |
| [sqlparse](https://github.com/andialbrecht/sqlparse) | BSD-3-Clause | `rebuild-check` definition comparison | extra `sqlite` (`>=0.5.3,<0.7`) |
| [TableProof](https://github.com/suguangliang3083-jpg/tableproof) | MIT | `join-witness` | extra `tableproof` (`>=0.1.1,<0.2`) |
| [ijson](https://github.com/ICRAR/ijson) | BSD-3-Clause AND ISC (bundled yajl) | `grain-contract` (`from-dbt` manifest streaming) | extra `ijson` (`>=3.4,<4`) |
| [ftfy](https://github.com/rspeer/python-ftfy) | Apache-2.0 | `key-audit` (`width` rule) | extra `ftfy` (`>=6.3.1,<7`) |
| [Pandera](https://github.com/unionai-oss/pandera) | MIT | `failurepack` (validator subprocess, YAML schemas) | extra `pandera` (`>=0.36,<0.37`) |
| [dfshrink](https://github.com/Tomperez98/dfshrink) | MIT | `failurepack` row reduction (`shrink_rows`) | extra `pandera` (`>=0.1.2,<0.2`; needs Python 3.12+) |
| [packaging](https://github.com/pypa/packaging) | Apache-2.0 OR BSD-2-Clause | `failurepack` replay dependency pinning | extra `pandera` |
| [pytest](https://github.com/pytest-dev/pytest) | MIT | test runner | test |
| [pandas](https://github.com/pandas-dev/pandas) | BSD-3-Clause | `failurepack` CSV parsing for Pandera; `polars-vet` PLV007 benchmark replay | extra `pandera` (`>=3.1,<3.2`); test |
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
| [`licenses/rebuild-check/`](licenses/rebuild-check) | Original MIT license of sqlite-utils-rebuild-check; Chinook sample database MIT license (test fixture) |
| [`licenses/join-witness/`](licenses/join-witness) | Original MIT notice of join-witness; TableProof and Northwind-SQLite3 MIT notices (TableProof runtime dependency and test fixtures) |
| [`licenses/grain-contract/`](licenses/grain-contract) | Original MIT license of grain-contract; ijson notice (BSD-3-Clause, with the bundled yajl ISC notice) |
| [`licenses/key-audit/`](licenses/key-audit) | Original MIT notice of key-audit; ftfy Apache-2.0 notice; Miller MIT and Unicode License v3 for test fixtures |
| [`licenses/csv-rewind/`](licenses/csv-rewind) | Original csv-rewind MIT notice; csvkit MIT notice for the copied real-data fixture |
| [`licenses/failurepack/`](licenses/failurepack) | Original failurepack MIT license |

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

## rebuild-check (formerly sqlite-rebuild-check / sqlite-utils-rebuild-check)

Originally released under the MIT License as the `sqlite-utils-rebuild-check` sqlite-utils plugin
with a `sqlite-rebuild-check` console script; its license is kept in
[`licenses/rebuild-check/LICENSE`](licenses/rebuild-check/LICENSE). The check, the policy format,
the argparse front end, tests and the example ledger and migrations in `examples/rebuild_check/` are
original work. SQLite supplies backup, statement completion, metadata and compilation; no SQLite,
sqlite-utils or sqlparse source is vendored or patched. `constraints.py` calls private helpers of
sqlite-utils' CREATE TABLE parser (lexer, item splitter, quote handling) to read foreign-key
timing; draft upstream notes are in [`docs/rebuild-check/upstream.md`](docs/rebuild-check/upstream.md).

| Component | Role | License |
| --- | --- | --- |
| [sqlite-utils](https://github.com/simonw/sqlite-utils) | Runtime (extra `sqlite`; originally pinned 4.2.1, datawrench installs `>=4.2.1,<4.3` because of the private helpers): `quote_identifier`, CREATE TABLE parser (CHECK / AUTOINCREMENT parsing and private FK-timing helpers), plugin hooks (`register_commands`, `prepare_connection`) | Apache-2.0 |
| [sqlparse](https://github.com/andialbrecht/sqlparse) | Runtime (extra `sqlite`; `>=0.5.3,<0.7`): SQL lexer for comment/whitespace/case-insensitive definition fingerprints and `UPDATE OF` trigger headers | BSD-3-Clause |
| [Click](https://github.com/pallets/click) | Runtime of the `sqlite-utils rebuild-check` plugin command only (installed with sqlite-utils) | BSD-3-Clause |
| [SQLite](https://www.sqlite.org/) via Python's `sqlite3` | Backup API, schema catalog, `foreign_key_check`, `integrity_check`, `EXPLAIN` compilation (tested 3.53.1; 3.37+ required) | Public domain |
| [Alembic](https://github.com/sqlalchemy/alembic) / [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) | Optional, test only (tested 1.20.1 / 2.1.8): generate real batch-mode rebuild SQL; the test is skipped without them | MIT / MIT |
| [pytest](https://github.com/pytest-dev/pytest) | Test runner | MIT |

Test fixtures (not shipped in the wheel):

- `tests/rebuild_check/fixtures/chinook.sql`: the unmodified SQLite dump of the
  [Chinook sample database](https://github.com/lerocha/chinook-database) (commit
  `7f67772503d71ba90f19283c38e93923addb43fa`, SHA-256
  `caf31d698a4a79c628215b552dfe6575e71be052ae02b8f18e763498f55f5d44`), Copyright (c) 2008-2024 Luis
  Rocha, MIT License; the license is kept next to it (`chinook.LICENSE.md`) and in
  [`licenses/rebuild-check/chinook-LICENSE.md`](licenses/rebuild-check/chinook-LICENSE.md).
- `tests/rebuild_check/fixtures/django-5.2-rebuild.sql`: SQL emitted by Django 5.2.19's SQLite schema
  editor (`alter_field`, `collect_sql=True`) for a synthetic model; Django (BSD-3-Clause) is not a
  dependency and no Django source is included.
- Provenance details: `tests/rebuild_check/fixtures/README.md`.

Design references (no code copied): SQLite documentation on table rebuilds, foreign keys, the backup
API and `sqldiff`; Atlas migration lint docs; sqlite-schema-diff, mdg-labs/sqlite-migrate and
migration-validator READMEs (surveyed 2026-11-11, not run); SQLGlot 30.31.0 was probed as a parser
alternative and rejected (not a dependency of this command). See
[`docs/rebuild-check/prior-art.md`](docs/rebuild-check/prior-art.md). The report format
(`schema_version` 1) and the policy file format (`{"allow": [...]}`) are defined by this project.

## join-witness

Originally released under the MIT License (the standalone `tableproof-join-witness`
distribution; its notice is kept in
[`licenses/join-witness/LICENSE`](licenses/join-witness/LICENSE)). The adapter code, the
JSON report schema `src/datawrench/join_witness/data/report-v1.schema.json`, the
synthetic demo exports (`examples/join_witness/{orders,customers}.csv`, byte-identical
packaged copies in `src/datawrench/join_witness/data/`, written with Polars 1.36.1
`DataFrame.write_csv`) and the tests are original work. It is a thin adapter on
TableProof: parsing, exact key counts, relationship policy, join-size predictions and
fingerprints stay TableProof's; no TableProof source is copied or patched.

| Component | Role | License | Notice |
| --- | --- | --- | --- |
| [TableProof](https://github.com/suguangliang3083-jpg/tableproof) | Runtime (extra `tableproof`; tested 0.1.1, datawrench installs `>=0.1.1,<0.2`): `audit_join`, `load_config`, `read_header` and the module helper `tableproof.audit.read_table`; TableProof v1 TOML contract format (`examples/join_witness/tableproof.toml`) | MIT | [`licenses/join-witness/tableproof-LICENSE`](licenses/join-witness/tableproof-LICENSE) |
| TableProof example data | Test fixtures `tests/join_witness/fixtures/tableproof/{samples,results,merged}.tsv`, unchanged copies of upstream `examples/data/` at commit `c4758ec3d9884338758555d98adba275209b5722` | MIT | `tests/join_witness/fixtures/tableproof/LICENSE` (same text as above) |
| [Northwind-SQLite3](https://github.com/jpwhite3/northwind-SQLite3) | Test fixtures `tests/join_witness/fixtures/northwind/{orders,customers}.csv`: Polars 1.36.1 exports of the public sample database at commit `4f56e7f5906dfd23b25244c5bfe8fb5da6402efd` (queries and SHA-256 in `tests/join_witness/fixtures/SOURCES.md`) | MIT | [`licenses/join-witness/northwind-SQLite3-LICENSE`](licenses/join-witness/northwind-SQLite3-LICENSE) |
| [DuckDB](https://github.com/duckdb/duckdb) | Execution target of the generated `--assertions-output` SQL and independent oracle in tests (tested 2.0.0 and 1.4.3); not imported by the command | MIT | — |
| [Polars](https://github.com/pola-rs/polars) | Tests: writes temporary CSV/TSV inputs and checks predicted counts and sums against real joins (tested 1.36.1) | MIT | — |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | Tests: validates reports against the packaged schema | MIT | — |
| [pytest](https://github.com/pytest-dev/pytest) | Test runner | MIT | — |

Design references (no code or text copied): TableProof 0.1.1 source and documented report
schema (gap: non-key provenance and measure attribution), [joinspy](https://github.com/gcol33/joinspy)
0.8.3 documentation (R; join-size predictions, ranked Cartesian key products, duplicate
rows), Polars `DataFrame.join(validate=...)` and pandas `merge(validate=...)`
documentation, the [Frictionless Table Schema](https://specs.frictionlessdata.io/table-schema/)
v1 specification and the DuckDB CSV and utility-function documentation; reviewed
2026-11-11. The `new_line` workaround in the generated SQL relates to
[duckdb/duckdb#25827](https://github.com/duckdb/duckdb/issues/25827) (shared cause not
established). The report format (`schema_version` "1.0", `tool.name` "join-witness") is
defined by this project.

## grain-contract (formerly sql-grain-contract)

Originally released under the MIT License as `sql-grain-contract` (console script
`grain-contract`); its license is kept in
[`licenses/grain-contract/LICENSE`](licenses/grain-contract/LICENSE). The compiler, dbt
artifact adapter, DuckDB runner, examples (`examples/grain_contract/`) and synthetic test
data are original work. It is a small SQLGlot extension: all SQL parsing, CTE pruning and
SQL generation are done by SQLGlot; no SQLGlot, dbt, dbt-utils or ijson source is copied.
The generated output format (`manifest.json` with `"generator": "sql-grain-contract"`,
bundle `version` 1) and the contract declaration format (`version` 1) are defined by this
project and unchanged by the merge.

| Component | Role | License |
| --- | --- | --- |
| [SQLGlot](https://github.com/tobymao/sqlglot) | Runtime (extra `sqlglot`; built and tested on 30.31.0; the AST classes, `from_`/`with_` argument names and `eliminate_ctes` / `normalize_identifiers` rules it uses are unchanged in 30.8.x): parser, AST, dialect generators | MIT; notice in [`licenses/contract-diff/SQLGLOT-MIT.txt`](licenses/contract-diff/SQLGLOT-MIT.txt) |
| [ijson](https://github.com/ICRAR/ijson) | Runtime (extra `ijson`; tested 3.6.0): event-level streaming of dbt `manifest.json`, selective node/source loading and duplicate-key checks; no JSON parser reimplemented | BSD-3-Clause AND ISC; notice in [`licenses/grain-contract/ijson-LICENSE.txt`](licenses/grain-contract/ijson-LICENSE.txt) |
| [DuckDB](https://github.com/duckdb/duckdb) | Optional runtime for `grain-contract check` (extra `duckdb`; tested 2.0.0 and 1.4.2): read-only execution of generated checks | MIT |
| [Jinja2](https://github.com/pallets/jinja) | Tests only (tested 3.1.6): renders the upstream Jaffle Shop model SQL | BSD-3-Clause |
| [dbt Core](https://github.com/dbt-labs/dbt-core) 1.12.6 + [dbt-duckdb](https://github.com/duckdb/dbt-duckdb) 1.11.1, and 1.10.15 + 1.10.0 | Not a dependency. Used once, outside this repository, to produce the compiled-manifest extracts in `tests/grain_contract/fixtures/dbt*/` (selected fields of real `dbt compile` output; see each `provenance.json`) and to validate the generated singular tests end to end; telemetry disabled. The integration script is not part of datawrench | Apache-2.0; no code copied |

**Redistributed test fixtures (not shipped in the wheel):**
`tests/grain_contract/fixtures/upstream/` holds unmodified files from
[dbt-labs/jaffle_shop](https://github.com/dbt-labs/jaffle_shop/tree/fd7bfacae4f497ff044a6a0275268676bf1b64c3)
at commit `fd7bfacae4f497ff044a6a0275268676bf1b64c3` (the raw customer/order/payment CSV
seeds, three staging models and the customers/orders models), retrieved 2026-11-11, with
source URLs and SHA-256 hashes in `provenance.json` and the upstream Apache-2.0 license in
`tests/grain_contract/fixtures/upstream/LICENSE`. The tests render the upstream SQL with
Jinja2 (`ref` resolves to local table names) and load the CSVs in DuckDB and SQLite; this is
not a dbt run.

**Design sources (prior art, verified 2026-11-11; no code copied):**
[SQLGlot 30.31.0](https://github.com/tobymao/sqlglot/tree/v30.31.0) (parser/generator
reused; no grain declarations or per-join runtime assertions);
[ijson 3.6.0](https://pypi.org/project/ijson/) (items/kvitems/event-interception API);
[dbt-utils 1.3.3](https://github.com/dbt-labs/dbt-utils/tree/1.3.3/macros/generic_tests)
`equal_rowcount`, `unique_combination_of_columns` and `cardinality_equality` (Apache-2.0;
the macros were rendered and executed for comparison in the standalone project; they are
not redistributed here); dbt [constraints](https://docs.getdbt.com/reference/resource-properties/constraints)
and [singular data tests](https://docs.getdbt.com/docs/build/data-tests) (zero-failing-rows
convention); [Polars 2.1.0 `DataFrame.join(validate=...)`](https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.join.html)
and its [row-preservation proposal](https://github.com/pola-rs/polars/issues/28093) (MIT);
[sqlproctor 0.1.0](https://github.com/debabsah/sqlproctor/tree/20525945159ac5d7c4af5df72d986257945bac55)
(static join-path and fan-out verifier; not recreated); [SQLMesh 0.236.4](https://github.com/SQLMesh/sqlmesh)
grain metadata and [audits](https://sqlmesh.readthedocs.io/en/stable/concepts/audits/)
(documentation only, not run). dbt, SQLMesh, DuckDB and Polars are projects of their
respective owners.

## key-audit (formerly unicode-key-audit / keyaudit)

Originally released under the MIT License as `unicode-key-audit` (console script `keyaudit`);
its permission notice is kept in [`licenses/key-audit/LICENSE`](licenses/key-audit/LICENSE).
The review/export code, the synthetic CRM-style example `examples/key_audit/customers.csv`
and the tests are original work. It is a thin wrapper around Python's `csv`, `unicodedata`,
`sqlite3` and `difflib` plus ftfy's isolated character-width fixer; it is not a new CSV
parser, normalization algorithm, fuzzy matcher or deduplicator. The review format
(`schema` `keyaudit.review/v1`) and the mapping CSV are defined by this project and are
unchanged from the standalone tool.

| Component | Role | License | Notice |
| --- | --- | --- | --- |
| [ftfy](https://github.com/rspeer/python-ftfy) | Runtime (extra `ftfy`; tested 6.3.1, pulls `wcwidth`): only `ftfy.fixes.fix_character_width`, behind the explicit `width` rule; its version is part of every review's engine identity. ftfy's broad default repair bundle is deliberately not used | Apache-2.0 | [`licenses/key-audit/ftfy-LICENSE.txt`](licenses/key-audit/ftfy-LICENSE.txt) |
| [wcwidth](https://github.com/jquast/wcwidth) | Transitive (via ftfy) | MIT | — |
| [Miller](https://github.com/johnkerl/miller) 6.22.0 | Test fixtures only: unchanged upstream CSV inputs and outputs freshly produced by the official `mlr --csv cat` binary (`tests/key_audit/fixtures/upstream/miller/`, URLs and SHA-256 in `tests/key_audit/fixtures/manifest.json`); recipes in [docs/key-audit/integrations.md](docs/key-audit/integrations.md) | MIT | `tests/key_audit/fixtures/upstream/miller/LICENSE.txt`, [`licenses/key-audit/miller-LICENSE.txt`](licenses/key-audit/miller-LICENSE.txt) |
| [csvkit](https://github.com/wireservice/csvkit) 2.2.0 | Test fixture only: `csvkit-output-semicolon.csv` produced by the real `csvformat -D ';'` from the synthetic example; not a dependency | MIT | — |
| [Unicode Character Database](https://www.unicode.org/Public/15.0.0/ucd/) | Test fixture only: 15 unchanged rows of `NormalizationTest-15.0.0.txt` (`tests/key_audit/fixtures/upstream/unicode/NormalizationTest-sample.txt`, with the official URL and full-file SHA-256 in its header) | Unicode License v3 | `tests/key_audit/fixtures/upstream/unicode/UNICODE-LICENSE.txt`, [`licenses/key-audit/UNICODE-LICENSE.txt`](licenses/key-audit/UNICODE-LICENSE.txt) |

Fixture provenance is in `tests/key_audit/fixtures/PROVENANCE.md`; none of these fixtures is
shipped in the wheel.

**Prior art surveyed (2026-11-11; no code or text copied):** Miller 6.22.0 (run: `strip` does not
remove U+3000 and `count-distinct` cannot tell an existing duplicate from a new collision);
OpenRefine 3.9/3.10 clustering and GREL `normalize()` documentation (not run; interactive
key-collision clustering already exists there); csvkit 2.2.0 `csvsql -I -y 0` (run; useful for
duplicate queries, no rule-confirmation workflow); ftfy 6.3.1 (run; reused only for
`fix_character_width`); qsv `apply`/`dedup` (documentation and source only, MIT/Unlicense, not run);
[Uneff](https://github.com/mkiiim/uneff) (source only, not run; its broad defaults are not copied);
two closely named web tools (pages only, not run, no license established). Python 3.12 `csv`,
`unicodedata`, `sqlite3` and `difflib` documentation was consulted. Miller, OpenRefine, csvkit,
qsv and Unicode are names of their respective owners.

## csv-rewind

Originally released under the MIT License (notice kept in
[`licenses/csv-rewind/LICENSE`](licenses/csv-rewind/LICENSE)). The audit, recipe format,
tests, the synthetic `survey.csv` sample and the fixture generators in `examples/csv_rewind/`
are original work. csv-rewind is a thin adapter around DuckDB's CSV sniffer, reader and casts;
it does not parse CSV or infer types itself, and no DuckDB or csvkit source is copied. The one
fallback parser is Python's standard `csv` module (record width only, for a narrow UTF-16/CR
sniffer failure).

| Component | Role | License | Notice |
| --- | --- | --- | --- |
| [DuckDB](https://github.com/duckdb/duckdb) | Runtime (extra `duckdb`): `sniff_csv`, `read_csv`, `TRY_CAST`, optimizer settings (tested 1.4.3, 1.5.6 and 2.0.0; datawrench installs `>=1.4.3,<2`) | MIT | [`licenses/duckmate/duckdb.txt`](licenses/duckmate/duckdb.txt) |
| [csvkit](https://github.com/wireservice/csvkit/tree/2.2.0) 2.2.0 | Test fixture `tests/csv_rewind/fixtures/csvkit-FY09_EDU_Recipients_by_State.csv` copied unmodified; `csvkit-bom-semicolon.csv` is `csvformat` output; not a dependency | MIT | [`licenses/csv-rewind/csvkit-COPYING`](licenses/csv-rewind/csvkit-COPYING) |
| [pytest](https://github.com/pytest-dev/pytest) | Test runner | MIT | — |

Fixture provenance (sources, dates, SHA-256, generators) is recorded in
`tests/csv_rewind/fixtures/PROVENANCE.md`. The `duckdb-*` fixtures are real DuckDB 2.0.0
`COPY` output and the `python-csv-utf16-cr-*` files real Python `csv.writer` output; none
contains real supplier data. Prior art consulted (documentation and, where noted, local runs;
no code copied): DuckDB CSV auto-detection docs, csvkit 2.2.0, qsv 28.0.0 (MIT/Unlicense),
Frictionless Framework 5.21.0 (MIT), csv_plus 1.13.0; see [docs/csv-rewind.md](docs/csv-rewind.md#prior-art).

Formats: RFC 4180-style CSV/TSV (optionally gzip or zstd compressed). The reading recipe
(`"format": "csv-rewind.duckdb"`, `"version": 1`), the report (`format_version` 1) and the
JSONL evidence events are defined by this project and unchanged from the standalone tool.

## failurepack

Originally released under the MIT License (its original notice, copyright "failurepack
contributors", is kept in [`licenses/failurepack/LICENSE`](licenses/failurepack/LICENSE)).
The command's code, the replay script template, the synthetic example
(`examples/failurepack/orders.csv`, `orders.yaml`, generated by `make_fixtures.py` with the
real pandas `DataFrame.to_csv` and Pandera `DataFrameSchema.to_yaml` writers) and tests are
original work. `examples/failurepack/orders-pandera-0.26.1.yaml` was written by the actual
Pandera 0.26.1 `to_yaml` writer (MIT); provenance is in
`examples/failurepack/PROVENANCE.md`. It is an integration layer: row reduction is
delegated to dfshrink's public `shrink_rows` engine and validation to Pandera; no dfshrink,
Pandera, Picire or other reducer source was copied or vendored, and no data is sent anywhere.

| Component | Role | License |
| --- | --- | --- |
| [dfshrink](https://github.com/Tomperez98/dfshrink) | Runtime (extra `pandera`; tested 0.1.2, requires Python 3.12+): row delta debugging, evaluation budgets, minimality status. failurepack is a file/bundle adapter around it | MIT |
| [Pandera](https://github.com/unionai-oss/pandera) | Runtime (extra `pandera`; tested 0.36.0 with `pandas,io` extras): lazy validation, failure identities, YAML schema IO | MIT |
| [pandas](https://github.com/pandas-dev/pandas) | Runtime (extra `pandera`; tested 3.1.1): re-reads every candidate CSV before validation | BSD-3-Clause |
| [Polars](https://github.com/pola-rs/polars) | Runtime (via dfshrink, `>=1.44.2`): record-id frames passed to `shrink_rows` | MIT |
| [packaging](https://github.com/pypa/packaging) | Runtime: pins the installed Pandera/pandas dependency graph into the bundle's `requirements.txt` | Apache-2.0 OR BSD-2-Clause |

Prior art surveyed (docs/source, no code copied; details in
[`docs/failurepack/prior-art.md`](docs/failurepack/prior-art.md)): dfshrink 0.1.2 (the
closest existing tool; its core is reused, not reimplemented), Pandera 0.36.0 lazy
validation, [Picire](https://github.com/renatahodovan/picire) 26.11 (surveyed,
not a dependency), Great Expectations Core 1.24.1 documentation (not used),
[Shrink Ray](https://github.com/DRMacIver/shrinkray) (README only; not used) and the R package
[minex](https://github.com/DIGlabUAB/minex) 0.2.0 (docs only). Observations about dfshrink
and the Pandera adapter are recorded in
[`docs/failurepack/upstream-notes.md`](docs/failurepack/upstream-notes.md); nothing was
submitted upstream.

Formats: the bundle (`manifest.json` `format_version` 1 with `tool.name` `failurepack`,
`reduction.jsonl`, `read-options.json`, `replay.py`, `_validator.py`, `requirements.txt`) and
the `pa:<sha256>` failure identity are defined by this project. Pandera is a project of
Union.ai and its contributors; dfshrink, Picire, Shrink Ray and minex belong to their
respective authors.
