# Retained SARIF validation schema

`sarif-schema-2.1.0.json` is an unmodified copy of the official OASIS
SARIF 2.1.0 plus Errata 01 schema, retrieved 2026-11-11 from:

https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json

SHA-256: `c3b4bb2d6093897483348925aaa73af03b3e3f4bd4ca38cef26dcb4212a2682e`

The copyright and Notices section is retained in `OASIS-NOTICE.txt` from:

https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/sarif-v2.1.0-errata01-os-complete.html

This third-party schema is governed by that notice, separately from the MIT
license for this project's implementation and synthetic SQL fixtures. It is
used locally by the tests with jsonschema's Draft4Validator and URI format
checking; no schema download or remote validator is used during tests/audits.
It is not needed or bundled in the runtime wheel.
