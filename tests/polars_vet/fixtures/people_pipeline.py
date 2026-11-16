"""Executable offline fixture; run beside people.csv."""
import polars as pl

query = pl.read_csv("people.csv").lazy()
result = query.filter(pl.col("score") >= 20).select("id", "name", "score").collect()

if __name__ == "__main__":
    print(result.write_json())
