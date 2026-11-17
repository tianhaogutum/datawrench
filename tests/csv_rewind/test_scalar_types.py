"""Replay genuine typed COPY output, including engine-specific scalar extremes."""

import duckdb
import pytest

from datawrench.csv_rewind import AuditOptions, audit
from datawrench.csv_rewind.audit import sql_value


@pytest.mark.parametrize(
    ("candidate", "value"),
    [
        ("BOOLEAN", "true"),
        ("TINYINT", "-128"),
        ("UTINYINT", "255"),
        ("SMALLINT", "-32768"),
        ("USMALLINT", "65535"),
        ("INTEGER", "-2147483648"),
        ("UINTEGER", "4294967295"),
        ("BIGINT", "-9223372036854775808"),
        ("UBIGINT", "18446744073709551615"),
        ("HUGEINT", "170141183460469231731687303715884105727"),
        ("UHUGEINT", "340282366920938463463374607431768211455"),
        ("DECIMAL(38,6)", "12345678901234567890.123456"),
        ("FLOAT", "1.25"),
        ("DOUBLE", "1.25"),
        ("DOUBLE", "nan"),
        ("DOUBLE", "inf"),
        ("DATE", "0001-01-01"),
        ("DATE", "9999-12-31"),
        ("DATE", "infinity"),
        ("DATE", "-infinity"),
        ("DATE", "0001-01-01 (BC)"),
        ("TIME", "23:59:59.123456"),
        ("TIMESTAMP", "2024-02-29 01:02:03.123456"),
        ("TIMESTAMP_S", "2024-02-29 01:02:03"),
        ("TIMESTAMP_MS", "2024-02-29 01:02:03.123"),
        ("TIMESTAMP_NS", "2024-02-29 01:02:03.123456789"),
        ("TIMESTAMPTZ", "2024-02-29 01:02:03+00"),
    ],
)
def test_typed_copy_roundtrips_canonically(tmp_path, candidate, value):
    source = tmp_path / "typed.csv"
    with duckdb.connect(config={"threads": 1, "memory_limit": "128MB"}) as connection:
        connection.execute("SET TimeZone='UTC'")
        connection.execute(
            f"COPY (SELECT CAST({sql_value(value)} AS {candidate}) AS value) "
            f"TO {sql_value(str(source))} (FORMAT CSV, HEADER true)"
        )
    report = audit(source, AuditOptions(header=True, types={"value": candidate}, null_values=()))
    assert report["lossless"], report["examples"]
    assert report["frozen"]["verified_rows"] == 1


@pytest.mark.parametrize(
    ("raw", "candidate", "reason"),
    [
        ("1.234", "DECIMAL(4,2)", "numeric_value_change"),
        ("1e400", "DOUBLE", "numeric_value_change"),
        ("1e-400", "DOUBLE", "numeric_value_change"),
        ("128", "TINYINT", "parse_error"),
        ("-1", "UBIGINT", "parse_error"),
        ("2024-02-30", "DATE", "parse_error"),
        ("2024-01-01T00:00:00.123456789", "TIMESTAMP", "temporal_format_change"),
    ],
)
def test_loss_reasons_at_numeric_and_temporal_boundaries(tmp_path, raw, candidate, reason):
    source = tmp_path / "boundary.csv"
    source.write_text("value\n" + raw + "\n")
    report = audit(source, AuditOptions(header=True, types={"value": candidate}))
    assert report["examples"][0]["reason"] == reason
    assert report["columns"][0]["recommended_type"] == "VARCHAR"
