# datawrench singer-replay

`datawrench singer-replay` (formerly `singer-replay`) records a Singer tap once, then
lets you test loaders and pipeline staging without source credentials.
`datawrench singer-replay record` writes a scrubbed, checksummed cassette;
`datawrench singer-replay tap` (formerly the separate `tap-replay` command) acts as a
real Singer tap with discovery, catalog selection, resumable bookmarks, timestamp
shifting and deliberate failures.

Python 3.11+; no runtime dependencies beyond the standard library. Independent of
Meltano and the Singer SDK; an integration tool, not an SDK extension.

## Install and try it

```bash
pipx install datawrench                       # or: pip install datawrench
datawrench singer-replay --version            # prints the bare datawrench version, e.g. 0.1.0
datawrench singer-replay tap --about --format json
```

No extra is needed for this command. `python -m datawrench singer-replay` is
equivalent to `datawrench singer-replay`. The subcommands are `record`, `trim`,
`verify` and `scrub`, plus the hand-routed `tap` mode:

| Former command | Now |
| --- | --- |
| `singer-replay record\|trim\|verify\|scrub ...` | `datawrench singer-replay record\|trim\|verify\|scrub ...` |
| `tap-replay [--config FILE] [--discover\|--about] [--catalog FILE] [--state FILE]` | `datawrench singer-replay tap [--config FILE] [--discover\|--about] [--catalog FILE] [--state FILE]` |

`datawrench singer-replay tap --help` shows the tap options. Diagnostics on stderr are
prefixed `datawrench singer-replay:` (recorder) or `datawrench singer-replay tap:`
(tap). The `--about` output keeps the tap name `tap-replay`.

Record the bundled synthetic shop (two streams, four records), from the datawrench
checkout root:

```bash
set -o pipefail
python tests/singer_replay/fixtures/fake_tap.py |
  datawrench singer-replay record -o /tmp/singer-replay-cassette --scrub customers.email=hash
# stderr: datawrench singer-replay: recorded 4 records; truncated=False

datawrench singer-replay tap --config examples/singer_replay/config.json --discover
datawrench singer-replay tap --config examples/singer_replay/config.json | head -5
datawrench singer-replay tap --config examples/singer_replay/config.json \
  --catalog examples/singer_replay/catalog.selected.json --state examples/singer_replay/state.json
```

The output directory must **not already exist**. For another recording choose a
new path and update `cassette_path` in your config. `examples/singer_replay/config.json` points
to `/tmp/singer-replay-cassette`, relative config paths otherwise resolve against the current
working directory (not against the config file).

The selected example emits only the `customers` schema and records with IDs **2,
3**, plus STATE boundaries. `profile` and `tags` are omitted from both schema and
records, `email` is a 64-character HMAC-SHA256 hex string, and the final STATE is:

```json
{"type":"STATE","value":{"bookmarks":{"customers":{"replication_key":"updated_at","replication_key_value":"2024-01-03T12:00:00Z"}}}}
```

Logs go to stderr; stdout contains only machine-readable messages, summaries or discovery
JSON. `--discover` returns a Singer catalog including `tap_stream_id`, schema,
primary keys, replication key, stream `selected` metadata and property inclusion.

## Record stdin or a tap command

```bash
datawrench singer-replay record -o ./cassette \
  --scrub customers.email=hash --scrub customers.profile=drop \
  --scrub products.price=mask --limit 1000 -- python tests/singer_replay/fixtures/fake_tap.py
```

`-- TAP CMD ARG ...` invokes exactly the supplied executable and arguments, with
no shell expansion. Only explicitly requested commands are executed. Without a
command, the recorder reads UTF-8 Singer JSONL from stdin. Command stderr is
suppressed so credentials or raw data are not accidentally forwarded. Diagnose
your trusted tap separately if it fails.

- `--limit N` counts RECORD messages across streams; N must be positive. It stops
  reading immediately at N and terminates the command's process group. The
  resulting valid prefix has `complete: true, truncated: true`. Rules must refer
  to schemas observed before the limit. A command still running after it closes
  stdout gets 10 seconds to exit; stopped commands get 2 seconds before SIGKILL.
- A tap's nonzero exit, invalid message, invalid scrub rule or interruption leaves
  a manifest marked `complete: false`. Replay refuses it. Nonzero tap exit yields
  recorder exit 2 and preserves `source_exit_code` in the manifest.
- A new output directory is created exclusively with mode 0700, files with 0600.
  Existing output directories are never overwritten. No filenames come from
  stream names, records, manifests or archive members; there is no extraction.

A cassette has three files:

| File | Contents |
| --- | --- |
| `messages.jsonl.gz` | Validated SCHEMA/RECORD/STATE messages; scrubbing happens **before** gzip writes. |
| `catalog.json` | Discovery catalog derived from the scrubbed SCHEMA messages. |
| `manifest.json` | Format version, UTC `recorded_at`, completeness/truncation, per-stream counts, message count, scrub rule descriptions, source exit code, SHA256 of both other files. |

Replay checks checksums, gzip structure, messages, counts and schema/catalog
agreement **before emitting anything**, including in discovery mode. Symlink and
non-regular cassette members are refused. Checksums detect damage, not malicious
replacement: a cassette is a local test artifact, not a signed trust boundary.

