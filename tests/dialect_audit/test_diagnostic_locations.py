"""Parser highlights map from sliced source into inclusive original-file spans."""
from copy import deepcopy
import json
import xml.etree.ElementTree as ET

import pytest
from sqlglot.errors import ParseError, TokenError

from datawrench.dialect_audit.audit import audit_statement, run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, save_baseline, snapshot
from datawrench.dialect_audit.report import emit, exit_code, render
from datawrench.dialect_audit.schema import InputError


BATCH = '-- header\n  SELECT 1; SELECT * FROM; SELECT 3;\n'
SPAN = dict(line_start=2, line_end=2, column_start=22, column_end=25)


def audit(tmp_path, text=BATCH, target='duckdb'):
    path = tmp_path / 'query.sql'
    path.write_bytes(text.encode('utf-8'))
    return run_audit([str(path)], 'snowflake', target)


def test_original_file_highlight_preserves_statement_span_outcomes_and_continuation(tmp_path):
    report = audit(tmp_path)
    first, broken, last = report['statements']
    assert {k: broken[k] for k in SPAN} == dict(line_start=2, line_end=2, column_start=13, column_end=25)
    assert broken['source_sql'] == 'SELECT * FROM'
    assert broken['diagnostics'][0]['location'] == SPAN
    assert [s['categories'] for s in (first, broken, last)] == [['OK'], ['PARSE_ERROR'], ['OK']]
    assert [s['binding_status'] for s in (first, broken, last)] == ['bound', 'not-attempted', 'bound']
    assert report['summary']['errors'] == 1 and exit_code(report, 'error') == 1
    assert '\x1b' not in broken['diagnostics'][0]['message']
    # Existing message remains explicitly slice-relative, not falsely rewritten.
    assert 'Line 1, Col: 13' in broken['diagnostics'][0]['message']


@pytest.mark.parametrize(('text', 'expected'), [
    ('\ufeff-- π😀\r\n\t/* lead */ SELECT 1;\tSELECT * FROM; SELECT 3;', (2, 32, 2, 35)),
    ("-- lead\r\n\tSELECT 'é😀'; /* gap */ SELECT\r\n\t'é😀'\r\n\tFROM; SELECT 3;", (4, 2, 4, 5)),
    ("-- lead\n SELECT 1; SELECT 'é😀' FROM; SELECT 3;", (2, 24, 2, 27)),
    ("-- lead\r\n\tSELECT 1; SELECT 1 'é\r\n😀'; SELECT 3;", (2, 21, 3, 2)),
    ('-- lead\n  SELECT 1; SELECT (1 + ); SELECT 3;', (2, 25, 2, 25)),
    ('-- lead\n  SELECT 1; SELECT * /* π😀\n a comment */ FROM; SELECT 3;', (3, 15, 3, 18)),
])
def test_real_parser_unicode_tabs_crlf_multiline_and_midstatement_highlights(tmp_path, text, expected):
    report = audit(tmp_path, text)
    broken = report['statements'][1]
    loc = broken['diagnostics'][0]['location']
    assert (loc['line_start'], loc['column_start'], loc['line_end'], loc['column_end']) == expected
    assert broken['categories'] == ['PARSE_ERROR']
    assert report['statements'][2]['binding_status'] == 'bound'


@pytest.mark.parametrize('target', ['duckdb', 'postgres'])
def test_tokenizer_failure_has_no_diagnostic_location(tmp_path, target):
    report = audit(tmp_path, "SELECT 1;\r\n SELECT 'unterminated\n", target)
    stmt, = report['statements']
    assert stmt['column_start'] is None and stmt['column_end'] is None
    assert stmt['diagnostics'][0]['location'] is None
    assert stmt['categories'] == ['PARSE_ERROR']


def test_binder_and_static_diagnostics_have_no_source_highlight(tmp_path):
    report = audit(tmp_path, 'SELECT NO_FN(1); SELECT x FROM absent; SELECT 3;')
    findings = [d for stmt in report['statements'] for d in stmt['diagnostics']]
    assert {d['category'] for d in findings} == {'ANONYMOUS_FUNCTION', 'BIND_ERROR', 'UNKNOWN_TABLE'}
    assert all(d['location'] is None for d in findings)
    assert ' [L' not in render(report, 'text')
    assert report['statements'][-1]['binding_status'] == 'bound'


VALID = dict(line=1, col=13, highlight='FROM', start_context='SELECT * ', end_context='')


@pytest.mark.parametrize('metadata', [
    [], [dict(VALID, line=None)], [dict(VALID, line=True)], [dict(VALID, line=0)],
    [dict(VALID, line=2)], [dict(VALID, col='13')], [dict(VALID, col=False)],
    [dict(VALID, col=0)], [dict(VALID, col=14)], [dict(VALID, col=3)],
    [dict(VALID, highlight=None)], [dict(VALID, highlight='')],
    [dict(VALID, highlight='from')], [dict(VALID, highlight='SELECT * FROM EXTRA')],
    [dict(VALID, start_context='wrong')], [dict(VALID, end_context='wrong')],
    [dict(VALID), dict(VALID)], [None],
])
def test_untrustworthy_or_ambiguous_parser_metadata_stays_unknown(monkeypatch, metadata):
    def fail(*args, **kwargs):
        raise ParseError('diagnostic text must not be scraped: Line 1, Col: 13 FROM', errors=metadata)
    monkeypatch.setattr('datawrench.dialect_audit.audit.sqlglot.parse', fail)
    stmt = audit_statement('SELECT * FROM', 'query.sql', 1, 4, 4, 'snowflake', 'postgres',
                           column_start=7, column_end=19)
    assert stmt['diagnostics'][0]['location'] is None
    assert stmt['binding_status'] == 'static-only'


