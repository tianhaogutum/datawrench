"""CSV handling and atomic local files; format parsing stays in Python's csv."""

from __future__ import annotations

import csv
import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, TextIO


class AuditError(ValueError):
    """An actionable input or review error."""


class LimitedWriter:
    def __init__(self, target: TextIO, limit: int):
        self.target = target
        self.limit = limit
        self.written = 0

    def write(self, text: str):
        self.written += len(text.encode("utf-8"))
        if self.written > self.limit:
            raise AuditError(
                "review exceeds 32 MiB; use smaller --groups/--variants/--samples limits"
            )
        return self.target.write(text)


@dataclass(frozen=True)
class CSVOptions:
    delimiter: str = ","
    encoding: str = "utf-8-sig"
    quotechar: str = '"'
    escapechar: str | None = None
    doublequote: bool = True
    max_field_chars: int = 1_048_576
    max_record_chars: int = 8_388_608
    max_key_chars: int = 4096

    def __post_init__(self):
        for label, value in (("delimiter", self.delimiter), ("quotechar", self.quotechar)):
            if (
                not isinstance(value, str)
                or len(value) != 1
                or value in "\r\n\0"
                or 0xD800 <= ord(value) <= 0xDFFF
            ):
                raise AuditError(f"{label} must be one Unicode scalar other than CR, LF or NUL")
        if self.delimiter == self.quotechar:
            raise AuditError("delimiter and quotechar must differ")
        if self.escapechar is not None and (
            not isinstance(self.escapechar, str)
            or len(self.escapechar) != 1
            or self.escapechar in "\r\n\0"
            or 0xD800 <= ord(self.escapechar) <= 0xDFFF
        ):
            raise AuditError("escapechar must be one Unicode scalar other than CR, LF or NUL")
        if self.escapechar in (self.delimiter, self.quotechar):
            raise AuditError(
                "escapechar must differ from delimiter and quotechar for reversible CSV"
            )
        if self.encoding not in (
            "utf-8-sig",
            "utf-8",
            "utf-16",
            "utf-16-le",
            "utf-16-be",
            "cp1252",
        ):
            raise AuditError("unsupported encoding; use utf-8[-sig], utf-16[-le/-be], or cp1252")
        if not isinstance(self.doublequote, bool):
            raise AuditError("doublequote must be boolean")
        if not self.doublequote and self.escapechar is None:
            raise AuditError(
                "--no-doublequote requires --escapechar so every transformed value can be exported"
            )
        for name in ("max_field_chars", "max_record_chars", "max_key_chars"):
            n = getattr(self, name)
            if type(n) is not int or n < 1 or n > 64 * 1024 * 1024:
                raise AuditError(f"{name} must be an integer between 1 and 67108864")
        if self.max_key_chars > self.max_field_chars:
            raise AuditError("max_key_chars must not exceed max_field_chars")

    def as_dict(self) -> dict:
        return asdict(self)

    def dialect(self) -> dict:
        return {
            "delimiter": self.delimiter,
            "quotechar": self.quotechar,
            "escapechar": self.escapechar,
            "doublequote": self.doublequote,
        }


class BoundedLines:
    def __init__(self, source: TextIO, limit: int):
        self.source = source
        self.limit = limit
        self.consumed = 0

    def __iter__(self):
        return self

    def __next__(self):
        remaining = self.limit - self.consumed
        line = self.source.readline(remaining + 1)
        if not line:
            raise StopIteration
        self.consumed += len(line)
        if self.consumed > self.limit:
            raise AuditError(f"CSV record exceeds max_record_chars={self.limit}")
        return line


class CSVRecords:
    def __init__(self, source: TextIO, options: CSVOptions):
        self.lines = BoundedLines(source, options.max_record_chars)
        self.reader = csv.reader(self.lines, strict=True, **options.dialect())
        self.header = self._next_row()
        if self.header is None or not self.header:
            raise AuditError("CSV must contain a nonempty header row")
        if len(self.header) > 1024:
            raise AuditError("CSV exceeds 1024 columns")
        if any(not name for name in self.header):
            raise AuditError("CSV header contains an empty column name")
        if len(set(self.header)) != len(self.header):
            raise AuditError("CSV header contains duplicate column names")

    def _next_row(self) -> list[str] | None:
        self.lines.consumed = 0
        try:
            return next(self.reader, None)
        except csv.Error as e:
            raise AuditError(f"invalid CSV near physical line {self.reader.line_num}: {e}") from e

    def __iter__(self) -> Iterator[tuple[int, int, list[str]]]:
        record = 0
        while True:
            start_line = self.reader.line_num + 1
            row = self._next_row()
            if row is None:
                return
            record += 1
            if len(row) != len(self.header):
                raise AuditError(
                    f"data record {record} (physical line {start_line}) has {len(row)} "
                    f"fields; expected {len(self.header)}"
                )
            yield record, start_line, row


@contextmanager
def open_records(path: Path, options: CSVOptions):
    old_limit = csv.field_size_limit(options.max_field_chars)
    try:
        with path.open("r", encoding=options.encoding, newline="") as source:
            yield CSVRecords(source, options)
    except UnicodeError as e:
        raise AuditError(f"cannot decode CSV as {options.encoding}: {e}") from e
    finally:
        csv.field_size_limit(old_limit)


def snapshot_source(source: Path, destination: Path) -> dict:
    # O_NONBLOCK lets us reject FIFOs/devices without ever waiting for a writer.
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    digest = hashlib.sha256()
    total = 0
    with os.fdopen(fd, "rb") as src:
        before = os.fstat(src.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise AuditError("input must be a regular file, not a pipe or device")
        with destination.open("xb") as dst:
            # Do not chase a producer that keeps appending faster than we copy.
            while total < before.st_size:
                chunk = src.read(min(1024 * 1024, before.st_size - total))
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                dst.write(chunk)
        after = os.fstat(src.fileno())
        if total != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise AuditError("input changed during snapshot; retry after the producer finishes")
    return {"path": str(source.resolve()), "sha256": digest.hexdigest(), "bytes": total}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def new_atomic_file(destination: Path, *, encoding: str = "utf-8"):
    """Publish a complete file without overwriting any existing target."""
    require_new_output(destination)
    fd, name = tempfile.mkstemp(prefix=".keyaudit-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as target:
            yield target, temporary
            target.flush()
            os.fsync(target.fileno())
        try:
            # Same-directory hard link is atomic, and fails if target appeared.
            os.link(temporary, destination)
        except FileExistsError as e:
            raise AuditError(f"output already exists: {str(destination)!r}") from e
    finally:
        temporary.unlink(missing_ok=True)


def require_new_output(destination: Path):
    if destination.exists() or destination.is_symlink():
        raise AuditError(f"output already exists: {str(destination)!r}")
    if not destination.parent.is_dir():
        raise AuditError(
            f"output parent must be an existing directory: {str(destination.parent)!r}"
        )