## Verify an existing cassette

```bash
datawrench singer-replay verify /tmp/singer-replay-cassette
# {"counts":{"customers":3,"products":1},"message_count":8,"truncated":false,"streams":["customers","products"]}
```

`verify SOURCE` validates the entire cassette without a replay config or trim
options. It checks completeness, regular non-symlink members, checksums, gzip,
every message, schema/catalog agreement and counts, using the same size and
schema bounds as replay. Only after validation succeeds does it print one compact
JSON line (at most 16 MiB including UTF-8 encoding and newline), with exit 0 and
empty stderr. Invalid or incomplete input returns exit 2 with a safe diagnostic
and empty stdout.

The four fields describe the actual source: `counts` includes zero-record streams,
`message_count` includes every SCHEMA/RECORD/STATE, `truncated` carries the source
flag, and `streams` follows first-schema order. For example, SCHEMA alpha, RECORD 1,
an identical SCHEMA alpha, RECORD 2 and another identical SCHEMA alpha report
`{"counts":{"alpha":2},"message_count":5,"truncated":false,"streams":["alpha"]}`.
Verification keeps repeated/late schemas and existing checkpoint counts; it adds
no checkpoint, applies no selection and buffers no records. It creates no files
and leaves source bytes, permissions and modification times unchanged. The report
contains no records, schemas, bookmarks or scrub field names/provenance; it does
include stream IDs. Filesystem access times may update when files are read.
Checksums are not authentication, and concurrent in-place source mutation is
outside scope, as with replay.

## Scrub an existing cassette offline

When the original tap is unavailable, mask or drop more fields into a new cassette:

```bash
datawrench singer-replay scrub /tmp/singer-replay-cassette -o /tmp/singer-replay-private \
  --scrub customers.email=mask --scrub customers:profile.phone=drop
# stderr: datawrench singer-replay: scrubbed 4 records; truncated=False
```

For a keyed `alpha` record `{"id":1,"secret":"synthetic-private"}`,
`datawrench singer-replay scrub SOURCE -o DEST --scrub alpha.secret=mask` retains `id: 1`
and changes `secret` to `"***"`; its schema permits string/null. Null stays null.
Drop removes the property from records, schemas and required lists. Stdout is empty;
success exits 0 with the bounded diagnostic above. No tap or external service runs.

At least one `--scrub` is required. Only `mask` and `drop` are supported; hash and
salt options are unavailable. The same legacy `STREAM.FIELD`, explicit
`STREAM:object.field` and escaped `STREAM:/JSON/Pointer` paths apply as for recording.
Duplicate/overlapping rules, missing schema paths, unknown streams and scrubbing
primary/replication keys fail with exit 2 before destination creation. Explicit
missing record paths or null parents fail during streaming and leave incomplete
output; legacy optional top-level fields may be absent. Explicit paths remove
examples/defaults on targeted schema ancestors; unrelated fields and free-text
annotations are not anonymized. Legacy rules keep their existing annotation behavior.

The entire source receives replay's checksum, gzip, schema, record, checkpoint,
catalog and count validation before output creation. Destination must be new and
outside source; existing paths, symlinks, aliases and nested destinations are refused.
Output uses private 0700/0600 permissions and the shared writer's encoded message,
total-message, catalog and manifest bounds. Write failures can leave an incomplete
manifest or an empty directory; replay refuses these results. Source bytes, modes
and modification times stay unchanged; reading may update access times.

Every message retains its source position, including repeated schemas and STATE
boundaries. There is no selection, timestamp shift, checkpoint rebuild or added final
STATE, and records are not buffered. Original keys, extraction times, safe bookmarks,
`recorded_at`, counts, message count and inherited truncation stay unchanged. Catalog
and checksums are regenerated. Manifest `scrub` preserves inherited descriptions
and appends the new parsed descriptions (`field` for legacy, decoded `path` list for
explicit rules). `rescrub` stores these new descriptions as `rules` plus
`source_sha256` for the immediate source's messages/catalog. Earlier trim/rescrub
operation objects are not copied; inherited scrub descriptions remain. `source_exit_code`
is null. Source scrub provenance must be a list; its contents are preserved without
authentication. Python callers can use `scrub(source, destination, rules=[...])` from `datawrench.singer_replay.cassette`.

## Trim a small reproduction cassette

To inspect a trim without creating a destination, use `--dry-run`:

```bash
datawrench singer-replay trim /tmp/singer-replay-cassette --dry-run --per-stream 1
```

Stdout is exactly one JSON object followed by a newline, with `counts`,
`message_count`, `truncated` and `streams`. Stream IDs follow retained catalog
order, and counts include zero-record schemas. Truncation means records were
removed or the source was already truncated; changing schemas/checkpoints alone
does not set it. All trim options apply through the same streaming selector as
an actual trim, including the final checkpoint. Full-source and planned-message
validation finish before stdout; invalid options, corruption anywhere in the
source, oversized generated metadata and all-empty removal fail with exit 2 and empty stdout. The source stays
unchanged and no destination, temporary file or cassette artifact is created.
`--output` is forbidden with `--dry-run`; otherwise it remains required. Python
callers can use `trim(source, per_stream=N, dry_run=True)` (from `datawrench.singer_replay.cassette`) to return the same dict;
`dry_run` must be an actual boolean, and a destination must not be supplied.

