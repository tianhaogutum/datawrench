"""Seeded format generation checks deletion against independent writer output."""

import csv
import io
import random

from datawrench.failurepack.table import CsvTable


def encode_record(record, delimiter, newline):
    stream = io.StringIO(newline="")
    csv.writer(
        stream,
        delimiter=delimiter,
        lineterminator=newline,
        quoting=csv.QUOTE_ALL,
    ).writerow(record)
    return stream.getvalue().encode("utf-8")


def test_one_thousand_generated_csv_subsequences(tmp_path):
    rng = random.Random(39417)
    alphabet = 'abcXYZ0123 ,;|"\t\n\r café漢字🙂'
    source, destination = tmp_path / "source.csv", tmp_path / "selected.csv"
    for case in range(1000):
        width, count = rng.randint(1, 6), rng.randint(0, 12)
        delimiter = rng.choice([",", ";", "\t", "|"])
        newline = rng.choice(["\n", "\r\n", "\r"])
        header = [f"column_{number}" for number in range(width)]
        records = [
            ["".join(rng.choices(alphabet, k=rng.randint(0, 30))) for _ in range(width)]
            for _ in range(count)
        ]
        parts = [encode_record(row, delimiter, newline) for row in [header, *records]]
        if rng.choice([False, True]):
            parts[0] = b"\xef\xbb\xbf" + parts[0]
        if rng.choice([False, True]):
            parts[-1] = parts[-1].removesuffix(newline.encode())
        source.write_bytes(b"".join(parts))
        selected = [row for row in range(count) if rng.random() < 0.5]
        table = CsvTable.scan(source, delimiter)
        assert table.rows == count, f"case {case}"
        table.write_subset(selected, destination)
        assert destination.read_bytes() == b"".join(
            [parts[0], *(parts[position + 1] for position in selected)]
        ), f"case {case}"
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            assert list(csv.reader(stream, delimiter=delimiter)) == [
                header,
                *(records[position] for position in selected),
            ], f"case {case}"
