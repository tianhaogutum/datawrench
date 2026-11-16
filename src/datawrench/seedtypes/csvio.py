"""Bounded, strict UTF-8 RFC-4180 CSV input with physical source positions."""
from __future__ import annotations

import csv
import io
from pathlib import Path
from dataclasses import dataclass


class SeedtypesError(ValueError):
    pass


MAX_FIELD = 1_048_576
MAX_COLUMNS = 256
MAX_BATCH_BYTES = 4_194_304
BATCH_ROWS = 1024
MAX_RECORD_BYTES = 9_000_000
csv.field_size_limit(MAX_FIELD)
DELIMITERS = {'comma': ',', 'tab': '\t', 'semicolon': ';'}
INPUT_SUFFIXES = ('.csv', '.tsv')


def validate_delimiter(delimiter: str) -> str:
    if delimiter not in DELIMITERS.values():
        raise SeedtypesError('delimiter must be comma (","), tab ("\\t"), or semicolon (";")')
    return delimiter


def local_csv(path: str | Path) -> Path:
    path = Path(path)
    if any(p.is_symlink() for p in (path.absolute(), *path.absolute().parents)) or not path.is_file() or path.suffix.lower() not in INPUT_SUFFIXES:
        raise SeedtypesError(f'{path}: expected a regular local .csv or .tsv file (no symlinks)')
    if any(c in str(path.resolve()) for c in '*?[]'):
        raise SeedtypesError(f'{path}: glob metacharacters in CSV paths are unsupported')
    return path.resolve()


def discover(inputs: list[str]) -> list[Path]:
    found: set[Path] = set()
    for name in inputs:
        path = Path(name)
        if path.is_dir() and not path.is_symlink():
            for item in sorted(path.rglob('*')):
                if item.suffix.lower() in INPUT_SUFFIXES:
                    # Do not follow a symlinked file or directory, including ancestors.
                    if any(p.is_symlink() for p in (item, *(path / p for p in item.relative_to(path).parents))):
                        raise SeedtypesError(f'{item}: symlinks are not supported')
                    found.add(local_csv(item))
        else:
            found.add(local_csv(path))
    if not found:
        raise SeedtypesError('no CSV files found')
    return sorted(found)


@dataclass(frozen=True)
class Row:
    line: int
    values: list[str]


class LimitedLines:
    """Bound physical reads and accumulated multiline records before csv allocates fields."""
    def __init__(self, stream, path):
        self.stream = stream
        self.path = path
        self.record_bytes = 0
        self.line = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline(MAX_RECORD_BYTES + 1)
        if not line:
            raise StopIteration
        self.line += 1
        self.record_bytes += len(line.encode('utf-8'))
        if self.record_bytes > MAX_RECORD_BYTES:
            raise SeedtypesError(f'{self.path}: line {self.line}: CSV record exceeds {MAX_RECORD_BYTES} bytes')
        return line


class CSVSource:
    def __init__(self, path: str | Path, *, delimiter: str = ','):
        self.delimiter = validate_delimiter(delimiter)
        self.path = local_csv(path)
        self.signature = self._signature()
        self.headers: list[str] = []
        self.empty = False

    def _signature(self):
        s = self.path.stat()
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns

    def unchanged(self):
        if self.signature != self._signature():
            raise SeedtypesError(f'{self.path}: input changed during scan; retry with a stable file')

    def batches(self):
        """Open once; caller must consume to exhaustion for validation of the entire file."""
        self.unchanged()
        start = 1
        try:
            with self.path.open(encoding='utf-8-sig', newline='') as stream:
                lines = LimitedLines(stream, self.path)
                reader = csv.reader(lines, dialect='excel', delimiter=self.delimiter, strict=True)
                try:
                    self.headers = next(reader)
                except StopIteration:
                    self.empty = True
                    return
                if not self.headers or any(not x or '\x00' in x for x in self.headers):
                    raise SeedtypesError(f'{self.path}: line 1: empty or NUL-containing header')
                if any('\n' in x or '\r' in x for x in self.headers):
                    raise SeedtypesError(f'{self.path}: line 1: multiline column names are unsupported')
                if sum(len(x.encode('utf-8')) for x in self.headers) > MAX_BATCH_BYTES:
                    raise SeedtypesError(f'{self.path}: line 1: header exceeds {MAX_BATCH_BYTES} bytes')
                if len(self.headers) > MAX_COLUMNS:
                    raise SeedtypesError(f'{self.path}: line 1: more than {MAX_COLUMNS} columns')
                if len({x.casefold() for x in self.headers}) != len(self.headers):
                    raise SeedtypesError(f'{self.path}: line 1: duplicate headers (case insensitive)')
                batch: list[Row] = []
                size = 0
                while True:
                    start = reader.line_num + 1
                    lines.record_bytes = 0
                    try:
                        values = next(reader)
                    except StopIteration:
                        break
                    if len(values) != len(self.headers):
                        raise SeedtypesError(f'{self.path}: line {start}: ragged row: expected '
                                             f'{len(self.headers)} fields, got {len(values)}')
                    if any('\x00' in x for x in values):
                        raise SeedtypesError(f'{self.path}: line {start}: NUL bytes are unsupported')
                    row_size = sum(len(x.encode('utf-8')) for x in values)
                    if row_size > MAX_BATCH_BYTES:
                        raise SeedtypesError(f'{self.path}: line {start}: row exceeds {MAX_BATCH_BYTES} bytes')
                    if batch and (size + row_size > MAX_BATCH_BYTES or len(batch) >= BATCH_ROWS):
                        yield batch
                        batch, size = [], 0
                    batch.append(Row(start, values))
                    size += row_size
                if batch:
                    yield batch
        except UnicodeDecodeError as exc:
            raise SeedtypesError(f'{self.path}: non-UTF-8 input; only UTF-8 (optional BOM) is supported') from exc
        except csv.Error as exc:
            raise SeedtypesError(f'{self.path}: line {start}: malformed CSV: {exc}') from exc
        finally:
            self.unchanged()


def batch_csv(rows: list[Row]) -> io.StringIO:
    buf = io.StringIO(newline='')
    writer = csv.writer(buf, lineterminator='\r\n')
    # A simple synthetic header makes DuckDB detect the record terminator
    # before it sees any embedded newline inside a quoted data field.
    writer.writerow([f'c{i}' for i in range(len(rows[0].values))])
    writer.writerows(row.values for row in rows)
    buf.seek(0)
    return buf
