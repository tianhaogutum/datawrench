"""Source-location contracts: one-based inclusive Unicode code-point columns."""
from __future__ import annotations

from copy import deepcopy
import json
import xml.etree.ElementTree as ET

import pytest

from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, save_baseline, snapshot
from datawrench.dialect_audit.report import emit, exit_code, render
from datawrench.dialect_audit.schema import InputError


BATCH = 'SELECT 1; SELECT NO_FN(1); SELECT 3;\n'


def audit(tmp_path, text=BATCH, target='duckdb', read='snowflake'):
    source = tmp_path / 'query.sql'
    source.write_bytes(text.encode('utf-8'))
    return run_audit([str(source)], read, target)


def locations(report):
    return [(s['line_start'], s['column_start'], s['line_end'], s['column_end'])
            for s in report['statements']]


def test_same_line_batch_has_distinct_exact_spans_and_binding_outcomes(tmp_path):
    report = audit(tmp_path)
    assert locations(report) == [(1, 1, 1, 8), (1, 11, 1, 25), (1, 28, 1, 35)]
    assert [s['source_sql'] for s in report['statements']] == ['SELECT 1', 'SELECT NO_FN(1)', 'SELECT 3']
    assert [s['binding_status'] for s in report['statements']] == ['bound', 'failed', 'bound']
    assert report['statements'][1]['categories'] == ['ANONYMOUS_FUNCTION', 'BIND_ERROR']
    assert exit_code(report, 'error') == 1


def test_comments_unicode_tabs_crlf_and_multiline_final_token(tmp_path):
    # Columns count code points, including one for a tab and one for an emoji.
    text = "-- lead\r\n\t/* π; */ SELECT 'é;😀'; /* gap; */ SELECT\n  $$x;\ny$$; -- tail\r\n"
    report = audit(tmp_path, text, read='postgres')
    assert locations(report) == [(2, 11, 2, 22), (2, 36, 4, 3)]
    assert [s['source_sql'] for s in report['statements']] == ["SELECT 'é;😀'", 'SELECT\n  $$x;\ny$$']
    assert all(s['binding_status'] == 'bound' for s in report['statements'])


def test_statement_parse_error_retains_span_and_continues(tmp_path):
    report = audit(tmp_path, 'SELECT * FROM; SELECT 2;')
    assert locations(report) == [(1, 1, 1, 13), (1, 16, 1, 23)]
    assert [s['categories'] for s in report['statements']] == [['PARSE_ERROR'], ['OK']]


def test_tokenizer_failure_does_not_invent_statement_columns(tmp_path):
    report = audit(tmp_path, "SELECT 1;\n SELECT 'unterminated\n")
    assert locations(report) == [(1, None, 3, None)]
    assert report['statements'][0]['categories'] == ['PARSE_ERROR']
    assert 'L1-3 (columns unavailable)' in render(report, 'text')


def test_utf8_bom_is_not_a_source_column(tmp_path):
    report = audit(tmp_path, '\ufeffSELECT 1; SELECT 2;', target='postgres')
    assert locations(report) == [(1, 1, 1, 8), (1, 11, 1, 18)]
    assert all(s['binding_status'] == 'static-only' for s in report['statements'])


def test_no_statement_locations_for_comment_only_or_skipped_inputs(tmp_path):
    for text, category in [('; /* only; */ -- comment\n;', 'EMPTY_INPUT'),
                           ('SELECT {{ value }};', 'JINJA_SKIPPED')]:
        report = audit(tmp_path, text)
        assert report['statements'] == []
        assert report['notices'][0]['category'] == category


@pytest.mark.parametrize('format', ['json', 'text', 'md', 'junit'])
def test_all_report_formats_locate_second_statement(tmp_path, format):
    report = audit(tmp_path)
    output = render(report, format)
    if format == 'json':
        assert locations(json.loads(output)) == locations(report)
    elif format == 'junit':
        case = ET.fromstring(output).findall('testcase')[1]
        assert case.attrib['name'] == 'statement-2:L1-1 C11-25'
        payload = json.loads(case.find('system-out').text)
        assert (payload['column_start'], payload['column_end']) == (11, 25)
    else:
        assert 'L1-1 C11-25' in output
        assert 'ANONYMOUS_FUNCTION' in output and 'BIND_ERROR' in output


