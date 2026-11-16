# Demo data repository for `datawrench check`

Everything in this folder is **synthetic**: a made-up `shop` dbt project for
the README demo and the tests. No warehouse, tap or dbt run produced it, and
nothing in it is ever executed by `check`.

| Path | What it stands for |
| --- | --- |
| `dbt_project.yml`, `models/` | A dbt project; `orders` has an enforced contract, both models use Jinja |
| `seeds/zip_codes.csv` | A seed with leading-zero ZIP codes and no `column_types` pin |
| `target/manifest.json` | The manifest of this branch (hand-trimmed): `orders.customer_id` is now `integer`, `coupon_code` is gone |
| `prod/manifest.json` | The manifest of production, to pass as `--base-manifest` |
| `pipelines/daily_revenue.py` | A Polars pipeline with an eager read turned lazy, `with_columns` in a loop and `sort().head()` |
| `singer/orders.jsonl` | Singer tap output: a record without its primary key, a wrongly typed field, no final STATE |
| `db/migrations/`, `db/schema.sql` | dbmate-style DuckDB migrations (duckmate) and their schema snapshot: one creates a temporary table, one has an empty down section |

```sh
cd examples/check
datawrench check --base-manifest prod/manifest.json
```

It needs the extras for the checks that find something here
(`pipx install 'datawrench[all]'`). Without `--base-manifest`, contract-diff
only notes that `orders` has an enforced contract to compare.

`tests/check/test_cli.py::test_readme_demo_output` pins the key lines of the output.
