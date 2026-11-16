"""Reorder context uses exact paired sources and both stored statement spans."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, snapshot


LOCATION_KEYS = ('file', 'index', 'line_start', 'line_end', 'column_start', 'column_end')
BATCH = 'SELECT 1; SELECT 2; SELECT 3;'
ROTATED = 'SELECT 3; SELECT 1; SELECT 2;'


def capture(tmp_path, sql, name='query.sql'):
    path = tmp_path / name
    path.write_bytes(sql.encode('utf-8'))
    return snapshot(run_audit([str(path)], 'snowflake', 'postgres'), str(tmp_path))


def context(before, after):
    return {'source_sql': before['source_sql'],
            'baseline_location': {k: before.get(k) for k in LOCATION_KEYS},
            'current_location': {k: after.get(k) for k in LOCATION_KEYS}}


def check_pairs(delta, old, new, pairs):
    item, = delta['statement_order_changes']
    assert item['baseline_indices'] == sorted(left for left, _ in pairs)
    assert item['current_order'] == [left for left, _ in pairs]
    assert item['matched_statements'] == [context(old['statements'][left - 1], new['statements'][right - 1])
                                           for left, right in pairs]
    assert json.loads(render_diff(delta, 'json')) == delta
    return item


@pytest.mark.parametrize('reverse', [False, True])
def test_reorder_source_context_forward_reverse(tmp_path, reverse):
    original, rotated = capture(tmp_path, BATCH), capture(tmp_path, ROTATED)
    old, new = (rotated, original) if reverse else (original, rotated)
    saved = deepcopy((old, new))
    delta = compare(old, new)
    check_pairs(delta, old, new, [(2, 1), (3, 2), (1, 3)] if reverse else [(3, 1), (1, 2), (2, 3)])
    assert delta['reasons'] == ['statement_order_changed'] and delta['changed'] is True
    for key in ('statement_changes', 'source_changes', 'sql_changes', 'binding_changes',
                'analysis_changes', 'new_findings', 'resolved_findings'):
        assert delta[key] == []
    if not reverse:
        assert render_diff(delta, 'text').splitlines()[1:8] == [
            'STATEMENT_ORDER_CHANGED query.sql: [1, 2, 3] -> [3, 1, 2]',
            '  matched query.sql#1 L1-1 C1-8 (baseline: query.sql#3 L1-1 C21-28)',
            '    source: "SELECT 3"',
            '  matched query.sql#2 L1-1 C11-18 (baseline: query.sql#1 L1-1 C1-8)',
            '    source: "SELECT 1"',
            '  matched query.sql#3 L1-1 C21-28 (baseline: query.sql#2 L1-1 C11-18)',
            '    source: "SELECT 2"']
    assert (old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
def test_reorder_and_addition_keep_actual_current_indices(tmp_path, reverse):
    old = capture(tmp_path, BATCH)
    new = capture(tmp_path, 'SELECT 0; ' + ROTATED)
    old, new = (new, old) if reverse else (old, new)
    delta = compare(old, new)
    pairs = [(3, 1), (4, 2), (2, 3)] if reverse else [(3, 2), (1, 3), (2, 4)]
    check_pairs(delta, old, new, pairs)
    assert delta['reasons'] == ['statements_changed', 'statement_order_changed']
    change, = delta['statement_changes']
    assert (change['change'], change['index']) == ('removed' if reverse else 'added', 1)
    assert change['statement']['source_sql'] == 'SELECT 0'


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_reorder_exact_multiline_source_not_generated_sql_or_reread(tmp_path, monkeypatch, reverse, newline):
    source = 'SELECT /* café 😀 "quote" \\path */' + newline + '\t4::INT AS "雪"'
    old = capture(tmp_path, 'SELECT 1; ' + source + '; SELECT 2;')
    new = capture(tmp_path, 'SELECT 2; SELECT 1; ' + source + ';')
    old, new = (new, old) if reverse else (old, new)
    saved = deepcopy((old, new))
    assert next(s for s in old['statements'] if s['source_sql'] == source)['target_sql'] != source
    def deny_read(*args, **kwargs):
        raise AssertionError('compare/render must use stored records without file reads')
    with monkeypatch.context() as m:
        m.setattr(Path, 'open', deny_read)
        delta = compare(old, new)
        output = render_diff(delta, 'text')
    check_pairs(delta, old, new, [(2, 1), (3, 2), (1, 3)] if reverse else [(3, 1), (1, 2), (2, 3)])
    lines = [line for line in output.splitlines() if line.startswith('    source: ')]
    assert len(lines) == 3
    assert all(line.isascii() for line in lines)
    assert '    source: ' + json.dumps(source, ensure_ascii=True) in lines
    assert [json.loads(line.removeprefix('    source: ')) for line in lines] == [
        s['source_sql'] for s in new['statements']]
    assert (old, new) == saved


@pytest.mark.parametrize('unknown_side', ['baseline', 'current'])
def test_reorder_legacy_and_unknown_columns_remain_unavailable(tmp_path, unknown_side):
    old, new = capture(tmp_path, BATCH), capture(tmp_path, ROTATED)
    for s in (old if unknown_side == 'baseline' else new)['statements']:
        if unknown_side == 'baseline':
            del s['column_start'], s['column_end']
        else:
            s.update(column_start=None, column_end=None)
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps(old))
    baseline_bytes = path.read_bytes()
    old = load_baseline(str(path))
    delta = compare(old, new)
    check_pairs(delta, old, new, [(3, 1), (1, 2), (2, 3)])
    output = render_diff(delta, 'text')
    assert delta['reasons'] == ['statement_order_changed']
    expected = ('  matched query.sql#1 L1-1 C1-8 (baseline: query.sql#3 L1-1 (columns unavailable))'
                if unknown_side == 'baseline' else
                '  matched query.sql#1 L1-1 (columns unavailable) (baseline: query.sql#3 L1-1 C21-28)')
    assert expected in output.splitlines()
    assert output.count('(columns unavailable)') == 3
    assert path.read_bytes() == baseline_bytes


@pytest.mark.parametrize('reverse', [False, True])
def test_reorder_equal_duplicates_keep_each_occurrence(tmp_path, reverse):
    old = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 1;')
    new = capture(tmp_path, 'SELECT 1; SELECT 1; SELECT 2;')
    old, new = (new, old) if reverse else (old, new)
    delta = compare(old, new)
    check_pairs(delta, old, new, [(1, 1), (3, 2), (2, 3)])
    assert render_diff(delta, 'text').count('    source: "SELECT 1"') == 2
    assert not delta['statement_changes']


@pytest.mark.parametrize('reverse', [False, True])
def test_reorder_ambiguous_duplicate_counts_excluded_from_context(tmp_path, reverse):
    old = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 1; SELECT 3;')
    new = capture(tmp_path, 'SELECT 3; SELECT 1; SELECT 2; SELECT 1; SELECT 1;')
    old, new = (new, old) if reverse else (old, new)
    delta = compare(old, new)
    check_pairs(delta, old, new, [(3, 2), (1, 4)] if reverse else [(4, 1), (2, 3)])
    assert '    source: "SELECT 1"' not in render_diff(delta, 'text')
    change, = delta['statement_changes']
    assert change['statement']['source_sql'] == 'SELECT 1'
    assert change['change'] == ('removed' if reverse else 'added')


@pytest.mark.parametrize('current', [BATCH, 'SELECT 0; ' + BATCH, 'SELECT 1; SELECT 3;',
                                    'SELECT 1; SELECT 20; SELECT 3;',
                                    'SELECT 1; SELECT 1; SELECT 2; SELECT 3;',
                                    'select 1; SELECT 2; SELECT 3;'])
def test_no_reorder_has_no_context(tmp_path, current):
    delta = compare(capture(tmp_path, BATCH), capture(tmp_path, current))
    assert delta['statement_order_changes'] == []
    assert '  matched ' not in render_diff(delta, 'text')
    assert '    source: ' not in render_diff(delta, 'text')
    if current == BATCH:
        assert delta['changed'] is False and delta['reasons'] == []
