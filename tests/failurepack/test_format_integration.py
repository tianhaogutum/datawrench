import pytest

# Optional `pandera` extra (pandas, Pandera and dfshrink, which needs Python 3.12+);
# skip this module cleanly when it is not installed.
pytest.importorskip("pandas")
pytest.importorskip("pandera")
pytest.importorskip("dfshrink")

import pandas as pd
import pandera.pandas as pa

from datawrench.failurepack.bundle import list_failures, reduce_to_bundle


@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", [False, True])
def test_real_pandas_csv_dialects_preserve_the_selected_failure(tmp_path, delimiter, newline, bom):
    frame = pd.DataFrame(
        {
            "id": list(range(20)),
            "amount": [10.5] * 20,
            "note": [f"row {number}" for number in range(20)],
        }
    )
    frame.loc[17, "id"] = 5
    frame.loc[0, "amount"] = -4.5
    frame.loc[5, "note"] = '包含 "quoted, text"\nsecond line'
    data, schema = tmp_path / "input.csv", tmp_path / "schema.yaml"
    frame.to_csv(
        data,
        sep=delimiter,
        lineterminator=newline,
        encoding="utf-8-sig" if bom else "utf-8",
        index=False,
    )
    pa.DataFrameSchema(
        {
            "id": pa.Column(int, unique=True),
            "amount": pa.Column(float, pa.Check.ge(0)),
            "note": pa.Column(str),
        }
    ).to_yaml(schema)
    failures = list_failures(data, schema, delimiter, {}, 10, 32 * 1024 * 1024)
    selected = next(f["id"] for f in failures if f["check"] == "field_uniqueness")
    output = tmp_path / "bundle"
    manifest = reduce_to_bundle(data, schema, output, selected, delimiter=delimiter)
    assert manifest["result"]["source_record_ranges"] == [[6, 6], [18, 18]]
    assert manifest["reduction"]["one_minimal"] is True
    result = pd.read_csv(output / "data.csv", sep=delimiter, encoding="utf-8-sig")
    pd.testing.assert_frame_equal(result, frame.iloc[[5, 17]].reset_index(drop=True))
