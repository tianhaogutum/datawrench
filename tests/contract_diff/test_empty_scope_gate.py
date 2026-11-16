"""Empty scope is a model-count gate, independent of findings and policy usage."""
from copy import deepcopy
import json

import pytest

from .conftest import artifact, node
from .test_model_selection import ORDERS, CUSTOMERS, model, cli


def test_selected_and_excluded_contract(tmp_path, capsys):
    data = artifact()
    opts = ['--model', ORDERS, '--exclude-model', ORDERS, '--fail-on', 'none', '--format', 'json']
    code, out, err = cli(tmp_path, capsys, data, data, *opts)
    assert code == 0 and not err
    report = json.loads(out)
    assert report['models_compared'] == 0 and report['changes'] == []
    assert report['selected_models'] == report['excluded_models'] == [ORDERS]
    assert any('selected scope after exclusions' in n for n in report['notices'])
    gated, gated_out, gated_err = cli(tmp_path, capsys, data, data, *opts, '--fail-on-empty-scope')
    assert gated == 1 and not gated_err
    assert gated_out == out
    assert 'fail_on_empty_scope' not in report and report['schema_version'] == 1


@pytest.mark.parametrize('fmt', ['json', 'text', 'markdown', 'github'])
@pytest.mark.parametrize('fail_on', ['breaking', 'any', 'none'])
@pytest.mark.parametrize('scope', ['empty', 'unenforced', 'selected-unenforced', 'disabled', 'config-disabled', 'excluded', 'selected-excluded', 'unchanged', 'included-unenforced', 'selected-included', 'excluded-ineligible'])
def test_model_count_gate_preserves_report(tmp_path, capsys, fmt, fail_on, scope):
    data, opts = artifact(), []
    if scope == 'empty': data = artifact(nodes={})
    elif scope in {'unenforced', 'selected-unenforced', 'included-unenforced', 'selected-included'}:
        data = artifact(node(enforced=False))
        if scope in {'selected-unenforced', 'selected-included'}: opts += ['--model', ORDERS]
        if scope in {'included-unenforced', 'selected-included'}: opts += ['--include-unenforced']
    elif scope == 'disabled':
        data = artifact(nodes={}, disabled={ORDERS: [node()]}); opts += ['--model', ORDERS]
    elif scope == 'config-disabled':
        n = node(); n['config']['enabled'] = False
        data = artifact(n); opts += ['--model', ORDERS]
    elif scope in {'excluded', 'selected-excluded'}:
        opts += ['--exclude-model', ORDERS]
        if scope == 'selected-excluded': opts += ['--model', ORDERS]
    elif scope == 'excluded-ineligible':
        data = artifact(nodes={ORDERS: node(), CUSTOMERS: model(CUSTOMERS, enforced=False)})
        opts += ['--exclude-model', CUSTOMERS]
    expected_empty = scope not in {'unchanged', 'included-unenforced', 'selected-included', 'excluded-ineligible'}
    opts += ['--format', fmt, '--fail-on', fail_on]
    code, out, err = cli(tmp_path, capsys, data, data, *opts)
    gated, gated_out, gated_err = cli(tmp_path, capsys, data, data, *opts, '--fail-on-empty-scope')
    assert code == 0 and gated == int(expected_empty) and not err and not gated_err
    assert out == gated_out
    if fmt == 'json':
        report = json.loads(out)
        assert report['models_compared'] == int(not expected_empty) and not report['changes']
        assert bool(report['notices']) == expected_empty


@pytest.mark.parametrize('fail_on', ['breaking', 'any', 'none'])
@pytest.mark.parametrize('policy_gate', [False, True])
@pytest.mark.parametrize('scenario', ['empty', 'unchanged', 'widening', 'narrowing', 'unused'])
def test_composes_with_findings_and_unused_policy(tmp_path, capsys, fail_on, policy_gate, scenario):
    data = artifact(nodes={}) if scenario == 'empty' else artifact()
    head = deepcopy(data)
    if scenario in {'widening', 'narrowing'}:
        head['nodes'][ORDERS]['columns']['id']['data_type'] = 'BIGINT'
        if scenario == 'narrowing': data, head = head, data
    # Empty policy must not suppress empty-scope failure; unused policy must still fail nonempty scope.
    rules = [] if scenario != 'unused' else [{'model': ORDERS, 'column': 'id', 'path_segments': [], 'rule': 'integer_range', 'severity': 'non_breaking'}]
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'version': 1, 'rules': rules}))
    opts = ['--policy', str(policy), '--format', 'json', '--fail-on', fail_on]
    if policy_gate: opts += ['--fail-on-unused-policy']
    code, out, err = cli(tmp_path, capsys, data, head, *opts)
    gated, gated_out, gated_err = cli(tmp_path, capsys, data, head, *opts, '--fail-on-empty-scope')
    finding_failure = (scenario == 'narrowing' and fail_on != 'none') or (scenario == 'widening' and fail_on == 'any')
    expected = finding_failure or (scenario == 'unused' and policy_gate)
    assert code == int(expected) and gated == int(expected or scenario == 'empty')
    assert out == gated_out and not err and not gated_err


