"""Lint fixture: do not run; paths and input frames are illustrative."""
import polars as pl


def pipeline(path):
    query = pl.read_csv(path).lazy()
    df = pl.DataFrame({"a": ["first", "second"]})
    return query, df.select(pl.col("a").map_elements(lambda s: s.upper()))
