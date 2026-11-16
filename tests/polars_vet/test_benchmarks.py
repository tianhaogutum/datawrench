"""Run every real comparison at a small size; no speed thresholds or fake clocks."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

# The replay scripts import Polars directly; PLV007 round-trips through pandas.
pytest.importorskip("polars")
pytest.importorskip("pandas")


def test_all_replay_scripts():
    benchmarks = Path(__file__).resolve().parents[2] / 'examples' / 'polars_vet' / 'benchmarks'
    run = subprocess.run(
        [sys.executable, str(benchmarks / 'bench_all.py'), '--rows', '100', '--repeats', '1', '--json'],
        env={**os.environ, 'POLARS_MAX_THREADS': '2'}, capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    assert report['threads'] == 2
    assert len(report['results']) == 11
    assert sum(r['equivalent'] for r in report['results']) == 10
    for result in report['results']:
        assert (benchmarks / (result['rule'].lower() + '.py')).is_file()
        if result['rule'] != 'PLV008':
            assert result['bad_ms'] > 0 and result['good_ms'] > 0
