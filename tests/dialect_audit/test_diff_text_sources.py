"""Single-sided text changes retain exact source and nullable generated SQL."""
from copy import deepcopy
import json

import pytest

from datawrench.dialect_audit.audit import run_audit
from datawrench.dialect_audit.baseline import compare, load_baseline, render_diff, snapshot


def capture(tmp_path, sql):
    path = tmp_path / 'query.sql'
    path.write_bytes(sql.encode('utf-8'))
    return snapshot(run_audit([str(path)], 'snowflake', 'duckdb'), str(tmp_path))


def stored_comparison(tmp_path, short, extended, reverse):
    # Reverse removals must use the saved statement, after the input file has
    # been replaced by SQL that no longer contains the removed source.
    old = capture(tmp_path, extended if reverse else short)
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    baseline = load_baseline(str(path))
    current = capture(tmp_path, short if reverse else extended)
    saved = deepcopy((baseline, current))
    delta = compare(baseline, current)
    structured = render_diff(delta, 'json')
    output = render_diff(delta, 'text')
    assert (baseline, current) == saved
    assert json.loads(structured) == delta == json.loads(render_diff(delta, 'json'))
    assert path.read_bytes() == original
    return delta, output


@pytest.mark.parametrize('reverse', [False, True])
def test_statement_source_cast_and_null_target(tmp_path, reverse):
    short = 'SELECT 1; SELECT 2; SELECT 3;'
    extended = short + ' SELECT 4::INT; SELECT * FROM;'
    delta, output = stored_comparison(tmp_path, short, extended, reverse)
    action = 'REMOVED' if reverse else 'ADDED'
    assert delta['reasons'] == ['statements_changed', 'resolved_findings' if reverse else 'new_findings']
    assert [item['statement']['source_sql'] for item in delta['statement_changes']] == [
        'SELECT 4::INT', 'SELECT * FROM']
    assert [item['statement']['target_sql'] for item in delta['statement_changes']] == [
        'SELECT CAST(4 AS INT)', None]
    assert (f'STATEMENT_{action} query.sql#4 L1-1 C31-43 (binding: bound)\n'
            '  source: "SELECT 4::INT"\n'
            '  SQL: "SELECT CAST(4 AS INT)"\n'
            f'STATEMENT_{action} query.sql#5 L1-1 C46-58 (binding: not-attempted)\n'
            '  source: "SELECT * FROM"\n'
            '  SQL: null\n') in output
    finding, = delta['resolved_findings' if reverse else 'new_findings']
    assert finding['index'] == 5
    assert finding['location'] == dict(line_start=1, line_end=1, column_start=55, column_end=58)
    label = 'RESOLVED' if reverse else 'NEW'
    assert f'{label} query.sql#5 L1-1 C46-58 PARSE_ERROR (error) [L1-1 C55-58]:' in output
    assert finding['source_sql'] == 'SELECT * FROM'
    assert output.count('  source: ') == 3  # Two statement entries plus the parse finding.
    assert '(baseline:' not in output


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_statement_source_multiline_unicode_escaping(tmp_path, reverse, newline):
    short = 'SELECT 1; SELECT 2; SELECT 3;'
    source = 'SELECT /* café 😀 "quote" \\path */' + newline + '\t4::INT AS "雪"'
    delta, output = stored_comparison(tmp_path, short, short + ' ' + source + ';', reverse)
    item, = delta['statement_changes']
    stmt = item['statement']
    assert stmt['source_sql'] == source
    assert stmt['target_sql'] != source
    assert stmt['binding_status'] == 'bound'
    action = 'REMOVED' if reverse else 'ADDED'
    heading = f'STATEMENT_{action} query.sql#4 L1-2 C31-14 (binding: bound)'
    lines = output.splitlines()
    position = lines.index(heading)
    source_line, target_line = lines[position + 1:position + 3]
    assert source_line == '  source: ' + json.dumps(source, ensure_ascii=True)
    assert source_line.isascii()
    assert json.loads(source_line.removeprefix('  source: ')) == source
    assert target_line == '  SQL: ' + json.dumps(stmt['target_sql'])
    assert output.count('  source: ') == 1
    assert delta['reasons'] == ['statements_changed']
    assert '(baseline:' not in output