Planning emits metadata only, consumes one message at a time, and enforces the
writer's 16 MiB encoded UTF-8 message and 1 GiB uncompressed message-total limits,
plus 16 MiB limits for the planned catalog, summary, initial incomplete manifest
and final successful manifest. Every bound counts encoded UTF-8 bytes, JSON
escaping/framing and the trailing newline; exactly the limit is allowed. Manifest
construction and metadata checks are shared with the writer, including inherited
scrub descriptions and new trim provenance. The final manifest size accounts for
exactly 64 ASCII hex bytes per SHA256 value without computing output checksums.
It does not predict gzip size, cassette member file-size feasibility, available
disk space, destination permissions or other write failures. A
successful plan describes the selection, not a guarantee that a later write will
succeed. The entire source still receives its usual format/size validation.
Omitting dry-run keeps real trim artifacts, stdout and provenance unchanged.
For example, if inherited metadata fits the source limit but new trim provenance
makes the output manifest exceed 16 MiB, dry-run exits 2 with empty stdout and
`datawrench singer-replay: cassette metadata exceeds 16 MiB` on stderr, without writes.
A real trim still creates output as before: a final metadata size failure leaves
the initial `complete: false` manifest and any already-written members. An initial
manifest size failure can leave an empty destination. Replay refuses incomplete
output; planning does not simulate failure manifests or guarantee a later write.

```bash
datawrench singer-replay trim /tmp/singer-replay-cassette -o /tmp/singer-replay-small --per-stream 1
printf '%s\n' '{"cassette_path":"/tmp/singer-replay-small"}' > /tmp/singer-replay-small-config.json
datawrench singer-replay tap --config /tmp/singer-replay-small-config.json
```

This retains the **first N RECORDs of each stream**, in their original interleaved
order. For the bundled shop, N=1 retains customer ID **1** and product **DEMO-1**.
By default both schemas remain, and every customer checkpoint is rebuilt to January 1,
including the source boundary that originally pointed to January 3:

```json
{"type":"STATE","value":{"bookmarks":{"customers":{"replication_key":"updated_at","replication_key_value":"2024-01-01T12:00:00Z"}}}}
```

- `--per-stream N` is required and supplies the default cap for every stream.
  Repeat `--stream-limit STREAM=N` to override individual streams, including with
  zero or a cap above the default. Unspecified streams keep the default. N=0
  retains that stream's schema without records or bookmarks by default; all zero
  caps produce a schema-only cassette. Caps at or above the count retain all records.
  There are no offset/range options.
- Add `--max-records N` to bound the **total** retained records. Only records
  that survive stream filters and per-stream caps count toward this maximum;
  the first N eligible records remain in source order. By default eligible schemas remain
  even if they appear after the maximum is reached. Zero keeps schemas and empty
  checkpoints only. Omit this option for no global cap. Negative, empty,
  fractional or noninteger bounds fail with exit 2 before creating output.
- Override IDs are exact, case-sensitive Singer stream IDs, including dots and
  hyphens. Counts use nonnegative ASCII decimal digits; leading zeros normalize
  to integers. Missing `=`, empty IDs/counts, signs, whitespace, fractional or
  noninteger counts, unknown IDs and duplicate IDs (even with equal caps) fail
  with exit 2 and empty stdout before any destination is created.
- Repeat `--include-stream STREAM` to keep only named streams, including their
  schemas, records, catalog entries, counts and bookmarks. Alternatively, repeat
  `--exclude-stream STREAM` to omit named streams and keep the rest. With neither
  filter, all streams remain. IDs are exact and case-sensitive; unknown, empty or duplicate
  IDs fail with exit 2 before creating output. Overrides for excluded streams
  also fail, even if zero. Included streams may have zero records or a zero cap.
  Inclusion and exclusion cannot be combined, even for disjoint IDs. Excluding
  every schema fails with exit 2 before output creation; keep at least one schema.
  Option order does not reorder source messages or catalog entries.
- Add `--drop-empty-streams` to omit streams with **zero retained records** after
  stream selection, default/override caps and the optional global cap. Their schemas,
  catalog entries, counts and bookmarks disappear, including schemas that occur
  after the global cap is reached. If every stream would be empty, exit 2 with
  empty stdout before creating output: a valid cassette needs at least one schema.
  The entire source is still validated, including at zero caps. Without the flag,
  schemas with no retained records remain available to discovery.
- By default, all remaining SCHEMA messages and all source STATE boundaries remain. STATE values contain
  only the latest retained replication keys; streams with no retained records
  or no replication key have no bookmark. A final STATE is added when needed.
  Records, extraction timestamps, schemas and existing scrubbed values stay as
  recorded. Replay selection, timestamp shifting and failure/resume work normally;
  full-table streams still replay in full on resume.
