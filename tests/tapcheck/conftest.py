import io
import json

import pytest

from datawrench.tapcheck.engine import Validator

SCHEMA = {"type": "SCHEMA", "stream": "users", "schema": {
    "type": "object", "properties": {"id": {"type": "integer"}, "updated": {"type": "integer"},
    "note": {"type": ["null", "string"]}, "created": {"type": "string", "format": "date-time"}}},
    "key_properties": ["id"], "bookmark_properties": ["updated"]}
RECORD = {"type": "RECORD", "stream": "users", "record": {"id": 1, "updated": 1, "note": None, "created": "2026-01-01T00:00:00Z"}, "time_extracted": "2026-01-01T00:00:00Z"}
STATE = {"type": "STATE", "value": {}}


def run(messages, **options):
    raw = messages if isinstance(messages, bytes) else ("\n".join(json.dumps(m) for m in messages) + "\n").encode()
    return Validator(**options).run(io.BytesIO(raw))


@pytest.fixture
def validate():
    return run
