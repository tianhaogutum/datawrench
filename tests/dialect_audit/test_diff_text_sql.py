"""Generated-SQL drift retains exact source from both selected records."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, snapshot


def capture(tmp_path, sql, target='postgres'):
    path = tmp_path / 'query.sql'
    path.write_bytes(sql.encode('utf-8'))
    return snapshot(run_audit([str(path)], 'snowflake', target), str(tmp_path))


@pytest.mark.parametrize('reverse', [False, True])
def test_sql_only_exact_source_forward_reverse(tmp_path, reverse):
    sql = 'SELECT 1001::INT AS amount;'
    old = capture(tmp_path, sql, 'mysql' if reverse else 'postgres')
    new = capture(tmp_path, sql, 'postgres' if reverse else 'mysql')
    saved = deepcopy((old, new))
    delta = compare(old, new)
    item, = delta['sql_changes']
    assert item['source_sql'] == {'before': sql[:-1], 'after': sql[:-1]}
    assert delta['diff_version'] == 1
    assert delta['reasons'] == ['configuration_changed', 'target_sql_changed']
    left, right = 'SELECT CAST(1001 AS INT) AS amount', 'SELECT CAST(1001 AS SIGNED) AS amount'
    if reverse:
        left, right = right, left
    assert (item['before'], item['after']) == (left, right)
    assert ('SQL_CHANGED query.sql#1 L1-1 C1-26\n'
            '  before: ' + json.dumps(left) + '\n  after: ' + json.dumps(right) + '\n'
            '  source: "SELECT 1001::INT AS amount"\n') in render_diff(delta, 'text')
    assert delta['current_binding_statuses'] == {'static-only': 1}
    for field in ('binding_changes', 'source_changes', 'statement_changes', 'statement_order_changes',
                  'analysis_changes', 'new_findings', 'resolved_findings'):
        assert delta[field] == []
    assert json.loads(render_diff(delta, 'json')) == delta
    assert (old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_shifted_sql_source_uses_records_without_reads(tmp_path, monkeypatch, reverse, newline):
    source = 'SELECT /* café 😀 "quote" \\path */' + newline + '\t4::INT AS "雪"'
    old = capture(tmp_path, source + ';')
    new = capture(tmp_path, 'SELECT 0; ' + source + ';', 'mysql')
    old, new = (new, old) if reverse else (old, new)
    saved = deepcopy((old, new))

    def deny_read(*args, **kwargs):
        raise AssertionError('compare/render must use stored sources without reads')

    with monkeypatch.context() as m:
        m.setattr(Path, 'open', deny_read)
        delta = compare(old, new)
        output = render_diff(delta, 'text')
    item, = delta['sql_changes']
    assert item['source_sql'] == {'before': source, 'after': source}
    assert item['before'] != source and item['after'] != source
    assert (item['baseline_location']['index'], item['index']) == ((2, 1) if reverse else (1, 2))
    baseline, current = ('#2 L1-2 C11-14', '#1 L1-2 C1-14') if reverse else (
        '#1 L1-2 C1-14', '#2 L1-2 C11-14')
    lines = output.splitlines()
    pos = lines.index(f'SQL_CHANGED query.sql{current} (baseline: query.sql{baseline})')
    assert lines[pos + 1:pos + 4] == ['  before: ' + json.dumps(item['before']),
                                    '  after: ' + json.dumps(item['after']),
                                    '  source: ' + json.dumps(source)]
    assert lines[pos + 3].isascii()
    assert json.loads(lines[pos + 3].removeprefix('  source: ')) == source
    assert delta['reasons'] == ['configuration_changed', 'statements_changed', 'target_sql_changed']
    assert delta['source_changes'] == delta['binding_changes'] == delta['statement_order_changes'] == []
    assert json.loads(render_diff(delta, 'json')) == delta
    assert (old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('shifted', [False, True])
def test_different_sql_sources_follow_edit_fallback_and_keep_null(tmp_path, reverse, shifted):
    # Same-index fallback, or a shifted singleton gap between stable anchors.
    old = capture(tmp_path, 'SELECT 1; SELECT * FROM; SELECT 3;', 'duckdb')
    new = capture(tmp_path, ('SELECT 0; ' if shifted else '') +
                  'SELECT 1; SELECT NO_FN(1); SELECT 3;', 'duckdb')
    old, new = (new, old) if reverse else (old, new)
    saved = deepcopy((old, new))
    (tmp_path / 'query.sql').unlink()
    delta = compare(old, new)
    item, = delta['sql_changes']
    left, right = ('SELECT NO_FN(1)', 'SELECT * FROM') if reverse else ('SELECT * FROM', 'SELECT NO_FN(1)')
    assert item['source_sql'] == {'before': left, 'after': right}
    assert (item['before'], item['after']) == (('SELECT NO_FN(1)', None) if reverse else (None, 'SELECT NO_FN(1)'))
    assert (item['baseline_location']['index'], item['index']) == (
        ((3, 2) if reverse else (2, 3)) if shifted else (2, 2))
    lines = render_diff(delta, 'text').splitlines()
    pos = next(i for i, line in enumerate(lines) if line.startswith('SQL_CHANGED '))
    assert lines[pos + 1:pos + 5] == ['  before: ' + json.dumps(item['before']),
                                    '  after: ' + json.dumps(item['after']),
                                    '  source before: ' + json.dumps(left),
                                    '  source after: ' + json.dumps(right)]
    # Binding context is independent and still retains its original status pair.
    binding, = delta['binding_changes']
    assert binding['source_sql'] == item['source_sql']
    assert (binding['before'], binding['after']) == (('failed', 'not-attempted') if reverse else ('not-attempted', 'failed'))
    new_categories = {'PARSE_ERROR'} if reverse else {'ANONYMOUS_FUNCTION', 'BIND_ERROR'}
    old_categories = {'ANONYMOUS_FUNCTION', 'BIND_ERROR'} if reverse else {'PARSE_ERROR'}
    assert {f['category'] for f in delta['new_findings']} == new_categories
    assert {f['category'] for f in delta['resolved_findings']} == old_categories
    parse = next(f for f in delta['new_findings'] + delta['resolved_findings'] if f['category'] == 'PARSE_ERROR')
    assert parse['location'] == dict(line_start=1, line_end=1, column_start=20, column_end=23)
    assert delta['statement_order_changes'] == []
    assert json.loads(render_diff(delta, 'json')) == delta
    assert (old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
def test_different_sql_sources_escape_each_side(tmp_path, reverse):
    left = 'SELECT /* café 😀 "old" \\a */\r\n\t1::INT AS "雪"'
    right = 'SELECT /* naïve ☃ "new" \\b */\n\t2::INT AS "月"'
    old, new = capture(tmp_path, left + ';'), capture(tmp_path, right + ';', 'mysql')
    if reverse:
        old, new, left, right = new, old, right, left
    delta = compare(old, new)
    assert delta['sql_changes'][0]['source_sql'] == {'before': left, 'after': right}
    lines = [line for line in render_diff(delta, 'text').splitlines() if line.startswith('  source ')]
    assert lines == ['  source before: ' + json.dumps(left), '  source after: ' + json.dumps(right)]
    assert all(line.isascii() for line in lines)
    assert [json.loads(line.split(': ', 1)[1]) for line in lines] == [left, right]


@pytest.mark.parametrize('unknown_side', ['baseline', 'current', 'both'])
def test_sql_legacy_unknown_columns_and_baseline_bytes(tmp_path, unknown_side):
    source = 'SELECT 1001::INT AS amount'
    old, new = capture(tmp_path, source + ';'), capture(tmp_path, source + ';', 'mysql')
    if unknown_side in ('baseline', 'both'):
        for key in ('column_start', 'column_end'):
            del old['statements'][0][key]
    if unknown_side in ('current', 'both'):
        new['statements'][0].update(column_start=None, column_end=None)
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    delta = compare(load_baseline(str(path)), new)
    item, = delta['sql_changes']
    assert item['source_sql'] == {'before': source, 'after': source}
    old_span = 'L1-1 (columns unavailable)' if unknown_side != 'current' else 'L1-1 C1-26'
    new_span = 'L1-1 (columns unavailable)' if unknown_side != 'baseline' else 'L1-1 C1-26'
    heading = f'SQL_CHANGED query.sql#1 {new_span}'
    if unknown_side != 'both':
        heading += f' (baseline: query.sql#1 {old_span})'
    assert heading in render_diff(delta, 'text').splitlines()
    assert delta['reasons'] == ['configuration_changed', 'target_sql_changed']
    assert path.read_bytes() == original


def test_sql_duplicates_keep_occurrences_and_order_exclusion(tmp_path):
    old = capture(tmp_path, 'SELECT 1::INT; SELECT 2::INT; SELECT 1::INT; SELECT 3::INT;')
    new = capture(tmp_path, 'SELECT 3::INT; SELECT 1::INT; SELECT 2::INT; SELECT 1::INT; SELECT 1::INT;', 'mysql')
    delta = compare(old, new)
    assert [(b['baseline_location']['index'], b['index'], b['source_sql']['after'])
            for b in delta['sql_changes']] == [(4, 1, 'SELECT 3::INT'), (1, 2, 'SELECT 1::INT'),
                                              (2, 3, 'SELECT 2::INT'), (3, 4, 'SELECT 1::INT')]
    assert [m['source_sql'] for m in delta['statement_order_changes'][0]['matched_statements']] == [
        'SELECT 3::INT', 'SELECT 2::INT']
    assert delta['statement_changes'][0]['index'] == 5
    assert render_diff(delta, 'text').count('  source: "SELECT 1::INT"') == 3


@pytest.mark.parametrize('change', ['none', 'configuration', 'source', 'binding', 'analysis', 'location', 'addition', 'removal', 'null'])
def test_no_sql_change_has_no_sql_context(tmp_path, change):
    old = capture(tmp_path, 'SELECT 1;')
    new = deepcopy(old)
    if change == 'configuration':
        new['configuration']['write'] = 'mysql'
    elif change == 'source':
        new['statements'][0]['source_sql'] = 'select 1'
    elif change == 'binding':
        new['statements'][0]['binding_status'] = 'bound'
    elif change == 'analysis':
        new['statements'][0]['functions'] = {'sum': 1}
    elif change == 'location':
        new['statements'][0].update(column_start=3, column_end=10)
    elif change == 'addition':
        new = capture(tmp_path, 'SELECT 1; SELECT 2;')
    elif change == 'removal':
        new['statements'] = []
    elif change == 'null':
        old = capture(tmp_path, 'SELECT * FROM;')
        new = capture(tmp_path, 'SELECT * FROM; -- trailing comment')
        assert old['statements'][0]['target_sql'] is new['statements'][0]['target_sql'] is None
    delta = compare(old, new)
    output = render_diff(delta, 'text')
    assert delta['sql_changes'] == [] and 'SQL_CHANGED' not in output
    if change not in ('binding', 'analysis', 'addition', 'removal'):
        assert '\n  source: ' not in output
    if change in ('none', 'null'):
        assert delta['reasons'] == [] and delta['changed'] is False
