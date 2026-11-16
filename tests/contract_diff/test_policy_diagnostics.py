"""Unused policy entries describe this comparison without widening selectors."""
from copy import deepcopy
import json

import pytest

from .conftest import artifact, node
from datawrench.contract_diff.cli import render
from datawrench.contract_diff.diff import compare_manifests
from datawrench.contract_diff.manifest import Manifest
from datawrench.contract_diff.policy import ProjectPolicy
from .test_cli import ROOT
from .test_policy import LEAF, compare, entry, nested, policy_file

EXAMPLE = ROOT / 'examples/contract_diff/unused-policy.json'


def diagnostics(report, entries, source, indices):
    values = report['policy_diagnostics']
    assert len(values) == len(indices)
    for diagnostic, index in zip(values, indices):
        assert diagnostic == {
            **entries[index], 'code': 'unused_policy_entry', 'source': str(source),
            'pointer': f'/rules/{index}', 'message': diagnostic['message'],
        }
        assert 'unused_policy_entry' in diagnostic['message']
        assert f'{source}#/rules/{index}' in diagnostic['message']
        assert diagnostic['message'] in report['warnings']
    assert report['summary']['warnings'] == len(report['warnings'])
    return values


def test_mixed_policy_reports_exact_unused_selectors_in_input_order(tmp_path):
    entries = json.loads(EXAMPLE.read_text())['rules']
    path = policy_file(tmp_path, *entries)
    result = nested('--policy', path)
    assert result.returncode == 1 and not result.stderr
    report = json.loads(result.stdout)
    assert report['schema_version'] == 1
    diagnostics(report, entries, path, [1, 2, 3])
    assert len(report['warnings']) == 3
    assert report['summary']['changes'] == report['summary']['breaking'] == 1
    nullable, decimal = report['changes'][0]['details']
    assert nullable['breaking'] and 'policy' not in nullable
    assert not decimal['breaking'] and decimal['policy']['pointer'] == '/rules/0'
    assert decimal['path_segments'] == LEAF and decimal['classification'] == 'narrowing'


@pytest.mark.parametrize('fmt', ['text', 'markdown', 'github'])
def test_unused_policy_warning_visible_in_every_human_format(tmp_path, fmt):
    path = policy_file(tmp_path, entry(model='model.shop.missing'))
    result = nested('--policy', path, fmt=fmt)
    assert result.returncode == 1 and not result.stderr
    assert 'unused_policy_entry' in result.stdout and f'{path}#/rules/0' in result.stdout
    assert 'model.shop.missing' in result.stdout and 'decimal_capacity' in result.stdout
    if fmt == 'github':
        assert len([line for line in result.stdout.splitlines() if line.startswith('::warning ')]) == 1


@pytest.mark.parametrize('fail_on', ['breaking', 'any', 'none'])
@pytest.mark.parametrize('scope', ['unchanged', 'empty', 'excluded', 'alias'])
def test_diagnostics_alone_do_not_fail_even_without_active_models(tmp_path, fail_on, scope):
    a, b = artifact(), artifact()
    if scope == 'empty':
        a = b = artifact(nodes={})
    elif scope == 'excluded':
        a, b = artifact(node('INT', enforced=False)), artifact(node('BIGINT', enforced=False))
    elif scope == 'alias':
        b = artifact(node('INTEGER'))
    entries = [entry(column='id', path=[], rule='integer_range')]
    result = compare(tmp_path, a, b, *entries, extra=('--fail-on', fail_on))
    assert result.returncode == 0 and not result.stderr
    report = json.loads(result.stdout)
    diagnostics(report, entries, tmp_path / 'policy.json', [0])
    assert not report['changes'] and report['summary']['breaking'] == 0
    assert bool(report['notices']) == (scope in {'empty', 'excluded'})
    assert len(report['equivalents']) == (1 if scope == 'alias' else 0)


def test_including_unenforced_consumes_only_the_actual_detail(tmp_path):
    entries = [entry(column='id', path=[], rule='integer_range', severity='breaking')]
    result = compare(tmp_path, artifact(node('INT', enforced=False)), artifact(node('BIGINT', enforced=False)),
                     *entries, extra=('--include-unenforced',))
    assert result.returncode == 1 and not result.stderr
    report = json.loads(result.stdout)
    assert report['policy_diagnostics'] == [] and not report['warnings']
    assert report['changes'][0]['details'][0]['policy']['pointer'] == '/rules/0'


def test_accounting_is_local_to_comparison_and_noop_matches_are_used(manifest):
    entries = [entry(column='id', path=[], rule='integer_range', severity='breaking')]
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': entries})
    original = deepcopy(policy)
    # INT <- BIGINT is already breaking: matching does not require an impact change.
    first = compare_manifests(manifest(node('BIGINT')), manifest(node('INT')), policy=policy)
    saved = first.to_dict()
    second = compare_manifests(manifest(), manifest(), policy=policy)
    diagnostics(second.to_dict(), entries, '<memory>', [0])
    third = compare_manifests(manifest(node('BIGINT')), manifest(node('INT')), policy=policy)
    assert first.to_dict() == saved == third.to_dict()
    assert saved['policy_diagnostics'] == [] and not saved['warnings']
    assert policy == original


