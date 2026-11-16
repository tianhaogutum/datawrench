"""Aggregates retain ordered, independently verified evidence, never a primary."""
from copy import deepcopy
import json
import re
import xml.etree.ElementTree as ET

import pytest
import sqlglot
from sqlglot import ErrorLevel
from sqlglot.errors import ParseError, TokenError

from datawrench.dialect_audit.audit import audit_statement, run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, save_baseline, snapshot
from datawrench.dialect_audit.report import emit, exit_code, render
from datawrench.dialect_audit.schema import InputError


BATCH = '-- header\n  SELECT 1; SELECT (SELECT * FROM), (SELECT * FROM); SELECT 3;\n'
SOURCE = 'SELECT (SELECT * FROM), (SELECT * FROM)'
SPANS = [dict(line_start=2, line_end=2, column_start=c, column_end=c) for c in (34, 51)]
VALID = dict(description='missing table', line=1, col=13, highlight='FROM',
             start_context='SELECT * ', end_context='')
MAPPED = dict(line_start=4, line_end=4, column_start=16, column_end=19)


def audit(tmp_path, text=BATCH):
    path = tmp_path / 'query.sql'
    path.write_bytes(text.encode('utf-8'))
    return run_audit([str(path)], 'snowflake', 'duckdb')


def fake(monkeypatch, entries, *, origin=7):
    def fail(*args, **kwargs):
        raise ParseError('aggregate Line 1, Col: 13 FROM', errors=entries)
    monkeypatch.setattr('datawrench.dialect_audit.audit.sqlglot.parse', fail)
    stmt = audit_statement('SELECT * FROM', 'query.sql', 1, 4, 4, 'snowflake', 'postgres',
                           column_start=origin, column_end=19 if origin else None)
    diagnostic, = stmt['diagnostics']
    assert diagnostic['location'] is None
    assert diagnostic['message'] == 'aggregate Line 1, Col: 13 FROM'
    assert stmt['categories'] == ['PARSE_ERROR']
    return diagnostic


def test_real_aggregate_keeps_message_spans_counts_and_recovery(tmp_path):
    with pytest.raises(ParseError) as caught:
        sqlglot.parse(SOURCE, read='snowflake', error_level=ErrorLevel.RAISE)
    entries = caught.value.errors
    assert len(entries) == 2
    report = audit(tmp_path)
    first, broken, last = report['statements']
    diagnostic, = broken['diagnostics']
    assert diagnostic['location'] is None
    assert diagnostic['message'] == re.sub(r'\x1b\[[0-9;]*m', '', str(caught.value))
    assert diagnostic['related_locations'] == [
        {'description': entry['description'], 'location': span} for entry, span in zip(entries, SPANS)]
    assert [tuple(s[k] for k in ('line_start', 'line_end', 'column_start', 'column_end'))
            for s in (first, broken, last)] == [(2, 2, 3, 10), (2, 2, 13, 51), (2, 2, 54, 61)]
    assert [s['binding_status'] for s in (first, broken, last)] == ['bound', 'not-attempted', 'bound']
    assert [s['categories'] for s in (first, broken, last)] == [['OK'], ['PARSE_ERROR'], ['OK']]
    assert report['summary'] == dict(files=1, statements=3, ok=2, flagged=1, errors=1, warnings=0,
                                     skipped_files=0, categories={'PARSE_ERROR': 1, 'OK': 2})
    assert [exit_code(report, p) for p in ('error', 'warn', 'never')] == [1, 1, 0]


@pytest.mark.parametrize('bad', [
    None, {}, dict(VALID, line=None), dict(VALID, line=True), dict(VALID, line=0),
    dict(VALID, line=2), dict(VALID, col='13'), dict(VALID, col=False),
    dict(VALID, col=0), dict(VALID, col=14), dict(VALID, col=3),
    dict(VALID, highlight=None), dict(VALID, highlight=''), dict(VALID, highlight='from'),
    dict(VALID, highlight='SELECT * FROM EXTRA'), dict(VALID, start_context='wrong'),
    dict(VALID, end_context='wrong'), dict(VALID, start_context=42),
])
def test_each_related_entry_is_verified_independently(monkeypatch, bad):
    diagnostic = fake(monkeypatch, [dict(VALID), bad, dict(VALID, description='last')])
    related = diagnostic['related_locations']
    assert related == [dict(description='missing table', location=MAPPED),
                       dict(description=bad.get('description') if isinstance(bad, dict) else None, location=None),
                       dict(description='last', location=MAPPED)]


