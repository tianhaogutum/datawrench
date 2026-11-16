# Replayable experiments

Recorded on 2026-11-11 on Linux for the standalone `polars-vet` project (now `datawrench polars-vet`). These are local synthetic measurements, not general speed guarantees. All measured pairs passed exact frame-equality checks before timing.

Engine: Polars 1.35.2; Python 3.11.15; Linux x86_64; 2 Polars threads; 30,000 rows; median of 5 timed runs.

| Rule | Bad ms | Good ms | Bad / good |
| --- | ---: | ---: | ---: |
| PLV001 | 2.405 | 1.013 | 2.37x |
| PLV002 | 1.526 | 0.036 | 42.85x |
| PLV003 | 8.978 | 0.041 | 219.55x |
| PLV004 | 0.183 | 0.106 | 1.72x |
| PLV005 | 10.530 | 9.956 | 1.06x |
| PLV006 | 2.976 | 1.018 | 2.92x |
| PLV007 | 0.676 | 0.002 | 309.54x |
| PLV008 | n/a | n/a | API validation only |
| PLV009 | 0.879 | 0.336 | 2.62x |
| PLV010 | 2.334 | 0.157 | 14.88x |
| PLV011 | 4.201 | 1.069 | 3.93x |

## Reproduce

From the datawrench checkout root (needs Polars and pandas, both in the `test` extra):

```sh
POLARS_MAX_THREADS=2 uv run --extra test python examples/polars_vet/benchmarks/bench_all.py
POLARS_MAX_THREADS=2 uv run --extra test python examples/polars_vet/benchmarks/bench_all.py --json
POLARS_MAX_THREADS=2 uv run --extra test python examples/polars_vet/benchmarks/plv009.py --rows 30000 --repeats 5
```

The table above is the recorded run (the raw JSON dump was not carried into datawrench). Each `plvNNN.py` is a separately runnable entry point to its comparison in [common.py](common.py); fixture preparation is shared, not timed. Fixed random seed 44 produces non-null, unique shuffled integer keys and 11 additional integer columns. CSV data is created in a temporary directory and removed afterward. Rows are bounded to 100..200000 and repeats to 1..20. A contradictory POLARS_MAX_THREADS value is refused. No network, sleeps, subprocesses, fake clocks or cached timing results are used by the experiments.

The first bad/good calls warm caches and assert actual output equality. Timed calls alternate bad/good order, use perf_counter_ns, and report the median. No minimum speedup is asserted: filesystem cache, hardware and optimizer changes can erase or reverse a benefit. These are elapsed wall times, not memory or peak-RSS measurements. Python/pandas import and fixture creation are excluded. Both sides create results; PLV007 deliberately measures an unnecessary conversion against a cheap clone.

## What is compared

- PLV001: eager CSV read + lazy projection vs scan + projection.
- PLV002: numeric map_elements lambda vs native addition.
- PLV003: Python row summation vs native aggregation.
- PLV004: eight independent with_columns calls vs one batched call.
- PLV005: four scans collected individually vs lazy concatenation then collection (input file and filters are the same).
- PLV006: scan collected before filter/projection vs filter/projection before collection.
- PLV007: Polars → pandas → Polars vs clone on supported integer data.
- PLV008: boolean row indexing must be rejected for the synthetic shape; filter output is checked. There is no valid bad result or speed ratio.
- PLV009: full descending sort + head(25) vs top_k(25) + descending sort. Unique non-null keys eliminate ambiguous ties/null ordering.
- PLV010: repeated concat of 60 pieces vs a single concat.
- PLV011: eager CSV read + projection vs scan + projection + collect.

SQL/remote files, arbitrary user expressions, large media and production data are not used. The small-size test suite replays all comparisons without imposing performance thresholds.
