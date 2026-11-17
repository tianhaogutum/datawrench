# failurepack: verified prior art and scope decision

Survey date: 2026-11-11. Public docs/source inspected before implementation.

## dfshrink 0.1.2 — closest existing tool, core already covered

- https://github.com/Tomperez98/dfshrink
- https://pypi.org/project/dfshrink/0.1.2/
- Read installed `shrink_rows`, `Repro`, and `ext/pandera.py`, plus upstream
  `docs/diagnose.md`, `docs/contract.md` and issue tracker (no open issues at survey).
- **Ran:** installed `dfshrink[pandera]==0.1.2`, shrank a real Polars frame with
  a real Pandera schema. The adapter reproduces *any* schema failure; with both
  duplicate IDs and a negative amount present, reduction can keep the negative
  amount and remove the duplicate-ID failure.
- Provides the core row delta debugging, evaluation budgets, order-preserving
  row selection, minimality status, validator adapters, and code/Markdown repros.
- **Remaining gap:** explicitly preserve a selected failure identity; consume a
  local CSV and rerun its real parser after each deletion; freeze the rules and
  parser settings in a replayable, integrity-checked directory; log decisions.
- **Decision:** extend dfshrink with a thin file/bundle adapter, using its public
  `shrink_rows` engine over record IDs. Do not implement another ddmin algorithm.

## Pandera 0.36.0

- https://pandera.readthedocs.io/en/stable/lazy_validation.html
- https://github.com/unionai-oss/pandera
- **Ran:** pandas 3.1.1 schema validation with `lazy=True`, inspected actual
  `SchemaErrors.schema_errors` and `failure_cases`, and generated YAML through
  `DataFrameSchema.to_yaml()`. Real uniqueness and numeric failures verified.
- Supplies validators, lazy error identities, and schema serialization. It does
  not itself shrink a batch or package a selected failure. Reuse these APIs.

## Picire 26.11

- https://github.com/renatahodovan/picire
- Read README and installed `DD` implementation.
- **Ran:** its serial `DD` library reduced 20 record IDs to the required pair
  `[2, 7]`. It also offers a CLI with line/character splitting and an external
  interestingness test. The algorithm is already solved here too.
- CSV physical lines can split quoted multiline records; a data-aware oracle,
  rule identity, and reproducibility bundle would still be required. dfshrink
  is closer to this task, so Picire is surveyed but not a runtime dependency.

## Great Expectations Core 1.24.1 (docs version)

- https://docs.greatexpectations.io/docs/core/run_validations/run_a_validation_definition/
- https://docs.greatexpectations.io/docs/core/trigger_actions_based_on_results/create_a_checkpoint_with_actions/
- **Docs/source only, not installed or run.** Validation Definitions execute
  against batches and return expectation results; Checkpoints run actions.
  This is validation/reporting infrastructure, not a selected-failure row
  reducer. A GX adapter remains future work; no GX compatibility is claimed.

## Shrink Ray (current main inspected; not run)

- https://github.com/DRMacIver/shrinkray
- Read upstream README. Generic multiformat reducer with interestingness tests,
  value/text mutations and parallel execution; latest README also describes a
  default local LLM download. No model was installed or run.
- **Not runtime-verified; no release version claimed.** This task only permits
  record deletion and has a small shared-memory budget. dfshrink is the narrower
  dependency and avoids implementing generic reduction passes.

## minex 0.2.0 — R analogue (follow-up survey)

- https://cran.r-project.org/web/packages/minex/refman/minex.html
- https://github.com/DIGlabUAB/minex
- **Docs/source only, not run.** CRAN's reference manual and published tests
  cover `reduce_rows` over R data frames, generic delta debugging, same-condition
  script reduction, budgets, and incomplete-result status. This confirms again
  that the reduction core is established, including outside Python.
- It does not provide this Python/Pandera CSV round-trip and selected-rule
  bundle adapter. No R runtime or optional LLM integration was installed.

## What is deliberately not new

Row delta debugging, validators, CSV parsing, hashing, and JSON handling are
existing components. The deliverable is the integration gap above, not a claim
that minimizing data-quality failures is a new idea. No upstream issue or PR
was submitted and no external data was sent.