- Add `--schema-once` to keep the first SCHEMA for each retained stream in its
  source position and omit later identical copies. Schema equality follows the
  existing whole-message validation (JSON object member order does not matter).
  Schema evolution anywhere in the source still fails before output creation,
  including schemas that filters or this option would remove. Catalog schemas,
  records and original-time keys stay unchanged. This composes with all filters,
  caps, empty-stream removal and checkpoint modes. Suppressed schemas do not
  break state adjacency or cause an extra final cadence checkpoint; a late first
  schema still does. Omitted flag or API `schema_once=False` preserves all baseline
  artifacts/provenance; API values must be actual booleans.
- Add `--compact-states` to collapse adjacent identical STATE messages **after**
  filtering, capping and rebuilding checkpoints. Discarded schemas/records do not
  break adjacency; every retained SCHEMA or RECORD does, even when the bookmark
  stays the same. Distinct checkpoints and the final safe state remain. Without
  any state option all source boundaries remain, including repeated empty states.
- Add `--final-state-only` to omit every source STATE boundary and append exactly
  one safe final STATE after all retained schemas and records. This also works with
  `--compact-states`, filters, caps and `--drop-empty-streams`. Bookmarks use only
  retained original-time replication keys; schema-only and full-table-only outputs
  end with `{"bookmarks":{}}`. Retained messages keep source order. This controls
  cassette boundaries; replay may still emit frequency or injected-failure checkpoints.
- Add `--state-every N` to replace source STATE boundaries with a checkpoint
  immediately after every N **retained records**, counted across streams after
  filters and all caps. N must be a positive integer; zero, negative and noninteger
  intervals fail before creating output. It cannot be combined with `--final-state-only`.
  A final checkpoint is added if needed after a partial interval or trailing retained
  schemas; discarded messages and source states do not add another checkpoint.
  Schema-only output ends with one empty checkpoint. Original-time bookmarks,
  full-table resume behavior, `--compact-states` and `--drop-empty-streams` remain
  supported. Full-table records count toward the cadence even though they add no bookmark.
- The entire source is validated before the destination is created, even for
  N=0 or corruption in records that would be discarded. The source is read only.
  The destination must be new and outside the source directory; overwrites,
  aliases of the source and nested destinations fail with exit 2. Output files
  use the same private permissions and format validation as recordings. An output
  size failure leaves `complete: false`, which replay refuses.
- Catalog, counts, message count and checksums are regenerated. The manifest
  preserves the source's `recorded_at` and scrub descriptions, and adds
  `trim: {"per_stream": N, "source_sha256": {...}}` for the immediate source's
  message/catalog checksums. When overrides are supplied, `trim.stream_limits`
  stores only their explicit IDs and normalized integer caps, sorted by ID;
  otherwise that field is omitted. With a filter, `trim.include_streams` or
  `trim.exclude_streams` stores the explicit IDs as a sorted list; without that
  filter its field is omitted. `truncated` is true if this trim omits any records
  **or** the source was already truncated. Excluding a stream with records counts
  as truncation; excluding only an empty schema does not. `source_exit_code` is null because no
  tap command runs. Repeating the same trim on the same source produces identical
  bytes in all three files, including the manifest, regardless of override order
  or leading zeros (and regardless of inclusion/exclusion order); output paths are not stored.
  Source scrub descriptions are preserved as provenance, including descriptions
  for excluded streams; this is a reproduction filter, not a privacy scrub.
  With `--compact-states`, `trim.compact_states` is `true`; otherwise that field
  is omitted. Removing duplicate states alone does not set `truncated`.
  With `--max-records`, `trim.max_records` stores the normalized integer;
  otherwise that field is omitted. A global maximum at or above the eligible
  record count does not itself set `truncated`.
  With `--drop-empty-streams`, `trim.drop_empty_streams` is `true`; otherwise that
  field is omitted. Dropping an already schema-only stream does not set `truncated`.
  With `--final-state-only`, `trim.final_state_only` is `true`; otherwise that
  field is omitted. Removing source boundaries alone does not set `truncated`.
  With `--state-every`, `trim.state_every` stores the normalized positive integer;
  otherwise the field is omitted. Changing cadence alone does not set `truncated`.
  With `--schema-once`, `trim.schema_once` is `true`; otherwise the field is omitted.
  Removing only repeated schemas does not set `truncated`.

For a keyed `alpha` cassette with `SCHEMA alpha, RECORD id=1, identical SCHEMA
alpha, RECORD id=2, identical SCHEMA alpha`:

```bash
datawrench singer-replay trim /tmp/singer-replay-alpha -o /tmp/singer-replay-alpha-once \
  --per-stream 2 --schema-once --state-every 1
```

The result is `SCHEMA alpha, RECORD id=1, STATE alpha=1, RECORD id=2, STATE alpha=2`:
five messages, `counts: {"alpha": 2}`, `truncated: false`. The trailing suppressed
schema adds no checkpoint; the source remains unchanged.

To checkpoint every two retained records in the shop reproduction:

```bash
datawrench singer-replay trim /tmp/singer-replay-cassette -o /tmp/singer-replay-cadence \
  --per-stream 2 --state-every 2
```

This contains both schemas, customer **1**, product **DEMO-1**, a January 1
customer checkpoint, customer **2**, then a final January 2 customer checkpoint:
seven messages with counts `{"customers":2,"products":1}`. No source checkpoint
is copied, and the source remains unchanged.

To keep one checkpoint at the end of the same shop reproduction:

