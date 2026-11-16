#!/usr/bin/env python3
"""Single-process reproducible benchmark; no services, downloads or fake timings."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import tempfile
from time import perf_counter

import jsonschema
from jsonschema.exceptions import ValidationError

from datawrench.tapcheck.engine import Validator
from datawrench.tapcheck.schema import compile_schema

SCHEMA = {"type": "object", "properties": {
    "id": {"type": "integer"}, "updated": {"type": "integer"},
    "name": {"type": ["string", "null"]}}, "required": ["id", "updated"]}


def create_stream(path, records):
    with path.open('w', encoding='utf-8') as out:
        def emit(m): out.write(json.dumps(m, separators=(',', ':')) + '\n')
        emit({'type':'SCHEMA', 'stream':'bench', 'schema': SCHEMA, 'key_properties':['id'], 'bookmark_properties':['updated']})
        for i in range(records):
            emit({'type':'RECORD','stream':'bench','record': {'id':'invalid' if i % 997 == 0 else i, 'updated':i,'name':None if i % 7 == 0 else 'synthetic'}})
        emit({'type':'STATE','value':{}})


def measure(path, compiled):
    # Initialization is included once in the cached timing; per-record schema
    # checking is intentionally retained in the naive baseline.
    start = perf_counter()
    validator = compile_schema(SCHEMA) if compiled else None
    errors = count = 0
    digest = hashlib.sha256()
    with path.open(encoding='utf-8') as handle:
        for raw in handle:
            message = json.loads(raw)
            if message['type'] != 'RECORD': continue
            count += 1
            data = message['record']
            if compiled:
                invalid = next(validator.iter_errors(data), None) is not None
            else:
                try:
                    jsonschema.validate(data, SCHEMA, cls=jsonschema.Draft7Validator)
                    invalid = False
                except ValidationError: invalid = True
            if invalid:
                errors += 1
                digest.update(str(count).encode() + b'\n')
    elapsed = perf_counter() - start
    return {'records': count, 'invalid_records':errors, 'violation_positions_sha256':digest.hexdigest(),
            'seconds': elapsed, 'records_per_second': count / elapsed}


def benchmark(records):
    root = Path(__file__).resolve().parents[2]  # repository root (tests/tapcheck/bench.py)
    with tempfile.TemporaryDirectory(prefix='.tapcheck-bench-', dir=root) as temp:
        path = Path(temp) / 'synthetic.jsonl'
        create_stream(path, records)
        baseline = measure(path, False)
        cached = measure(path, True)
        equivalent = all(baseline[k] == cached[k] for k in ['records','invalid_records','violation_positions_sha256'])
        expected = (records - 1) // 997 + 1
        if not equivalent or cached['invalid_records'] != expected:
            raise RuntimeError('benchmark equivalence failed')
        start = perf_counter()
        with path.open('rb') as handle:
            report = Validator(max_errors=expected + 1).run(handle)
        elapsed = perf_counter() - start
        if report['errors'] != expected or report['stopped_early'] or report['streams'][0]['records'] != records:
            raise RuntimeError('full streaming engine equivalence failed')
        return {'python':platform.python_version(), 'records':records, 'equivalent':True,
                'naive_jsonschema_validate':baseline, 'cached_tapcheck_validator':cached,
                'speedup':baseline['seconds'] / cached['seconds'],
                'full_tapcheck_stream':{'seconds':elapsed, 'records_per_second':records/elapsed,
                                      'schema_violations':report['errors'], 'exit_code':report['exit_code']},
                'notes':'Single run, warm local file cache, synthetic data. Cached validator is an interpreted reusable jsonschema validator, not generated Python. Full-engine rate includes JSONL decoding, key checks and statistics; other two rates isolate JSONL decoding plus schema validation.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records',type=int,default=200_000)
    parser.add_argument('--output',type=Path,help='optionally save the measured JSON report')
    args = parser.parse_args()
    if args.records < 1: parser.error('--records must be positive')
    result = benchmark(args.records)
    rendered = json.dumps(result,indent=2)
    print(rendered, flush=True)
    if args.output: args.output.write_text(rendered + '\n', encoding='utf-8')


if __name__ == '__main__': main()
