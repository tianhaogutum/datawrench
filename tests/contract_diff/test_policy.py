"""Exact, opt-in project policy acceptance through the real offline CLI."""
from copy import deepcopy
import json
import os

import pytest

from .conftest import artifact, node
from .test_cli import BASE, HEAD, ROOT, run

NESTED = ROOT / 'tests/contract_diff/fixtures/explanations'
LEAF = [{'kind': 'field', 'name': 'items'}, {'kind': 'array_element'},
        {'kind': 'field', 'name': 'amount'}]


def entry(*, model='model.shop.orders', column='payload', path=None,
          rule='decimal_capacity', severity='non_breaking'):
    return {'model': model, 'column': column, 'path_segments': LEAF if path is None else path,
            'rule': rule, 'severity': severity}


def policy_file(tmp_path, *entries):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'version': 1, 'rules': entries}))
    return path


def nested(*args, fmt='json'):
    return run('--base', NESTED / 'nested-base.json', '--head', NESTED / 'nested-head.json',
               '--format', fmt, *args)


def compare(tmp_path, before, after, *entries, extra=()):
    base, head = tmp_path / 'base.json', tmp_path / 'head.json'
    base.write_text(json.dumps(before))
    head.write_text(json.dumps(after))
    path = policy_file(tmp_path, *entries)
    return run('--base', base, '--head', head, '--format', 'json', '--policy', path, *extra)


def test_empty_policy_preserves_default_report(tmp_path):
    default = run('--base', BASE, '--head', HEAD, '--format', 'json')
    configured = run('--base', BASE, '--head', HEAD, '--format', 'json', '--policy', policy_file(tmp_path))
    assert configured.returncode == default.returncode == 1
    assert configured.stdout == default.stdout and not configured.stderr


def test_one_leaf_override_keeps_other_atomic_change_breaking(tmp_path):
    path = policy_file(tmp_path, entry())
    result = nested('--policy', path)
    assert result.returncode == 1 and not result.stderr
    change, = json.loads(result.stdout)['changes']
    nullable, decimal = change['details']
    assert change['breaking'] and nullable['breaking'] and 'policy' not in nullable
    assert not decimal['breaking'] and decimal['classification'] == 'narrowing'
    assert decimal['certainty'] == 'known' and 'integer digits 8 -> 6' in decimal['reason']
    assert decimal['policy'] == {'source': str(path), 'pointer': '/rules/0',
                                 'severity': 'non_breaking', 'default_breaking': True}


def test_all_atomic_overrides_control_summary_exit_and_version_message(tmp_path):
    before = artifact(node('Tuple(items Array(Tuple(amount Nullable(Decimal(10,2)))))', version=1), adapter='clickhouse')
    after = artifact(node('Tuple(items Array(Tuple(amount Decimal(8,2))))', version=1), adapter='clickhouse')
    # Fixture helper calls the column id; select that exact column.
    entries = [entry(column='id'), entry(column='id', path=LEAF + [{'kind': 'nullability'}], rule='nullability_removed')]
    result = compare(tmp_path, before, after, *entries)
    assert result.returncode == 0 and not result.stderr
    report = json.loads(result.stdout)
    assert report['summary']['breaking'] == 0 and report['summary']['non_breaking'] == 1
    change, = report['changes']
    assert not change['breaking'] and 'without version bump' not in change['message']
    assert all(not d['breaking'] and d['policy'] for d in change['details'])
    assert compare(tmp_path, before, after, *entries, extra=('--fail-on', 'any')).returncode == 1


def test_exact_model_column_path_and_rule_isolation(tmp_path):
    a = node('STRUCT(amount DECIMAL(10,2), other DECIMAL(10,2))')
    b = node('STRUCT(amount DECIMAL(8,2), other DECIMAL(8,2))')
    for n in (a, b):
        n['columns']['payload'] = n['columns']['id'].copy()
        n['columns']['payload']['name'] = 'payload'
    a2, b2 = deepcopy(a), deepcopy(b)
    a2['unique_id'] = b2['unique_id'] = 'model.shop.other'
    result = compare(tmp_path, artifact(nodes={n['unique_id']: n for n in (a, a2)}),
                     artifact(nodes={n['unique_id']: n for n in (b, b2)}),
                     entry(path=[{'kind': 'field', 'name': 'amount'}]))
    assert result.returncode == 1 and not result.stderr
    report = json.loads(result.stdout)
    assert report['summary']['breaking'] == 4  # Partial column override still breaks.
    matched = [(c['model'], c['column'], d['path']) for c in report['changes'] for d in c['details'] if 'policy' in d]
    assert matched == [('model.shop.orders', 'payload', 'type.amount')]
    wrong_rule = entry(path=[{'kind': 'field', 'name': 'amount'}], rule='string_capacity')
    result = compare(tmp_path, artifact(a), artifact(b), wrong_rule)
    assert all('policy' not in d for c in json.loads(result.stdout)['changes'] for d in c['details'])