```bash
datawrench singer-replay trim /tmp/singer-replay-cassette -o /tmp/singer-replay-final-only \
  --per-stream 1 --final-state-only
```

The cassette contains five messages: both schemas, customer **1**, product
**DEMO-1**, then one STATE with the January 1 customer bookmark shown above.
The source is unchanged. At zero caps, both schemas remain followed by one empty STATE;
combining zero caps with `--drop-empty-streams` instead rejects the all-empty result.

For example, using the same shop cassette:

```bash
datawrench singer-replay trim /tmp/singer-replay-cassette -o /tmp/singer-replay-products-only \
  --per-stream 2 --stream-limit customers=0 --stream-limit products=1
```

The result retains only product **DEMO-1**, both schemas and the original STATE
boundaries. Every checkpoint has `{"bookmarks":{}}`: no customer record survives,
and products has no replication key. The manifest counts are
`{"customers":0,"products":1}`, with `truncated: true`. Overrides affect record
caps; schemas remain available to discovery.

To omit the customer schema as well, use the inclusion filter instead:

```bash
datawrench singer-replay trim /tmp/singer-replay-cassette -o /tmp/singer-replay-products-subset \
  --per-stream 1 --include-stream products
```

This retains only the products schema and **DEMO-1**, with counts
`{"products":1}` and empty bookmarks (products is a full-table stream).
Discovery lists only products. All source checkpoint boundaries remain, and
validation still covers the entire source, including excluded records.

To also reduce repeated rebuilt checkpoints:

```bash
datawrench singer-replay trim /tmp/singer-replay-cassette -o /tmp/singer-replay-products-compact \
  --per-stream 1 --include-stream products --compact-states
```

For the bundled shop this yields exactly the products SCHEMA, product **DEMO-1**
and one empty STATE (`message_count: 3`), with the same counts and catalog as the
products subset above. For a keyed stream with records 1 and 2 and a STATE after
each, a cap of 1 similarly keeps record 1 and a single checkpoint at key 1.
Compaction never skips full-source validation or changes source files.

To bound the total across streams as well, using the same shop cassette:

```bash
datawrench singer-replay trim /tmp/singer-replay-cassette -o /tmp/singer-replay-total-two \
  --per-stream 2 --max-records 2 --compact-states
```

This retains customer **1** followed by product **DEMO-1**, both schemas, counts
`{"customers":1,"products":1}` and the final source-time customer bookmark at
`2024-01-01T12:00:00Z`. Products has
no bookmark, and `truncated` is true. A global zero still validates the entire
source, including records in excluded streams, before creating output.

To also omit schemas left empty by that total bound:

```bash
datawrench singer-replay trim /tmp/singer-replay-cassette -o /tmp/singer-replay-one-nonempty \
  --per-stream 2 --max-records 1 --drop-empty-streams --compact-states
```

This retains only the customers schema and customer **1**, with counts
`{"customers":1}`, the source-time customer bookmark at `2024-01-01T12:00:00Z`,
`message_count: 3` and `truncated: true`. Products has no schema, count or bookmark.
The option makes an extra scan to determine which streams retain records before
writing schemas; it keeps stream IDs in memory without buffering records.

For cassettes with many streams and only a few unwanted ones, exclude those IDs:

```bash
datawrench singer-replay trim /tmp/singer-replay-cassette -o /tmp/singer-replay-without-customers \
  --per-stream 1 --exclude-stream customers --compact-states
```

For the bundled shop this also yields the products SCHEMA, product **DEMO-1**
and one empty STATE, with counts `{"products":1}` and `message_count: 3`.
Its manifest records `trim.exclude_streams: ["customers"]` instead of inclusion
provenance. Repeat `--exclude-stream` to omit more IDs. Excluded records and
checkpoints still undergo full-source validation, even with a zero cap.

## Scrubbing and its boundaries

`--scrub STREAM:PATH=hash|mask|drop` addresses an explicit object path, for example
`--scrub customers:profile.phone=mask`. The colon separates the stream from the
dot-separated property names, so dotted stream names such as `shop.customers`
remain unambiguous. Each component must match `[A-Za-z0-9_-]+`; paths have 1–32
components. For literal dots, Unicode or other property-name characters, use
`--scrub 'STREAM:/JSON/Pointer=MODE'` instead. Both explicit forms traverse
declared object properties only, without wildcard or expression evaluation.
The original `STREAM.FIELD=MODE` syntax still addresses a **top-level field**,
with the final dot separating stream and field. Objects/arrays may be scrubbed
as a whole with any syntax.

For example, record the bundled fixture with:

```bash
datawrench singer-replay record -o ./nested-cassette \
  --scrub customers:profile.phone=mask -- python tests/singer_replay/fixtures/fake_tap.py
```

Its `{"profile":{"phone":"555-0100","city":"Demo City"}}` becomes
`{"profile":{"phone":"***","city":"Demo City"}}`; the sibling city is retained.

For a source containing literal property names, use:

```bash
datawrench singer-replay record -o ./literal-cassette \
  --scrub 'audit.customers:/profile.info/電話~1番号=mask' \
  --scrub 'audit.customers:/a~0b=drop' < literal-source.jsonl
```

