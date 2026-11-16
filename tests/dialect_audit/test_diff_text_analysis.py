"""Analysis drift carries exact source from the two actual matched records."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, snapshot


SOURCE = 'SELECT t.amount FROM sales AS t'
STUB = {'columns': ['amount'], 'table': 'main.sales', 'type': 'DOUBLE'}


def capture(tmp_path, sql=SOURCE + ';', provided=False):
    path = tmp_path / 'query.sql'
    path.write_bytes(sql.encode('utf-8'))
    schema = None
    if provided:
        schema = tmp_path / 'schema.sql'
        schema.write_text('CREATE TABLE sales (amount DOUBLE);\n')
    return snapshot(run_audit([str(path)], 'snowflake', 'duckdb',
                              schema=str(schema) if schema else None, auto_stub=True), str(tmp_path))


def analysis_lines(delta):
    lines = render_diff(delta, 'text').splitlines()
    pos = next(i for i, line in enumerate(lines) if line.startswith('ANALYSIS_CHANGED '))
    end = next((i for i in range(pos + 1, len(lines)) if not lines[i].startswith('  ')), len(lines))
    return lines[pos:end]


@pytest.mark.parametrize('reverse', [False, True])
def test_analysis_only_exact_source_forward_reverse(tmp_path, reverse):
    old, new = capture(tmp_path), capture(tmp_path, provided=True)
    if reverse:
        old, new = new, old
    saved = deepcopy((old, new))
    delta = compare(old, new)
    item, = delta['analysis_changes']
    assert item['source_sql'] == {'before': SOURCE, 'after': SOURCE}
    left, right = {'functions': {}, 'auto_stubs': [STUB]}, {'functions': {}, 'auto_stubs': []}
    if reverse:
        left, right = right, left
    assert (item['before'], item['after']) == (left, right)
    assert analysis_lines(delta) == ['ANALYSIS_CHANGED query.sql#1 L1-1 C1-31',
        '  before: ' + json.dumps(item['before']), '  after: ' + json.dumps(item['after']),
        '  source: ' + json.dumps(SOURCE)]
    assert delta['diff_version'] == 1
    assert delta['reasons'] == ['configuration_changed', 'analysis_changed']
    assert delta['current_binding_statuses'] == {'bound': 1}
    for field in ('statement_changes', 'statement_order_changes', 'source_changes', 'sql_changes',
                  'binding_changes', 'new_findings', 'resolved_findings'):
        assert delta[field] == []
    assert json.loads(render_diff(delta, 'json')) == delta
    assert (old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_shifted_analysis_sources_escape_without_rereads(tmp_path, monkeypatch, reverse, newline):
    source = 'SELECT /* café 😀 "quote" \\path */' + newline + '\tt.amount::INT AS "雪" FROM sales AS t'
    old = capture(tmp_path, source + ';')
    new = capture(tmp_path, 'SELECT 0; ' + source + ';', provided=True)
    if reverse:
        old, new = new, old
    saved = deepcopy((old, new))

    def deny_read(*args, **kwargs):
        raise AssertionError('compare/render must use stored records without reads')

    with monkeypatch.context() as m:
        m.setattr(Path, 'open', deny_read)
        delta = compare(old, new)
        lines = analysis_lines(delta)
    item, = delta['analysis_changes']
    assert item['source_sql'] == {'before': source, 'after': source}
    assert old['statements'][-1]['target_sql'] != source
    assert (item['baseline_location']['index'], item['index']) == ((2, 1) if reverse else (1, 2))
    current, baseline = ('#1 L1-2 C1-37', '#2 L1-2 C11-37') if reverse else (
        '#2 L1-2 C11-37', '#1 L1-2 C1-37')
    assert lines == [f'ANALYSIS_CHANGED query.sql{current} (baseline: query.sql{baseline})',
        '  before: ' + json.dumps(item['before']), '  after: ' + json.dumps(item['after']),
        '  source: ' + json.dumps(source)]
    assert lines[-1].isascii() and json.loads(lines[-1].split(': ', 1)[1]) == source
    assert delta['reasons'] == ['configuration_changed', 'statements_changed', 'analysis_changed']
    assert delta['source_changes'] == delta['sql_changes'] == delta['binding_changes'] == []
    assert delta['statement_order_changes'] == delta['new_findings'] == delta['resolved_findings'] == []
    assert json.loads(render_diff(delta, 'json')) == delta
    assert (old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('shifted', [False, True])
def test_differing_analysis_sources_follow_existing_fallback(tmp_path, reverse, shifted):
    old = capture(tmp_path, 'SELECT 1; ' + SOURCE + '; SELECT 3;')
    after_source = 'SELECT SUM(t.amount) FROM sales AS t'
    new = capture(tmp_path, ('SELECT 0; ' if shifted else '') +
                  'SELECT 1; ' + after_source + '; SELECT 3;', provided=True)
    if reverse:
        old, new = new, old
    saved = deepcopy((old, new))
    (tmp_path / 'query.sql').unlink()
    (tmp_path / 'schema.sql').unlink()
    delta = compare(old, new)
    item, = delta['analysis_changes']
    left, right = (after_source, SOURCE) if reverse else (SOURCE, after_source)
    assert item['source_sql'] == {'before': left, 'after': right}
    before, after = {'functions': {}, 'auto_stubs': [STUB]}, {'functions': {'sum': 1}, 'auto_stubs': []}
    assert (item['before'], item['after']) == ((after, before) if reverse else (before, after))
    assert (item['baseline_location']['index'], item['index']) == (
        ((3, 2) if reverse else (2, 3)) if shifted else (2, 2))
    assert analysis_lines(delta)[1:] == ['  before: ' + json.dumps(item['before']),
        '  after: ' + json.dumps(item['after']), '  source before: ' + json.dumps(left),
        '  source after: ' + json.dumps(right)]
    assert delta['source_changes'][0]['before']['source_sql'] == left
    assert delta['source_changes'][0]['after']['source_sql'] == right
    assert delta['sql_changes'][0]['source_sql'] == item['source_sql']
    assert delta['binding_changes'] == delta['statement_order_changes'] == []
    assert delta['new_findings'] == delta['resolved_findings'] == []
    assert delta['reasons'] == ['configuration_changed', *(['statements_changed'] if shifted else []),
                               'source_changed', 'target_sql_changed', 'analysis_changed']
    assert json.loads(render_diff(delta, 'json')) == delta
    assert (old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
def test_differing_analysis_sources_escape_each_side(tmp_path, reverse):
    left = 'SELECT /* café 😀 "old" \\a */\r\n\tt.amount FROM sales AS t'
    right = 'SELECT /* naïve ☃ "new" \\b */\n\tSUM(t.amount) FROM sales AS t'
    old, new = capture(tmp_path, left + ';'), capture(tmp_path, right + ';', provided=True)
    if reverse:
        old, new, left, right = new, old, right, left
    delta = compare(old, new)
    assert delta['analysis_changes'][0]['source_sql'] == {'before': left, 'after': right}
    lines = analysis_lines(delta)[3:]
    assert lines == ['  source before: ' + json.dumps(left), '  source after: ' + json.dumps(right)]
    assert all(line.isascii() for line in lines)
    assert [json.loads(line.split(': ', 1)[1]) for line in lines] == [left, right]


@pytest.mark.parametrize('unknown_side', ['baseline', 'current', 'both'])
def test_analysis_legacy_unknown_columns_and_baseline_bytes(tmp_path, unknown_side):
    old, new = capture(tmp_path), capture(tmp_path, provided=True)
    if unknown_side in ('baseline', 'both'):
        for key in ('column_start', 'column_end'):
            del old['statements'][0][key]
    if unknown_side in ('current', 'both'):
        new['statements'][0].update(column_start=None, column_end=None)
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    old = load_baseline(str(path))
    saved = deepcopy((old, new))
    delta = compare(old, new)
    item, = delta['analysis_changes']
    assert item['source_sql'] == {'before': SOURCE, 'after': SOURCE}
    old_span = 'L1-1 (columns unavailable)' if unknown_side != 'current' else 'L1-1 C1-31'
    new_span = 'L1-1 (columns unavailable)' if unknown_side != 'baseline' else 'L1-1 C1-31'
    heading = f'ANALYSIS_CHANGED query.sql#1 {new_span}'
    if unknown_side != 'both':
        heading += f' (baseline: query.sql#1 {old_span})'
    assert analysis_lines(delta)[0] == heading
    assert delta['reasons'] == ['configuration_changed', 'analysis_changed']
    assert path.read_bytes() == original
    assert (old, new) == saved


def test_analysis_duplicates_keep_occurrences_and_order_exclusion(tmp_path):
    a, b, c = SOURCE, SOURCE.replace('t.amount', 't.amount + 1'), SOURCE.replace('t.amount', 't.amount + 2')
    old = capture(tmp_path, '; '.join([a, b, a, c]) + ';')
    new = capture(tmp_path, '; '.join([c, a, b, a, a]) + ';', provided=True)
    delta = compare(old, new)
    assert [(x['baseline_location']['index'], x['index'], x['source_sql']['after'])
            for x in delta['analysis_changes']] == [(4, 1, c), (1, 2, a), (2, 3, b), (3, 4, a)]
    assert [m['source_sql'] for m in delta['statement_order_changes'][0]['matched_statements']] == [c, b]
    assert delta['statement_changes'][0]['index'] == 5
    assert render_diff(delta, 'text').count('  source: ' + json.dumps(a)) == 3


def test_functions_only_analysis_retains_source_and_null_targets(tmp_path):
    # Synthetic analysis/tool drift isolates functions from every other field.
    old = capture(tmp_path, 'SELECT * FROM;')
    new = deepcopy(old)
    new['statements'][0]['functions'] = {'sum': 1}
    delta = compare(old, new)
    item, = delta['analysis_changes']
    assert item['source_sql'] == {'before': 'SELECT * FROM', 'after': 'SELECT * FROM'}
    assert item['before'] == {'functions': {}, 'auto_stubs': []}
    assert item['after'] == {'functions': {'sum': 1}, 'auto_stubs': []}
    assert delta['reasons'] == ['analysis_changed']
    assert old['statements'][0]['target_sql'] is new['statements'][0]['target_sql'] is None
    assert analysis_lines(delta)[-1] == '  source: "SELECT * FROM"'
    assert delta['new_findings'] == delta['resolved_findings'] == []


@pytest.mark.parametrize('reverse', [False, True])
def test_analysis_context_preserves_new_resolved_parser_and_binding_evidence(tmp_path, reverse):
    old = capture(tmp_path, 'SELECT 1; SELECT * FROM; SELECT 3;')
    new = capture(tmp_path, 'SELECT 0; SELECT 1; SELECT NO_FN(1); SELECT 3;')
    if reverse:
        old, new = new, old
    saved = deepcopy((old, new))
    delta = compare(old, new)
    item, = delta['analysis_changes']
    left, right = ('SELECT NO_FN(1)', 'SELECT * FROM') if reverse else ('SELECT * FROM', 'SELECT NO_FN(1)')
    assert item['source_sql'] == {'before': left, 'after': right}
    assert analysis_lines(delta)[3:] == ['  source before: ' + json.dumps(left), '  source after: ' + json.dumps(right)]
    assert {f['category'] for f in delta['new_findings']} == ({'PARSE_ERROR'} if reverse else {'ANONYMOUS_FUNCTION', 'BIND_ERROR'})
    assert {f['category'] for f in delta['resolved_findings']} == ({'ANONYMOUS_FUNCTION', 'BIND_ERROR'} if reverse else {'PARSE_ERROR'})
    parse = next(f for f in delta['new_findings'] + delta['resolved_findings'] if f['category'] == 'PARSE_ERROR')
    assert parse['location'] == dict(line_start=1, line_end=1, column_start=20, column_end=23)
    assert delta['sql_changes'][0]['source_sql'] == delta['binding_changes'][0]['source_sql'] == item['source_sql']
    assert json.loads(render_diff(delta, 'json')) == delta
    assert (old, new) == saved


@pytest.mark.parametrize('change', ['none', 'configuration', 'source', 'target', 'binding', 'location',
                                   'addition', 'removal', 'order', 'null'])
def test_no_analysis_change_has_no_analysis_context(tmp_path, change):
    old = capture(tmp_path, 'SELECT 1;')
    new = deepcopy(old)
    if change == 'configuration':
        new['configuration']['auto_stub'] = False
    elif change == 'source':
        new['statements'][0]['source_sql'] = 'select 1'
    elif change == 'target':
        new['statements'][0]['target_sql'] = 'SELECT CAST(1 AS INT)'
    elif change == 'binding':
        new['statements'][0]['binding_status'] = 'static-only'
    elif change == 'location':
        new['statements'][0].update(column_start=3, column_end=10)
    elif change == 'addition':
        new = capture(tmp_path, 'SELECT 1; SELECT 2;')
    elif change == 'removal':
        new['statements'] = []
    elif change == 'order':
        old = capture(tmp_path, 'SELECT 1; SELECT 2; SELECT 3;')
        new = capture(tmp_path, 'SELECT 3; SELECT 1; SELECT 2;')
    elif change == 'null':
        old = capture(tmp_path, 'SELECT * FROM;')
        new = capture(tmp_path, 'SELECT * FROM; -- trailing comment')
    delta = compare(old, new)
    output = render_diff(delta, 'text')
    assert delta['analysis_changes'] == [] and 'ANALYSIS_CHANGED' not in output
    if change not in ('target', 'binding', 'addition', 'removal', 'order'):
        assert '\n  source: ' not in output
    if change in ('none', 'null'):
        assert delta['reasons'] == [] and delta['changed'] is False