def test_duplicates_and_unknown_descriptions_preserve_entries_without_promotion(monkeypatch):
    diagnostic = fake(monkeypatch, [dict(VALID), dict(VALID), dict(VALID, description=12),
                                   dict(VALID, description='\x1b[31mred\x1b[0m'), None])
    assert diagnostic['related_locations'] == [dict(description=d, location=loc)
        for d, loc in [('missing table', MAPPED), ('missing table', MAPPED),
                       (None, MAPPED), ('red', MAPPED), (None, None)]]


@pytest.mark.parametrize('entries', [[None, None], [None, dict(VALID)], [dict(VALID), None]])
def test_zero_or_one_verified_related_entry_never_becomes_primary(monkeypatch, entries):
    diagnostic = fake(monkeypatch, entries)
    assert diagnostic['related_locations'] == [
        dict(description=e['description'], location=MAPPED) if e else dict(description=None, location=None)
        for e in entries]


def test_all_unknown_entries_stay_visible_without_origin_columns(monkeypatch):
    diagnostic = fake(monkeypatch, [dict(VALID), None], origin=None)
    assert diagnostic['related_locations'] == [dict(description='missing table', location=None),
                                               dict(description=None, location=None)]


@pytest.mark.parametrize(('source', 'entries', 'expected'), [
    ('SELECT * FROM\r\nWHERE', [dict(description='first', line=1, col=13, highlight='FROM'),
                              dict(description='second', line=2, col=5, highlight='WHERE')],
     [(4, 4, 16, 19), (5, 5, 1, 5)]),
    ("SELECT 1 'é\r\n😀'", [dict(description='multiline', line=2, col=2, highlight="'é\r\n😀'"),
                           dict(description='same endpoint', line=2, col=2, highlight="'")],
     [(4, 5, 16, 2), (5, 5, 2, 2)]),
])
def test_related_multiline_unicode_crlf_endpoints(monkeypatch, source, entries, expected):
    def fail(*args, **kwargs):
        raise ParseError('aggregate', errors=entries)
    monkeypatch.setattr('datawrench.dialect_audit.audit.sqlglot.parse', fail)
    stmt = audit_statement(source, 'query.sql', 1, 4, 5, 'snowflake', 'postgres', column_start=7)
    diagnostic, = stmt['diagnostics']
    assert diagnostic['location'] is None
    assert [tuple(r['location'][k] for k in ('line_start', 'line_end', 'column_start', 'column_end'))
            for r in diagnostic['related_locations']] == expected


@pytest.mark.parametrize('exc', [ParseError('Line 1, Col: 13 FROM', errors=[]),
                               TokenError('Line 1, Col: 13 FROM'), RecursionError('FROM')])
def test_nonaggregate_and_nonparser_messages_do_not_create_related_entries(monkeypatch, exc):
    def fail(*args, **kwargs):
        raise exc
    monkeypatch.setattr('datawrench.dialect_audit.audit.sqlglot.parse', fail)
    stmt = audit_statement('SELECT * FROM', 'query.sql', 1, 4, 4, 'snowflake', 'postgres', column_start=7)
    assert stmt['diagnostics'][0]['location'] is None
    assert 'related_locations' not in stmt['diagnostics'][0]


def test_single_parser_binder_and_tokenizer_contract_stays_unchanged(tmp_path):
    for text in ('SELECT * FROM;', 'SELECT NO_FN(1);', "SELECT 'unterminated"):
        report = audit(tmp_path, text)
        assert all('related_locations' not in d for s in report['statements'] for d in s['diagnostics'])
    diagnostic = audit(tmp_path, 'SELECT * FROM;')['statements'][0]['diagnostics'][0]
    assert diagnostic['location'] == dict(line_start=1, line_end=1, column_start=10, column_end=13)


@pytest.mark.parametrize('format', ['json', 'text', 'md', 'junit'])
def test_aggregate_related_evidence_in_reports(tmp_path, format):
    report = audit(tmp_path)
    expected = report['statements'][1]['diagnostics'][0]['related_locations']
    output = render(report, format)
    if format == 'json':
        assert json.loads(output)['statements'][1]['diagnostics'][0]['related_locations'] == expected
    else:
        if format == 'junit':
            case = ET.fromstring(output).findall('testcase')[1]
            assert case.attrib['name'] == 'statement-2:L2-2 C13-51'
            assert json.loads(case.find('system-out').text)['diagnostics'][0]['related_locations'] == expected
            output = case.find('failure').text
        assert output.index('related #1 [L2-2 C34-34]:') < output.index('related #2 [L2-2 C51-51]:')
        assert 'PARSE_ERROR [L' not in output
        assert 'Expected table name' in output