@pytest.mark.parametrize('before,after,rule', [
    ('STRUCT(amount DECIMAL(10,2))', 'STRUCT(amount DECIMAL(8,2))', 'decimal_capacity'),
    ('custom_type', 'custom_type', 'scalar_incompatible'),
    ('INT', 'BIGINT', 'integer_range'),
])
def test_root_unknown_and_strict_rule_mismatch_stay_unused(tmp_path, before, after, rule):
    entries = [entry(column='id', path=[], rule=rule)]
    extra = ('--strict-widening',) if before == 'INT' else ()
    result = compare(tmp_path, artifact(node(before)), artifact(node(after)), *entries, extra=extra)
    report = json.loads(result.stdout)
    diagnostics(report, entries, tmp_path / 'policy.json', [0])
    assert all('policy' not in d for c in report['changes'] for d in c['details'])
    assert report['summary']['breaking'] == (0 if before == 'custom_type' else 1)


@pytest.mark.parametrize('loss', ['column', 'model'])
def test_lifecycle_findings_do_not_consume_type_entries(tmp_path, loss):
    before, after = artifact(), artifact()
    if loss == 'column':
        after['nodes']['model.shop.orders']['columns'] = {}
    else:
        after['nodes'] = {}
    entries = [entry(column='id', path=[], rule='integer_range')]
    result = compare(tmp_path, before, after, *entries)
    assert result.returncode == 1 and not result.stderr
    report = json.loads(result.stdout)
    diagnostics(report, entries, tmp_path / 'policy.json', [0])
    assert report['changes'][0]['kind'] == f'{loss}_removed'


def test_quoted_fields_and_container_selectors_stay_exact(tmp_path):
    before = node('STRUCT("a.b" INT, items ARRAY(INT))')
    after = node('STRUCT("a.b" VARCHAR, items ARRAY(BIGINT))')
    entries = [
        entry(column='id', path=[{'kind': 'field', 'name': '"a.b"'}], rule='scalar_incompatible'),
        entry(column='id', path=[{'kind': 'field', 'name': 'a'}, {'kind': 'field', 'name': 'b'}], rule='scalar_incompatible'),
        entry(column='id', path=[{'kind': 'field', 'name': 'items'}, {'kind': 'field', 'name': 'element'}], rule='nested_type_invariant'),
        entry(column='ID', path=[{'kind': 'field', 'name': '"a.b"'}], rule='scalar_incompatible'),
        entry(model='model.shop.Orders', column='id', path=[{'kind': 'field', 'name': '"a.b"'}], rule='scalar_incompatible'),
    ]
    result = compare(tmp_path, artifact(before), artifact(after), *entries)
    report = json.loads(result.stdout)
    diagnostics(report, entries, tmp_path / 'policy.json', [1, 2, 3, 4])
    assert report['summary']['breaking'] == 1
    assert [d['policy']['pointer'] for c in report['changes'] for d in c['details'] if 'policy' in d] == ['/rules/0']


def test_constraint_scope_and_signature_are_exact(tmp_path):
    before, after = node(), node()
    before['constraints'] = [{'type': 'check', 'expression': 'id > 0'}]
    path = [{'kind': 'constraint', 'signature': json.dumps(before['constraints'][0], sort_keys=True)}]
    entries = [entry(column=None, path=path, rule='constraint_guarantee_removed'),
               entry(column='id', path=path, rule='constraint_guarantee_removed'),
               entry(column=None, path=[{'kind': 'constraint', 'signature': json.dumps(
                   {'type': 'check', 'expression': 'id >= 0'}, sort_keys=True)}], rule='constraint_guarantee_removed')]
    result = compare(tmp_path, artifact(before), artifact(after), *entries)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    diagnostics(report, entries, tmp_path / 'policy.json', [1, 2])
    assert report['changes'][0]['details'][0]['policy']['pointer'] == '/rules/0'


def test_no_policy_and_empty_policy_have_empty_diagnostics(manifest):
    before, after = manifest(), manifest()
    default = compare_manifests(before, after)
    empty = compare_manifests(before, after, policy=ProjectPolicy.from_dict({'version': 1, 'rules': []}))
    assert default.to_dict() == empty.to_dict()
    assert default.to_dict()['policy_diagnostics'] == [] and not default.warnings


def test_maximum_policy_diagnostics_follow_numeric_input_order(manifest):
    entries = [entry(model=f'model.shop.m{i}') for i in range(1000)]
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': entries})
    report = compare_manifests(manifest(), manifest(), policy=policy).to_dict()
    diagnostics(report, entries, '<memory>', list(range(1000)))
    assert len(report['warnings']) == 1000 and not report['changes']


def test_unused_selector_content_is_preserved_and_rendered_safely():
    malicious = 'x\n::error::injected|<script>`\x1b\u202e'
    entries = [entry(model='model.' + malicious, column=malicious,
                     path=[{'kind': 'field', 'name': malicious}])]
    policy = ProjectPolicy.from_dict({'version': 1, 'rules': entries}, source=malicious)
    empty = Manifest.from_dict(artifact(nodes={}))
    report = compare_manifests(empty, empty, policy=policy)
    data = json.loads(render(report, 'json'))
    diagnostics(data, entries, malicious, [0])
    github = render(report, 'github')
    assert len(github.splitlines()) == 3 and '%0A::error::injected' in github
    assert len([line for line in github.splitlines() if line.startswith('::warning ')]) == 1
    markdown = render(report, 'markdown')
    assert '<script>' not in markdown and '&#124;' in markdown and '&#96;' in markdown
    assert '\x1b' not in github + markdown + render(report, 'text')
    assert '\u202e' not in github + markdown + render(report, 'text')
