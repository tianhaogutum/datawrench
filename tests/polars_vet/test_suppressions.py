"""Reasoned, rule-specific suppression contracts for the standalone analyzer."""
import pytest

from datawrench.polars_vet import analyze, fix_source

HEADER = 'import polars as p\nfrom polars import read_csv as read, col as c\n'
READ = 'read("local.csv").lazy()'
LAMBDA = 'c("a").map_elements(lambda x: x + 1)'


def test_precise_rule_line_and_alias_suppression():
    source = HEADER + (
        f'q = {READ}; expr = {LAMBDA}  '
        '# polars-vet: ignore[PLV001] -- eager read is intentional\n'
        f'q2 = {READ}\n'
    )
    assert [(d.line, d.code) for d in analyze(source)] == [(3, 'PLV002'), (4, 'PLV001')]


def test_multiple_codes_and_reason_preserved_without_fix():
    source = HEADER + (
        f'q = {READ}; expr = {LAMBDA}  '
        '# polars-vet: ignore[PLV001, PLV002] -- reviewed eager boundary and callback\n'
    )
    assert analyze(source, report_unused_suppressions=True) == []
    assert fix_source(source, analyze(source)) == source


@pytest.mark.parametrize('directive,message', [
    ('# polars-vet: ignore[PLV999] -- typo', 'Unknown suppression rule code'),
    ('# polars-vet: ignore[PLV001, PLV999] -- typo', 'Unknown suppression rule code'),
    ('# polars-vet: ignore[PLV000] -- error', 'Unknown suppression rule code'),
    ('# polars-vet: ignore[PLV001]', 'Malformed suppression'),
    ('# polars-vet: ignore[PLV001] --   ', 'Malformed suppression'),
    ('# polars-vet: ignore[] -- empty', 'Malformed suppression'),
    ('# polars-vet: ignore[PLV001,] -- trailing comma', 'Malformed suppression'),
    ('# polars-vet: ignore[plv001] -- wrong case', 'Malformed suppression'),
    ('# polars-vet: ignore[PLV001, PLV001] -- duplicate', 'Duplicate suppression rule code'),
    ('# polars-vet: ignore PLV001 -- brackets missing', 'Malformed suppression'),
    ('# polars-vet: ignore[PLV001] garbage -- misplaced', 'Malformed suppression'),
])
def test_invalid_directives_fail_closed(directive, message):
    source = 'import polars as p\nq = p.read_csv("local.csv").lazy() ' + directive
    found = analyze(source)
    assert [d.code for d in found] == ['PLV001', 'PLV000']
    assert message in found[1].message
    assert not found[1].edits
    # Validation cannot be hidden by selection or legacy noqa.
    assert [d.code for d in analyze(source, selected=set())] == ['PLV000']


def test_unused_per_code_is_optional_and_respects_selection():
    source = HEADER + f'q = {READ}  # polars-vet: ignore[PLV001, PLV002] -- expected both\n'
    assert analyze(source) == []
    unused, = analyze(source, report_unused_suppressions=True)
    assert unused.code == 'PLV000'
    assert unused.line == 3
    assert unused.column == source.splitlines()[2].index('#') + 1
    assert unused.end_column == len(source.splitlines()[2]) + 1
    assert 'Unused suppression for PLV002' in unused.message
    assert not unused.edits
    assert analyze(source, report_unused_suppressions=True, selected={'PLV001'}) == []
    assert analyze(source, report_unused_suppressions=True, selected=set()) == []


def test_comment_only_directive_never_suppresses_next_line():
    source = HEADER + '# polars-vet: ignore[PLV001] -- on wrong line\n' + READ
    assert [(d.line, d.code) for d in analyze(source, report_unused_suppressions=True)] == [
        (3, 'PLV000'), (4, 'PLV001'),
    ]


def test_only_comment_tokens_and_diagnostic_start_line_count():
    source = HEADER + (
        'note = "# polars-vet: ignore[PLV001] -- string"\n'
        'q = read(\n'
        '    "local.csv",  # polars-vet: ignore[PLV001] -- wrong starting line\n'
        ').lazy()\n'
    )
    assert [(d.line, d.code) for d in analyze(source, report_unused_suppressions=True)] == [
        (4, 'PLV001'), (5, 'PLV000'),
    ]


def test_unicode_crlf_and_no_source_reason_in_diagnostics():
    source = HEADER + 'é = 1  # polars-vet: ignore[PLV001] -- private reason 私密\r\n'
    unused, = analyze(source, report_unused_suppressions=True)
    assert unused.column == source.splitlines()[2].index('#') + 1
    assert unused.end_column == len(source.splitlines()[2]) + 1
    assert 'private' not in unused.message
    assert '私密' not in unused.message