@pytest.mark.parametrize('path', [[{'kind': 'field', 'name': '"a.b"'}],
                                   [{'kind': 'field', 'name': 'a'}, {'kind': 'field', 'name': 'b'}]])
def test_quoted_dotted_name_is_not_a_nested_path(tmp_path, path):
    a = node('STRUCT("a.b" INT, a STRUCT(b INT))')
    b = node('STRUCT("a.b" VARCHAR, a STRUCT(b VARCHAR))')
    result = compare(tmp_path, artifact(a), artifact(b), entry(column='id', path=path, rule='scalar_incompatible'))
    assert result.returncode == 1 and not result.stderr
    change, = json.loads(result.stdout)['changes']
    assert [d['path_segments'] for d in change['details'] if 'policy' in d] == [path]
    assert sum(d['breaking'] for d in change['details']) == 1


@pytest.mark.parametrize('path,other', [([{'kind': 'map_key'}], [{'kind': 'map_value'}]),
                                       ([{'kind': 'map_value'}], [{'kind': 'map_key'}])])
def test_container_selectors_are_exact(tmp_path, path, other):
    result = compare(tmp_path, artifact(node('MAP(INT, INT)')), artifact(node('MAP(BIGINT, BIGINT)')),
                     entry(column='id', path=path, rule='nested_type_invariant'))
    assert result.returncode == 1 and not result.stderr
    details = json.loads(result.stdout)['changes'][0]['details']
    assert next(d for d in details if d['path_segments'] == other)['breaking']
    assert not next(d for d in details if d['path_segments'] == path)['breaking']


@pytest.mark.parametrize('strict,rule', [(False, 'integer_range'), (True, 'strict_widening')])
def test_root_escalation_and_strict_rule_precedence(tmp_path, strict, rule):
    result = compare(tmp_path, artifact(node('INT', version=1)), artifact(node('BIGINT', version=1)),
                     entry(column='id', path=[], rule=rule, severity='breaking'),
                     extra=('--strict-widening',) if strict else ())
    assert result.returncode == 1 and not result.stderr
    change, = json.loads(result.stdout)['changes']
    detail, = change['details']
    assert change['breaking'] and 'without version bump' in change['message']
    assert detail['policy']['default_breaking'] is strict
    # Default rule does not override the separate strict_widening rule.
    result = compare(tmp_path, artifact(node('INT')), artifact(node('BIGINT')),
                     entry(column='id', path=[], rule='integer_range'), extra=('--strict-widening',))
    assert result.returncode == 1 and 'policy' not in json.loads(result.stdout)['changes'][0]['details'][0]


@pytest.mark.parametrize('column', [None, 'id'])
def test_exact_constraint_signature_scope(tmp_path, column):
    a, b = node(), node()
    guarantees = [{'type': 'check', 'expression': 'id > 0'}, {'type': 'unique', 'columns': ['id']}]
    target = a if column is None else a['columns'][column]
    target['constraints'] = guarantees
    selector = [{'kind': 'constraint', 'signature': json.dumps(guarantees[0], sort_keys=True)}]
    result = compare(tmp_path, artifact(a), artifact(b),
                     entry(column=column, path=selector, rule='constraint_guarantee_removed'))
    assert result.returncode == 1 and not result.stderr
    report = json.loads(result.stdout)
    assert report['summary']['breaking'] == 1
    matched, = [d for c in report['changes'] for d in c['details'] if 'policy' in d]
    assert matched['path_segments'] == selector and not matched['breaking']


@pytest.mark.parametrize('fmt', ['text', 'markdown', 'github'])
def test_human_reports_show_policy_source(tmp_path, fmt):
    path = policy_file(tmp_path, entry())
    result = nested('--policy', path, fmt=fmt)
    assert result.returncode == 1 and not result.stderr
    assert 'Policy:' in result.stdout and str(path) in result.stdout
    assert '/rules/0' in result.stdout and 'non_breaking' in result.stdout


@pytest.mark.parametrize('payload', [
    {}, [], {'version': True, 'rules': []}, {'version': 2, 'rules': []},
    {'version': 1, 'rules': {}}, {'version': 1, 'rules': [], 'include': 'other.json'},
    {'version': 1, 'rules': [entry(rule='typo')]},
    {'version': 1, 'rules': [entry(rule='unparseable_type', path=[])]},
    {'version': 1, 'rules': [entry(severity='safe')]},
    {'version': 1, 'rules': [entry(column=None)]},
    {'version': 1, 'rules': [entry(path='type.items.element.amount')]},
    {'version': 1, 'rules': [entry(path=[{'kind': 'field'}])]},
    {'version': 1, 'rules': [entry(path=[{'kind': 'array_element', 'name': 'x'}])]},
    {'version': 1, 'rules': [entry(path=[{'kind': 'typo'}])]},
    {'version': 1, 'rules': [entry(path=[{'kind': 'nullability'}, {'kind': 'field', 'name': 'x'}])]},
    {'version': 1, 'rules': [entry(path=[{'kind': 'constraint', 'signature': 'not-json'}], rule='constraint_guarantee_removed')]},
    {'version': 1, 'rules': [entry(path=[]), entry(path=[], severity='breaking')]},
    {'version': 1, 'rules': [entry(), entry()]},
    {'version': 1, 'rules': [{k: v for k, v in entry().items() if k != 'model'}]},
    {'version': 1, 'rules': [{**entry(), 'classification': 'widening'}]},
    {'version': 1, 'rules': [entry(model=[])]},
    {'version': 1, 'rules': [entry(rule=[])]},
    {'version': 1, 'rules': [entry(severity=[])]},
])
def test_invalid_policy_rejected_before_report(tmp_path, payload):
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(payload))
    result = nested('--policy', path)
    assert result.returncode == 2 and not result.stdout
    assert 'input error:' in result.stderr and 'policy' in result.stderr and 'Traceback' not in result.stderr


