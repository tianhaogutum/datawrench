import os
os.environ["POLARS_MAX_THREADS"] = "2"
os.environ["OMP_NUM_THREADS"] = "2"

import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("make_drift_dataset", Path(__file__).parents[2] / "examples" / "parquet_unify" / "make_drift_dataset.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "drift"
    module.make_dataset(path)
    return path


@pytest.fixture
def make_dataset():
    return module.make_dataset
