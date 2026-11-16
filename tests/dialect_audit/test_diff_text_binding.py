"""Binding drift retains exact sources from both actual paired records."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, snapshot


def capture(tmp_path, sql, target='duckdb'):
    path = tmp_path / 'query.sql'
    path.write_bytes(sql.encode('utf-8'))
    return snapshot(run_audit([str(path)], 'snowflake', target), str(tmp_path))


@pytest.mark.parametrize('reverse', [False, True])
def test_binding_only_exact_source_forward_reverse(tmp_path, reverse):
    sql = 'SELECT 1001 AS amount;'
    old = capture(tmp_path, sql, 'duckdb' if reverse else 'postgres')
    new = capture(tmp_path, sql, 'postgres' if reverse else 'duckdb')
    saved = deepcopy((old, new))
    delta = compare(old, new)
    item, = delta['binding_changes']
    assert item['source_sql'] == {'before': sql[:-1], 'after': sql[:-1]}
    assert delta['reasons'] == ['configuration_changed', 'binding_changed']
    left, right = ('bound', 'static-only') if reverse else ('static-only', 'bound')
    assert item['before'] == left and item['after'] == right
    assert (f'BINDING_CHANGED query.sql#1 L1-1 C1-21\n'
            f'  before: "{left}"\n  after: "{right}"\n'
            '  source: "SELECT 1001 AS amount"\n') in render_diff(delta, 'text')
    assert json.loads(render_diff(delta, 'json')) == delta
    assert (old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_shifted_binding_source_uses_records_without_rereads(tmp_path, monkeypatch, reverse, newline):
    source = 'SELECT /* café 😀 "quote" \\path */' + newline + '\t4::INT AS "雪"'
    old = capture(tmp_path, source + ';', 'postgres')
    new = capture(tmp_path, 'SELECT 0; ' + source + ';')
    old, new = (new, old) if reverse else (old, new)
    saved = deepcopy((old, new))
    def deny_read(*args, **kwargs):
        raise AssertionError('compare/render must use stored records without file reads')
    with monkeypatch.context() as m:
        m.setattr(Path, 'open', deny_read)
        delta = compare(old, new)
        output = render_diff(delta, 'text')
    item, = delta['binding_changes']
    assert item['source_sql'] == {'before': source, 'after': source}
    assert source != new['statements'][-1]['target_sql']
    assert (item['baseline_location']['index'], item['index']) == ((2, 1) if reverse else (1, 2))
    assert delta['reasons'] == ['configuration_changed', 'statements_changed', 'binding_changed']
    baseline, current = ('#2 L1-2 C11-14', '#1 L1-2 C1-14') if reverse else (
        '#1 L1-2 C1-14', '#2 L1-2 C11-14')
    lines = output.splitlines()
    pos = lines.index(f'BINDING_CHANGED query.sql{current} (baseline: query.sql{baseline})')
    assert lines[pos + 3] == '  source: ' + json.dumps(source, ensure_ascii=True)
    assert lines[pos + 3].isascii()
    assert json.loads(lines[pos + 3].removeprefix('  source: ')) == source
    assert len(delta['statement_changes']) == 1
    assert delta['source_changes'] == delta['sql_changes'] == delta['statement_order_changes'] == []
    assert json.loads(render_diff(delta, 'json')) == delta
    assert (old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('shifted', [False, True])
def test_different_binding_sources_follow_existing_edit_fallback(tmp_path, monkeypatch, reverse, shifted):
    # The shifted variant requires the existing bounded singleton-anchor rule;
    # the other variant uses the same-index fallback. Both keep actual sources.
    old_sql = 'SELECT 1; SELECT * FROM; SELECT 3;'
    new_sql = ('SELECT 0; ' if shifted else '') + 'SELECT 1; SELECT NO_FN(1); SELECT 3;'
    old, new = capture(tmp_path, old_sql), capture(tmp_path, new_sql)
    old, new = (new, old) if reverse else (old, new)
    saved = deepcopy((old, new))
    (tmp_path / 'query.sql').unlink()  # In particular, the baseline source no longer exists.
    delta = compare(old, new)
    item, = delta['binding_changes']
    left, right = ('SELECT NO_FN(1)', 'SELECT * FROM') if reverse else ('SELECT * FROM', 'SELECT NO_FN(1)')
    assert item['source_sql'] == {'before': left, 'after': right}
    assert (item['before'], item['after']) == (('failed', 'not-attempted') if reverse else ('not-attempted', 'failed'))
    assert (item['baseline_location']['index'], item['index']) == (
        ((3, 2) if reverse else (2, 3)) if shifted else (2, 2))
    output = render_diff(delta, 'text')
    lines = output.splitlines()
    pos = next(i for i, line in enumerate(lines) if line.startswith('BINDING_CHANGED '))
    assert lines[pos + 3:pos + 5] == ['  source before: ' + json.dumps(left),
                                         '  source after: ' + json.dumps(right)]
    assert delta['source_changes'][0]['before']['source_sql'] == left
    assert delta['source_changes'][0]['after']['source_sql'] == right
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
def test_differing_sources_escape_each_payload_independently(tmp_path, reverse):
    old_source = 'SELECT /* café 😀 "old" \\a */\r\n\t1::INT AS "雪"'
    new_source = 'SELECT /* naïve ☃ "new" \\b */\n\t2::INT AS "月"'
    old, new = capture(tmp_path, old_source + ';', 'postgres'), capture(tmp_path, new_source + ';')
    old, new = (new, old) if reverse else (old, new)
    left, right = (new_source, old_source) if reverse else (old_source, new_source)
    delta = compare(old, new)
    assert delta['binding_changes'][0]['source_sql'] == {'before': left, 'after': right}
    output = render_diff(delta, 'text').splitlines()
    pos = next(i for i, line in enumerate(output) if line.startswith('BINDING_CHANGED '))
    lines = output[pos + 3:pos + 5]
    assert lines == ['  source before: ' + json.dumps(left), '  source after: ' + json.dumps(right)]
    assert all(line.isascii() for line in lines)


@pytest.mark.parametrize('unknown_side', ['baseline', 'current', 'both'])
def test_binding_legacy_unknown_columns_and_baseline_bytes(tmp_path, unknown_side):
    sql = 'SELECT 1001 AS amount;'
    old, new = capture(tmp_path, sql, 'postgres'), capture(tmp_path, sql)
    if unknown_side in ('baseline', 'both'):
        for key in ('column_start', 'column_end'):
            del old['statements'][0][key]
    if unknown_side in ('current', 'both'):
        new['statements'][0].update(column_start=None, column_end=None)
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    old = load_baseline(str(path))
    delta = compare(old, new)
    item, = delta['binding_changes']
    assert item['source_sql'] == {'before': sql[:-1], 'after': sql[:-1]}
    lines = render_diff(delta, 'text').splitlines()
    old_span = 'L1-1 (columns unavailable)' if unknown_side != 'current' else 'L1-1 C1-21'
    new_span = 'L1-1 (columns unavailable)' if unknown_side != 'baseline' else 'L1-1 C1-21'
    heading = f'BINDING_CHANGED query.sql#1 {new_span}'
    if unknown_side != 'both':
        heading += f' (baseline: query.sql#1 {old_span})'
    assert heading in lines
    assert delta['reasons'] == ['configuration_changed', 'binding_changed']
    assert path.read_bytes() == original


def test_binding_duplicates_keep_occurrences_and_order_exclusion(tmp_path):
    old = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 1; SELECT 3;', 'postgres')
    new = capture(tmp_path, 'SELECT 3; SELECT 1; SELECT 2; SELECT 1; SELECT 1;')
    delta = compare(old, new)
    assert [(b['baseline_location']['index'], b['index'], b['source_sql']['after'])
            for b in delta['binding_changes']] == [(4, 1, 'SELECT 3'), (1, 2, 'SELECT 1'),
                                                    (2, 3, 'SELECT 2'), (3, 4, 'SELECT 1')]
    assert [m['source_sql'] for m in delta['statement_order_changes'][0]['matched_statements']] == [
        'SELECT 3', 'SELECT 2']
    assert delta['statement_changes'][0]['index'] == 5
    # Two paired occurrences plus the added occurrence all retain their source.
    assert render_diff(delta, 'text').count('  source: "SELECT 1"') == 3


@pytest.mark.parametrize('change', ['none', 'configuration', 'source', 'target', 'analysis', 'location', 'addition', 'removal'])
def test_no_binding_change_has_no_binding_context(tmp_path, change):
    old = capture(tmp_path, 'SELECT 1;', 'postgres')
    new = deepcopy(old)
    if change == 'configuration':
        new['configuration']['write'] = 'mysql'
    elif change == 'source':
        new['statements'][0]['source_sql'] = 'select 1'
    elif change == 'target':
        new['statements'][0]['target_sql'] = 'SELECT CAST(1 AS INT)'
    elif change == 'analysis':
        new['statements'][0]['functions'] = {'sum': 1}
    elif change == 'location':
        new['statements'][0].update(column_start=3, column_end=10)
    elif change == 'addition':
        new = capture(tmp_path, 'SELECT 1; SELECT 2;', 'postgres')
    elif change == 'removal':
        new['statements'] = []
    delta = compare(old, new)
    output = render_diff(delta, 'text')
    assert delta['binding_changes'] == []
    assert 'BINDING_CHANGED' not in output
    assert '  source before:' not in output and '  source after:' not in output
    if change not in ('target', 'analysis', 'addition', 'removal'):
        assert '\n  source: ' not in output
    if change == 'none':
        assert delta['reasons'] == [] and delta['changed'] is False