Given a matching schema, the record
`{"profile.info":{"電話/番号":"555-0100","city":"Demo City"},"a~b":"private"}`
becomes `{"profile.info":{"電話/番号":"***","city":"Demo City"}}`.
The dot in `profile.info` is literal; `/profile/info` would address two separate
properties. JSON Pointer tokens decode `~1` to `/` and `~0` to `~` exactly once
(`~01` names the literal `~1`). Unicode is matched exactly, without normalization.
The last `=` separates the mode, so property names may also contain `=` or `:`.

- Pointer paths require 1–32 **nonempty** tokens. Root/empty-name pointers, invalid
  escapes such as `~2` or trailing `~`, and URI fragment form `#/...` are refused.
  Percent escapes are not decoded; `%2F` names the literal `%2F` property.
- Every token is an exact object-property name: `/0` may name a declared property
  `"0"`, but cannot index an array. Tokens such as `*`, `[0]` or `__call__()` have
  no wildcard, indexing or expression behavior; they match only such literal
  declared names. Unknown names fail rather than expanding or executing.
- Decoded paths share key protection and duplicate/ancestor overlap checks with
  both older syntaxes. For example, `customers.email=hash` conflicts with
  `customers:/email=mask`, and `customers:profile=drop` conflicts with
  `customers:/profile/phone=mask`. Manifest `path` arrays store decoded names.

- `hash`: HMAC-SHA256 over compact UTF-8 JSON of `[stream, path, value]`, with sorted
  object keys. For nested paths, `path` is an array such as `["profile","phone"]`;
  top-level rules retain the original field-string domain and hashes.
- `mask`: non-null values become `"***"`.
- `drop`: remove the leaf from records, its parent's schema properties and `required`.

Null stays null for hash/mask. Hash/mask replace that property's schema with
`{"type":["string","null"]}`, removing its old examples/defaults/format.
Explicit paths also remove `examples`/`default` from ancestor object schemas,
including the root, because those annotations can contain the raw leaf value.
Other sibling schemas are retained. Primary and replication keys, including any
path starting at such a key, cannot be scrubbed. Unknown streams/fields, duplicate
rules (including aliases) and overlapping ancestor/descendant rules cause an error.

Every explicit path must exist in the schema **and in each recorded record**.
Intermediate schemas must be objects or object/null unions, and intermediate
record values must be non-null objects. Missing optional parents/leaves, null
parents, scalar traversal and array traversal return explicit errors and leave
`complete: false`; replay refuses the cassette. A null **leaf** stays null for
hash/mask. Legacy top-level rules retain their behavior of skipping absent
optional record fields. No matching rule is silently skipped for an unknown schema.

By default a random 32-byte salt exists only in memory. Equal values in one
stream/field get the same hash within that recording. For stable hashes across
recordings, supply the **same** private 32–4096 byte `--salt-file`; neither salt
nor command arguments are stored in the cassette. For example:

```bash
python -c 'import os; from pathlib import Path; p=Path(".demo-salt"); p.write_bytes(os.urandom(32)); p.chmod(0o600)'
datawrench singer-replay record -o ./stable-cassette --salt-file .demo-salt \
  --scrub customers.email=hash -- python tests/singer_replay/fixtures/fake_tap.py
```

Keep salt files outside version control. **Hashing is not anonymization**: hashes
preserve equality, and knowledge of the salt permits testing guessed inputs.
Only named record fields, their property schemas and the explicit-path ancestor
annotations described above are transformed. Other fields, names, free-text
schema descriptions and bookmark values remain as supplied; legacy rules also
retain ancestor examples/defaults. Inspect schemas and choose sensitive fields yourself.
Opaque source state is deliberately refused. Errors do not print input values.

## Configure replay

```json
{
  "cassette_path": "/tmp/singer-replay-cassette",
  "time_shift": "now",
  "state_message_frequency": 100,
  "delay_ms": 0,
  "fail_after_records": null,
  "fail_exit_code": 17
}
```

| Setting | Default and behavior |
| --- | --- |
| `cassette_path` | Required local directory; never a URL. |
| `time_shift` | `false`. `true` or `"now"` shifts the newest schema-declared `date-time` field across the entire cassette to current UTC. A fixed RFC3339 timestamp instead of `"now"` makes replay reproducible. |
| `state_message_frequency` | `1000` emitted RECORDs. `0` disables periodic states; recorded boundaries and final STATE remain. |
| `delay_ms` | `0`; integer 0–60000, applied only before each emitted RECORD. |
| `fail_after_records` | `null` disables. Nonnegative integer; after exactly N emitted records, write a checkpoint and exit with `fail_exit_code`. `0` fails before records. If fewer than N records are emitted, replay completes normally. |
| `fail_exit_code` | `1`; integer 1–125, used only for injected failure. |

All schema-declared date-time fields, including nested objects/arrays, and
`time_extracted` shift by the same delta. The anchor excludes `time_extracted`.
With selection or state filtering, the newest **emitted** record need not reach
the anchor because the anchor is computed from the whole cassette. Missing
date-time fields or timestamp overflow is an error. Timestamps require explicit
timezones and at most six fractional digits. Numeric timezone offsets require
hours 00–23 and minutes 00–59; offsets such as `+00:60` are rejected in records,
extraction timestamps, bookmarks and fixed replay anchors, before output.