@pytest.mark.parametrize('fmt', ['json', 'text', 'markdown', 'github'])
@pytest.mark.parametrize('invalid', ['json', 'policy', 'missing', 'unknown-selection', 'unknown-exclusion', 'excluded-dtype', 'unselected-constraint', 'unenforced-family'])
def test_invalid_inputs_precede_gate(tmp_path, capsys, fmt, invalid):
    data = artifact(nodes={ORDERS: node(), CUSTOMERS: model(CUSTOMERS, enforced=False)})
    head = deepcopy(data)
    opts = ['--model', ORDERS, '--exclude-model', ORDERS, '--format', fmt, '--fail-on', 'none', '--fail-on-unused-policy']
    if invalid == 'unknown-selection': opts += ['--model', ORDERS + ' ']
    elif invalid == 'unknown-exclusion': opts += ['--exclude-model', 'model.shop.Orders']
    elif invalid == 'excluded-dtype': head['nodes'][ORDERS]['columns']['id']['data_type'] = 42
    elif invalid == 'unselected-constraint': data['nodes'][CUSTOMERS]['constraints'] = [{'type': 'custom'}]
    elif invalid == 'unenforced-family':
        data['nodes'][CUSTOMERS]['latest_version'] = 1
        data['nodes'][CUSTOMERS + '.v2'] = model(CUSTOMERS + '.v2', enforced=False, latest=2)
    elif invalid in {'policy', 'missing'}:
        p = tmp_path / 'policy.json'
        if invalid == 'policy': p.write_text('{"version": 99, "rules": []}')
        opts += ['--policy', str(p)]
    if invalid == 'json':
        from datawrench.contract_diff.cli import main
        p = tmp_path / 'invalid.json'; p.write_text('{broken')
        code = main(['--base', str(p), '--head', str(p), *opts, '--fail-on-empty-scope'])
        captured = capsys.readouterr(); out, err = captured.out, captured.err
    else:
        code, out, err = cli(tmp_path, capsys, data, head, *opts, '--fail-on-empty-scope')
    assert code == 2 and not out and 'input error:' in err and 'Traceback' not in err


@pytest.mark.parametrize('case', ['disabled', 'config-disabled', 'excluded', 'added', 'removed', 'enforcement-removed', 'alias', 'warning'])
def test_include_unenforced_and_compared_boundaries(tmp_path, capsys, case):
    base, head, opts = artifact(), artifact(), ['--include-unenforced']
    if case == 'disabled': base = head = artifact(nodes={}, disabled={ORDERS: [node()]})
    elif case == 'config-disabled':
        n = node(); n['config']['enabled'] = False
        base = head = artifact(n)
    elif case == 'excluded': opts += ['--exclude-model', ORDERS]
    elif case == 'added': base = artifact(nodes={})
    elif case == 'removed': head = artifact(nodes={})
    elif case == 'enforcement-removed': head = artifact(node(enforced=False))
    elif case == 'alias': head = artifact(node('INTEGER'))
    elif case == 'warning': base = head = artifact(node('opaque_type'))
    code, out, err = cli(tmp_path, capsys, base, head, *opts, '--format', 'json', '--fail-on', 'none', '--fail-on-empty-scope')
    empty = case in {'disabled', 'config-disabled', 'excluded'}
    assert code == int(empty) and not err
    d = json.loads(out)
    assert d['models_compared'] == int(not empty)
    if case == 'alias': assert d['equivalents'] and not d['changes']
    if case == 'warning': assert d['warnings'] and d['changes']
