# Jaffle Shop fixtures

Downloaded without edits from dbt-labs/jaffle_shop at commit
`fd7bfacae4f497ff044a6a0275268676bf1b64c3` on 2026-11-11.
`provenance.json` contains exact source URLs and SHA256 hashes. The upstream
Apache 2.0 license is included as `LICENSE`.

These are the original raw customer/order/payment CSV seeds, three staging
models, and the customer/order mart models. Tests use Jinja2 to render the
actual upstream SQL (`ref` resolves to the local table name), including the
payment-method loop. They load the actual CSVs with DuckDB and create staging
views from the rendered upstream staging SQL. This is not a full dbt run.
