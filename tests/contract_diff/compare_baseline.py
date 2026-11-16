#!/usr/bin/env python3
"""Replay the specified prefix-before-'(' policy against the AST classifier.

This is an independent, deliberately limited emulation, not an invocation or
complete reproduction of any dbt release. Report detection counts, not speed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from datawrench.contract_diff.manifest import InputError, load_manifest
from datawrench.contract_diff.types import DIALECTS, compare_types

HERE = Path(__file__).resolve().parent  # tests/contract_diff


def prefix(value: str | None) -> str:
    return (value or '').split('(', 1)[0].strip().lower()


def compare(base_path: Path, head_path: Path, dialect: str = 'duckdb') -> dict:
    base, head = load_manifest(base_path), load_manifest(head_path)
    rows = []
    for key in sorted(base.models.keys() & head.models.keys()):
        left, right = base.models[key].get('columns', {}), head.models[key].get('columns', {})
        for name in sorted(left.keys() & right.keys()):
            a, b = left[name].get('data_type'), right[name].get('data_type')
            if a == b:
                continue
            result = compare_types(a, b, dialect)
            rows.append({'model': key, 'column': name, 'before': a, 'after': b,
                         'baseline_unchanged': prefix(a) == prefix(b),
                         'classification': result.classification, 'breaking': result.breaking,
                         'tool_changed': result.classification != 'identical'})
    return {'scope': 'changed type strings on matched model columns only; constraints/lifecycle excluded',
            'summary': {'type_pairs': len(rows),
                        'baseline_unchanged': sum(r['baseline_unchanged'] for r in rows),
                        'baseline_missed_breaking': sum(r['baseline_unchanged'] and r['breaking'] for r in rows),
                        'baseline_false_mismatches': sum(not r['baseline_unchanged'] and not r['tool_changed'] for r in rows)},
            'pairs': rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=HERE / 'fixtures/base/manifest.json')
    parser.add_argument('--head', type=Path, default=HERE / 'fixtures/head/manifest.json')
    parser.add_argument('--dialect', choices=DIALECTS, default='duckdb')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        result = compare(args.base, args.head, args.dialect)
    except InputError as e:
        parser.exit(2, f'input error: {e}\n')
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print('Prefix-normalisation baseline vs type-AST contract policy (offline replay)')
        for r in result['pairs']:
            baseline = 'UNCHANGED' if r['baseline_unchanged'] else 'CHANGED'
            impact = 'BREAKING' if r['breaking'] else 'non-breaking'
            print(f"{r['model']} column {r['column']}: baseline={baseline}; AST={r['classification']} {impact}")
        print(json.dumps(result['summary'], sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
