import io
import json
from pathlib import Path

import pytest

from datawrench.singer_replay.cassette import record
from datawrench.singer_replay.common import encode

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def messages():
    return [json.loads(line) for line in (FIXTURES / "shop.jsonl").read_text().splitlines()]


@pytest.fixture
def make_cassette(tmp_path):
    counter = 0

    def make(messages, **kwargs):
        nonlocal counter
        counter += 1
        path = tmp_path / f"cassette-{counter}"
        record(io.BytesIO(''.join(encode(m) + '\n' for m in messages).encode()), path, **kwargs)
        return path
    return make
