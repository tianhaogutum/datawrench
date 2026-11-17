# datawrench failurepack

A Pandera validation of a large CSV extract often fails in several ways at once, and you
need a small file that shows exactly one of those failures to a colleague, in a bug report
or in a regression test. `datawrench failurepack` deletes records from the CSV until only
the ones needed to trigger **one selected Pandera failure** remain, and packages them with
the unchanged schema, the parser settings and a standalone `replay.py` into a bundle that
anyone can replay. Use it when "the batch fails validation" is not specific enough and you
want a minimal, verifiable reproduction of one rule failure.

It is a file and reproducibility adapter for [dfshrink](https://github.com/Tomperez98/dfshrink),
which already implements row delta debugging; it does not add another reduction algorithm
or validator. The distinction matters when a batch has several failures: preserving *any*
failure can remove the very anomaly you need to report.

## Quick start

```sh
pipx install 'datawrench[pandera]'      # or: pip install 'datawrench[pandera]'
datawrench failurepack list examples/failurepack/orders.csv --schema examples/failurepack/orders.yaml
```

`list` prints every failure as JSON with an exact `id`. Choose the one whose `check` is
`field_uniqueness` and `column` is `order_id`:

```sh
datawrench failurepack reduce examples/failurepack/orders.csv \
  --schema examples/failurepack/orders.yaml --target 'pa:ID_FROM_LIST' --out output-orders
datawrench failurepack replay output-orders
```

If a budget stopped reduction, continue from that verified bundle with a fresh budget; the
original full CSV is no longer needed:

```sh
datawrench failurepack resume output-orders --out output-orders-continued --max-evals 2000
```

Continuation verifies all parent hashes and versions and replays its failure before
reducing the bundled data. The new manifest records the parent manifest hash and its
source/result metadata. Record ranges in the new log refer to the parent's bundled CSV, so
retain parent bundles when tracing several continuations back to the original extract. The
parent is never modified.

Or run the complete example with automatic exact-ID selection (from a clone of the
repository):

```sh
python examples/failurepack/demo.py
```

Each demo run creates a new `failurepack-demo-*/bundle` directory in the system temporary
folder and prints its path. `python examples/failurepack/make_fixtures.py` regenerates the
example CSV and YAML with the real pandas and Pandera writers.

The example has 256 records, including a quoted multiline note, a negative amount, and two
records sharing an order ID. The selected uniqueness failure reduces to the duplicate pair;
the unrelated negative amount is removed.

## Requirements

The `pandera` extra installs Pandera (with its pandas and IO extras) 0.36.x, pandas 3.1.x,
dfshrink 0.1.x (which brings Polars) and packaging. **dfshrink requires Python 3.12 or
later**, so `datawrench failurepack` needs Python 3.12+, while the rest of datawrench runs
on 3.11. macOS and Linux; everything runs locally and offline.

`datawrench failurepack --version` prints the bare datawrench version.
`python -m datawrench failurepack` is equivalent to `datawrench failurepack`. Without the
extra, the command exits 2 with a one-line install hint.

## Commands and options

| Command | Arguments | What it does |
| --- | --- | --- |
| `list DATA --schema SCHEMA` | `--delimiter`, `--read-options`, `--timeout`, `--max-bytes`, `--max-rows`, `--max-columns` | Validate the CSV once and print its failures (JSON, with ids) |
| `reduce DATA --schema SCHEMA --target ID --out DIR` | as `list`, plus `--max-evals`, `--max-seconds`, `--stability-runs` | Reduce the records while keeping failure `ID`; publish a bundle in the new directory `DIR` |
| `replay BUNDLE` | `--allow-version-drift` | Verify a bundle and replay it with the installed validators |
| `resume BUNDLE --out DIR` | `--max-evals`, `--max-seconds`, `--timeout`, `--max-bytes`, `--max-rows`, `--max-columns` | Continue reduction from a verified bundle |

Defaults: `--delimiter ,`, `--timeout 10` seconds per validator run (maximum 300),
`--max-bytes 33554432` (32 MiB), `--max-rows 250000`, `--max-columns 2048`,
`--max-evals 1000`, `--max-seconds 300`, `--stability-runs 2` (2–10). Errors are printed as
`datawrench failurepack: <message>` with exit status 2; an interrupted run (Ctrl-C or
SIGTERM) exits 130 and publishes nothing.

## What the bundle contains

`data.csv` contains only original records, in original order, with the original header and
exact cell/quote/newline bytes. `schema.yaml` (or `schema.py`) is an unchanged copy of the
fixed rules. `read-options.json` freezes parser settings. `reduction.jsonl` records each
oracle decision, repeated validation outcomes, source record ranges, timings, and unresolved
errors. `manifest.json` (`format_version` 1, `tool.name` `failurepack`) includes the
selected failure, hashes, source provenance, runtime versions, evaluation budget, and honest
1-minimality status. Record numbers are **one-based logical data records**, excluding the
header; they are not physical text line numbers.

`replay.py`, `_validator.py`, and `requirements.txt` work without datawrench or dfshrink
installed. In a fresh local environment:

```sh
python -m pip install -r output-orders/requirements.txt
python output-orders/replay.py
```

Replay verifies all bundle-member hashes, checks validator versions, and runs the copied
schema in fresh subprocesses. Exit status: **0** selected failure reproduced, **1** target
absent, **2** invalid input, version drift, timeout, changed package, or nondeterministic
result. `--allow-version-drift` lets you intentionally test another validator version;
integrity checks remain enabled. `datawrench failurepack replay BUNDLE` runs this
installation's copy of the replay script against the bundle, so a modified `replay.py` in
the bundle is never executed by it.

## Fixed validation and failure identity

Use a Pandera-generated YAML schema, or a self-contained Python file defining
`schema = pa.DataFrameSchema(...)` (a `DataFrameModel` class is also accepted). Python
schemas support aggregate and cross-row checks. Schemas are trusted executable code; this is
a subprocess timeout boundary, not a sandbox. Additional third-party imports in Python
schemas must be installed separately; only the Pandera/pandas runtime dependency graph is
automatically pinned.

An ID hashes the schema context, column, check description, check index, compound
uniqueness group when present, and Pandera reason code. Row indices and observed bad values
are deliberately not part of identity: deleting records changes their locations. Thus it
preserves the same **rule failure**, not necessarily the same offending value or root cause.
The full identity is always displayed beside its ID.

Each candidate is written as a real CSV and read again with pandas before the unchanged
schema validates it. This catches dtype/NA inference changes that an in-memory slice alone
can hide. Checks that raise exceptions and worker timeouts are unresolved, never accepted as
the selected quality failure. Target presence must agree across at least two runs for every
candidate; baseline and final CSV are independently checked. This detects observed
nondeterminism, but cannot prove an arbitrary validator deterministic. Repeated subsets
reuse a bounded cache of previously checked decisions; the log links each cache hit to its
original evidence. Final verification bypasses the cache and starts a fresh worker.

Use `--read-options parser.json` for a JSON object of supported pandas options, such as
`{"dtype":{"order_id":"string"},"keep_default_na":false}`. The command rejects options that
skip/reorder records or replace the fixed CSV dialect. Use `--delimiter ';'` for semicolon
CSV or a literal tab for TSV. Parser configuration is limited to 1 MiB; it is
configuration, not a second dataset. Actual pandas row counts must agree with the indexed
logical records.

## Budgets and limitations

- Deterministic single-table Pandera/pandas validation only. No Great Expectations, SQL,
  joins, Parquet, multi-file schemas, or automatic packaging of Python helper modules.
- Record deletion only. It does not edit cells, remove columns, anonymize, or guarantee a
  globally smallest result. A 1-minimal result means removing any single retained record
  loses the target (under the fixed oracle).
- `--max-evals` bounds dfshrink predicate calls (default 1000). Baseline, empty, and final
  evidence are additional. Budget exhaustion still produces a checked bundle, with
  `one_minimal: false`.
- `--max-seconds` sets a soft reduction time budget (default 300 seconds). The current
  candidate finishes before stopping; baseline and fresh final evidence are additional. The
  last verified subset is published with a `time_budget` stop reason and conservative
  minimality.
- `--timeout` bounds each serial validator run (default 10 seconds, maximum 300).
  `--stability-runs` accepts 2–10 attempts. A timed-out worker and its process group are
  killed. No validators run concurrently. Workers also recycle after 128 validations or
  observed peak RSS reaches 192 MiB. This limits retained state between requests; a single
  validation can exceed that soft threshold, so it is not a hard memory sandbox.
- UTF-8 CSV/TSV, a single header, unique named columns, fixed field counts, standard
  double-quote escaping. Blank records, NULs, malformed UTF-8, and malformed quoted records
  are rejected. CRLF, CR, BOM, and multiline cells retain their original bytes.
- Default input ceilings: 32 MiB, 250,000 records, and 2048 columns (`--max-bytes`,
  `--max-rows`, and `--max-columns` override). Indexing and copying stream the source;
  pandas still materializes each candidate table. Keep the ceiling appropriate for your
  machine. Output is a directory, not a ZIP file.
- Only a complete, finally verified bundle is published. Existing destinations are never
  overwritten. Interrupted/invalid runs remove their temporary work; reduction does not yet
  resume from an interrupted run.
- Hashes detect changes, not malicious authorship. A bundle includes selected original data
  and executable rules; review it before sharing or running.

`datawrench check` does not run failurepack: it needs a user-chosen failure and writes a
bundle, so it is not a read-only scan.

## More

- [failurepack/prior-art.md](failurepack/prior-art.md): verified versions of dfshrink,
  Pandera and the other reducers surveyed, and why failurepack builds on dfshrink.
- [failurepack/upstream-notes.md](failurepack/upstream-notes.md): observations about
  dfshrink and the Pandera adapter, recorded locally and not submitted upstream.
- [`examples/failurepack/PROVENANCE.md`](../examples/failurepack/PROVENANCE.md): how the
  example CSV and schemas were generated.
- Attribution and licenses: [THIRD_PARTY.md](../THIRD_PARTY.md#failurepack).
