"""Synthetic demo pipeline for `datawrench check`. It is linted, never run."""
import polars as pl


def daily_revenue(path, currencies):
    orders = pl.read_csv(path).lazy()
    frame = orders.collect()
    for code, rate in currencies.items():
        frame = frame.with_columns((pl.col("amount") * rate).alias(f"amount_{code}"))
    return frame.sort("amount", descending=True).head(10)
