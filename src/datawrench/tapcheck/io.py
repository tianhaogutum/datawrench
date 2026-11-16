"""Bounded JSONL reader, strict JSON decoder and protocol identification."""
from __future__ import annotations

import json
import math
from io import BufferedReader, RawIOBase
from dataclasses import dataclass, asdict
from typing import BinaryIO, Iterator, Any

MAX_LINE_BYTES = 8 * 1024 * 1024


@dataclass
class Diagnostic:
    code: str
    message: str
    line: int = 0
    stream: str | None = None
    source: str = "<stdin>"
    path: list[str | int] | None = None

    def as_dict(self):
        return asdict(self)


class InputError(Exception):
    def __init__(self, message: str, code: str = "TC021", *, line: int = 0):
        super().__init__(message)
        self.code = code
        self.line = line


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object member")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def decode(raw: bytes | str) -> Any:
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)


@dataclass
class ByteBudget:
    """Shared allowance charged for bytes delivered by one or more readers."""
    limit: int
    delivered: int = 0


class RawBudgetReader(RawIOBase):
    """Charge raw bytes before buffering/decoding, with one overflow probe.

    Does not own the underlying handle. Return allowed bytes first so callers
    can evaluate complete buffered rows before the next read detects overflow.
    """
    def __init__(self, handle, budget, message):
        super().__init__()
        self.read_chunk = getattr(handle, "read1", handle.read)
        self.budget = budget
        self.message = message

    def readable(self):
        return True

    def readinto(self, buffer):
        if not len(buffer):
            return 0
        remaining = self.budget.limit - self.budget.delivered
        if remaining < 0:
            raise InputError(self.message, "TC020")
        raw = self.read_chunk(min(len(buffer), remaining) if remaining else 1)
        self.budget.delivered += len(raw)
        if not remaining and raw:
            raise InputError(self.message, "TC020")
        buffer[:len(raw)] = raw
        return len(raw)


class _ReadLimit(RawIOBase):
    """Cap delivered bytes without owning or bypassing the caller's buffer."""
    def __init__(self, handle, allowance, budget=None):
        super().__init__()
        self.read_chunk = getattr(handle, "read1", handle.read)
        self.remaining = allowance
        self.budget = budget

    def readable(self):
        return True

    def readinto(self, buffer):
        if not self.remaining:
            return 0
        # For raw buffered input, read1 also avoids source prefetch. A gzip
        # reader may still buffer compressed bytes and process metadata internally.
        raw = self.read_chunk(min(len(buffer), self.remaining))
        buffer[:len(raw)] = raw
        self.remaining -= len(raw)
        if self.budget is not None:
            self.budget.delivered += len(raw)
        return len(raw)


def lines(handle: BinaryIO, source: str, limit=MAX_LINE_BYTES, *, max_bytes=None,
          byte_budget=None, budget_message="main input exceeds --max-input-bytes budget; remaining input and final STATE coverage not checked") -> Iterator[tuple[int, Any | Diagnostic]]:
    if byte_budget is not None:
        max_bytes = byte_budget.limit - byte_budget.delivered
    if max_bytes is None:
        yield from _lines(handle, source, limit)
    else:
        # One extra byte distinguishes an exact fit from overflow. Closing this
        # adapter leaves the caller's stream open, including stdin.
        with BufferedReader(_ReadLimit(handle, max_bytes + 1, byte_budget)) as bounded:
            yield from _lines(bounded, source, limit, max_bytes, budget_message)


def _lines(handle, source, limit, remaining=None, budget_message=None):
    def read_line(number):
        nonlocal remaining
        size = limit + 1 if remaining is None else min(limit + 1, remaining + 1)
        try:
            raw = handle.readline(size)
        except InputError as exc:
            # A raw BATCH limit can interrupt gzip metadata/read-ahead before
            # a decompressed row exists. Identify the row being requested.
            if not exc.line:
                exc.line = number
            raise
        if remaining is not None:
            remaining -= len(raw)
            if remaining < 0:
                raise InputError(budget_message, "TC020", line=number)
        return raw

    number = 0
    while True:
        raw = read_line(number + 1)
        if not raw:
            break
        number += 1
        if len(raw) > limit:
            # Drain in bounded chunks; never treat a tail as a new JSON message.
            while raw and not raw.endswith(b"\n"):
                raw = read_line(number)
            yield number, Diagnostic("TC020", "line exceeds byte limit", number, source=source)
            continue
        if not raw.strip():
            continue
        try:
            text = raw.decode("utf-8")
            value = decode(text)
            # Python accepts 1e999 as inf even with parse_constant.
            json.dumps(value, allow_nan=False)
            yield number, value
        except (UnicodeError, ValueError, RecursionError):
            # Do not echo record values, state or credentials in diagnostics.
            yield number, Diagnostic("TC001", "invalid UTF-8 or JSON (including duplicate members/non-finite numbers)", number, source=source)


def identify(message: dict) -> str | None:
    kind = message.get("type")
    if not isinstance(kind, str):
        return None
    kind = kind.upper()
    singer = kind in {"SCHEMA", "BATCH", "ACTIVATE_VERSION"}
    airbyte = kind in {"CATALOG", "TRACE", "LOG", "CONTROL", "SPEC", "CONNECTION_STATUS"}
    if kind == "RECORD":
        singer = "stream" in message or "time_extracted" in message
        rec = message.get("record")
        airbyte = isinstance(rec, dict) and "stream" in rec and "data" in rec
    elif kind == "STATE":
        singer, airbyte = "value" in message, "state" in message
    if singer and airbyte:
        raise InputError("ambiguous message; select --protocol singer or airbyte", "TC022")
    return "singer" if singer else "airbyte" if airbyte else None


def dumps(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def open_beneath(root, relative):
    """Open a regular file through directory FDs, refusing symlink races (Linux)."""
    import os
    import stat
    parts = relative.parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise OSError("invalid relative path")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise OSError("not a regular file")
            handle = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        return handle
    finally:
        os.close(directory)
