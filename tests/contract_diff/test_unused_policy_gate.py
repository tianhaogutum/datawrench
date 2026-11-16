"""Opt-in unused-policy exit gate; comparison reports remain byte-for-byte equal."""
import json
import subprocess
import sys

import pytest

from .conftest import artifact, node
from .test_cli import run
from .test_policy import NESTED, entry, policy_file
from .test_policy_diagnostics import EXAMPLE, diagnostics


@pytest.mark.parametrize('fail_on', [None, 'breaking', 'any', 'none'])
@pytest.mark.parametrize('fmt', ['json', 'text', 'markdown', 'github'])
def test_identical_example_only_changes_exit_status(fail_on, fmt):
    args = ['--base', NESTED / 'nested-base.json', '--head', NESTED / 'nested-base.json',
            '--policy', EXAMPLE, '--format', fmt]
    if fail_on is not None:
        args += ['--fail-on', fail_on]
    default = run(*args)
    gated = run(*args, '--fail-on-unused-policy')
    assert default.returncode == 0 and not default.stderr
    assert gated.returncode == 1 and not gated.stderr
    assert gated.stdout == default.stdout
    if fmt == 'json':
        report = json.loads(gated.stdout)
        diagnostics(report, json.loads(EXAMPLE.read_text())['rules'], EXAMPLE, [0, 1, 2, 3])
        assert report['schema_version'] == 1 and not report['changes']
        assert report['summary']['breaking'] == 0
    else:
        assert gated.stdout.count('unused_policy_entry') == 4


@pytest.mark.parametrize('fail_on', ['breaking', 'any', 'none'])
@pytest.mark.parametrize('policy_state', ['absent', 'empty', 'matched', 'unused'])
@pytest.mark.parametrize('change', ['breaking', 'non_breaking', 'unknown'])
def test_gate_composes_with_existing_finding_gates(tmp_path, fail_on, policy_state, change):
    before, after, rule = {
        'breaking': ('BIGINT', 'INT', 'integer_range'),
        'non_breaking': ('INT', 'BIGINT', 'integer_range'),
        'unknown': ('custom_type', 'custom_type', 'scalar_incompatible'),
    }[change]
    base, head = tmp_path / 'base.json', tmp_path / 'head.json'
    base.write_text(json.dumps(artifact(node(before))))
    head.write_text(json.dumps(artifact(node(after))))
    args = ['--base', base, '--head', head, '--format', 'json', '--fail-on', fail_on]
    if policy_state != 'absent':
        # Same-severity overrides still count as used; unknown details never match.
        entries = [] if policy_state == 'empty' else [entry(
            model='model.shop.missing' if policy_state == 'unused' else 'model.shop.orders',
            column='id', path=[], rule=rule,
            severity='breaking' if change in {'breaking', 'unknown'} else 'non_breaking')]
        args += ['--policy', policy_file(tmp_path, *entries)]
    default = run(*args)
    gated = run(*args, '--fail-on-unused-policy')
    unused = policy_state == 'unused' or (policy_state == 'matched' and change == 'unknown')
    finding_failure = fail_on == 'any' or (fail_on == 'breaking' and change == 'breaking')
    assert default.returncode == int(finding_failure) and not default.stderr
    assert gated.returncode == int(unused or finding_failure) and not gated.stderr
    assert gated.stdout == default.stdout
    report = json.loads(gated.stdout)
    assert len(report['changes']) == 1
    assert report['summary']['breaking'] == int(change == 'breaking')
    assert bool(report['policy_diagnostics']) == unused
    if change == 'unknown':
        detail, = report['changes'][0]['details']
        assert detail['certainty'] == 'unknown' and 'policy' not in detail


@pytest.mark.parametrize('fail_on', ['breaking', 'any', 'none'])
@pytest.mark.parametrize('scope', ['no_contracts', 'excluded', 'included', 'wrong_case'])
def test_unused_gate_respects_comparison_scope(tmp_path, fail_on, scope):
    base, head = tmp_path / 'base.json', tmp_path / 'head.json'
    before = artifact(nodes={}) if scope == 'no_contracts' else artifact(node(enforced=False))
    after = artifact(nodes={}) if scope == 'no_contracts' else artifact(node('BIGINT', enforced=False))
    base.write_text(json.dumps(before))
    head.write_text(json.dumps(after))
    path = policy_file(tmp_path, entry(column='ID' if scope == 'wrong_case' else 'id',
                                      path=[], rule='integer_range'))
    args = ['--base', base, '--head', head, '--policy', path, '--format', 'json', '--fail-on', fail_on]
    if scope in {'included', 'wrong_case'}:
        args += ['--include-unenforced']
    default = run(*args)
    gated = run(*args, '--fail-on-unused-policy')
    assert gated.stdout == default.stdout and not gated.stderr
    assert default.returncode == int(fail_on == 'any' and scope in {'included', 'wrong_case'})
    assert gated.returncode == int(scope != 'included' or fail_on == 'any')
    report = json.loads(gated.stdout)
    assert bool(report['policy_diagnostics']) == (scope != 'included')
    assert bool(report['notices']) == (scope in {'no_contracts', 'excluded'})


@pytest.mark.parametrize('fail_on', ['breaking', 'any', 'none'])
@pytest.mark.parametrize('invalid', ['manifest', 'policy', 'missing_file'])
def test_invalid_input_keeps_exit_two(tmp_path, fail_on, invalid):
    base, path = tmp_path / 'base.json', policy_file(tmp_path, entry())
    base.write_text('{broken' if invalid == 'manifest' else json.dumps(artifact()))
    if invalid == 'policy':
        path.write_text('{"version": 1, "rules": [{"rule": "unparseable_type"}]}')
    if invalid == 'missing_file':
        base.unlink()
    args = ['--base', base, '--head', NESTED / 'nested-base.json', '--policy', path,
            '--fail-on', fail_on]
    default = run(*args)
    gated = run(*args, '--fail-on-unused-policy')
    assert gated.returncode == default.returncode == 2
    assert not gated.stdout and gated.stderr == default.stderr
    assert 'input error:' in gated.stderr and 'Traceback' not in gated.stderr


def test_no_discovery_or_gate_on_unrelated_warnings(tmp_path):
    # Plausible policy names in cwd must never be read without --policy.
    for name in ['policy.json', 'project-policy.json', 'unused-policy.json']:
        (tmp_path / name).write_text('{broken')
    base = tmp_path / 'base.json'
    base.write_text(json.dumps(artifact(adapter='unsupported')))
    args = [sys.executable, '-m', 'datawrench', 'contract-diff', '--base', str(base), '--head', str(base),
            '--format', 'json', '--fail-on', 'any']
    default = subprocess.run(args, cwd=tmp_path, text=True, capture_output=True)
    gated = subprocess.run([*args, '--fail-on-unused-policy'], cwd=tmp_path, text=True, capture_output=True)
    assert default.returncode == gated.returncode == 0 and not gated.stderr
    assert default.stdout == gated.stdout
    report = json.loads(gated.stdout)
    assert report['warnings'] and not report['policy_diagnostics'] and not report['changes']