@pytest.mark.parametrize('exc', [TokenError('Line 1, Col: 13 FROM'), RecursionError('FROM')])
def test_nonparser_exceptions_do_not_gain_locations(monkeypatch, exc):
    def fail(*args, **kwargs):
        raise exc
    monkeypatch.setattr('datawrench.dialect_audit.audit.sqlglot.parse', fail)
    stmt = audit_statement('SELECT * FROM', 'query.sql', 1, 4, 4, 'snowflake', 'postgres',
                           column_start=7, column_end=19)
    assert stmt['diagnostics'][0]['location'] is None


def test_direct_call_without_known_origin_columns_stays_unknown():
    stmt = audit_statement('SELECT * FROM', 'query.sql', 1, 4, 4, 'snowflake', 'postgres')
    assert stmt['diagnostics'][0]['location'] is None


@pytest.mark.parametrize('format', ['json', 'text', 'md', 'junit'])
def test_report_formats_carry_diagnostic_location_separately_from_statement(tmp_path, format):
    report = audit(tmp_path)
    output = render(report, format)
    if format == 'json':
        assert json.loads(output)['statements'][1]['diagnostics'][0]['location'] == SPAN
    elif format == 'junit':
        case = ET.fromstring(output).findall('testcase')[1]
        assert case.attrib['name'] == 'statement-2:L2-2 C13-25'
        assert 'PARSE_ERROR [L2-2 C22-25]:' in case.find('failure').text
        assert json.loads(case.find('system-out').text)['diagnostics'][0]['location'] == SPAN
    else:
        assert 'L2-2 C13-25' in output
        assert 'PARSE_ERROR [L2-2 C22-25]:' in output


def test_emit_and_baseline_roundtrip_keep_diagnostic_highlight(tmp_path):
    report = audit(tmp_path)
    emit(report, str(tmp_path / 'emitted'))
    assert json.loads((tmp_path / 'emitted/report.json').read_text())['statements'][1]['diagnostics'][0]['location'] == SPAN
    data = snapshot(report, str(tmp_path))
    path = tmp_path / 'baseline.json'
    save_baseline(data, str(path), False, [])
    assert load_baseline(str(path)) == data
    assert compare(data, data)['changed'] is False


def test_legacy_diagnostics_and_shifted_highlights_do_not_change_finding_identity(tmp_path):
    data = snapshot(audit(tmp_path), str(tmp_path))
    legacy = deepcopy(data)
    del legacy['statements'][1]['diagnostics'][0]['location']
    path = tmp_path / 'legacy.json'
    path.write_text(json.dumps(legacy))
    original = path.read_bytes()
    assert compare(load_baseline(str(path)), data)['changed'] is False
    shifted = snapshot(audit(tmp_path, 'SELECT 0;\n' + BATCH), str(tmp_path))
    delta = compare(data, shifted)
    assert delta['reasons'] == ['statements_changed']
    assert delta['new_findings'] == delta['resolved_findings'] == []
    assert path.read_bytes() == original


def test_new_and_resolved_findings_keep_their_own_diagnostic_spans(tmp_path):
    valid = snapshot(audit(tmp_path, '-- header\n  SELECT 1; SELECT 2; SELECT 3;\n'), str(tmp_path))
    broken = snapshot(audit(tmp_path), str(tmp_path))
    new = compare(valid, broken)
    resolved = compare(broken, valid)
    for delta, key in [(new, 'new_findings'), (resolved, 'resolved_findings')]:
        finding, = delta[key]
        assert finding['location'] == SPAN
        assert finding['column_start'] == 13 and finding['column_end'] == 25
        assert 'PARSE_ERROR (error) [L2-2 C22-25]:' in render_diff(delta, 'text')


@pytest.mark.parametrize('location', [
    {}, [], {'line_start': 1}, dict(SPAN, column_start=0), dict(SPAN, column_end=True),
    dict(SPAN, line_start='2'), dict(SPAN, line_end=1), dict(SPAN, column_end=21),
    dict(SPAN, column_end=None), dict(SPAN, extra=1),
    dict(SPAN, line_start=1, line_end=1), dict(SPAN, column_start=12),
    dict(SPAN, column_end=26),
])
def test_baseline_rejects_invalid_or_outside_statement_diagnostic_spans(tmp_path, location):
    data = snapshot(audit(tmp_path), str(tmp_path))
    data['statements'][1]['diagnostics'][0]['location'] = location
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(data))
    with pytest.raises(InputError, match='baseline'):
        load_baseline(str(path))
