"""Exact-source and bounded anchor-gap contracts; see docs/dialect-audit.md for pairing policy."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, snapshot


BATCH = 'SELECT 1;\nSELECT NO_FN(1);\nSELECT 3;\n'
PAYLOAD_CHANGES = ('source_changes', 'sql_changes', 'binding_changes',
                   'analysis_changes', 'new_findings', 'resolved_findings')


def capture(tmp_path, text, name='query.sql', write='duckdb'):
    path = tmp_path / name
    path.write_text(text)
    return snapshot(run_audit([str(path)], 'snowflake', write), str(tmp_path))


@pytest.mark.parametrize('insertion,index', [
    ('SELECT 0;\n' + BATCH, 1),
    (BATCH.replace('SELECT NO_FN', 'SELECT 0;\nSELECT NO_FN'), 2),
    (BATCH + 'SELECT 0;\n', 4),
])
def test_insertion_and_deletion_do_not_cascade(tmp_path, insertion, index):
    before = capture(tmp_path, BATCH)
    after = capture(tmp_path, insertion)
    for old, new, change in [(before, after, 'added'), (after, before, 'removed')]:
        delta = compare(old, new)
        assert delta['reasons'] == ['statements_changed']
        assert [(s['change'], s['index'], s['statement']['source_sql'])
                for s in delta['statement_changes']] == [(change, index, 'SELECT 0')]
        assert all(delta[field] == [] for field in PAYLOAD_CHANGES)
        assert delta['statement_order_changes'] == []
        assert delta['current_binding_statuses']['failed'] == 1


@pytest.mark.parametrize('original,current,added_index', [
    ('SELECT 1; SELECT 2;', 'SELECT 1; SELECT 1; SELECT 2;', 2),
    ('SELECT 1; SELECT 1;', 'SELECT 1; SELECT 1; SELECT 1;', 3),
    ('SELECT NO_FN(1); SELECT 2;', 'SELECT NO_FN(1); SELECT NO_FN(1); SELECT 2;', 2),
])
def test_duplicate_occurrences_consumed_once_in_source_order(tmp_path, original, current, added_index):
    before, after = capture(tmp_path, original), capture(tmp_path, current)
    delta = compare(before, after)
    assert [s['index'] for s in delta['statement_changes']] == [added_index]
    assert delta['statement_changes'][0]['change'] == 'added'
    assert not delta['resolved_findings'] and not delta['sql_changes'] and not delta['source_changes']
    expected_findings = after['statements'][added_index - 1]['diagnostics']
    assert len(delta['new_findings']) == len(expected_findings)
    assert all(f['index'] == added_index for f in delta['new_findings'])
    reverse = compare(after, before)
    assert [s['index'] for s in reverse['statement_changes']] == [added_index]
    assert len(reverse['resolved_findings']) == len(expected_findings)


def test_inserted_duplicate_does_not_invent_relative_reordering(tmp_path):
    before = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 1;')
    after = capture(tmp_path, 'SELECT 1; SELECT 1; SELECT 2; SELECT 1;')
    for old, new in ((before, after), (after, before)):
        delta = compare(old, new)
        assert delta['reasons'] == ['statements_changed']
        assert not delta['statement_order_changes']
        assert len(delta['statement_changes']) == 1
        assert all(delta[field] == [] for field in PAYLOAD_CHANGES)


def test_repeated_sources_with_equal_counts_still_detect_reordering(tmp_path):
    before = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 1;')
    after = capture(tmp_path, 'SELECT 1; SELECT 1; SELECT 2;')
    delta = compare(before, after)
    assert [{k: v for k, v in item.items() if k != 'matched_statements'}
            for item in delta['statement_order_changes']] == [
        {'file': 'query.sql', 'baseline_indices': [1, 2, 3], 'current_order': [1, 3, 2]}]
    assert not delta['statement_changes']
    assert not delta['sql_changes']


def test_same_index_edit_still_uses_fallback_and_localizes_changes(tmp_path):
    before = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 3;')
    after = capture(tmp_path, 'SELECT 1; SELECT NO_FN(1); SELECT 3;')
    delta = compare(before, after)
    assert not delta['statement_changes']
    assert [s['index'] for s in delta['sql_changes']] == [2]
    assert [s['index'] for s in delta['binding_changes']] == [2]
    assert {s['category'] for s in delta['new_findings']} == {'ANONYMOUS_FUNCTION', 'BIND_ERROR'}
    assert all(s['index'] == 2 for s in delta['new_findings'])
    # Same-index location-only shifts still drift, including statement 3.
    assert [s['index'] for s in delta['source_changes']] == [2, 3]


def test_single_shifted_edit_between_unique_stable_anchors(tmp_path):
    before = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 3;')
    after = capture(tmp_path, 'SELECT 0; SELECT 1; SELECT 20; SELECT 3;')
    delta = compare(before, after)
    assert [(s['change'], s['index']) for s in delta['statement_changes']] == [('added', 1)]
    assert delta['reasons'] == ['statements_changed', 'source_changed', 'target_sql_changed']
    for field in ('source_changes', 'sql_changes'):
        change, = delta[field]
        assert (change['index'], change['column_start'], change['column_end']) == (3, 21, 29)
        assert change['baseline_location'] == {
            'file': 'query.sql', 'index': 2, 'line_start': 1, 'line_end': 1,
            'column_start': 11, 'column_end': 18}
    assert delta['source_changes'][0]['before']['source_sql'] == 'SELECT 2'
    assert delta['source_changes'][0]['after']['source_sql'] == 'SELECT 20'
    assert delta['sql_changes'][0]['before'] == 'SELECT 2'
    assert delta['sql_changes'][0]['after'] == 'SELECT 20'
    assert all(delta[field] == [] for field in PAYLOAD_CHANGES[2:])
    assert not delta['statement_order_changes']
    reverse = compare(after, before)
    assert [(s['change'], s['index']) for s in reverse['statement_changes']] == [('removed', 1)]
    assert reverse['sql_changes'][0]['index'] == 2
    assert reverse['sql_changes'][0]['baseline_location']['index'] == 3


@pytest.mark.parametrize('current', ['select 1;', 'SELECT  1;', 'SELECT /* inner */ 1;'])
def test_target_equivalence_does_not_replace_exact_source_identity(tmp_path, current):
    before = capture(tmp_path, 'SELECT 1;')
    after = capture(tmp_path, current)
    delta = compare(before, after)
    assert delta['reasons'] == ['source_changed']
    assert delta['source_changes'][0]['before']['source_sql'] == 'SELECT 1'
    assert not delta['statement_changes']


def test_no_matching_across_files(tmp_path):
    before = capture(tmp_path, BATCH, 'old.sql')
    after = capture(tmp_path, BATCH, 'new.sql')
    delta = compare(before, after)
    assert len(delta['statement_changes']) == 6
    assert len(delta['new_findings']) == len(delta['resolved_findings']) == 2
    assert not delta['sql_changes']


def test_reordering_is_one_file_order_change_without_payload_cascade(tmp_path):
    before = capture(tmp_path, BATCH)
    after = capture(tmp_path, 'SELECT 3;\nSELECT 1;\nSELECT NO_FN(1);\n')
    delta = compare(before, after)
    assert delta['reasons'] == ['statement_order_changed']
    assert [{k: v for k, v in item.items() if k != 'matched_statements'}
            for item in delta['statement_order_changes']] == [
        {'file': 'query.sql', 'baseline_indices': [1, 2, 3], 'current_order': [3, 1, 2]}]
    assert all(delta[field] == [] for field in PAYLOAD_CHANGES)
    assert not delta['statement_changes']
    assert 'STATEMENT_ORDER_CHANGED query.sql: [1, 2, 3] -> [3, 1, 2]' in render_diff(delta, 'text')


def test_exact_matches_still_compare_analysis_and_outcomes_at_correct_locations(tmp_path):
    before = capture(tmp_path, BATCH)
    after = capture(tmp_path, 'SELECT 0;\n' + BATCH)
    # Simulated older tool outputs, not a claim about an actual engine upgrade.
    before['tool_versions']['sqlglot'] = 'older-fixture'
    stmt = before['statements'][1]
    stmt['target_sql'] = 'SELECT OLD_FN(1)'
    stmt['binding_status'] = 'not-attempted'
    stmt['functions'] = {'old_fn': 1}
    stmt['auto_stubs'] = [{'table': 'old', 'columns': ['x'], 'type': 'DOUBLE'}]
    stmt['diagnostics'].append({'category': 'UNSUPPORTED', 'severity': 'error', 'message': 'old finding'})
    delta = compare(before, after)
    assert 'tool_versions_changed' in delta['reasons']
    assert not delta['new_findings']
    assert [(f['index'], f['line_start'], f['category']) for f in delta['resolved_findings']] == [(2, 2, 'UNSUPPORTED')]
    for key in ('sql_changes', 'binding_changes', 'analysis_changes'):
        assert len(delta[key]) == 1
        change = delta[key][0]
        assert (change['index'], change['line_start']) == (3, 3)
        assert (change['baseline_location']['index'], change['baseline_location']['line_start']) == (2, 2)
    assert not delta['source_changes']


def test_legacy_baseline_and_static_only_sources_match_without_mutation(tmp_path):
    before = capture(tmp_path, BATCH, write='postgres')
    for stmt in before['statements']:
        del stmt['column_start'], stmt['column_end']
    baseline = tmp_path / 'baseline.json'
    baseline.write_text(json.dumps(before))
    original = baseline.read_bytes()
    after = capture(tmp_path, 'SELECT 0;\n' + BATCH, write='postgres')
    saved = load_baseline(str(baseline))
    unchanged = deepcopy(saved)
    delta = compare(saved, after)
    assert delta['reasons'] == ['statements_changed']
    assert delta['current_binding_statuses'] == {'static-only': 4}
    assert saved == unchanged and baseline.read_bytes() == original


def test_cli_insertion_outputs_only_added_statement_and_preserves_baseline(tmp_path):
    source = tmp_path / 'query.sql'
    source.write_text(BATCH)
    prefix = [sys.executable, '-m', 'datawrench', 'dialect-audit']
    options = ['query.sql', '--read', 'snowflake', '--write', 'duckdb']
    saved = subprocess.run(prefix + ['baseline'] + options + ['--output', 'baseline.json'],
                           cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert saved.returncode == 0, saved.stderr
    original = (tmp_path / 'baseline.json').read_bytes()
    source.write_text('SELECT 0;\n' + BATCH)
    for format in ('json', 'text'):
        result = subprocess.run(prefix + ['diff'] + options + ['--baseline', 'baseline.json', '--format', format],
                                cwd=tmp_path, capture_output=True, text=True, timeout=15)
        assert result.returncode == 1 and not result.stderr
        if format == 'json':
            delta = json.loads(result.stdout)
            assert delta['reasons'] == ['statements_changed']
            assert [s['index'] for s in delta['statement_changes']] == [1]
        else:
            assert 'STATEMENT_ADDED query.sql#1 L1-1 C1-8' in result.stdout
            assert 'SQL_CHANGED' not in result.stdout and 'SOURCE_CHANGED' not in result.stdout
            assert 'NEW ' not in result.stdout and 'RESOLVED ' not in result.stdout
    assert (tmp_path / 'baseline.json').read_bytes() == original


@pytest.mark.parametrize('original,current', [
    # Multiple new/old candidates: eligibility is measured before fallback.
    ('SELECT 1; SELECT 2; SELECT 3;', 'SELECT 0; SELECT 1; SELECT 20; SELECT 21; SELECT 3;'),
    ('SELECT 1; SELECT 2; SELECT 4; SELECT 3;',
     'SELECT 0; SELECT 9; SELECT 8; SELECT 7; SELECT 1; SELECT 20; SELECT 3;'),
    # Same-index fallback can consume one of several candidates, never make a singleton gap.
    ('SELECT 1; SELECT 2; SELECT 4; SELECT 3;',
     'SELECT 0; SELECT 1; SELECT 20; SELECT 21; SELECT 3;'),
    # Duplicate left/right anchors, with both equal and changed occurrence counts.
    ('SELECT 1; SELECT 2; SELECT 3; SELECT 1;',
     'SELECT 0; SELECT 1; SELECT 20; SELECT 3; SELECT 1;'),
    ('SELECT 3; SELECT 1; SELECT 2; SELECT 3;',
     'SELECT 0; SELECT 3; SELECT 1; SELECT 20; SELECT 3;'),
    ('SELECT 1; SELECT 2; SELECT 3;',
     'SELECT 0; SELECT 1; SELECT 20; SELECT 3; SELECT 1;'),
    # A duplicate exact match in the gap is a barrier, not an anchor to skip over.
    ('SELECT 1; SELECT 5; SELECT 2; SELECT 5; SELECT 3;',
     'SELECT 0; SELECT 1; SELECT 5; SELECT 20; SELECT 5; SELECT 3;'),
    # Reversed anchors, then increasing anchors crossed by a distant exact match.
    ('SELECT 1; SELECT 2; SELECT 3;', 'SELECT 0; SELECT 3; SELECT 20; SELECT 1;'),
    ('SELECT 9; SELECT 1; SELECT 2; SELECT 3;',
     'SELECT 0; SELECT 1; SELECT 20; SELECT 3; SELECT 9;'),
    ('SELECT 1; SELECT 2; SELECT 3; SELECT 9;',
     'SELECT 9; SELECT 0; SELECT 1; SELECT 20; SELECT 3;'),
    # Real anchors on both sides are required; no implicit file-edge anchors.
    ('SELECT 1; SELECT 2;', 'SELECT 0; SELECT 1; SELECT 20;'),
    ('SELECT 2; SELECT 3;', 'SELECT 0; SELECT 20; SELECT 3;'),
    ('SELECT 1; SELECT 2;', 'SELECT 0; SELECT 10; SELECT 20;'),
    # Target-equivalent SQL does not supply an exact-source anchor.
    ('SELECT 1; SELECT 2; SELECT 3;', 'SELECT 0; select 1; SELECT 20; SELECT 3;'),
])
def test_ambiguous_or_unbounded_gaps_do_not_infer_shifted_edits(tmp_path, original, current):
    before = capture(tmp_path, original, write='postgres')
    after = capture(tmp_path, current, write='postgres')
    for old, new in ((before, after), (after, before)):
        delta = compare(old, new)
        # Legacy equal-index fallback may compare edits even here. The extension
        # must never introduce a shifted source pair for these unsafe gaps.
        assert all(c['index'] == c['baseline_location']['index'] for c in delta['source_changes'])
        assert delta['statement_changes']


def test_gap_pairing_does_not_displace_same_index_fallback(tmp_path):
    before = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 3; SELECT 4;', write='postgres')
    after = capture(tmp_path, 'SELECT 0; SELECT 9; SELECT 1; SELECT 20; SELECT 3;', write='postgres')
    delta = compare(before, after)
    # Old #2/new #4 form a singleton gap, but both are already reserved by
    # legacy same-index fallback with statements outside that gap.
    assert [(c['baseline_location']['index'], c['index']) for c in delta['sql_changes']] == [(2, 2), (4, 4)]
    assert [(c['change'], c['index']) for c in delta['statement_changes']] == [('added', 1)]


@pytest.mark.parametrize('legacy', [False, True])
def test_gap_edits_preserve_outcomes_analysis_and_old_current_evidence(tmp_path, legacy):
    before = capture(tmp_path, 'SELECT 1;\n  SELECT * FROM;\nSELECT 3;')
    after = capture(tmp_path, 'SELECT 0;\nSELECT 1;\n    SELECT NO_FN(1);\nSELECT 3;')
    # Synthetic prior analysis evidence, independent of actual engine inference.
    before['statements'][1]['auto_stubs'] = [{'table': 'old', 'columns': ['x'], 'type': 'DOUBLE'}]
    if legacy:
        for s in before['statements']:
            del s['column_start'], s['column_end']
            for d in s['diagnostics']:
                del d['location']
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps(before))
    original_bytes = path.read_bytes()
    saved = load_baseline(str(path))
    original = deepcopy(saved)
    delta = compare(saved, after)
    assert [(s['change'], s['index']) for s in delta['statement_changes']] == [('added', 1)]
    for field in ('source_changes', 'sql_changes', 'binding_changes', 'analysis_changes'):
        change, = delta[field]
        assert (change['index'], change['line_start'], change['column_start']) == (3, 3, 5)
        assert (change['baseline_location']['index'], change['baseline_location']['line_start']) == (2, 2)
        assert change['baseline_location']['column_start'] == (None if legacy else 3)
    assert delta['binding_changes'][0]['before'] == 'not-attempted'
    assert delta['binding_changes'][0]['after'] == 'failed'
    assert delta['analysis_changes'][0]['after']['functions'] == after['statements'][2]['functions']
    assert delta['analysis_changes'][0]['before']['auto_stubs'] == before['statements'][1]['auto_stubs']
    assert delta['analysis_changes'][0]['after']['auto_stubs'] == []
    assert {f['category'] for f in delta['new_findings']} == {'ANONYMOUS_FUNCTION', 'BIND_ERROR'}
    assert all((f['index'], f['column_start']) == (3, 5) for f in delta['new_findings'])
    resolved, = delta['resolved_findings']
    assert (resolved['index'], resolved['line_start'], resolved['category']) == (2, 2, 'PARSE_ERROR')
    assert resolved.get('location') == (None if legacy else {
        'line_start': 2, 'line_end': 2, 'column_start': 12, 'column_end': 15})
    assert saved == original and path.read_bytes() == original_bytes
    # Restoring the failed parse proves the current diagnostic highlight travels
    # with a NEW finding, independently of its enclosing statement's location.
    reverse = compare(after, saved)
    assert reverse['new_findings'][0].get('location') == resolved.get('location')


def test_gap_pairing_keeps_exact_match_priority_and_independent_file_gaps(tmp_path):
    old = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 3;', write='postgres')
    moved = capture(tmp_path, 'SELECT 0; SELECT 1; SELECT 20; SELECT 3; SELECT 2;', write='postgres')
    delta = compare(old, moved)
    assert not delta['sql_changes']  # SELECT 2 is reserved as an exact match.
    assert {(c['change'], c['index']) for c in delta['statement_changes']} == {('added', 1), ('added', 3)}
    new = capture(tmp_path, 'SELECT 0; SELECT 1; SELECT 20; SELECT 3;', name='other.sql', write='postgres')
    delta = compare(old, new)
    assert len(delta['statement_changes']) == 7
    assert all(delta[field] == [] for field in ('source_changes', 'sql_changes', 'analysis_changes'))


@pytest.mark.parametrize('original,current,expected', [
    ('SELECT 1; SELECT 2; SELECT 3; SELECT 4; SELECT 5;',
     'SELECT 0; SELECT 1; SELECT 20; SELECT 3; SELECT 40; SELECT 5;', [(2, 3), (4, 5)]),
    # A reorder elsewhere does not invalidate independently stable boundaries.
    ('SELECT 9; SELECT 8; SELECT 1; SELECT 2; SELECT 3;',
     'SELECT 8; SELECT 9; SELECT 0; SELECT 1; SELECT 20; SELECT 3;', [(4, 5)]),
])
def test_each_stable_singleton_gap_is_independent(tmp_path, original, current, expected):
    before = capture(tmp_path, original, write='postgres')
    after = capture(tmp_path, current, write='postgres')
    delta = compare(before, after)
    assert [(c['baseline_location']['index'], c['index']) for c in delta['sql_changes']] == expected
    assert len(delta['statement_changes']) == 1
    assert delta['statement_changes'][0]['statement']['source_sql'] == 'SELECT 0'
    assert bool(delta['statement_order_changes']) == ('SELECT 9' in original)
