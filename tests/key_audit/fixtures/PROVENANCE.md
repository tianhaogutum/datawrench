# Fixture provenance

`upstream/miller/*.csv` comes from John Kerl's Miller v6.22.0 repository.
`manifest.json` records every original URL and SHA-256. Original bytes were
fetched on 2026-11-11; files named `mlr-output-*` were freshly produced by the
official Miller 6.22.0 macOS arm64 binary with `mlr --csv cat INPUT`.
Its downloaded archive SHA-256 was
`b99af86bf902c4fd269c28ec4142111dc27815b3ae63801baceaa001d09b9fd7`.

The upstream UTF-8, quoted commas, quoted newlines, unterminated-final-line,
and quoted numeric-string variants exercise real upstream CSV formats.
Despite their upstream `*-truncated.csv` names, those two files are valid
CSV records that simply lack a trailing newline, so they must be accepted.

Miller is MIT licensed; see `upstream/miller/LICENSE.txt`.

`examples/key_audit/customers.csv` is a synthetic CRM-style export produced using the
real Python 3.12 `csv.writer` in Excel dialect with a UTF-8 BOM. It contains
one pre-existing duplicate, canonical/compatibility variants, U+3000, U+200B,
a composite tenant key, quoted multiline data, an internal BOM in a non-key
cell, and an emoji ZWJ sequence. Tests also derive NFD variants from the actual
Miller UTF-8 fixture and generate dialect/encoding exports with `csv.writer`.

Additional real Miller fixtures include BOM-prefixed plain and quoted headers,
CRLF records with doubled quotes, and its named line-ending fixture (whose
fetched bytes use LF despite the filename). Exact hashes remain authoritative.
`upstream/csvkit/csvkit-output-semicolon.csv` was produced by the real csvkit
2.2.0 `csvformat -D ';' examples/key_audit/customers.csv` command, not a manual fixture.

`upstream/unicode/NormalizationTest-sample.txt` contains 15 unchanged rows
selected from the Unicode 15.0.0 normalization conformance corpus. Its header
records the official URL and original SHA-256, and the Unicode license is
included beside it. The offline suite generates actual CSV from its original
forms and normative expected values across several scripts. The full 2.6 MB
corpus was also checked once with a separate development script; it is not
vendored. This tests the audit's grouping, duplicate distinction and CSV replay
against independent normative expectations, not a new normalizer.
