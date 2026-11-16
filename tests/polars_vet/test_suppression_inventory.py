"""Opt-in reasoned suppression audit contracts, including incomplete analysis."""
import json
import subprocess
import sys

import pytest

BAD = 'q = pl.read_csv("local.csv").lazy()'
DIRECTIVE = '# polars-vet: ignore[PLV002, PLV001] -- eager validation café 私密  '


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'datawrench', 'polars-vet', 'check', *map(str, args)],
                          capture_output=True, text=True)


def inventory(path, *args, exit_code=0):
    result = cli(path, '--format', 'json', '--show-suppressions', *args)
    assert result.returncode == exit_code, result.stdout + result.stderr
    return json.loads(result.stdout)


def entry(path, line, code, status, reason='eager validation café 私密'):
    return dict(path=str(path), line=line, code=code, reason=reason, status=status)


def test_inventory_per_exact_code_read_only_and_opt_in(tmp_path):
    path = tmp_path / 'job.py'
    raw = ('import polars as pl\r\n' + BAD + '  ' + DIRECTIVE + '\r\n').encode('utf-8')
    path.write_bytes(raw)
    ordinary = cli(path, '--format', 'json')
    assert ordinary.returncode == 0
    assert set(json.loads(ordinary.stdout)) == {'version', 'diagnostics', 'fixedFiles'}
    assert 'eager validation' not in ordinary.stdout
    data = inventory(path)
    assert data['suppressions'] == [entry(path, 2, 'PLV001', 'used'), entry(path, 2, 'PLV002', 'unused')]
    assert {k: v for k, v in data.items() if k not in {'suppressions', 'suppressionAudit'}} == json.loads(ordinary.stdout)
    assert path.read_bytes() == raw
    assert inventory(path)['suppressions'] == data['suppressions']


@pytest.mark.parametrize('args,expected', [
    (('--select', 'PLV001'), ('used', 'disabled')),
    (('--ignore', 'PLV001'), ('disabled', 'unused')),
    (('--select', 'PLV003'), ('disabled', 'disabled')),
])
def test_selection_and_unused_reporting_are_distinct(tmp_path, args, expected):
    path = tmp_path / 'job.py'
    path.write_text('import polars as pl\n' + BAD + ' ' + DIRECTIVE + '\n')
    data = inventory(path, *args, '--report-unused-suppressions', exit_code=int(expected[1] == 'unused'))
    assert [e['status'] for e in data['suppressions']] == list(expected)
    assert [d['message'] for d in data['diagnostics']] == (
        ['Unused suppression for PLV002'] if expected[1] == 'unused' else [])
    assert inventory(path, *args, '--report-unused-suppressions', '--exit-zero')['suppressions'] == data['suppressions']


def test_inventory_only_valid_reasoned_comment_tokens_on_start_line(tmp_path):
    path = tmp_path / 'tokens.py'
    path.write_text(
        'import polars as pl\n'
        'note = "# polars-vet: ignore[PLV001] -- string"\n'
        + BAD + ' # noqa: PLV001\n'
        + BAD + ' # noqa\n'
        '# polars-vet: ignore[PLV001] -- comment only\n'
        'q = pl.read_csv(\n'
        '    "local.csv", # polars-vet: ignore[PLV001] -- continuation\n'
        ').lazy()\n'
        + BAD + ' # polars-vet: ignore[PLV001, PLV999] -- secret invalid\n'
        + BAD + ' # polars-vet: ignore[PLV001] --   \n'
        + BAD + ' # polars-vet: ignore[PLV001, PLV001] -- secret duplicate\n'
    )
    data = inventory(path, exit_code=1)
    assert data['suppressions'] == [entry(path, 5, 'PLV001', 'unused', 'comment only'),
                                    entry(path, 7, 'PLV001', 'unused', 'continuation')]
    assert [(d['line'], d['code']) for d in data['diagnostics']] == [
        (6, 'PLV001'), (9, 'PLV001'), (9, 'PLV000'), (10, 'PLV001'), (10, 'PLV000'),
        (11, 'PLV001'), (11, 'PLV000'),
    ]
    assert 'secret' not in json.dumps(data)


def test_multiple_findings_and_aliases_count_once(tmp_path):
    path = tmp_path / 'aliases.py'
    path.write_text('from polars import read_csv as read\n'
                    'a = read("a.csv").lazy(); b = read("b.csv").lazy() '
                    '# polars-vet: ignore[PLV001] -- two findings\n')
    assert inventory(path)['suppressions'] == [entry(path, 2, 'PLV001', 'used', 'two findings')]