## Catalog selection and checkpoints

Without `--catalog`, all streams/properties are selected. With a catalog, omitted
streams are excluded. Use root `metadata` with `breadcrumb: []` and
`selected: false` to deselect a stream; `selected-by-default` is the fallback
when `selected` is absent. Properties default to selected and inherit exclusion
from their parent. For example:

```json
{"streams":[{"tap_stream_id":"customers","metadata":[
  {"breadcrumb":[],"metadata":{"selected":true}},
  {"breadcrumb":["properties","profile","properties","phone"],"metadata":{"selected":false}},
  {"breadcrumb":["properties","tags","items","properties","internal"],"metadata":{"selected":false}}
]}]}
```

Property projection updates nested schemas and `required` together with records.
Primary and replication keys are automatic and retained with a notice if
explicitly deselected. Unknown stream IDs produce a warning and are skipped.
Unknown breadcrumbs, duplicate entries, nonboolean flags and attempts to change
schemas, keys, inclusion or replication method produce errors.

A state file contains the **value** of a STATE message, not its wrapper:

```json
{"bookmarks":{"customers":{"replication_key":"updated_at","replication_key_value":"2024-01-01T12:00:00Z"}}}
```

Records at or below the supplied bookmark are skipped. String keys compare
lexicographically, numbers numerically, date-time keys by UTC instant. Replication
values must be **strictly increasing per stream** and cannot be null. Repeated
values are explicitly refused: a single `<=` bookmark cannot safely resume in the
middle of a tie after fault injection. Full-table streams always replay in full;
a supplied bookmark for such a stream is ignored with a notice.

Recorded STATE boundaries are re-emitted using current safe bookmarks. Periodic
and final states describe only selected keyed streams and never advance beyond
an emitted record or regress the supplied state. A final duplicate is avoided
when the last message already contains the final state. **Bookmarks always use
original cassette time**, even when output timestamps shift, so the same state
can resume a later `time_shift: "now"` invocation. Persist only states acknowledged
by your target; full-table streams can produce duplicates when restarted.

## Real target-jsonl and Meltano

The optional loader is old and pins old dependencies, so install it into its own
isolated environment (it is never a dependency of datawrench; the standalone project
kept a separate `integration/` uv project, pinned to Python 3.11, just for this):

```bash
uv venv --python 3.11 .venv-target
uv pip install --python .venv-target/bin/python target-jsonl==0.1.4
printf '%s\n' '{"destination_path":"./target-output","do_timestamp_file":false}' > target-config.json
set -o pipefail
datawrench singer-replay tap --config examples/singer_replay/config.json |
  .venv-target/bin/target-jsonl --config target-config.json
```

This writes four records to `target-output/customers.jsonl` and
`target-output/products.jsonl`; target stdout is the final state value. The target
appends to existing files, so use a fresh destination when comparing results.
Its published package does not declare a license; see [THIRD_PARTY.md](../THIRD_PARTY.md)
before redistributing it. It is not part of this tool's installation.

[examples/singer_replay/meltano.yml](../examples/singer_replay/meltano.yml) declares a
custom extractor and loader. Meltano runs an extractor as one executable followed by
the Singer arguments (`--config`, `--discover`, `--catalog`, `--state`, `--about`), so
the extractor's `executable` is the two-line wrapper
[examples/singer_replay/tap-replay](../examples/singer_replay/tap-replay), which runs
`exec datawrench singer-replay tap "$@"`. The extractor has no `pip_url`: install
datawrench yourself so that `datawrench` is on `PATH`. With Meltano already installed,
from the datawrench checkout root:

```bash
meltano --cwd examples/singer_replay install loader target-jsonl
meltano --cwd examples/singer_replay run tap-replay target-jsonl
```

The configuration is provided for local use; Meltano itself was not installed or
executed in the standalone project's acceptance run, and the wrapper-based extractor
definition has not been run under Meltano either. The tap-to-target pipeline is
exercised by pytest when `target-jsonl` is available (see below).

## Test and reproduce

From a datawrench checkout:

```bash
uv run --extra test pytest tests/singer_replay -q
```

To also run the optional real-loader test, put an isolated `target-jsonl` on `PATH`
(for example the `.venv-target` environment above):

```bash
PATH="$PWD/.venv-target/bin:$PATH" uv run --extra test pytest tests/singer_replay -q
```

