import importlib.util
from pathlib import Path


def test_benchmark_equivalent_real_violations():
    spec = importlib.util.spec_from_file_location('bench', Path(__file__).with_name('bench.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.benchmark(1001)
    assert result['equivalent'] is True
    assert result['naive_jsonschema_validate']['invalid_records'] == 2
    assert result['full_tapcheck_stream']['schema_violations'] == 2
    assert result['naive_jsonschema_validate']['records'] == 1001
