from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from fractions import Fraction
import re
from typing import Any

from .io import InputError

MISSING = object()


def at_path(data, path):
    for part in path:
        if not isinstance(data, dict) or part not in data:
            return MISSING
        data = data[part]
    return data


def paths(value, name):
    if not isinstance(value, list) or any(not isinstance(p, list) or not p or any(not isinstance(x, str) for x in p) for p in value):
        raise InputError(f"{name} must be an array of nonempty string paths", "TC003")
    return [tuple(p) for p in value]


def stream_id(name, namespace=None):
    if not isinstance(name, str) or (namespace is not None and not isinstance(namespace, str)):
        raise InputError("stream name must be a string; namespace must be string or null", "TC003")
    return name, namespace


def label(key):
    # JSON tuple representation avoids namespace/name delimiter collisions.
    import json
    return key[0] if key[1] is None else json.dumps([key[1], key[0]], ensure_ascii=True)


def timestamp(value, protocol):
    if value is None and protocol == "singer":
        return None
    try:
        if protocol == "airbyte":
            if type(value) is not int:
                raise ValueError()
            return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if not isinstance(value, str): raise ValueError()
        from jsonschema import FormatChecker
        if not FormatChecker(["date-time"]).conforms(value, "date-time"): raise ValueError()
        return datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        raise InputError("invalid extraction timestamp", "TC019") from None


@dataclass
class Stream:
    name: str
    namespace: str | None
    schema: Any
    primary: list[tuple[str, ...]]
    cursor: tuple[str, ...] | None
    validator: Any
    signature: str
    count: int = 0
    present: dict[str, int] = field(default_factory=dict)
    nulls: dict[str, int] = field(default_factory=dict)
    minimum: Any = None
    maximum: Any = None
    last_cursor: Any = MISSING
    first_time: str | None = None
    last_time: str | None = None
    dirty: bool = False

    def report(self):
        props = set(self.present)
        if isinstance(self.schema, dict): props.update(self.schema.get("properties", {}))
        return {
            "name": self.name, "namespace": self.namespace, "records": self.count,
            "properties": {p: {"present": self.present.get(p, 0), "nulls": self.nulls.get(p, 0),
                "missing": self.count - self.present.get(p, 0),
                "null_rate": self.nulls.get(p, 0) / self.count if self.count else 0.0} for p in sorted(props)},
            "replication_key": list(self.cursor) if self.cursor else None,
            "replication_min": self.minimum, "replication_max": self.maximum,
            "first_extraction_time": self.first_time, "last_extraction_time": self.last_time,
        }


def instant(value):
    """Exact RFC3339 instant for cursor ordering, including fractional seconds."""
    if not isinstance(value, str):
        raise InputError("date-time cursor must be string", "TC014")
    from jsonschema import FormatChecker
    if not FormatChecker(["date-time"]).conforms(value, "date-time"):
        raise InputError("invalid date-time cursor", "TC014")
    match = re.fullmatch(r"(.*[Tt]\d{2}:\d{2}:\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})", value)
    if match is None:
        raise InputError("unsupported date-time cursor", "TC014")
    base, fraction, offset = match.groups()
    try:
        dt = datetime.fromisoformat(base + offset.upper()).astimezone(timezone.utc)
    except ValueError:
        raise InputError("unsupported date-time cursor (e.g. leap second)", "TC014") from None
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    digits = (fraction or ".")[1:]
    if len(digits) > 1000:
        raise InputError("date-time cursor fraction exceeds 1000 digits", "TC014")
    return Fraction(delta.days * 86400 + delta.seconds) + (Fraction(int(digits), 10 ** len(digits)) if digits else 0)
