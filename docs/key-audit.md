# datawrench key-audit

`datawrench key-audit` (formerly `keyaudit`, the `unicode-key-audit` project)
previews Unicode cleanup **before distinct CSV join keys become the same key**.
Use it before you trim, NFC/NFKC-normalize, width-fold or casefold the key
columns of a CSV that feeds a join or a primary key: it separates duplicates
that were already in the source from collisions the chosen rules would
introduce, explains the changed code points, and only then exports a cleaned
CSV with the rules you explicitly confirm. It never removes rows.

It is a thin wrapper around Python's CSV parser, Unicode database, SQLite and
diff library, and ftfy's character-width fixer. OpenRefine already provides
interactive clustering; Miller, csvkit and qsv already provide broad CSV
operations. This command adds the narrower preflight and source-bound
selective export step. See [THIRD_PARTY.md](../THIRD_PARTY.md#key-audit-formerly-unicode-key-audit--keyaudit)
for the surveyed prior art and reused components.

## Install

Python 3.11+ with SQLite 3.35+ (normal current Python builds). The only
third-party runtime dependency is ftfy:

```sh
pipx install 'datawrench[ftfy]'    # or: pip install 'datawrench[ftfy]'
datawrench key-audit --version     # prints the bare datawrench version, e.g. 0.1.0
datawrench key-audit rules         # list the finite cleanup rules
```

`python -m datawrench key-audit` is equivalent. No service or server is used.
The subcommands are `rules`, `audit`, `show`, `clean` and `export-map`. Errors
are printed as `datawrench key-audit: <message>`.

## Quick start

From the repository root, create a new review file:

```sh
datawrench key-audit audit examples/key_audit/customers.csv \
  --key tenant --key customer_id \
  --rule width --rule remove-zwsp --rule nfc --rule strip \
  --report review.json
# Exit 1 means findings were found; review.json was still written successfully.

datawrench key-audit show review.json
```

The example contains 12 records, one existing duplicate group, and four new
collision groups. For instance, two original `001` rows are an existing
duplicate; a third `001　` key introduces a new collision after `strip`.
Width conversion and zero-width removal precede NFC so newly adjacent
characters can compose before the final whitespace trim.
The report identifies `U+3000 IDEOGRAPHIC SPACE` and the affected record numbers.
`tenant` is part of the composite key, so the US `001` stays a different key.

After reviewing the evidence, explicitly select which proposed rules to apply:

```sh
datawrench key-audit clean examples/key_audit/customers.csv --review review.json \
  --confirm nfc --confirm strip --output cleaned.csv
# Exit 1: these selected rules still introduce collisions; no CSV is created.

datawrench key-audit clean examples/key_audit/customers.csv --review review.json \
  --confirm nfc --confirm strip --accept-collisions --output cleaned.csv \
  > cleaned.receipt.json
```

Only `nfc` and `strip` run in that export. `width` and `remove-zwsp` remain
unconfirmed. Selection retains the reviewed rule order, regardless of flag order.
The selected subset is audited again against the whole input before writing.
This matters even if the full proposal has no collisions: `strip` followed by
`remove-zwsp` keeps `"x \\u200b"` and `"x "` distinct, but selecting only
`remove-zwsp` merges them.
`--accept-collisions` acknowledges key mergers; it does not deduplicate records.
If a nonempty key component would become empty, `--accept-empty` is also required.
Existing duplicates and already-empty keys do not themselves block export.

The receipt on stdout includes selected rules, complete counts, the review ID,
and source/output SHA-256 hashes. Review metadata binds the exact source bytes,
CSV settings, key columns, rule order, and Python implementation/patch version,
Unicode data version, and ftfy version.
Changed input or a different export runtime requires a new audit; `show` can
still display evidence from the older runtime. The review checksum
detects accidental metadata edits; it is not a digital signature or access control.

The review file format is unchanged from the standalone tool: its `schema` is
`keyaudit.review/v1`, so reviews written by `keyaudit` 0.1.0 are still read
(as long as the source and runtime match).

## Explicit finite rules

No transformation is enabled by default. Rules apply only to the chosen key
columns; headers and all other cell values remain unchanged.

| Rule | Exact operation |
| --- | --- |
| `nfc` | `unicodedata.normalize("NFC", value)` |
| `nfkc` | Compatibility normalization; e.g. `¹` becomes `1` |
| `width` | ftfy `fix_character_width`, including U+3000 → ASCII space |
| `strip` | Python Unicode `str.strip()` |
| `collapse-whitespace` | `" ".join(value.split())`; also trims edges |
| `casefold` | Locale-independent Unicode full case folding; e.g. `ß` → `ss` |
| `remove-zwsp` | Remove only U+200B |
| `remove-bom` | Remove only U+FEFF inside a key |

ZWJ, ZWNJ, bidi controls, accents and punctuation are not generically deleted.
Order is literal: halfwidth `ｶﾞ` reaches the same key as `ガ` under `width`
followed by `nfc`; `nfc` before `width` leaves a decomposed result. No implicit
normalization pass is added after your selected sequence.
The UTF-8 BOM at the *start of the file* is consumed by the default
`utf-8-sig` decoder; embedded U+FEFF stays data unless its rule is confirmed.
Every rule can change meaning in some identifier systems; select deliberately.

## Formats and evidence

- Repeat `--key` for composite keys; names match the header exactly. Column
  names containing commas need no special syntax beyond shell quoting.
- Comma delimiter, double-quote quoting, strict UTF-8 decoding by default.
  Use `--delimiter TAB` for TSV or `--delimiter ';'` for semicolon files.
  `--encoding` accepts `utf-8-sig` (default), `utf-8`, `utf-16`, `utf-16-le`,
  `utf-16-be` and `cp1252`.
- `--quotechar`, `--escapechar`, and `--no-doublequote` support explicit dialects.
  Disabling double quotes requires an escape character so transformations can
  introduce quote characters without creating an unexportable value.
  Delimiter, quote, and escape characters must differ and be Unicode scalars.
  There is no delimiter guessing, type inference, or numeric coercion.
- Quoted commas, multiline fields, CRLF, empty fields, NUL data, and a missing
  final newline are supported by the standard CSV library. Blank records,
  inconsistent row widths, duplicate/empty headers, and decoding errors fail.
- `clean` writes the reviewed delimiter/quote dialect, UTF-8 (optionally
  `--output-encoding utf-8-sig`), and CRLF record separators. Parsed cell
  contents are preserved except confirmed key changes; quote style, BOM,
  encoding and record separators are not byte-preserved.
- The JSON review includes per-rule cumulative cardinalities, original duplicate
  groups, new collision groups, original multiplicity, record/physical-line
  references, final character differences, and per-rule change explanations.
- Code-point edits include Unicode names/categories. JSON and terminal output
  escape hidden/control characters; indices refer to code points, not graphemes.

Details are bounded to 100 example groups/changed keys, 20 variants per group,
and five row references per original key by default. `--groups`, `--variants`,
and `--samples` adjust those limits (1–1000). Omission counts are explicit;
**all exact counts and export guards always include the entire input**.
Text examples show at most 160 code points. Equal prefixes/suffixes are removed
before diffing; an unmatched span above 512 code points omits the diff to avoid
quadratic work. Small changes in longer keys still get absolute character indices.
Grouping always uses the full original key.
Each diff shows at most 32 edit operations and 32 code points per edited span.
A shared 4 MiB compact evidence budget bounds the complete sampled report;
its exhaustion and all omissions are reported. JSON output is capped at 32 MiB
so every successfully written review is also readable by the command.

For complete collision membership, including full-length keys omitted from
the sampled report, stream a separate mapping CSV:

```sh
datawrench key-audit export-map examples/key_audit/customers.csv --review review.json \
  --output collisions.csv
# Or --scope changed / --scope all for other complete mappings.
```

This new file has one row per distinct original key, JSON-encoded original and
normalized tuples, original row count, a stable collision ID, and the first
record/physical-line reference. It is always comma-delimited UTF-8 and retains
full keys without truncation. It revalidates the source and reviewed engine,
and makes no changes to the input. Miller and csvkit can consume it directly;
see [key-audit/integrations.md](key-audit/integrations.md).

## Local resources and failure behavior

Rows stream into a disposable disk-backed SQLite index. The whole table is
not loaded into a DataFrame. A streamed source snapshot ensures the audited and
exported bytes match even if a producer later changes the input file. Temporary
files are removed on success, handled errors, SIGINT, and SIGTERM. Snapshotting
reads at most the source's initial size and rejects concurrent mutation, so
an appending producer cannot make the copy chase an ever-growing file.
Use `--work-dir EXISTING_DIRECTORY` to choose a disk with sufficient space.
SQLite cache is limited to 8 MiB; disk use grows with distinct keys and stages.

Defaults limit a field to 1 Mi code points (`--max-field-chars`), a CSV record
to 8 Mi (`--max-record-chars`), a key component to 4096 including each
intermediate rule result (`--max-key-chars`), a table to 1024 columns, and a
composite key to 32 columns. The character limits can be raised up to 64 Mi.
Outputs are staged next to their destination and atomically published without
overwriting any existing file, including the source.

As a rough guide (one macOS arm64 measurement with Python 3.12 and ftfy 6.3.1,
not a throughput guarantee): auditing a 500,000-row, 15 MB CSV with quoted
multiline notes, writing the review and exporting every colliding key took about
11 seconds with a peak RSS of about 60 MiB and roughly 235 MB of temporary disk
space, deleted at exit.

Exit codes: `0` success/no new findings; `1` new audit findings or blocked export;
`2` invalid input/review/I/O failure (and usage errors); `130` SIGINT; `141`
closed output pipe; `143` SIGTERM. An audit returning 1 still writes a valid
review. `show` returns 0 for a valid report with findings.

## Tests

The tests in `tests/key_audit/` use checked-in real Miller v6.22.0 CSV
fixtures and fresh Miller-produced exports; provenance and hashes are in
[tests/key_audit/fixtures/PROVENANCE.md](../tests/key_audit/fixtures/PROVENANCE.md).
They also check composite-key grouping against a deterministic independent
counting oracle, Unicode variants, selected-rule replay, export
refusal and `--accept-*` overrides, corruption, encoding/dialect variants, unchanged non-key
values, and atomic file protection. The suite includes 15 licensed rows of the
Unicode 15.0 normalization conformance corpus.

## Limits

This reviews exact string keys within one CSV. It does not infer which keys
represent the same entity, inspect both sides of a join, emulate a database
collation, or detect confusable glyphs. Empty strings are literal keys, not SQL
NULL. Locale-specific lowercasing, arbitrary regex/code transformations, Excel
workbooks, compressed input, and stdin are outside the current scope.
The review exposes key samples; keep it with the source's access controls.
Atomic publication requires a filesystem that supports same-directory hard
links. macOS arm64 was exercised; other operating systems were not tested.

`key-audit` is not part of `datawrench check`: it needs you to name the key
columns and the cleanup rules, which cannot be inferred from file content.
