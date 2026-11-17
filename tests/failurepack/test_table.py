import csv
import io

import pytest

from datawrench.failurepack.table import CsvTable, InputError


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
@pytest.mark.parametrize("bom", [False, True])
def test_record_deletion_preserves_exact_bytes(tmp_path, newline, delimiter, bom):
    records = [
        ["key", "note"],
        ["001", 'quoted "text", café\nsecond line'],
        ["002", "discard me"],
        ["003", "終わり"],
    ]
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter, lineterminator=newline)
    writer.writerows(records)
    raw = (b"\xef\xbb\xbf" if bom else b"") + output.getvalue().encode()
    # A real csv.writer fixture, with the last line ending deliberately removed.
    raw = raw.removesuffix(newline.encode())
    source = tmp_path / "source.csv"
    source.write_bytes(raw)
    table = CsvTable.scan(source, delimiter)
    assert table.rows == 3
    destination = tmp_path / "subset.csv"
    table.write_subset([0, 2], destination)
    expected = raw[: table.boundaries[1]] + raw[table.boundaries[2] :]
    assert destination.read_bytes() == expected
    with destination.open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.reader(stream, delimiter=delimiter)) == [records[0], records[1], records[3]]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"a,a\n1,2\n",
        b"a,b\n1\n",
        b'a,b\n"unclosed,1\n',
        b"a,b\n1,2\n\n",
        b"a,b\n1,\xff\n",
        b"a,b\n1,\x00\n",
        b",b\n1,2\n",
    ],
)
def test_invalid_csv_is_rejected(tmp_path, raw):
    source = tmp_path / "source.csv"
    source.write_bytes(raw)
    with pytest.raises(InputError):
        CsvTable.scan(source)


def test_header_only_and_invalid_positions(tmp_path):
    source = tmp_path / "source.csv"
    source.write_bytes(b"a,b")
    table = CsvTable.scan(source)
    assert table.rows == 0
    table.write_subset([], tmp_path / "empty.csv")
    assert (tmp_path / "empty.csv").read_bytes() == b"a,b"
    with pytest.raises(InputError):
        table.write_subset([0], tmp_path / "invalid.csv")


def test_input_size_ceiling(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("a\n1\n")
    with pytest.raises(InputError, match="limit"):
        CsvTable.scan(source, max_bytes=3)
