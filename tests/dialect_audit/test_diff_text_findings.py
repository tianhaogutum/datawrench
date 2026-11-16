"""Finding context uses the actual enclosing record, independent of identity/evidence."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, snapshot
from datawrench.dialect_audit.report import diagnostic_label, format_location, related_details


SOURCE = 'SELECT t.amount + 1 FROM sales AS t'
OLD_SCHEMA = 'CREATE TABLE sales (other DOUBLE);\n'
NEW_SCHEMA = 'CREATE TABLE sales (amount VARCHAR);\n'
LOCATION_KEYS = ('file', 'index', 'line_start', 'line_end', 'column_start', 'column_end')


def capture(tmp_path, sql=SOURCE + ';\n', schema=None):
    path = tmp_path / 'query.sql'
    path.write_bytes(sql.encode())
    if schema is not None:
        (tmp_path / 'schema.sql').write_text(schema)
    return snapshot(run_audit([str(path)], 'snowflake', 'duckdb',
                    schema=str(tmp_path / 'schema.sql') if schema is not None else None), str(tmp_path))


def check_context(delta, old, new):
    """Assert full diagnostic/evidence payload plus exact rendering, without mutation."""
    saved = deepcopy((delta, old, new))
    text = render_diff(delta, 'text')
    for field, label, data in [('new_findings', 'NEW', new), ('resolved_findings', 'RESOLVED', old)]:
        for item in delta[field]:
            if item['index'] is None:
                assert 'source_sql' not in item
                continue
            stmt, = [s for s in data['statements'] if (s['file'], s['index']) == (item['file'], item['index'])]
            diagnostic = {k: v for k, v in item.items() if k not in (*LOCATION_KEYS, 'source_sql')}
            assert diagnostic in stmt['diagnostics']
            assert item == {**{k: stmt.get(k) for k in LOCATION_KEYS}, **diagnostic,
                            'source_sql': stmt['source_sql']}
            heading = (f"{label} {item['file']}#{item['index']} {format_location(item)} "
                       f"{item['category']} ({item['severity']}){diagnostic_label(item)}: {item['message']}")
            payload = '  source: ' + json.dumps(stmt['source_sql'])
            block = '\n'.join([heading, *('  ' + line for line in related_details(item)), payload]) + '\n'
            assert block in text
            assert payload.isascii() and json.loads(payload.split(': ', 1)[1]) == stmt['source_sql']
    assert json.loads(render_diff(delta, 'json')) == delta
    assert (delta, old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
def test_finding_only_failed_to_failed_forward_reverse(tmp_path, reverse):
    old, new = capture(tmp_path, schema=OLD_SCHEMA), capture(tmp_path, schema=NEW_SCHEMA)
    if reverse:
        old, new = new, old
    saved = deepcopy((old, new))
    delta = compare(old, new)
    a, = delta['new_findings']
    b, = delta['resolved_findings']
    assert a['source_sql'] == b['source_sql'] == SOURCE
    assert a['location'] is b['location'] is None
    assert a['category'] == b['category'] == 'BIND_ERROR'
    assert a['message'] == new['statements'][0]['diagnostics'][0]['message']
    assert b['message'] == old['statements'][0]['diagnostics'][0]['message']
    assert delta['reasons'] == ['configuration_changed', 'new_findings', 'resolved_findings']
    assert delta['diff_version'] == 1 and delta['current_binding_statuses'] == {'failed': 1}
    for key in ('statement_changes', 'statement_order_changes', 'source_changes', 'sql_changes',
                'binding_changes', 'analysis_changes'):
        assert delta[key] == []
    text = render_diff(delta, 'text')
    for label in ('NEW', 'RESOLVED'):
        assert f'{label} query.sql#1 L1-1 C1-35 BIND_ERROR (error):' in text
    assert text.count('  source: ' + json.dumps(SOURCE)) == 2
    check_context(delta, old, new)
    assert (old, new) == saved


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_shifted_escaped_source_without_reads(tmp_path, monkeypatch, reverse, newline):
    source = 'SELECT /* café 😀 "quote" \\path */' + newline + '\tt.amount + 1 AS "雪" FROM sales AS t'
    old = capture(tmp_path, source + ';', OLD_SCHEMA)
    new = capture(tmp_path, 'SELECT 0; ' + source + ';', NEW_SCHEMA)
    if reverse:
        old, new = new, old
    def deny(*args, **kwargs):
        raise AssertionError('finding comparison/render must not read files')
    with monkeypatch.context() as m:
        m.setattr(Path, 'open', deny)
        delta = compare(old, new)
        check_context(delta, old, new)
    assert [x['source_sql'] for k in ('new_findings', 'resolved_findings') for x in delta[k]] == [source, source]
    assert (delta['resolved_findings'][0]['index'], delta['new_findings'][0]['index']) == ((2, 1) if reverse else (1, 2))
    assert delta['reasons'] == ['configuration_changed', 'statements_changed', 'new_findings', 'resolved_findings']
    assert delta['source_changes'] == delta['sql_changes'] == delta['binding_changes'] == delta['analysis_changes'] == []


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('shifted', [False, True])
def test_different_sources_through_both_existing_fallbacks(tmp_path, reverse, shifted):
    # Two real unique anchors enclose an edited statement; shifted variant uses anchor-gap fallback.
    left = 'SELECT /* old café "a" \\one */\r\n\tt.amount + 1 FROM sales AS t'
    right = 'SELECT /* new 雪 😀 "b" \\two */\n\tt.amount + 2 FROM sales AS t'
    old = capture(tmp_path, 'SELECT 1; ' + left + '; SELECT 3;', OLD_SCHEMA)
    new = capture(tmp_path, ('SELECT 0; ' if shifted else '') + 'SELECT 1; ' + right + '; SELECT 3;', NEW_SCHEMA)
    if reverse:
        old, new, left, right = new, old, right, left
    (tmp_path / 'query.sql').unlink()
    (tmp_path / 'schema.sql').unlink()
    delta = compare(old, new)
    assert [f['source_sql'] for f in delta['new_findings']] == [right]
    assert [f['source_sql'] for f in delta['resolved_findings']] == [left]
    expected_indices = ((3, 2) if reverse else (2, 3)) if shifted else (2, 2)
    assert (delta['source_changes'][0]['baseline_location']['index'], delta['source_changes'][0]['index']) == expected_indices
    assert delta['statement_order_changes'] == delta['binding_changes'] == []
    assert delta['statement_changes'] == [] if not shifted else len(delta['statement_changes']) == 1
    check_context(delta, old, new)


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('source', ['SELECT * FROM', 'SELECT (SELECT * FROM), (SELECT * FROM)', "SELECT 'unterminated\n"])
def test_added_removed_parser_primary_related_or_unknown(tmp_path, reverse, source):
    old, new = capture(tmp_path, 'SELECT 1;'), capture(tmp_path, source + (';' if 'unterminated' not in source else ''))
    # Isolate unpaired records (file boundaries prevent accidental index fallback).
    new['files'] = ['other.sql']
    new['statements'][0]['file'] = 'other.sql'
    if reverse:
        old, new = new, old
    delta = compare(old, new)
    field = 'resolved_findings' if reverse else 'new_findings'
    item, = delta[field]
    assert item['source_sql'] == source
    assert item['category'] == 'PARSE_ERROR'
    if 'unterminated' in source:
        assert item['location'] is item['column_start'] is item['column_end'] is None
    elif '(' in source:
        assert item['location'] is None and len(item['related_locations']) == 2
    else:
        assert item['location'] == dict(line_start=1, line_end=1, column_start=10, column_end=13)
    assert len(delta['statement_changes']) == 2
    check_context(delta, old, new)


@pytest.mark.parametrize('reverse', [False, True])
def test_duplicates_keep_counts_candidate_order_and_enclosing_occurrences(tmp_path, reverse):
    a, b, c = 'SELECT NO_FN(1)', 'SELECT OTHER_FN(2)', 'SELECT THIRD_FN(3)'
    old = capture(tmp_path, '; '.join([a, b, a, c]) + ';')
    new = capture(tmp_path, '; '.join([c, a, b, a, a]) + ';')
    # Synthetic diagnostic multiplicity drift exercises existing counter/candidate policy,
    # including two equal identities with different related evidence.
    d = dict(category='PARSE_ERROR', severity='error', message='same', location=None,
             related_locations=[dict(description='first', location=None), dict(description=None, location=None)])
    e = deepcopy(d);e['related_locations'][0]['description'] = 'second'
    old['statements'][0]['diagnostics'] = [deepcopy(d)]
    new['statements'][1]['diagnostics'] = [deepcopy(d), deepcopy(e), dict(category='A', severity='warn', message='z', location=None)]
    if reverse:
        old, new = new, old
    delta = compare(old, new)
    field = 'resolved_findings' if reverse else 'new_findings'
    findings = delta[field]
    assert [(f['index'], f['category'], f['source_sql']) for f in findings] == [
        (2, 'A', a), (2, 'PARSE_ERROR', a), (5, 'ANONYMOUS_FUNCTION', a), (5, 'BIND_ERROR', a)]
    # Existing selection chooses the first excess-count candidate; preserve that evidence.
    assert findings[1]['related_locations'] == d['related_locations']
    assert [m['source_sql'] for m in delta['statement_order_changes'][0]['matched_statements']] == ([b, c] if reverse else [c, b])
    check_context(delta, old, new)


@pytest.mark.parametrize('legacy', ['columns', 'diagnostic', 'both'])
def test_legacy_finding_fields_and_baseline_bytes(tmp_path, legacy):
    old, new = capture(tmp_path, 'SELECT (SELECT * FROM), (SELECT * FROM);'), capture(tmp_path, 'SELECT NO_FN(1);')
    if legacy in ('columns', 'both'):
        del old['statements'][0]['column_start'], old['statements'][0]['column_end']
    if legacy in ('diagnostic', 'both'):
        d = old['statements'][0]['diagnostics'][0]
        del d['location'], d['related_locations']
    path = tmp_path / 'baseline.json';path.write_text(json.dumps(old))
    original = path.read_bytes();old = load_baseline(str(path))
    delta = compare(old, new)
    item, = delta['resolved_findings']
    assert item['source_sql'] == 'SELECT (SELECT * FROM), (SELECT * FROM)'
    if legacy in ('columns', 'both'):
        assert item['column_start'] is item['column_end'] is None
        assert 'RESOLVED query.sql#1 L1-1 (columns unavailable)' in render_diff(delta, 'text')
    if legacy in ('diagnostic', 'both'):
        assert 'location' not in item and 'related_locations' not in item
    check_context(delta, old, new)
    assert path.read_bytes() == original


@pytest.mark.parametrize('reverse', [False, True])
def test_file_notices_omit_context(tmp_path, reverse):
    old, new = capture(tmp_path, '-- empty'), capture(tmp_path, 'SELECT {{ value }};')
    if reverse:
        old, new = new, old
    delta = compare(old, new)
    assert delta['reasons'] == ['new_findings', 'resolved_findings']
    for field in ('new_findings', 'resolved_findings'):
        item, = delta[field]
        assert 'source_sql' not in item
        assert all(item[k] is None for k in LOCATION_KEYS if k != 'file')
    text = render_diff(delta, 'text')
    assert '\n  source:' not in text
    check_context(delta, old, new)


@pytest.mark.parametrize('change', ['none', 'configuration', 'source', 'shift', 'evidence'])
def test_unchanged_finding_identity_has_no_finding_context(tmp_path, change):
    old = capture(tmp_path, 'SELECT (SELECT * FROM), (SELECT * FROM);')
    new = deepcopy(old)
    stmt = new['statements'][0]
    if change == 'configuration':
        new['configuration']['auto_stub'] = True
    elif change == 'source':
        stmt['source_sql'] += ' /* changed source */'
    elif change == 'shift':
        stmt['index'] = 2
    elif change == 'evidence':
        stmt['diagnostics'][0]['related_locations'].reverse()
        stmt['diagnostics'][0]['related_locations'][0]['location'] = None
        del old['statements'][0]['diagnostics'][0]['location']
    delta = compare(old, new)
    assert delta['new_findings'] == delta['resolved_findings'] == []
    text = render_diff(delta, 'text')
    assert '\nNEW ' not in text and '\nRESOLVED ' not in text
    assert '\n  source:' not in text
    if change in ('none', 'shift', 'evidence'):
        assert delta['changed'] is False
    check_context(delta, old, new)
