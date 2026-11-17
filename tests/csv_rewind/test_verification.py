"""The verifier must exercise physical conversion, even with optimizer rewrites."""

import duckdb
import pytest

from datawrench.csv_rewind import AuditError
from datawrench.csv_rewind.audit import _verify_frozen


@pytest.mark.parametrize(
    ("raw", "candidate"),
    [
        ("00123", "BIGINT"),
        ("9007199254740993", "DOUBLE"),
        ("1.9", "INTEGER"),
        ("TRUE", "BOOLEAN"),
    ],
)
def test_verifier_rejects_a_real_unsafe_import_plan(tmp_path, raw, candidate):
    source = tmp_path / "unsafe.csv"
    source.write_text("v\n" + raw + "\n")
    common = {"auto_detect": False, "header": True, "force_not_null": ["v"]}
    raw_parameters = {**common, "columns": {"v": "VARCHAR"}}
    unsafe_parameters = {**common, "columns": {"v": candidate}}
    with duckdb.connect(config={"threads": 1, "memory_limit": "128MB"}) as connection:
        with pytest.raises(AuditError, match="Frozen import changed"):
            _verify_frozen(connection, source, raw_parameters, unsafe_parameters, 1, 64)
