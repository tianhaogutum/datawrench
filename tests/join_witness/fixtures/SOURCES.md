# Fixture provenance

- `tableproof/{samples,results,merged}.tsv` are unchanged copies of
  `examples/data/` from https://github.com/suguangliang3083-jpg/tableproof at
  `c4758ec3d9884338758555d98adba275209b5722`, retrieved 2026-11-11 with a shallow
  clone. The adjacent `tableproof/LICENSE` preserves the MIT notice. They were
  run through the installed TableProof 0.1.1 CLI before integration work.
- `examples/join_witness/orders.csv` and `customers.csv` are synthetic sales data
  written with actual Polars 1.36.1 `DataFrame.write_csv`, not manually serialized
  pseudo-files. Two customer history records reproduce a common accidental
  many-to-one violation. Expected amounts are 35.00 before and 55.00 after a
  left join, with 20.00 added through repetition.
- `src/datawrench/join_witness/data/{orders,customers}.csv` are byte-identical packaged
  copies of those two Polars-produced demo exports. Tests enforce their identity.
- Temporary CSV/TSV files created by tests use real Polars writers, DuckDB COPY,
  or Python's standard CSV writer (for malformed/adversarial format cases).
  Their generating code and seed identify their origin; no private data is used.

## Northwind integration data

`northwind/{orders,customers}.csv` are Polars 1.36.1 exports from the public
Northwind-SQLite3 sample database:

- https://github.com/jpwhite3/northwind-SQLite3
- Frozen source commit: `4f56e7f5906dfd23b25244c5bfe8fb5da6402efd`.
- Download: `https://raw.githubusercontent.com/jpwhite3/northwind-SQLite3/4f56e7f5906dfd23b25244c5bfe8fb5da6402efd/dist/northwind.db`.
- Database size: 24,702,976 bytes (below the 50 MB download cap).
- Database SHA-256: `2f4f5c68dfcd33ba27373eae48c7a4869800c68095ee0f9f0da494f83382a877`.
- Read using Python `sqlite3` in read-only mode; ordered query results were
  serialized with `polars.DataFrame(...).write_csv(...)`. SQLite's text cast
  preserves the source's numeric representation for exact decimal tracing.
- This source includes extra/edited records compared with older Northwind
  releases. Do not assume canonical dataset totals. The customer extract retains
  all 93 source records, including missing country values and unusual names.
- Adjacent `northwind/LICENSE` is the source repository's MIT license.

Exact extraction queries:

```sql
SELECT OrderID, CustomerID, CAST(Freight AS TEXT) AS Freight, ShipCountry
FROM Orders WHERE OrderID BETWEEN 10248 AND 11077 ORDER BY OrderID;
SELECT CustomerID, CompanyName, Country FROM Customers ORDER BY CustomerID;
```

Export fingerprints:

| File | Rows | SHA-256 |
| --- | ---: | --- |
| orders.csv | 830 | `0e16d78a25f6daa42f503b8c73b9aebd64afc169f13a9c56f4c61b63e287d186` |
| customers.csv | 93 | `600eae66ff0de286367f4edd5a95d25dd38406386d88b7fb73259c994f79b9d2` |