def test_unknown_readable_related_entries_and_escaped_descriptions(tmp_path):
    report = audit(tmp_path)
    report['statements'][1]['diagnostics'][0]['related_locations'] = [
        dict(description=None, location=None), dict(description='<tag>|\nx', location=SPANS[1])]
    for format in ('text', 'md', 'junit'):
        output = render(report, format)
        if format == 'junit':
            output = ET.fromstring(output).findall('testcase')[1].find('failure').text
        assert 'related #1 [location unavailable]: description unavailable' in output
        assert 'related #2 [L2-2 C51-51]:' in output
        if format == 'md':
            assert '&lt;tag&gt;\\|<br>x' in output
        else:
            assert '<tag>|\nx' in output


def test_emit_baseline_legacy_and_metadata_only_diff(tmp_path):
    report = audit(tmp_path)
    expected = report['statements'][1]['diagnostics'][0]['related_locations']
    emit(report, str(tmp_path / 'emitted'))
    assert json.loads((tmp_path / 'emitted/report.json').read_text())['statements'][1]['diagnostics'][0]['related_locations'] == expected
    data = snapshot(report, str(tmp_path))
    path = tmp_path / 'baseline.json'
    save_baseline(data, str(path), False, [])
    assert load_baseline(str(path)) == data
    for metadata in ('legacy-related', 'legacy-all', 'moved', 'descriptions', 'multiplicity'):
        changed = deepcopy(data)
        d = changed['statements'][1]['diagnostics'][0]
        if metadata.startswith('legacy'):
            del d['related_locations']
            if metadata == 'legacy-all':
                del d['location']
        elif metadata == 'moved':
            d['related_locations'][0]['location'] = None
        elif metadata == 'descriptions':
            d['related_locations'][0]['description'] = 'different'
        else:
            d['related_locations'].append(deepcopy(d['related_locations'][0]))
        path.write_text(json.dumps(changed))
        original = path.read_bytes()
        loaded = load_baseline(str(path))
        assert not compare(loaded, data)['changed']
        assert not compare(data, loaded)['changed']
        assert path.read_bytes() == original
    shifted = snapshot(audit(tmp_path, 'SELECT 0;\n' + BATCH), str(tmp_path))
    delta = compare(data, shifted)
    assert delta['reasons'] == ['statements_changed']
    assert delta['new_findings'] == delta['resolved_findings'] == []


def test_diff_keeps_current_or_baseline_related_evidence(tmp_path):
    valid = snapshot(audit(tmp_path, '-- header\n  SELECT 1; SELECT 2; SELECT 3;\n'), str(tmp_path))
    broken = snapshot(audit(tmp_path), str(tmp_path))
    for delta, key in ((compare(valid, broken), 'new_findings'), (compare(broken, valid), 'resolved_findings')):
        finding, = delta[key]
        assert finding['location'] is None and finding['column_end'] == 51
        assert [r['location'] for r in finding['related_locations']] == SPANS
        output = render_diff(delta, 'text')
        assert 'PARSE_ERROR (error):' in output
        assert output.index('related #1 [L2-2 C34-34]:') < output.index('related #2 [L2-2 C51-51]:')
        assert json.loads(render_diff(delta, 'json'))[key][0]['related_locations'] == finding['related_locations']


@pytest.mark.parametrize('related', [
    None, {}, [], [dict(description='one', location=None)],
    [None, None], [dict(description=1, location=None)] * 2,
    [dict(description='x')] * 2, [dict(description='x', location=None, extra=1)] * 2,
    [dict(description=None, location={})] * 2,
    *[[dict(description='x', location=span)] * 2 for span in [
        dict(SPANS[0], column_start=0), dict(SPANS[0], column_end=True),
        dict(SPANS[0], column_end=None), dict(SPANS[0], line_end=1),
        dict(SPANS[0], column_start=12), dict(SPANS[0], column_end=52),
        dict(SPANS[0], extra=1)]],
])
def test_baseline_rejects_invalid_related_schema_or_spans(tmp_path, related):
    data = snapshot(audit(tmp_path), str(tmp_path))
    data['statements'][1]['diagnostics'][0]['related_locations'] = related
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(data))
    with pytest.raises(InputError, match='baseline'):
        load_baseline(str(path))


@pytest.mark.parametrize('change', ['primary', 'category'])
def test_baseline_aggregate_contract_has_no_primary_or_nonparser_related(tmp_path, change):
    data = snapshot(audit(tmp_path), str(tmp_path))
    d = data['statements'][1]['diagnostics'][0]
    if change == 'primary':
        d['location'] = SPANS[0]
    else:
        d['category'] = 'BIND_ERROR'
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(data))
    with pytest.raises(InputError, match='baseline'):
        load_baseline(str(path))