@pytest.mark.parametrize('content', ['{broken', '{"version": 1, "rules": [], "rules": []}',
                                     '{"version": NaN, "rules": []}'])
def test_malformed_policy_json(tmp_path, content):
    path = tmp_path / 'bad.json'
    path.write_text(content)
    result = nested('--policy', path)
    assert result.returncode == 2 and not result.stdout and 'input error:' in result.stderr


def test_missing_nonregular_and_oversized_policy(tmp_path):
    path = tmp_path / 'missing.json'
    for kind in ('missing', 'fifo', 'large'):
        if kind == 'fifo':
            os.mkfifo(path)
        elif kind == 'large':
            path.unlink()
            path.write_bytes(b' ' * (1024 * 1024 + 1))
        result = nested('--policy', path)
        assert result.returncode == 2 and not result.stdout and 'input error:' in result.stderr


@pytest.mark.parametrize('case,code', [('opaque-equal', 0), ('opaque-changed', 1), ('malformed', 1)])
def test_known_rule_policy_cannot_reclassify_unknown_types(tmp_path, case, code):
    path = policy_file(tmp_path, entry(path=[], rule='scalar_incompatible'))
    result = run('--base', NESTED / f'{case}-base.json', '--head', NESTED / f'{case}-head.json',
                 '--format', 'json', '--policy', path)
    assert result.returncode == code and not result.stderr
    report = json.loads(result.stdout)
    detail, = report['changes'][0]['details']
    assert detail['certainty'] == 'unknown' and 'policy' not in detail and report['warnings']


@pytest.mark.parametrize('rules', [[entry(model=f'model.shop.m{i}') for i in range(1001)],
                                   [entry(path=[{'kind': 'array_element'}] * 66)]])
def test_policy_rule_and_selector_count_bounds(tmp_path, rules):
    path = policy_file(tmp_path, *rules)
    result = nested('--policy', path)
    assert result.returncode == 2 and not result.stdout and 'input error:' in result.stderr


def test_invalid_policy_is_rejected_even_with_no_comparisons(tmp_path):
    result = compare(tmp_path, artifact(nodes={}), artifact(nodes={}), entry(rule='unparseable_type', path=[]),
                     extra=('--fail-on', 'none'))
    assert result.returncode == 2 and not result.stdout
    assert 'unparseable_type cannot be overridden' in result.stderr


def test_policy_path_is_escaped_and_inputs_are_read_only(tmp_path):
    from datawrench.contract_diff.cli import main, render
    from datawrench.contract_diff.diff import compare_manifests
    from datawrench.contract_diff.manifest import load_manifest
    from datawrench.contract_diff.policy import load_policy

    # The source is untrusted report content too, including in workflow annotations.
    path = tmp_path / 'policy\n::error::injected|<script>`\x1b.json'
    path.write_text(json.dumps({'version': 1, 'rules': [entry()]}))
    base, head = NESTED / 'nested-base.json', NESTED / 'nested-head.json'
    inputs = {p: p.read_bytes() for p in (base, head, path)}
    report = compare_manifests(load_manifest(base), load_manifest(head), policy=load_policy(path))
    github = render(report, 'github')
    markdown = render(report, 'markdown')
    assert len(github.splitlines()) == 2 and '%0A::error::injected' in github
    assert '<script>' not in markdown and '&#124;' in markdown and '&#96;' in markdown
    assert '\x1b' not in github + markdown + render(report, 'text')
    assert main(['--base', str(base), '--head', str(head), '--policy', str(path)]) == 1
    assert inputs == {p: p.read_bytes() for p in inputs}


def test_shipped_policy_example(tmp_path):
    result = nested('--policy', ROOT / 'examples/contract_diff/project-policy.json')
    assert result.returncode == 1 and not result.stderr
    nullable, decimal = json.loads(result.stdout)['changes'][0]['details']
    assert nullable['breaking'] and not decimal['breaking'] and decimal['policy']['pointer'] == '/rules/0'
