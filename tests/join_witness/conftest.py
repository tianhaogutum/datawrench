import os

os.environ["POLARS_MAX_THREADS"] = "2"

import duckdb
import pytest


@pytest.fixture
def db():
    connection = duckdb.connect(config={"threads": 1, "memory_limit": "128MB"})
    yield connection
    connection.close()
