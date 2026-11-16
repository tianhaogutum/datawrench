#!/usr/bin/env python3
"""Replay the same synthetic SQL through plain SQLGlot defaults and dialect-audit."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import duckdb
import sqlglot

from datawrench.dialect_audit.audit import capture_warnings, run_audit

ROOT = Path(__file__).resolve().parents[2]


def compare() -> dict:
    corpus = ROOT / 'tests/dialect_audit/fixtures/corpus'
    manifest = json.loads((corpus / 'manifest.json').read_text())
    plain_counts, audit_counts = Counter(), Counter()
    rows = []
    for case in manifest:
        path = corpus / case['file']
        source = path.read_text()
        plain = set()
        with capture_warnings() as messages:
            try:
                sqlglot.transpile(source, read=case['read'], write='duckdb')
            except sqlglot.errors.ParseError:
                plain.add('PARSE_ERROR')
            except sqlglot.errors.SqlglotError:
                plain.add('UNSUPPORTED')
        for message in messages:
            plain.add('COMMAND_FALLBACK' if 'Falling back' in message else 'UNSUPPORTED')
        report = run_audit([str(path)], case['read'], 'duckdb', str(ROOT / 'examples/dialect_audit/schema.sql'), schema_read='snowflake')
        actual = report['statements'][0]['categories']
        if sorted(actual) != sorted(case['categories']):
            raise RuntimeError(f"corpus drift for {case['file']}: expected {case['categories']}, got {actual}")
        plain_counts.update(plain)
        audit_counts.update(category for category in actual if category != 'OK')
        rows.append({'file': case['file'], 'read': case['read'], 'plain_signals': sorted(plain),
                     'audit_categories': actual})
    return {'versions': {'sqlglot': sqlglot.__version__, 'duckdb': duckdb.__version__},
            'statements': len(rows), 'plain_flagged': sum(bool(row['plain_signals']) for row in rows),
            'audit_flagged': sum(row['audit_categories'] != ['OK'] for row in rows),
            'plain_categories': dict(sorted(plain_counts.items())),
            'audit_categories': dict(sorted(audit_counts.items())), 'cases': rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    result = compare()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Versions: {result['versions']}")
        print(f"Statements: {result['statements']}; plain flagged: {result['plain_flagged']}; audit flagged: {result['audit_flagged']}")
        print('Category                 Plain signals  Audit findings')
        for category in sorted(set(result['plain_categories']) | set(result['audit_categories'])):
            print(f"{category:25s} {result['plain_categories'].get(category, 0):13d} {result['audit_categories'].get(category, 0):15d}")
        print('Plain signals = exceptions + logged warnings at default settings, not target-engine validation.')
        print('Counts are fixture detection coverage, not performance or semantic-equivalence measurements.')


if __name__ == '__main__':
    main()
