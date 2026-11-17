"""Fixed rules and original bytes must stay fixed even if schema code writes files."""

import pytest


# Optional `pandera` extra (pandas, Pandera and dfshrink, which needs Python 3.12+);
# skip this module cleanly when it is not installed.
pytest.importorskip("pandas")
pytest.importorskip("pandera")
pytest.importorskip("dfshrink")

from datawrench.failurepack.bundle import list_failures, reduce_to_bundle
from datawrench.failurepack.validator import ValidationError


@pytest.mark.parametrize("mutate", ["schema", "source"])
def test_self_modifying_validation_does_not_publish_changed_rules_or_data(tmp_path, mutate):
    data, schema = tmp_path / "input.csv", tmp_path / "schema.py"
    data.write_text("id,note\n1,row\n2,row\n2,row\n3,row\n")
    mutation = (
        "with Path(__file__).open('a') as stream: stream.write('# changed by validation\\n')\n"
        if mutate == "schema"
        else "source=Path(__file__).with_name('source.csv')\n"
        "if source.exists(): source.write_bytes(source.read_bytes().replace(b'row',b'ROW'))\n"
    )
    schema.write_text(
        "from pathlib import Path\nimport pandera.pandas as pa\n"
        + mutation
        + "schema=pa.DataFrameSchema({'id':pa.Column(int,unique=True),'note':pa.Column(str)})\n"
    )
    failures = list_failures(data, schema, ",", {}, 10, 32 * 1024 * 1024)
    selected = next(f["id"] for f in failures if f["check"] == "field_uniqueness")
    original_data, original_schema = data.read_bytes(), schema.read_bytes()
    with pytest.raises(ValidationError, match="changed"):
        reduce_to_bundle(data, schema, tmp_path / "output", selected)
    assert data.read_bytes() == original_data
    assert schema.read_bytes() == original_schema
    assert not (tmp_path / "output").exists()