Tests are single-process and use synthetic fixtures, temporary local directories
and no APIs. CLI tests run the command as `python -m datawrench singer-replay ...`
and `python -m datawrench singer-replay tap ...`; the recorder tests also execute the
synthetic tap `tests/singer_replay/fixtures/fake_tap.py` as a subprocess. Without
`target-jsonl` on `PATH`, exactly that integration test reports a documented skip;
tests never install or fetch it themselves. Assertions cover message round-trip equality, discovery,
nested/stream selection, bookmarks, explicit nested scrub paths and failures,
JSON Pointer literal dots/Unicode/slashes/tildes, strict escapes, cross-syntax
hash compatibility and decoded-path protection/overlaps,
deterministic scrubbing/no raw field bytes, RFC3339 offset boundaries,
time arithmetic, fault checkpoints, delays, checksum/gzip corruption, schema
errors, invalid catalogs/state, tap failures, permissions and path boundaries.
Trim tests cover two interleaved keyed streams, failure/resume at every cut point,
safe rebuilt checkpoints, mixed zero/nonzero stream overrides, inherited defaults,
zero/exact/oversize caps, duplicate/unknown/malformed overrides, source immutability,
identical artifacts regardless of override order, preserved scrubbing, full-table
behavior and invalid/corrupt inputs. Inclusion-filter tests cover selected schemas,
records, catalogs and counts, excluded-stream checkpoints and overrides, schema-only
streams, final-state insertion, inclusion-order determinism and full-source validation.
Compaction tests cover adjacent rebuilt duplicates, retained schema/record barriers,
excluded-message adjacency, distinct checkpoints, default compatibility, strict
boolean/CLI validation, schema-only output and source-time failure/resume under
different time-shift anchors.
Exclusion tests cover exact-ID subtraction, inclusion conflicts, exclusion of all
schemas, excluded-stream overrides, zero caps, schema-only remainders, sorted
provenance, option-order determinism, full-source validation and safe keyed resume.
Global-cap tests cover selection/cap ordering, zero/exact/oversize totals, late
schemas, source-time checkpoints at every keyed failure cut, full-table resume,
strict API/CLI bounds, inherited truncation, deterministic provenance, unchanged
defaults and full-source validation/destination guards even at zero.
Empty-stream tests cover removal after filters and all caps, repeated/late schemas,
all-empty rejection before output creation, post-removal state adjacency, safe
source-time keyed resume, full-table repeat, strict boolean API validation,
default artifact compatibility, deterministic provenance and full-source preflight.
Cadence tests cover exact/partial intervals, retained-record selection, trailing
schemas, schema-only/full-table output, original-time checkpoints, every keyed
failure/resume cut with changed time anchors, strict intervals and option conflicts,
unchanged defaults, deterministic artifacts, whole-source preflight and output bounds.
Schema-once tests cover repeated/late first schemas, schema-only input, post-suppression
state adjacency and cadence, selection/cap composition, source-time resume at every
retained cut under changed anchors, full-table repeat, unchanged false artifacts,
true-only provenance, strict booleans, preflight and destination guards.
Verification covers actual message counts, late/repeated/
zero-record schemas, inherited truncation and scrub provenance, corruption through
the end of the source, member types, size limits and read-only behavior.
Offline scrub tests (`tests/singer_replay/test_rescrub.py`) cover legacy, dotted and JSON Pointer
mask/drop rules through the CLI and Python API; invalid, duplicate, overlapping and
protected-key rules; whole-source corruption before output; destination guards for
existing paths, source aliases, nesting, dangling and self-loop symlinks; explicit
missing or null-parent paths leaving `complete: false`; exact versus one-byte-over
output bounds; keyed resume after a scrub; and chained scrubs that keep inherited
provenance and source checksums.

## Supported scope and limits

- Uppercase SCHEMA, RECORD, STATE only; streams must match
  `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`. This also prevents path-like stream names
  from reaching file-based targets. No BATCH, ACTIVATE_VERSION, LOG or schema-less
  records; repeated identical schemas are allowed, schema evolution is refused.
- Explicitly typed object schemas with declared properties; nested objects,
  arrays/items, null unions and scalar primary keys are supported. Supported
  schema keywords: `type`, `properties`, `required`, `items`,
  `additionalProperties` (false only), `format`, `description`, `title`, `$schema`,
  `examples`, `default`. Unknown keywords such as `$ref`, `oneOf`, `pattern` or
  numeric constraints are refused. Records with undeclared fields are refused
  even when `additionalProperties` is omitted. Only `date-time` formats are
  validated beyond type; other formats are annotations. No remote schema fetches.
- One non-null scalar replication key per stream, strictly increasing values;
  no composite/partitioned state, opaque source state or bookmarks beyond recorded
  data. STATE accepts only the documented `bookmarks` shape. Replay output may add
  final checkpoints or replace source STATE values with safe current bookmarks.
- UTF-8 JSON with unique keys and finite numbers; 16 MiB per JSON file/message,
  1 GiB per cassette member and uncompressed message stream; schema/record nesting
  depth at most 32. The recorder streams records but retains schemas in memory;
  replay preflights the entire cassette and makes extra scans for time shifting.
- Scrub paths traverse declared object fields only, with the colon/dot or JSON
  Pointer syntax above; no traversal into arrays, wildcard expansion or expression
  evaluation. No privacy claim for unrelated fields or free-text schema
  annotations. Checksums are not signatures; concurrent mutation of an actively
  read local file is outside scope.
- No source API calls by this tool, Airbyte/Docker support, dry-run workflow mode,
  load-testing benchmark, external service mutation or automatic publication.
  A supplied tap command is trusted user code; the tool does not sandbox it.

## License

The standalone `singer-replay` project was licensed under Apache-2.0; datawrench's
license is Apache-2.0 too ([`../LICENSE`](../LICENSE)); the standalone project's NOTICE is
merged into [`../NOTICE`](../NOTICE).
Sources, design references and the optional loader's licensing are in
[`../THIRD_PARTY.md`](../THIRD_PARTY.md).
