# key-audit with other CSV tools

`datawrench key-audit` is the narrow collision-review step in a broader CSV
workflow. Keep conversion, filtering, and ordinary grouping in the existing
tools. Review keys before any upstream command that trims, normalizes or
deduplicates them; otherwise the original distinctions are already gone.

## Miller 6.22.0

Verified with its official macOS arm64 release binary. If your source is
already CSV, audit it directly. Miller's `--csv cat` is also a useful format
boundary for supported inputs; the regression suite contains freshly generated
Miller exports with quoted newlines and UTF-8 values.

```sh
datawrench key-audit audit examples/key_audit/customers.csv --key tenant --key customer_id \
  --rule width --rule remove-zwsp --rule nfc --rule strip --report review.json
# Findings return 1; inspect before continuing.
datawrench key-audit show review.json
datawrench key-audit export-map examples/key_audit/customers.csv --review review.json \
  --output collisions.csv

mlr --icsv --ocsv stats1 -a sum -f original_rows -g collision_id collisions.csv
```

The mapping has one row per *original distinct key*, so `original_rows` carries
its original multiplicity. There are four collision IDs in this example and
their row totals are 3, 2, 2 and 2. Use `original_key_json` and
`normalized_key_json` to retain exact composite-key boundaries and all invisible
characters. This is review evidence; it is not an automatic merge plan.

## csvkit 2.2.0

Verified with the release installed locally. csvkit can select columns,
convert spreadsheet formats, reformat delimiters, and query the complete map:

```sh
csvsql -I -y 0 --query \
  'select collision_id, sum(cast(original_rows as integer)) as affected_rows from collisions group by collision_id' \
  collisions.csv

csvformat -D ';' examples/key_audit/customers.csv > semicolon.csv
datawrench key-audit audit semicolon.csv --delimiter ';' --key tenant --key customer_id \
  --rule nfc --rule strip --report semicolon-review.json
```

`-I -y 0` disables inference and delimiter sniffing for csvsql. Parsed key values
must stay strings; inferred numbers can destroy leading zeros before review.
The semicolon CSV fixture in the test suite was produced by the real csvformat
CLI. Always audit the exact file later passed to `clean`: reformatting it after
review changes its source hash even if parsed cells happen to stay the same.

## OpenRefine

Documentation/source review only; this recipe was **not run**. OpenRefine
already provides custom key-collision clustering and human merge review. Use
that workflow when interactive faceting/reconciliation is the desired task.
For this command's source-bound batch step, first export the uncleaned project
to CSV, then audit and clean that exact exported file. Do not assume GREL
`normalize()` is Unicode NFC: the documented operation transliterates extended
western characters. Its fingerprint defaults also erase punctuation and token
order, which `key-audit` never silently does.

## qsv and Uneff

Source/documentation review only, not runtime-verified. Both can remain around
this narrow audit step, but do not first run a transformation that erases key
differences and then expect to recover the lost original identities.
`key-audit` does not consume OpenRefine project archives, qsv binary indices,
or Uneff mapping files. The surveyed coverage of each tool is summarised in
[THIRD_PARTY.md](../../THIRD_PARTY.md#key-audit-formerly-unicode-key-audit--keyaudit).
