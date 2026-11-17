"""Index CSV records with the standard library; copy original bytes on deletion."""

import csv
import sys
from array import array
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class InputError(ValueError):
    """An unsupported or invalid input, rather than a validation failure."""


@contextmanager
def _field_limit(max_bytes):
    previous = csv.field_size_limit()
    csv.field_size_limit(min(max_bytes, sys.maxsize))
    try:
        yield
    finally:
        csv.field_size_limit(previous)


class _Lines:
    def __init__(self, stream):
        self.stream = stream
        self.offset = 0
        self.first = True
        self.nonspace = False

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline()
        if not line:
            raise StopIteration
        self.offset += len(line.encode("utf-8"))
        if self.first:
            self.first = False
            line = line.removeprefix("\ufeff")
        self.nonspace = self.nonspace or bool(line.strip())
        return line


@dataclass
class CsvTable:
    path: Path
    boundaries: array
    columns: list[str]

    @property
    def rows(self) -> int:
        return len(self.boundaries) - 1

    @classmethod
    def scan(
        cls,
        path: Path,
        delimiter: str = ",",
        max_bytes: int = 32 * 1024 * 1024,
        *,
        max_rows: int = 250_000,
        max_columns: int = 2048,
    ):
        if len(delimiter) != 1 or delimiter in '\r\n"\0':
            raise InputError("delimiter must be one character other than quote, newline or NUL")
        if max_bytes < 1 or path.stat().st_size > max_bytes:
            raise InputError(f"input exceeds the {max_bytes}-byte limit")
        if max_rows < 1 or max_columns < 1:
            raise InputError("max-rows and max-columns must be positive")
        boundaries = array("Q")
        try:
            with _field_limit(max_bytes), path.open(encoding="utf-8", newline="") as stream:
                lines = _Lines(stream)
                reader = csv.reader(lines, delimiter=delimiter, strict=True)
                header = next(reader, None)
                if not header or any(not name for name in header):
                    raise InputError("CSV must have a non-empty header with named columns")
                if len(header) > max_columns:
                    raise InputError(f"CSV exceeds the {max_columns}-column limit")
                if len(set(header)) != len(header):
                    raise InputError("duplicate CSV column names are unsupported")
                if any("\0" in name for name in header):
                    raise InputError("NUL bytes are unsupported")
                boundaries.append(lines.offset)
                lines.nonspace = False
                for row in reader:
                    if len(boundaries) > max_rows:
                        raise InputError(f"CSV exceeds the {max_rows}-row limit")
                    if not lines.nonspace:
                        raise InputError(f"blank CSV record {len(boundaries)} is unsupported")
                    if len(row) != len(header):
                        raise InputError(
                            f"CSV record {len(boundaries)} has {len(row)} fields; "
                            f"expected {len(header)} (blank records are unsupported)"
                        )
                    if any("\0" in value for value in row):
                        raise InputError(f"NUL byte in CSV record {len(boundaries)}")
                    boundaries.append(lines.offset)
                    lines.nonspace = False
        except (UnicodeError, csv.Error) as exc:
            raise InputError(f"invalid UTF-8 CSV: {exc}") from exc
        return cls(path, boundaries, header)

    def write_subset(self, positions: Iterable[int], destination: Path) -> None:
        """Preserve header, cell bytes, record order, and physical line endings."""
        with self.path.open("rb") as source, destination.open("wb") as output:
            self._copy_span(source, output, 0, self.boundaries[0])
            previous = -1
            span_start = span_end = None
            for position in positions:
                if position <= previous or not 0 <= position < self.rows:
                    raise InputError("row positions must be unique, increasing, and in range")
                start, end = self.boundaries[position], self.boundaries[position + 1]
                if span_end == start:
                    span_end = end
                else:
                    if span_start is not None:
                        self._copy_span(source, output, span_start, span_end)
                    span_start, span_end = start, end
                previous = position
            if span_start is not None:
                self._copy_span(source, output, span_start, span_end)

    @staticmethod
    def _copy_span(source, output, start: int, end: int) -> None:
        source.seek(start)
        remaining = end - start
        while remaining:
            block = source.read(min(remaining, 1024 * 1024))
            if not block:
                raise InputError("source CSV changed while copying records")
            output.write(block)
            remaining -= len(block)
