"""Bounded synthetic experiments. No network or external input code is evaluated."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import random
import statistics
import tempfile
import time
import warnings

# Must be set before importing Polars. Refuse a contradictory setting.
if os.environ.get("POLARS_MAX_THREADS", "2") != "2":
    raise SystemExit("Benchmarks require POLARS_MAX_THREADS=2")
os.environ["POLARS_MAX_THREADS"] = "2"
import polars as pl
from polars.testing import assert_frame_equal


@contextmanager
def cases(rows=30_000):
    randomizer = random.Random(44)
    values = list(range(rows))
    randomizer.shuffle(values)
    df = pl.DataFrame({"a": values, **{f"v{i}": [x + i for x in values] for i in range(11)}})
    with tempfile.TemporaryDirectory(prefix="polars-vet-bench-") as directory:
        path = Path(directory) / 'synthetic.csv'
        df.write_csv(path)
        pieces = [df.slice(i, max(1, rows // 60)) for i in range(0, rows, max(1, rows // 60))]
        scans = [pl.scan_csv(path).filter(pl.col('a') % 4 == i) for i in range(4)]

        def loop_rows():
            return pl.DataFrame({'a': [sum(row[0] for row in df.iter_rows())]})

        def repeated_columns():
            out = df
            for i in range(8):
                out = out.with_columns((pl.col('a') + i).alias(f'new{i}'))
            return out

        def repeated_collect():
            results = []
            for query in scans:
                results.append(query.collect())
            return pl.concat(results)

        def repeated_concat():
            out = pieces[0]
            for piece in pieces[1:]:
                out = pl.concat([out, piece])
            return out

        experiments = {
            'PLV001': (lambda: pl.read_csv(path).lazy().select('a').collect(),
                       lambda: pl.scan_csv(path).select('a').collect()),
            'PLV002': (lambda: df.select(pl.col('a').map_elements(lambda x: x + 1, return_dtype=pl.Int64)),
                       lambda: df.select(pl.col('a') + 1)),
            'PLV003': (loop_rows, lambda: df.select(pl.col('a').sum())),
            'PLV004': (repeated_columns, lambda: df.with_columns([(pl.col('a') + i).alias(f'new{i}') for i in range(8)])),
            'PLV005': (repeated_collect, lambda: pl.concat(scans).collect()),
            'PLV006': (lambda: pl.scan_csv(path).collect().filter(pl.col('a') < 100).select('a'),
                       lambda: pl.scan_csv(path).filter(pl.col('a') < 100).select('a').collect()),
            'PLV007': (lambda: pl.from_pandas(df.to_pandas()), lambda: df.clone()),
            # PLV008 is an invalid API pattern, rather than a measurable optimization.
            'PLV009': (lambda: df.sort('a', descending=True).head(25),
                       lambda: df.top_k(25, by='a').sort('a', descending=True)),
            'PLV010': (repeated_concat, lambda: pl.concat(pieces)),
            'PLV011': (lambda: pl.read_csv(path).select('a'),
                       lambda: pl.scan_csv(path).select('a').collect()),
        }
        yield df, experiments


def indexing_check(df):
    try:
        df[df['a'] > 10]
    except (TypeError, ValueError):
        result = df.filter(pl.col('a') > 10)
        assert result.height == max(0, df.height - 11)
        return {'rule': 'PLV008', 'equivalent': False, 'status': 'invalid row indexing rejected; filter validated',
                'bad_ms': None, 'good_ms': None, 'speedup': None}
    raise AssertionError('Pinned Polars unexpectedly accepted boolean DataFrame indexing')


def measure(code, bad, good, repeats):
    assert_frame_equal(bad(), good())  # Equivalence is checked before any timing.
    samples = {"bad": [], "good": []}
    # Alternate order to reduce warm-cache/order bias; timings include no fixture creation.
    for iteration in range(repeats):
        ordered = [('bad', bad), ('good', good)]
        if iteration % 2:
            ordered.reverse()
        for name, function in ordered:
            start = time.perf_counter_ns()
            function()
            samples[name].append((time.perf_counter_ns() - start) / 1_000_000)
    before, after = statistics.median(samples['bad']), statistics.median(samples['good'])
    return {'rule': code, 'equivalent': True, 'status': 'equal synthetic results',
            'bad_ms': before, 'good_ms': after, 'speedup': before / after}


def run(selected=None, argv=None):
    parser = argparse.ArgumentParser(description='Replay bounded synthetic Polars comparisons (2 threads).')
    parser.add_argument('--rows', type=int, default=30_000)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if not 100 <= args.rows <= 200_000 or not 1 <= args.repeats <= 20:
        parser.error('rows must be 100..200000 and repeats 1..20')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', pl.exceptions.PolarsInefficientMapWarning)
        with cases(args.rows) as (df, experiments):
            results = []
            for code in sorted([*experiments, 'PLV008']):
                if selected and code != selected:
                    continue
                results.append(indexing_check(df) if code == 'PLV008' else measure(code, *experiments[code], args.repeats))
    report = {'polars': pl.__version__, 'python': platform.python_version(),
              'system': platform.system(), 'machine': platform.machine(),
              'threads': pl.thread_pool_size(), 'rows': args.rows,
              'repeats': args.repeats, 'clock': 'perf_counter_ns; median; warm cache; alternating order', 'results': results}
    assert report['threads'] == 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Polars {pl.__version__}; 2 threads; {args.rows} rows; median of {args.repeats}; warm cache")
        print('Rule     bad ms   good ms   bad/good  Validation')
        for item in results:
            if item['bad_ms'] is None:
                print(f"{item['rule']}   n/a      n/a       n/a      {item['status']}")
            else:
                print(f"{item['rule']} {item['bad_ms']:8.3f} {item['good_ms']:9.3f} {item['speedup']:9.2f}x  equal")
    return report


def run_case(code):
    run(code)
