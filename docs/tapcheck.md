# datawrench tapcheck

`datawrench tapcheck` (formerly `tapcheck`) runs offline contract checks for recorded **Singer and Airbyte JSONL**. Catch missing primary keys (including the empty record from [singer-tools #19](https://github.com/singer-io/singer-tools/issues/19)), validate records against schemas, inspect per-stream statistics, and convert the common protocol subset without running a connector.

Python 3.11+, Linux. No network, Docker, credentials or paid services at runtime.

## Install

This command needs the `singer` extra (jsonschema and rfc3339-validator; the latter supplies jsonschema's `date-time` format checker, which tapcheck always uses for extraction timestamps and date-time cursors):

```sh
pipx install 'datawrench[singer]'      # or: pip install 'datawrench[singer]'
datawrench tapcheck --version          # prints the bare datawrench version, e.g. 0.1.0
datawrench tapcheck --help
```

`python -m datawrench tapcheck` is equivalent to `datawrench tapcheck`. The subcommands are `validate`, `quality` and `convert`. The Python API mentioned below (`Validator(...)`) is `datawrench.tapcheck.engine.Validator`; conversion is `datawrench.tapcheck.convert.convert`. Installation may fetch packages; tests and all tool operations run offline. Nothing is published or deployed. Commands below are run from the datawrench checkout root, where the example captures live in [`examples/tapcheck/`](../examples/tapcheck/).

## Validate

```sh
datawrench tapcheck validate examples/tapcheck/good_singer.jsonl --strict --check-order --check-duplicates
datawrench tapcheck validate examples/tapcheck/good_airbyte.jsonl --strict --format json
cat examples/tapcheck/good_singer.jsonl | datawrench tapcheck validate - --protocol singer
datawrench tapcheck validate recording.jsonl --protocol airbyte --catalog configured_catalog.json --format junit > report.xml
datawrench tapcheck validate tests/tapcheck/fixtures/singer_tools_issue_19.jsonl
```

The last command reports `line 2 TC010 stream 'stream': key property missing or null path ['id']` and exits **1**. `key_properties` is enforced even if the JSON Schema has no `required` list. Both missing **and null** primary keys fail.

```text
datawrench tapcheck validate [FILE|-]
  [--protocol auto|singer|airbyte] [--catalog FILE]
  [--strict] [--check-order] [--check-duplicates]
  [--max-errors N] [--format text|json|junit]
  [--duplicate-limit N] [--max-line-bytes N] [--max-input-bytes N] [--max-batch-bytes N] [--max-batch-raw-bytes N] [--max-batch-files N] [--max-records N] [--max-messages N] [--max-declarations N] [--batch-root DIR]
```

Exit codes: **0** valid; **1** contract violations; **2** unusable/empty input, ambiguous/mixed protocol in auto mode, I/O failure, exhausted tracking capacity or a main-input/BATCH byte, manifest-occurrence, record-evaluation or main-message processing budget. Oversized individual lines are recoverable violations; processing continues at the next physical line. `--max-errors` (default 100) stops at that many diagnostics and sets `stopped_early`; it does not report the rest of the file as checked. Blank lines are ignored. Nonblank lines must be individual JSON objects. Invalid UTF-8, duplicate object members, NaN/Infinity, overflowed JSON floats and truncated JSON fail with physical line numbers. A last line without a newline is accepted if its JSON is complete.

`--max-input-bytes N` optionally bounds **raw main-input bytes** for `validate` and `quality`, from files or stdin. N accepts **0 through 9223372036854775807**; omission is unlimited. Invalid options exit 2 during argument parsing. Count blank/whitespace bytes, CR/LF, encoded UTF-8, inline catalogs and a final line without LF. External catalogs and local JSONL/gzip BATCH files are excluded; their existing independent limits still apply. Use `--max-batch-bytes` separately for decompressed local BATCH payloads. Neither budget bounds CPU/time.

Overflow returns **TC020 / exit 2**, `stopped_early: true`, with counts/statistics/examples only from checked messages before the crossing line. The crossing line is not decoded or counted. Final STATE checks are skipped because the unread tail could cover the records; no missing-STATE verdict is inferred from a budget stop. Quality adds `coverage: "partial_input"`, `remaining_input_after_stop` and `final_state_coverage` in `not_evaluated`; TC020 is counted even with `--example-limit 0`. Text reports stopped status; JUnit uses an operational `<error type="TC020">` and properties `stopped_early`, `input_lines`, and `records` on stopped reports. JSON report schemas/versions are unchanged.

At most **N+1 newly read raw bytes** are consumed, including through buffering and oversized-line draining; the remaining capture is not drained. Reaching N requires actual EOF: one additional blank byte fails. Exact fits retain ordinary JSON/line/final-STATE checks. N=0 with empty input retains TC021; any nonempty input overflows with TC020. Earlier diagnostic/capacity stops can take precedence. `input_lines` retains its existing meaning: physical line number of the last checked nonblank message or parse diagnostic, including preceding blank lines; trailing blanks and the crossing line do not increment it. The budget diagnostic identifies the physical line containing the first excess byte.

For example, the following Singer STATE occupies **38 raw bytes** with LF:

```sh
printf '%s\n' '{"type":"STATE","value":{"cursor":1}}' | datawrench tapcheck validate - --max-input-bytes 38 --format json
printf '%s\n' '{"type":"STATE","value":{"cursor":1}}' | datawrench tapcheck quality - --max-input-bytes 37
```

The first exits **0**, checking one STATE. The second exits **2** with TC020, partial coverage and no counted messages (the first line crosses the budget). Adding a blank LF to the first needs N=39. Python API: `Validator(max_input_bytes=N)` or `Validator(quality=True, max_input_bytes=N)` uses the same bounds; invalid types including booleans raise `ValueError`. The reader leaves the caller's stream open and consumes any previously buffered bytes in order; it cannot undo reads made before validation began.

External catalog **registration** diagnostics use the catalog source, line **0**, and zero-based entry path `["streams", index]` once an entry is selected. This applies to invalid entries, names, key/cursor paths and schemas, changed contracts, and stream/property/declaration capacity stops, for Singer and Airbyte discovery/configured catalogs. Invalid root/streams containers and failed inference without an entry have `path: null`. JSON diagnostics, JUnit details and bounded quality examples share these locations; text retains its existing line/code prefix and shows the entry path. For example, an invalid schema in the second entry of `--catalog discovery.json` reports `source: "discovery.json", line: 0, path: ["streams", 1]`, even when main input is stdin. Quality example limits can omit the location while error counts remain exact. Registration restores the main location before reading input, so main/inline catalog and local BATCH physical locations are preserved. External malformed entries/schemas still abort operationally (exit 2); changed contracts retain the original contract and ordinary exit 1 behavior. CLI catalog I/O/JSON decoding happens before registration and retains its existing diagnostics.

Auto mode uses message envelopes, not schema contents, to select a protocol; an external catalog can select it before records arrive. A first ambiguous or unidentifiable message needs an explicit `--protocol`. A stream that switches protocols fails with guidance. Explicit selection still validates the selected protocol's envelope. Singer message types are case-insensitive; Airbyte types are uppercase.

### Contracts

* Singer `SCHEMA`, `RECORD`, `STATE`, local `BATCH`, `ACTIVATE_VERSION` are recognized. Records need a preceding schema or a matching external catalog. A repeated identical schema is accepted; any changed schema/key/cursor contract fails and the original stays active. `ACTIVATE_VERSION` requires a declared stream and integer version; tapcheck does not apply deletes or reset duplicate/order checks.
* JSON Schema drafts 4, 6, 7, 2019-09 and 2020-12 are supported (draft 7 by default). Validators are checked and cached once per stream. All `required`, type, range, composition, nested-object and other constraints come from jsonschema. Only local fragment `$ref` is supported; no filesystem/network retrieval. Dynamic/recursive-reference keywords and unknown dialects fail explicitly. Unresolvable or recursive schemas fail TC004 when evaluated.
* `--strict` additionally requires every **top-level** declared property whose schema rejects null; rejects undeclared top-level properties (allowing `patternProperties` matches); and enforces `date-time` formats at every schema depth. These extra presence/closure checks inspect root `properties` and `patternProperties`. For nested or `$ref`/composition-defined object presence/closure, use JSON Schema `required` and `additionalProperties: false`. Other format annotations remain annotations. Extraction timestamps are always checked when supplied.
* `--check-order` checks a single Singer `replication_key` / `bookmark_properties` entry, or catalog root metadata `replication-key`. Airbyte uses configured `cursor_field` or `default_cursor_field`, including nested paths. Missing/null/non-scalar cursors and changing cursor types fail. Equal values are allowed. Numeric cursors compare numerically; strings compare lexically, except directly declared `format: date-time` cursors compare normalized UTC instants. Date-time cursor fractions are supported up to 1,000 digits; unsupported values fail explicitly when checking order. Ordering is global per stream, without partitions or version resets.
* `--check-duplicates` checks composite primary keys across the whole capture, including BATCH files; Airbyte keys can be nested. Keys are SHA-256 digests of canonical JSON (object order ignored, 1 equals 1.0; booleans remain distinct). This is bounded, exact-digest tracking with a negligible hash-collision risk, not a sliding window. After 100,000 distinct keys by default, TC018 exits 2; increase `--duplicate-limit` to check more. It never silently evicts keys or claims full duplicate coverage after overflow.
* Airbyte accepts discovery `AirbyteCatalog` and configured `ConfiguredAirbyteCatalog`, externally or inside a `CATALOG` envelope. Stream identity is **(name, namespace)**, so equal names in different namespaces remain separate. Primary keys use configured `primary_key`, falling back to `source_defined_primary_key`. TRACE/LOG/CONTROL object payloads are counted without interpreting their internals. Other types fail TC002.
* Singer STATE must have an object `value`. Airbyte LEGACY (including omitted type), STREAM and GLOBAL states validate their matching payload and descriptor/blob shapes. Legacy and stream blobs can be null (reset). A stream with records needs a later valid STATE covering it: Singer/LEGACY covers all streams, STREAM covers its descriptor, GLOBAL covers descriptors in `stream_states`. A LOG after a covering STATE is fine. A STATE before the last record does not cover that record. No checkpoint-content or cursor-state correspondence is inferred.

### Local BATCH

The SDK-style envelope is supported, with **raw records**, not RECORD messages, in each batch file:

```json
{"type":"BATCH","stream":"users","encoding":{"format":"jsonl","compression":"gzip"},"manifest":["file://batch.jsonl.gz"]}
```

```sh
datawrench tapcheck validate tests/tapcheck/fixtures/batch_gzip.jsonl --strict --check-order
datawrench tapcheck validate capture.jsonl --batch-root ./allowlisted-batches
```

Plain JSONL supports omitted/null/`none` compression. `gzip` is decompressed incrementally. Paths resolve under `--batch-root` (default: input file's directory; cwd for stdin); relative paths and `file://relative/path` follow that root. Absolute file paths must also stay beneath it. Traversal, escaping symlinks, non-regular files, missing files, remote URLs and unsupported encodings fail TC017. Directory-FD opens with `O_NOFOLLOW` prevent symlink substitution during opening. Nothing in a manifest is executed or written. Batch diagnostics identify the local batch file and its physical record line. Parquet/Arrow, remote storage and credentials are out of scope.

Corrupt gzip headers, truncated streams, checksum errors and invalid DEFLATE blocks produce TC017 at the BATCH envelope's line. Validation continues with subsequent batches/messages unless `--max-errors` stops it; structured JSON/JUnit reports remain available.

`--max-batch-bytes N` optionally limits **cumulative decompressed local-BATCH payload bytes** for `validate` and `quality`. The Python API is `Validator(max_batch_bytes=N)` (optionally `quality=True`). N must be an integer **0 through 9223372036854775807**; omission/`None` is unlimited, and API booleans/non-integers raise `ValueError`. Invalid CLI values exit 2 before opening input. Plain and gzip files share one allowance across all streams, BATCH envelopes and manifest occurrences: repeated files count again. Count every delivered payload byte, including blanks/whitespace, CR/LF, UTF-8, malformed/oversized rows and a final line without LF. Main-capture bytes, inline records and external catalogs are excluded; their independent limits remain.

Overflow is **TC020 / exit 2**, with `stopped_early: true` and quality `coverage: "partial_input"`. Diagnostics identify the BATCH filename relative to its root and the **physical decompressed row** containing the first excess byte. Only complete rows before the crossing row contribute records, key/duplicate counts and examples. The consumed BATCH envelope remains in `messages` and `input_lines`; unread main messages do not. No final STATE verdict is made on the unread tail. Zero permits empty plain/gzip payloads but rejects even one blank byte. Exact fits require actual EOF and still check JSON, gzip integrity and later main STATE coverage. Earlier validation/capacity/read failures may take precedence.

The reader delivers at most **N+1 decompressed payload bytes cumulatively**, including oversized-row drains, then stops without draining the remaining row or opening later manifests. Payload bytes already delivered before a recoverable read error remain charged. This is **not a cap on compressed raw reads, gzip metadata, internal decompression/buffering, memory, CPU or time**: Python gzip may read ahead in compressed data and inspect headers/trailers or concatenated empty members even with a zero payload budget.

`--max-batch-raw-bytes N` separately limits **cumulative raw local-BATCH reads**, including gzip headers, trailers and concatenated empty members. The API is `Validator(max_batch_raw_bytes=N)`; the same integer range, unlimited default and invalid-value rules apply. All plain/gzip files, streams, envelopes and repeated manifest occurrences share one allowance. Main input and external catalogs are excluded. Combine it with `--max-batch-bytes` to bound payload delivery independently.

The raw reader sits beneath gzip and new buffering, reads at most **N+1 raw bytes cumulatively**, and never gives the excess probe byte to the decoder. An exact fit still probes real EOF. Zero accepts an empty physical file, but rejects even an empty gzip member because its metadata occupies raw bytes. Bytes read before a recoverable TC017 remain charged. Overflow is operational **TC020 / exit 2**, stops without draining tails or opening later manifests, and preserves only evaluated rows/counters with no unread-tail final STATE verdict. Its source is the relative BATCH filename and its line is the **physical decompressed row whose read was interrupted**. Gzip read-ahead can trigger the raw limit before any row is decoded; this location is not a compressed-byte offset or a claim that the excess byte belongs to that payload row. Corruption encountered before overflow remains recoverable TC017 at the main envelope; the first encountered failure wins.

Raw and payload budgets do not impose a hard memory, expansion, CPU or time limit: gzip may buffer/decompress already allowed raw bytes. Local BATCH handles are opened fresh; bytes prebuffered by an external caller cannot be unread. Omitting either allowance leaves that resource unlimited.

`--max-batch-files N` / `Validator(max_batch_files=N)` optionally limits **cumulative manifest occurrences** independently from both byte allowances. The same integer range and invalid-value rules apply; omission/`None` is unlimited. After the existing stream, encoding and manifest-shape checks, every considered path entry counts once, including missing/invalid paths and repeated files, across all streams and envelopes. Rejected envelopes consume no occurrences; manifest shape is still checked across the full already-parsed array.

Attempting occurrence N+1 stops with **TC020 / exit 2 before URI parsing, path resolution, stat or open**. The diagnostic identifies the main BATCH envelope source/physical line and `path: ["manifest", index]`, where index is zero-based within that envelope. Validation text/JSON/JUnit also name the one-based cumulative occurrence without copying the untrusted path. Quality keeps its existing code/source/line/path examples, bounded by `--example-limit`. Rows, key/duplicate counts and examples describe only the evaluated prefix; `stopped_early`/quality `partial_input` exclude unread-tail STATE conclusions. No later manifest or main-tail read is requested after the stop (ordinary main-reader buffering may already contain bytes). Earlier errors/capacity stops retain priority. At the exact count, actual file/main EOF, JSON/gzip integrity and final STATE checks still apply.

Zero permits no occurrences, even empty files or invalid paths, while ordinary captures without BATCH are unaffected. For 32 repeated empty files, both byte budgets can be zero and still accept all 32 by default; `--max-batch-files 2` opens only two and stops at manifest index 2. Count and byte allowances do **not** impose hard CPU, time or memory limits: parsing each admitted main line, checking the full envelope shape, schema evaluation and gzip internal work can still be expensive.

`--max-records N` / `Validator(max_records=N)` optionally bounds **cumulative accepted object record evaluations** for `validate` and `quality`. N is an integer **0 through 9223372036854775807**; omission/`None` is unlimited. Invalid CLI values exit 2 before input is opened; API booleans/nonintegers/out-of-range values raise `ValueError`. All streams/namespaces, inline rows and local plain/gzip BATCH rows share one allowance; repeated rows/files count again. An object for a declared stream counts even if it violates its schema, has missing/null keys, or schema evaluation fails. Blanks, declarations, STATE and ancillary messages do not count. Existing malformed JSON/envelope, undeclared-stream and nonobject-data rejection runs first and consumes no record allowance.

Attempting accepted object record N+1 stops with **TC020 / exit 2 before that record changes record counts or dirty state, evaluates schema/keys/cursors/duplicates/timestamps, updates statistics or retains record/key examples**. The operational TC020 can retain its usual bounded diagnostic example. The source/physical line identifies the main RECORD or relative BATCH file/row, including preceding blanks. Main `input_lines`/`messages` include the parsed crossing RECORD or BATCH envelope; they measure admitted main events, not evaluated records. The independent message allowance can stop an event before it is counted. Reports preserve the exact evaluated prefix, `stopped_early` and quality `partial_input`, with no unread-tail final STATE verdict. No later rows/files or main tail are requested after stopping; readers/gzip may already have buffered bytes.

Zero permits no declared-stream object records, but declarations/STATE and empty/blank BATCH files still work; empty main input remains TC021. At the exact allowance, scanning continues through actual EOF and ordinary JSON/gzip/final STATE checks; reaching N alone does not stop. Earlier max-errors, capacity, byte and manifest stops retain priority. All allowances are independent. This is a **record-evaluation allowance, not a decode, CPU/time or memory sandbox**: the crossing row is decoded and checked for admission, and each admitted schema/regex/gzip operation can still be expensive.

For a BATCH file containing 32 repeated `{"id":1}` LF rows, exact raw/payload allowances of 288 bytes and `--max-batch-files 1` permit all 32 evaluations by default. Adding `--max-records 2` evaluates two, then stops at physical row 3 with TC020/exit 2. The checked-in Singer example has two records:

```sh
datawrench tapcheck validate examples/tapcheck/good_singer.jsonl --max-records 2 --format json
datawrench tapcheck quality examples/tapcheck/good_singer.jsonl --max-records 1
```

The first exits **0**. The second exits **2**, reports one evaluated record and partial coverage, and skips the remaining STATE verdict.

`--max-messages N` / `Validator(max_messages=N)` optionally bounds **cumulative main-input processing events** for `validate` and `quality`, independently of record, byte and manifest allowances. N is an integer **0 through 9223372036854775807**; omission/`None` is unlimited. Invalid CLI values exit 2 before main/catalog input opens; API booleans/nonintegers/out-of-range values raise `ValueError`.

Each event yielded by the main JSONL reader counts once: SCHEMA, inline CATALOG (regardless of entries), RECORD, STATE, BATCH, ancillary/unknown/malformed envelopes, decoded nonobjects, and JSON/UTF-8 or oversized-line diagnostics. Ordinary blank lines do not count; oversized whitespace lines still produce the existing line-limit diagnostic and count once. External catalog loading is excluded and happens first. BATCH payload rows and their diagnostics are excluded; an admitted BATCH remains subject to the independent record/manifest/byte limits.

Attempting event N+1 stops with **TC020 / exit 2 before dispatch, protocol identification, stream registration, STATE coverage, record evaluation or reporting that event's own diagnostic**. The diagnostic identifies the main source and physical crossing line, including preceding blanks. `input_lines` remains the physical line of the last admitted event; `messages` keeps its existing typed-message counts, which can be fewer than events because parse diagnostics/nonobjects have no type. The crossing event changes neither counter, record/stream state nor record/key samples; only the operational TC020 may add its usual bounded error sample. Reports preserve the processed prefix, `stopped_early` and quality `partial_input`, with no unread-tail final STATE verdict. No further main read or later BATCH open is requested after stopping. Report schemas/versions are unchanged.

This is a **processing allowance, not a decode, CPU/time or memory sandbox**. The crossing event is already read and decoded (or turned into a parse diagnostic) before the gate; oversized-line draining also precedes it. Readers may have buffered later bytes. Main-byte read failures take precedence when producing that event; earlier admitted errors, max-errors, record/manifest/byte/capacity stops keep their priority. At exact N, actual EOF, JSON/gzip integrity and final STATE checks still apply. Trailing ordinary blanks are allowed. Empty/blank-only input at zero retains TC021; any reader event at zero fails the allowance.

For 32 minimal Singer STATE lines, exact main bytes (896), `--max-records 0`, `--max-batch-files 0` and both BATCH byte budgets zero still permit all 32 dispatches by default. `--max-messages 2` processes two, then stops at line 3. The checked-in Singer example has four main events:

```bash
datawrench tapcheck validate examples/tapcheck/good_singer.jsonl --max-messages 4 --format json
datawrench tapcheck quality examples/tapcheck/good_singer.jsonl --max-messages 2
```

The first exits **0** with the ordinary complete report. The second exits **2**, reports SCHEMA=1/RECORD=1 and `input_lines: 2`, and identifies crossing line 3 without processing that record or the final STATE.

`--max-declarations N` / `Validator(max_declarations=N)` optionally bounds **cumulative schema registration attempts** for `validate` and `quality`. N is an integer **0 through 9223372036854775807**; omission/`None` is unlimited. Invalid CLI values exit 2 before input/catalog opens; API booleans/nonintegers/out-of-range values raise `ValueError`. Singer inline SCHEMA and external catalog entries, and Airbyte inline/external CATALOG entries, share one allowance across all streams/namespaces. Identical repeats, changed contracts and schema-invalid attempts count, even when they do not add a stream or accepted schema. Singer inline CATALOG remains unsupported.

Existing envelope, key/cursor shape, name/namespace and unique-stream capacity checks run first; rejected input consumes no declaration allowance. Attempt N+1 stops operationally with **TC020 / exit 2 before schema compilation, contract-signature serialization, accepted schema counters or stream registration changes**. No later catalog entry, main read or BATCH open is requested. The admitted main message already counts in `messages`/`input_lines`. Stream/record/key statistics retain their processed prefix; only the usual bounded TC020 diagnostic sample may be added. Quality reports `partial_input`, and the unread tail receives no final STATE verdict. Report schemas/versions are unchanged.

Inline stops identify the physical main source/line; catalog stops additionally carry zero-based path `["streams", index]`. External registration diagnostics identify the catalog file at line **0**, since the catalog was decoded as a whole and no physical entry line is available. For predecoded API catalogs, `run(..., catalog=..., catalog_source="discovery.json")` supplies that label; default is `<catalog>`. At/below N, existing exits, actual EOF, parse/gzip integrity and final STATE checks remain. Zero allows no eligible attempts; empty catalogs consume none, while existing empty-input and protocol-inference rules remain.

This is a **registration-processing allowance, not a catalog-decoding, CPU/time or memory sandbox**. Catalog JSON and the crossing entry's envelope/name/path checks precede the gate; readers may prebuffer, and admitted schema work can still be expensive. Earlier message/byte/record/manifest/max-errors/capacity stops retain priority; all allowances are independent. One Airbyte CATALOG with 32 identical entries succeeds by default with `--max-messages 1`; `--max-declarations 2` compiles two and stops at entry index 2. The checked-in Singer example has one declaration:

```sh
datawrench tapcheck validate examples/tapcheck/good_singer.jsonl --max-declarations 1 --format json
datawrench tapcheck quality examples/tapcheck/good_singer.jsonl --max-declarations 0
```

The first exits **0**. The second exits **2** at main line 1 with no registered streams or evaluated records.

For two raw rows `{"id":1}` each followed by LF, the total BATCH payload is **18 bytes**. A budget of 18 admits both and continues to main STATE; 17 stops at BATCH row 2 with one checked row. For the same payload in gzip, the budgets count the same 18 bytes regardless of compressed file size. Combine budgets as needed:

```sh
datawrench tapcheck validate tests/tapcheck/fixtures/batch_gzip.jsonl --max-batch-files 100 --max-batch-bytes 1048576 --max-batch-raw-bytes 1048576 --max-input-bytes 1048576 --format json
datawrench tapcheck quality tests/tapcheck/fixtures/batch_gzip.jsonl --max-batch-raw-bytes 0
```

The first example exits 0; the second exits 2 with zero checked records and partial input.

### Reports and statistics

`--format json` has a versioned [report schema](tapcheck/report.schema.json). `--format junit` emits one well-formed test case per diagnostic (one passing case when valid). Text/diagnostics avoid echoing record/state values and escape user-controlled terminal characters.

Each stream reports record count; present/null/missing counts and null rate per top-level property; min/max cursor; and first/last extraction timestamps in encounter order (UTC milliseconds). Null rate is `explicit nulls / all object records`; missing is separate. Observed fields and unobserved root-declared fields are listed. Records that fail schema validation still contribute to statistics; malformed non-object records do not. Reports for stopped inputs contain partial statistics. Cursor values and property names are intentionally present in reports: treat them as data.

Memory is O(1) **with respect to record count**, without duplicate checking: one bounded line, cached schemas and per-stream/property aggregates. Defaults cap a line/catalog at 8 MiB, streams at 1,000, and observed/root-declared properties at 10,000 per stream. The latter two are Python API settings; overflow fails TC020. `--check-duplicates` adds at most `--duplicate-limit` digests globally. Complex schemas, regexes and highly compressed files can still take substantial CPU; this is an offline validator, not a hostile-input CPU sandbox. All commands accept an optional main-input byte budget; omission leaves total main input unlimited. No command imposes a CPU/time limit.

### Per-stream data quality

```sh
datawrench tapcheck quality examples/tapcheck/quality_singer.jsonl --check-duplicates
datawrench tapcheck quality capture.jsonl --catalog configured_catalog.json --check-duplicates --example-limit 3 --key-example-limit 2
cat capture.jsonl | datawrench tapcheck quality - --protocol singer --example-limit 0
```

`quality [FILE|-]` emits JSON using the separate [data quality report schema](tapcheck/quality-report.schema.json). It accepts `--protocol`, `--catalog`, `--strict`, `--check-order`, `--check-duplicates`, `--duplicate-limit`, `--max-line-bytes`, `--max-input-bytes`, `--max-batch-bytes`, `--max-batch-raw-bytes`, `--max-batch-files`, `--max-records`, `--max-messages`, `--max-declarations`, and `--batch-root` with the validation contracts above. Exit **0** means no violations in the enabled checks, **1** means contract violations, and **2** means unusable input or an operational/resource failure. Contract violations do not stop this command: it counts the entire readable capture, independent of `validate`'s `--max-errors`. There is no `--max-errors` option for `quality`.

The included example declares `users` and an empty stream, repeats key `id=1`, then emits an empty record and a STATE. With `--check-duplicates`, it exits **1** and reports these counts (excerpt):

```json
{"name":"empty","schemas":1,"records":0,"states":1,
 "keys":{"paths":[["id"]],"by_path":[{"path":["id"],"missing_fields":0,"null_fields":0,"examples":[],"omitted_examples":0}],"missing_fields":0,"null_fields":0,"records_with_missing_or_null_keys":0},
 "duplicates":{"enabled":true,"evaluated_records":0,"duplicate_keys":0}}
{"name":"users","schemas":1,"records":3,"states":1,
 "keys":{"paths":[["id"]],"by_path":[{"path":["id"],"missing_fields":1,"null_fields":0,"examples":[],"omitted_examples":1}],"missing_fields":1,"null_fields":0,"records_with_missing_or_null_keys":1},
 "duplicates":{"enabled":true,"evaluated_records":2,"duplicate_keys":1}}
```

Its `users.examples` contains TC016 at line 4 (`path: null`; the composite key paths are in `keys.paths`) and TC010 at line 5 (`path: ["id"]`). Examples contain only code, source, physical line and field path. No records, schemas, state blobs, cursor values or extraction timestamps are copied into the quality output. Stream names, namespaces, source paths and key/diagnostic field paths remain visible: treat this metadata as data.

* `schemas` counts accepted declarations, including identical repeats and external catalog entries. Invalid or changed contracts are excluded; the original contract stays active. `records` counts object records for declared streams, including schema-invalid records and decoded local BATCH rows. Non-object/undeclared records and unreadable lines generate errors but are excluded from that count.
* `states` counts valid STATE messages covering the stream **at the time encountered**. Singer and Airbyte LEGACY cover all declared streams; STREAM/GLOBAL cover their descriptors. A repeated descriptor in one GLOBAL message counts once; invalid STATE messages count zero. This is message coverage, without interpreting checkpoint contents.
* `keys.missing_fields` and `null_fields` count declared primary-key path occurrences; `records_with_missing_or_null_keys` counts affected records once. `keys.by_path` breaks these totals down into `{path, missing_fields, null_fields, examples, omitted_examples}` entries, aligned with `keys.paths` in declaration order. Every declared path appears, including zero counts for valid/empty streams; no declared keys gives `[]`. Repeated declared paths retain separate entries and count once per declaration occurrence. Component sums equal the corresponding totals, independently of either example limit. These counts are collected even when the schema cannot be evaluated.
* `--key-example-limit N` accepts **0–100**, default **0**, independently of `--example-limit`. Each `keys.by_path` entry retains the first N incomplete occurrences as `{kind: "missing"|"null", source, line}`; missing and null share that per-path budget. `omitted_examples` is `missing_fields + null_fields - len(examples)`, including all incomplete occurrences at the default zero. Complete components and empty streams have `examples: []` and zero omitted. Repeated paths have separate samples. These samples remain available when earlier diagnostics fill the general sample, and are collected even if schema evaluation fails without a TC010 diagnostic. The top-level `key_example_limit` records N.
* Location samples use **1-based physical lines**, including blank lines: main-input source is the CLI filename verbatim (`-` for stdin; Python API default `<stdin>`), while local BATCH source is the resolved filename relative to `--batch-root` and line is the physical JSONL/decompressed-gzip row, never the BATCH envelope line. Repeated manifest files can repeat the same source/line. Only kind/source/line are stored per key occurrence; key values are never copied.
* Airbyte nested paths and composite keys are supported. An absent or non-object parent (including null) makes a descendant key path **missing**; only explicit null at the complete path counts as **null**. Non-null values such as `0`, `false` and `""` are complete keys. Paths remain arrays of literal field names, so `["a.b"]` differs from `["a","b"]`.
* `duplicates.duplicate_keys` counts repeat composite-key occurrences after the first, using the existing canonical digest semantics. `evaluated_records` counts records with complete keys actually checked, including repeats. The duplicate count is `null` when disabled or when no primary key is declared. Missing/null keys are excluded. Schema evaluation failures skip later duplicate/order/timestamp checks and are listed in `not_evaluated`.
* `errors_by_code` counts diagnostic occurrences, so one record can contribute several errors. The first **5** examples per stream, plus 5 in the `unassigned` bucket, are retained by default; `--example-limit N` accepts **0–100**. `omitted_examples` counts the rest. Errors without an accepted stream contract go in `unassigned`. Unsupported message type names share the `UNSUPPORTED` message counter to keep full-scan storage bounded.

For a composite nested-key example with **no retained diagnostics**:

```sh
datawrench tapcheck quality examples/tapcheck/quality_airbyte_keys.jsonl --example-limit 0
```

This capture declares `[["obj","id"],["other"]]` and has four records: `{"obj":{"id":1},"other":2}`, `{"obj":{},"other":null}`, `{"obj":null,"other":3}`, and `{"obj":{"id":null}}`, followed by a covering STATE. It exits **1**, with `records: 4`, `examples: []`, `omitted_examples: 5`, and these exact key counts:

```json
{"paths":[["obj","id"],["other"]],
 "by_path":[{"path":["obj","id"],"missing_fields":2,"null_fields":1,"examples":[],"omitted_examples":3},
            {"path":["other"],"missing_fields":1,"null_fields":1,"examples":[],"omitted_examples":2}],
 "missing_fields":3,"null_fields":2,"records_with_missing_or_null_keys":3}
```

To keep one location per component with the general sample already full:

```sh
datawrench tapcheck quality examples/tapcheck/quality_airbyte_keys.jsonl --example-limit 1 --key-example-limit 1
```

It still exits **1** with the same counts. `obj.id` retains `{"kind":"missing","source":"examples/tapcheck/quality_airbyte_keys.jsonl","line":3}` with `omitted_examples: 2`; `other` retains `{"kind":"null","source":"examples/tapcheck/quality_airbyte_keys.jsonl","line":3}` with `omitted_examples: 1`. The general sample contains only the first `obj.id` diagnostic, but the later component has its own location. Missing/null share one budget per component, rather than N examples of each kind.

`by_path` and its required sample fields plus top-level `key_example_limit` are additive fields in quality report v1; consumers using the previous strict quality schema must use the updated schema. The validation report is unchanged. The Python API is `Validator(quality=True, key_example_limit=N)` with the same integer bounds (invalid types, including booleans, raise `ValueError`).

Streams sort by name, then namespace (null first); error-code maps sort by code; examples retain encounter order. Memory adds counters proportional to declared key-path occurrences and at most `example_limit × (streams + 1)` general examples plus `key_example_limit × declared key-path occurrences` key examples, independent of record count. At the default zero, no per-path location entries are retained. Existing line/catalog/stream/property and global duplicate-digest limits remain in force. Capacity exhaustion still exits 2 immediately with `stopped_early: true`, `coverage: "partial_input"`, and counts for the processed prefix; no key eviction occurs. Oversized/malformed lines and inaccessible BATCH contents are identified as unevaluated. `coverage: "complete_input"` means the main input was scanned through EOF, not that every payload could be evaluated.

Every report has `not_evaluated`, including disabled optional checks, checkpoint contents, full protocol certification, remote/non-JSONL BATCH and version delete semantics. Local JSONL/gzip BATCH and ACTIVATE_VERSION retain their documented boundaries; activation does not reset duplicate tracking. This report is an offline quality summary, with the same supported contracts as validation.

## Convert

```sh
datawrench tapcheck convert --to airbyte examples/tapcheck/good_singer.jsonl --output airbyte.jsonl
datawrench tapcheck convert --to airbyte examples/tapcheck/good_singer.jsonl --output bounded.jsonl --max-output-bytes 1048576 --max-input-bytes 1048576
datawrench tapcheck convert --to singer airbyte.jsonl --output singer.jsonl
datawrench tapcheck convert --to airbyte examples/tapcheck/good_singer.jsonl > airbyte.jsonl
datawrench tapcheck convert --to singer airbyte.jsonl > singer.jsonl
datawrench tapcheck convert --to airbyte examples/tapcheck/good_singer.jsonl | datawrench tapcheck validate - --protocol airbyte
```

`convert [FILE|-] --to singer|airbyte [--protocol auto|singer|airbyte] [--catalog FILE] [--output FILE|-] [--max-output-bytes N] [--max-input-bytes N]` validates its input's common contract. With `--output FILE`, conversion streams into a private temporary file in the destination directory. Only after the entire input passes validation (including final STATE coverage), all output lines fit the 8 MiB limit, the main input and total output fit their optional byte budgets, and writing, flushing, syncing and closing succeed does an atomic rename replace the destination. Success exits 0 without writing stdout. Invalid records, unsupported conversion types, input/output limits and write/sync/rename errors exit **2**, preserve an existing destination byte-for-byte (or leave a missing destination absent), and clean up the temporary file.

`--max-output-bytes N` limits the **total serialized conversion output**, in both directions and for file/stdout output. Conversion always writes UTF-8 JSONL with LF newlines, independent of stdout text-encoding settings. The budget includes every JSON escape and trailing LF byte, plus all messages generated from inline or external catalogs. It excludes input/catalog bytes, stderr and any old destination contents. An exact fit succeeds after the remaining input and final STATE coverage pass validation. Before writing any line that would exceed the budget, conversion fails with **TC020 / exit 2**. File mode rolls back; stdout retains only preceding complete lines on a budget failure. The default has no total output limit; the independent 8 MiB per-line limit always applies. `N` must be an integer from **0 through 9223372036854775807**; zero permits no output bytes. Negative, non-integer and out-of-range values fail argument parsing with exit 2 before output is opened. Use `--max-input-bytes` to bound main input separately. Neither budget imposes a CPU/time limit.

`--max-input-bytes N` limits **total raw main-input bytes** from a file or stdin in either conversion direction. It counts blank lines, whitespace, every CR/LF and every UTF-8 byte, including a final line without LF. External catalog bytes are excluded and keep their separate 8 MiB limit; inline catalogs count as main input. Omission leaves total input unlimited. The accepted range is **0 through 9223372036854775807**; invalid values fail argument parsing before output is opened. Zero rejects any nonempty input with TC020; empty input still fails TC021 (nothing converted).

Overflow fails **TC020 / exit 2**, preserving an existing/absent file destination and removing staging; stdout may contain partial output and warns on stderr. Conversion reads at most **N+1 raw bytes** to detect overflow, including through buffered file/stdin reads and oversized-line draining. Reaching N still requires a real EOF check: even one extra blank byte fails. Exact fits still validate JSON and final STATE coverage. The existing per-line, catalog and output limits remain independent, and conversion can stop on an earlier validation/limit error. These byte budgets do not bound CPU/time. Python callers cannot undo bytes buffered before conversion began.

The parent directory must exist. Destinations must be regular files or absent; symlinks and special files are refused. Input and catalog aliases, including hard links and a file redirected to stdin, are rejected. Inputs/catalogs are never opened for writing. Published files have private **0600** permissions; replacement creates a new inode and does not preserve the old file's permissions, ownership or hard-link relationships. Atomicity covers readers observing the destination; this is not a power-loss durability guarantee, and SIGKILL/power loss can leave a `.tapcheck-*.tmp` file. Use a destination directory under your control; concurrent hostile path changes are outside the guarantee.

Omitting `--output`, or using `--output -`, streams stdout as before. Stdout can be **partial** on any failure (exit 2); stderr explicitly warns that stdout may contain partial output. Shell redirection (`> FILE`) has this same partial-output behavior and cannot protect an existing file: use `--output FILE` for atomic publication. Conversion is not a dry-run of an external workflow: no workflow or connector is ever invoked.

For a concrete minimal input, `{"type":"STATE","value":{"cursor":1}}` followed by a newline converts to `{"type":"STATE","state":{"type":"LEGACY","data":{"cursor":1}}}` followed by a newline. If a later line is malformed JSON, file mode returns TC001/exit 2 and retains the previous destination; stdout mode has already emitted the converted STATE. Schema/record/state captures use the same mappings below.

That converted STATE is exactly **63 bytes**, including the newline. For example, `printf '%s\n' '{"type":"STATE","value":{"cursor":1}}' | datawrench tapcheck convert --to airbyte --max-output-bytes 63 --output state.jsonl` succeeds; a budget of **62** returns TC020/exit 2 and leaves `state.jsonl` unchanged (or absent if it did not exist).

The original minimal Singer STATE is **38 raw input bytes** with LF. `printf '%s\n' '{"type":"STATE","value":{"cursor":1}}' | datawrench tapcheck convert --to airbyte --max-input-bytes 38 --output state.jsonl` succeeds; **37** fails TC020/exit 2 with file rollback. Adding a blank line needs **39** bytes; omitting the final LF needs **37**. Input and output budgets can be combined.

| Singer | Airbyte | Mapping/boundary |
|---|---|---|
| SCHEMA | CATALOG | Schema preserved; `key_properties` ↔ primary-key paths; bookmark/replication key ↔ cursor. Each Singer SCHEMA emits a one-stream CATALOG update; tapcheck accumulates these. Consumers requiring a single discovery catalog must consolidate updates separately. |
| RECORD | RECORD | `record` ↔ `record.data`; `time_extracted` RFC3339 ↔ `emitted_at` integer epoch milliseconds, normalized to UTC. Missing time or sub-millisecond Singer timestamps fail; time is never invented. |
| STATE object | LEGACY STATE | `value` ↔ `state.data`; omitted LEGACY type is normalized to explicit LEGACY. |
| STATE reserved envelope | STREAM/GLOBAL or null/reset STATE | `value.__tapcheck_airbyte_state__` preserves the entire Airbyte state. Reserved envelope cannot have sibling keys. |
| stream name | (stream, namespace) | Null namespace uses the plain name unless it begins `tapcheck:ab:`. Otherwise Singer name is `tapcheck:ab:` plus unpadded URL-safe base64 of JSON `[namespace,name]`; fully reversible without delimiter collisions. Prefix is reserved; invalid reserved names fail. |

The supported subset round-trips schemas, records, primary/cursor fields, namespace and state. Protocol-specific envelope metadata (e.g. Airbyte `meta`, RECORD version or unknown extension fields), and catalog selection/sync configuration have no mapping and cause TC023; remove them explicitly before converting. Discovery `supported_sync_modes` is informational and normalized to full_refresh/incremental; timestamp spelling is normalized. Primary/cursor/schema contracts are preserved, not complete catalog documents. Nested Airbyte key/cursor paths cannot map to Singer and fail TC023. BATCH, ACTIVATE_VERSION, LOG/TRACE/CONTROL, SPEC/CONNECTION_STATUS, same-protocol conversion and other message types fail explicitly. Null LEGACY state and states containing the reserved key use the reserved envelope to avoid collisions. This mapping is tapcheck's documented interchange convention, not an upstream state-migration standard.

## Stable diagnostic codes

| Code | Meaning |
|---|---|
| TC001 | Invalid UTF-8/JSON, duplicate JSON members or non-finite number |
| TC002 | Unknown/unsupported protocol message type |
| TC003 | Invalid message, catalog, key/cursor path or envelope shape |
| TC004 | Invalid/unsupported/unresolvable JSON Schema |
| TC005 | Undeclared stream (RECORD/BATCH/ACTIVATE_VERSION) |
| TC006 | JSON Schema record violation |
| TC007 | Invalid STATE shape |
| TC008 | Missing final covering STATE |
| TC009 | Mid-stream schema/key/cursor contract change |
| TC010 | Missing/null primary key |
| TC011 | Absent strict non-nullable top-level property |
| TC012 | Undeclared strict top-level property |
| TC013 | Strict date-time format violation |
| TC014 | Missing/null/unsupported or changing cursor type |
| TC015 | Cursor moved backwards |
| TC016 | Duplicate primary key |
| TC017 | BATCH path, encoding or read error |
| TC018 | Duplicate tracking capacity exhausted (exit 2) |
| TC019 | Invalid extraction timestamp |
| TC020 | Line/catalog/stream/property size limit or total main-input, raw/decompressed BATCH, manifest-occurrence, record-evaluation, main-message/schema-registration processing or conversion-output budget |
| TC021 | Empty input or input/catalog I/O failure (exit 2) |
| TC022 | Auto protocol ambiguity, mixture or no identifiable protocol (exit 2) |
| TC023 | Conversion outside supported mapping |

## Tests and reproducible experiment

From a datawrench checkout (run from the repository root; several tests open `examples/tapcheck/`, `tests/tapcheck/fixtures/` and `docs/tapcheck/*.schema.json` relative to it):

```sh
uv run --extra test pytest tests/tapcheck -q
datawrench tapcheck validate tests/tapcheck/fixtures/singer_tools_issue_19.jsonl; test $? -eq 1
datawrench tapcheck validate examples/tapcheck/good_singer.jsonl --strict --format json
datawrench tapcheck convert --to airbyte examples/tapcheck/good_singer.jsonl | datawrench tapcheck validate - --protocol airbyte
uv run python tests/tapcheck/bench.py --records 200000
```

Fixtures are small, synthetic and redistributable. Regenerate using `uv run python tests/tapcheck/make_fixtures.py` (gzip timestamps fixed). Tests cover positive/negative checks, protocol boundaries, real JSONL/gzip files (including invalid DEFLATE), state coverage, key/cursor mapping, report schema, JUnit, CLI exits, converter round trips and benchmark equivalence. Atomic-output regressions cover both conversion directions, existing/absent destinations, late invalid records/JSON/unsupported types/missing STATE, a real Linux file-size limit, encoded-line limits, sync/rename failures, input aliases and temporary-file cleanup.

The JSON report schemas used by those tests are [docs/tapcheck/report.schema.json](tapcheck/report.schema.json) (validation, `report_version` 1) and [docs/tapcheck/quality-report.schema.json](tapcheck/quality-report.schema.json) (quality, `report_kind: "data_quality"`, `report_version` 1).

The single-process benchmark generates 200,000 records and 201 deliberately invalid records. It compares per-record `jsonschema.validate` (including repeated schema checking) with tapcheck's once-initialized validator. Both read/decode the same JSONL. It asserts identical invalid-record positions using SHA-256 and separately measures the complete tapcheck streaming engine including decoding, keys and statistics. Here "compiled" means a prechecked cached jsonschema validator; no generated Python or `eval` is used. This optimizes schema setup overhead, not jsonschema's validation algorithm. The benchmark creates (and removes) a temporary `.tapcheck-bench-*` directory in the repository root.

One actual run of the standalone project (Python 3.11.15) measured 5,801 records/s for naive validation, 72,336 records/s for the cached validator (12.47×), and 50,697 records/s for full tapcheck. These are one synthetic local run, not universal performance claims; machine load, schema complexity and options affect throughput. Re-run the command for your environment. Save a run with `--output PATH`.

## Relationship to upstreams and limits

Independent implementation; no upstream application code is copied or vendored. [Singer specification](https://github.com/singer-io/getting-started/blob/master/docs/SPEC.md), [singer-tools](https://github.com/singer-io/singer-tools), [Meltano BATCH conventions](https://sdk.meltano.com/en/latest/batch.html), and [Airbyte protocol](https://github.com/airbytehq/airbyte-protocol) inform message shapes. [jsonschema](https://github.com/python-jsonschema/jsonschema) supplies the schema engine. Source links, license attribution and dependency notices are in [THIRD_PARTY.md](../THIRD_PARTY.md).

This is not an official Singer/Meltano/Airbyte checker or a complete protocol conformance suite. It deliberately imposes stronger key, schema-before-record and final-state contracts. It does not run taps, connectors, Docker or loaders, inspect checkpoint contents, migrate production checkpoints, implement socket/partition coordination, or replace the Airbyte CDK acceptance suite. `--strict` presence/closure scope and conversion loss boundaries are described above. Large duplicate captures need a higher explicit limit; schema regexes have no CPU timeout.

## License

The standalone `tapcheck` project was licensed under Apache-2.0; datawrench is Apache-2.0 too ([`../LICENSE`](../LICENSE)); the standalone project's NOTICE is merged into [`../NOTICE`](../NOTICE). Third-party notices are in [`../THIRD_PARTY.md`](../THIRD_PARTY.md).
