"""Generate CSV and YAML with real upstream pandas and Pandera writers."""

from pathlib import Path

import pandas as pd
import pandera.pandas as pa


def make(root: Path):
    frame = pd.DataFrame(
        {
            "order_id": list(range(1, 257)),
            "amount": [round(10 + (i % 23) / 10, 2) for i in range(256)],
            "note": [f"order {i}" for i in range(256)],
        }
    )
    frame.loc[0, "amount"] = -4.5
    frame.loc[200, "order_id"] = frame.loc[47, "order_id"]
    frame.loc[47, "note"] = 'Customer said "please, keep this"\nsecond line: café'
    frame.to_csv(root / "orders.csv", index=False, lineterminator="\r\n")
    schema = pa.DataFrameSchema(
        {
            "order_id": pa.Column(int, unique=True),
            "amount": pa.Column(float, pa.Check.ge(0)),
            "note": pa.Column(str),
        },
        strict=True,
    )
    schema.to_yaml(root / "orders.yaml")


if __name__ == "__main__":
    make(Path(__file__).resolve().parent)
