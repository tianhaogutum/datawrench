import csv
import io

import pytest

from datawrench.failurepack.table import CsvTable, InputError


def test_large_free_text_field_within_input_ceiling_is_valid(tmp_path):
    source = tmp_path / "notes.csv"
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows([["id", "note"], ["1", "large note: " + "x" * 200_000]])
    source.write_text(stream.getvalue())
    previous_limit = csv.field_size_limit()
    table = CsvTable.scan(source)
    assert table.rows == 1
    assert csv.field_size_limit() == previous_limit
    subset = tmp_path / "subset.csv"
    table.write_subset([0], subset)
    assert subset.read_bytes() == source.read_bytes()


def test_whitespace_only_physical_record_is_not_silently_skipped(tmp_path):
    source = tmp_path / "spaces.csv"
    source.write_text("name\n   \nvalid\n")
    with pytest.raises(InputError, match="blank"):
        CsvTable.scan(source)


def test_quoted_whitespace_is_a_real_cell(tmp_path):
    source = tmp_path / "spaces.csv"
    source.write_text('name\n"   "\nvalid\n')
    assert CsvTable.scan(source).rows == 2


def test_csv_field_limit_restored_after_invalid_input(tmp_path):
    source = tmp_path / "invalid.csv"
    source.write_text('name\n"unclosed\n')
    previous_limit = csv.field_size_limit()
    with pytest.raises(InputError):
        CsvTable.scan(source)
    assert csv.field_size_limit() == previous_limit


def test_record_and_column_limits_are_independent_of_byte_limit(tmp_path):
    source = tmp_path / "narrow.csv"
    source.write_text("a\n1\n2\n3\n")
    with pytest.raises(InputError, match="row limit"):
        CsvTable.scan(source, max_rows=2)
    source.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(InputError, match="column limit"):
        CsvTable.scan(source, max_columns=2)