def test_emitted_report_keeps_source_columns(tmp_path):
    report = audit(tmp_path)
    emit(report, str(tmp_path / 'out'))
    assert locations(json.loads((tmp_path / 'out/report.json').read_text())) == locations(report)


def test_baseline_records_and_diffs_column_only_moves(tmp_path):
    before = snapshot(audit(tmp_path, 'SELECT 1; SELECT 2;'), str(tmp_path))
    path = tmp_path / 'baseline.json'
    save_baseline(before, str(path), False, [])
    assert load_baseline(str(path))['statements'][1]['column_start'] == 11
    after = snapshot(audit(tmp_path, 'SELECT 1; /* gap */ SELECT 2;'), str(tmp_path))
    delta = compare(load_baseline(str(path)), after)
    assert delta['reasons'] == ['source_changed']
    change = delta['source_changes'][0]
    assert change['index'] == 2
    assert (change['column_start'], change['column_end']) == (21, 28)
    assert (change['baseline_location']['column_start'], change['baseline_location']['column_end']) == (11, 18)
    assert change['before']['source_sql'] == change['after']['source_sql'] == 'SELECT 2'
    assert 'SOURCE_CHANGED query.sql#2 L1-1 C21-28' in render_diff(delta, 'text')


def test_diff_findings_sql_binding_additions_removals_and_notices_have_columns(tmp_path):
    before = snapshot(audit(tmp_path, 'SELECT 1; SELECT 2;'), str(tmp_path))
    after = snapshot(audit(tmp_path, BATCH.rstrip() + ' SELECT 4;'), str(tmp_path))
    delta = compare(before, after)
    for key in ('new_findings', 'sql_changes', 'binding_changes', 'source_changes'):
        assert delta[key]
        assert all((s['column_start'], s['column_end']) == (11, 25) for s in delta[key])
    assert [s['column_start'] for s in delta['statement_changes']] == [28, 38]
    reverse = compare(after, before)
    assert all(s['column_start'] == 11 for s in reverse['resolved_findings'])
    assert [s['column_start'] for s in reverse['statement_changes']] == [28, 38]
    skipped = snapshot(audit(tmp_path, 'SELECT {{ x }}'), str(tmp_path))
    notice = compare(before, skipped)['new_findings'][0]
    assert notice['index'] is None and notice['column_start'] is None and notice['column_end'] is None


def test_legacy_baseline_without_columns_remains_comparable_without_fake_drift(tmp_path):
    current = snapshot(audit(tmp_path), str(tmp_path))
    legacy = deepcopy(current)
    for stmt in legacy['statements']:
        del stmt['column_start'], stmt['column_end']
    path = tmp_path / 'legacy.json'
    path.write_text(json.dumps(legacy))
    original = path.read_bytes()
    loaded = load_baseline(str(path))
    assert compare(loaded, current)['changed'] is False
    after = snapshot(audit(tmp_path, 'SELECT 1; SELECT 2; SELECT 3;'), str(tmp_path))
    resolved = compare(loaded, after)['resolved_findings']
    assert resolved and all(s['column_start'] is None and s['column_end'] is None for s in resolved)
    assert path.read_bytes() == original


@pytest.mark.parametrize('columns', [
    {'column_start': 0, 'column_end': 8}, {'column_start': True, 'column_end': 8},
    {'column_start': 1, 'column_end': -1}, {'column_start': 9, 'column_end': 8},
    {'column_start': 1}, {'column_end': 8},
    {'column_start': None, 'column_end': 8}, {'column_start': '1', 'column_end': 8},
])
def test_malformed_baseline_column_pairs_rejected_without_mutation(tmp_path, columns):
    data = snapshot(audit(tmp_path, 'SELECT 1;'), str(tmp_path))
    stmt = data['statements'][0]
    stmt.pop('column_start', None)
    stmt.pop('column_end', None)
    stmt.update(columns)
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    with pytest.raises(InputError, match='baseline'):
        load_baseline(str(path))
    assert path.read_bytes() == original
