import polars as pl

query = pl.read_csv("tests/polars_vet/fixtures/people.csv").lazy()