def test_discovery_sorted_deduplicated_and_failure_does_not_hide_other_files(tmp_path):
    a, z = tmp_path / 'a.py', tmp_path / 'z.py'
    for path in (z, a):
        path.write_text('# polars-vet: ignore[PLV001] -- audit\n')
    invalid = tmp_path / 'encoding.py'
    invalid.write_bytes(b'\xff\xff')
    result = cli(tmp_path, a, '--format', 'json', '--show-suppressions')
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert data['suppressions'] == [entry(a, 1, 'PLV001', 'unused', 'audit'),
                                    entry(z, 1, 'PLV001', 'unused', 'audit')]
    assert [d['code'] for d in data['diagnostics']] == ['PLV000']
    assert inventory(tmp_path / 'missing.py', exit_code=1)['suppressions'] == []


def test_syntax_error_usage_unknown_but_excluded_rules_disabled(tmp_path):
    path = tmp_path / 'broken.py'
    path.write_text('import polars as pl\n' + BAD + ' ' + DIRECTIVE + '\nreturn\n')
    data = inventory(path, '--select', 'PLV001', '--report-unused-suppressions', exit_code=1)
    assert data['suppressions'] == [entry(path, 2, 'PLV001', 'unknown'), entry(path, 2, 'PLV002', 'disabled')]
    assert len(data['diagnostics']) == 1
    assert data['diagnostics'][0]['code'] == 'PLV000'


@pytest.mark.parametrize('format', [None, 'text', 'sarif', 'github'])
def test_non_json_inventory_is_usage_error_before_fix(tmp_path, format):
    path = tmp_path / 'job.py'
    path.write_text('import polars as pl\n' + BAD + '\n')
    before = path.read_bytes()
    args = [] if format is None else ['--format', format]
    result = cli(path, '--show-suppressions', '--fix', '--exit-zero', *args)
    assert result.returncode == 2
    assert '--show-suppressions requires --format json' in result.stderr
    assert result.stdout == ''
    assert path.read_bytes() == before


def test_fix_reports_reanalyzed_source_and_preserves_reason(tmp_path):
    path = tmp_path / 'fix.py'
    source = 'import polars as pl\n' + BAD + ' ' + DIRECTIVE + '\nother = pl.read_csv("other.csv").lazy()\n'
    path.write_text(source)
    data = inventory(path, '--fix')
    assert data['fixedFiles'] == 1
    assert data['suppressions'] == [entry(path, 2, 'PLV001', 'used'), entry(path, 2, 'PLV002', 'unused')]
    assert path.read_text() == source.replace('pl.read_csv("other.csv").lazy()', 'pl.scan_csv("other.csv")')
    assert inventory(path)['suppressions'] == data['suppressions']


def test_baseline_current_inventory_without_storing_reasons(tmp_path):
    path, baseline = tmp_path / 'job.py', tmp_path / 'baseline.json'
    path.write_text('import polars as pl\n' + BAD + '\n')
    assert cli(path, '--baseline', baseline, '--update-baseline').returncode == 0
    saved = baseline.read_bytes()
    path.write_text('import polars as pl\n' + BAD + ' ' + DIRECTIVE + '\n')
    before = path.read_bytes()
    data = inventory(path, '--baseline', baseline)
    assert [d['baselineStatus'] for d in data['diagnostics']] == ['resolved']
    assert data['suppressions'] == [entry(path, 2, 'PLV001', 'used'), entry(path, 2, 'PLV002', 'unused')]
    assert baseline.read_bytes() == saved
    assert path.read_bytes() == before
    inventory(path, '--baseline', baseline, '--update-baseline')
    assert 'eager validation' not in baseline.read_text()
    assert 'suppressions' not in json.loads(baseline.read_text())


def test_api_matches_existing_analyze_and_incomplete_analysis_is_unknown(monkeypatch):
    from datawrench.polars_vet import analyze, analyze_with_suppressions
    from datawrench.polars_vet.analysis import Analyzer

    source = 'import polars as pl\n' + BAD + ' ' + DIRECTIVE + '\n'
    result = analyze_with_suppressions(source, 'job.py', selected={'PLV001'})
    assert result.diagnostics == analyze(source, 'job.py', selected={'PLV001'}) == []
    assert [s.to_dict() for s in result.suppressions] == [entry('job.py', 2, 'PLV001', 'used'),
                                                         entry('job.py', 2, 'PLV002', 'disabled')]

    def incomplete(self, tree):
        self.used_suppressions.add((2, 'PLV001'))
        raise RecursionError

    monkeypatch.setattr(Analyzer, 'visit', incomplete)
    result = analyze_with_suppressions(source, 'job.py', selected={'PLV001'})
    assert [s.status for s in result.suppressions] == ['unknown', 'disabled']
    assert [d.code for d in result.diagnostics] == ['PLV000']
