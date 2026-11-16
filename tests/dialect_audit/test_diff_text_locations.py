"""Text paired-change headings show actual old locations only when different."""
from copy import deepcopy
import json

import pytest

from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, snapshot


def capture(tmp_path, sql):
    path = tmp_path / 'query.sql'
    path.write_text(sql)
    return snapshot(run_audit([str(path)], 'snowflake', 'duckdb'), str(tmp_path))


@pytest.mark.parametrize('reverse', [False, True])
def test_shifted_edit_headings_preserve_both_identities_and_payloads(tmp_path, reverse):
    before = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 3;')
    after = capture(tmp_path, 'SELECT 0; SELECT 1; SELECT 20; SELECT 3;')
    if reverse:
        before, after = after, before
    delta = compare(before, after)
    saved = deepcopy(delta)
    current, baseline = ('#2 L1-1 C11-18', '#3 L1-1 C21-29') if reverse else (
        '#3 L1-1 C21-29', '#2 L1-1 C11-18')
    output = render_diff(delta, 'text')
    for field, label in [('source_changes', 'SOURCE_CHANGED'), ('sql_changes', 'SQL_CHANGED')]:
        change, = delta[field]
        assert (f'{label} query.sql{current} (baseline: query.sql{baseline})\n'
                '  before: ' + json.dumps(change['before']) + '\n'
                '  after: ' + json.dumps(change['after']) + '\n') in output
    action = 'REMOVED' if reverse else 'ADDED'
    assert (f'STATEMENT_{action} query.sql#1 L1-1 C1-8 (binding: bound)\n'
            '  source: "SELECT 0"\n  SQL: "SELECT 0"\n') in output
    assert output.count('(baseline:') == 2
    assert delta == saved == json.loads(render_diff(delta, 'json'))


@pytest.mark.parametrize('field,label,changed', [
    ('source_changes', 'SOURCE_CHANGED', {'source_sql': 'SELECT 2'}),
    ('sql_changes', 'SQL_CHANGED', {'target_sql': 'SELECT 2'}),
    ('binding_changes', 'BINDING_CHANGED', {'binding_status': 'failed'}),
    ('analysis_changes', 'ANALYSIS_CHANGED', {'functions': {'sum': 1}, 'auto_stubs': [
        {'table': 't', 'columns': ['x'], 'type': 'DOUBLE'}]}),
])
@pytest.mark.parametrize('old_location,old_label', [
    ({}, None),  # Equal known locations: no redundant suffix.
    ({'index': 2}, 'query.sql#2 L1-1 C1-8'),  # Index alone differs.
    ({'column_start': 3, 'column_end': 10}, 'query.sql#1 L1-1 C3-10'),
    ({'line_start': 2, 'line_end': 3, 'column_end': 4}, 'query.sql#1 L2-3 C1-4'),
    ({'column_start': None, 'column_end': None}, 'query.sql#1 L1-1 (columns unavailable)'),
])
def test_all_paired_change_kinds_use_stored_location(tmp_path, field, label, changed,
                                                     old_location, old_label):
    before = capture(tmp_path, 'SELECT 1;')
    after = deepcopy(before)
    # Synthetic payload changes include tool/configuration drift with unchanged
    # source. Change the diff's old location directly to isolate the renderer
    # from pairing eligibility (a lone cross-index source edit has no anchors).
    after['statements'][0].update(changed)
    delta = compare(before, after)
    change, = delta[field]
    change['baseline_location'].update(old_location)
    saved = deepcopy(delta)
    expected = f'{label} query.sql#1 L1-1 C1-8'
    if old_label:
        expected += f' (baseline: {old_label})'
    assert expected in render_diff(delta, 'text').splitlines()
    assert ('  before: ' + json.dumps(change['before'])) in render_diff(delta, 'text').splitlines()
    assert ('  after: ' + json.dumps(change['after'])) in render_diff(delta, 'text').splitlines()
    assert delta == saved == json.loads(render_diff(delta, 'json'))


def test_same_index_column_only_move_displays_old_location(tmp_path):
    before = capture(tmp_path, 'SELECT 1; SELECT 2;')
    after = capture(tmp_path, 'SELECT 1; /* gap */ SELECT 2;')
    delta = compare(before, after)
    assert delta['reasons'] == ['source_changed']
    assert ('SOURCE_CHANGED query.sql#2 L1-1 C21-28 (baseline: query.sql#2 L1-1 C11-18)'
            in render_diff(delta, 'text').splitlines())


def test_legacy_baseline_columns_remain_unavailable_and_file_findings_stay_unpaired(tmp_path):
    before = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 3;')
    for stmt in before['statements']:
        del stmt['column_start'], stmt['column_end']
    path = tmp_path / 'legacy.json'
    path.write_text(json.dumps(before))
    original = path.read_bytes()
    loaded = load_baseline(str(path))
    assert not compare(loaded, capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 3;'))['changed']
    delta = compare(loaded, capture(tmp_path, 'SELECT 0; SELECT 1; SELECT 20; SELECT 3;'))
    assert ('SQL_CHANGED query.sql#3 L1-1 C21-29 (baseline: query.sql#2 L1-1 (columns unavailable))'
            in render_diff(delta, 'text').splitlines())
    assert path.read_bytes() == original
    skipped = capture(tmp_path, 'SELECT {{ value }};')
    output = render_diff(compare(loaded, skipped), 'text')
    assert 'NEW query.sql JINJA_SKIPPED (warn):' in output
    assert 'STATEMENT_REMOVED query.sql#2 L1-1 (columns unavailable)' in output
    assert '(baseline:' not in output


def test_equal_unknown_locations_do_not_add_suffix(tmp_path):
    before = capture(tmp_path, 'SELECT 1;')
    before['statements'][0].update(column_start=None, column_end=None)
    after = deepcopy(before)
    after['statements'][0]['target_sql'] = 'SELECT 2'
    delta = compare(before, after)
    assert 'SQL_CHANGED query.sql#1 L1-1 (columns unavailable)' in render_diff(delta, 'text').splitlines()
    assert '(baseline:' not in render_diff(delta, 'text')
