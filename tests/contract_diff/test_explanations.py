"""CLI acceptance for policy explanations on local synthetic dbt manifests."""
import json

import pytest

from .test_cli import ROOT, run
from datawrench.contract_diff.types import compare_types

FIXTURES = ROOT / 'tests/contract_diff/fixtures/explanations'


def compare_fixture(name, fmt='json', *args):
    return run('--base', FIXTURES / f'{name}-base.json', '--head',
               FIXTURES / f'{name}-head.json', '--format', fmt, *args)


def test_nested_nullable_decimal_explanations():
    result = compare_fixture('nested')
    assert result.returncode == 1 and not result.stderr
    report = json.loads(result.stdout)
    assert report['schema_version'] == 1
    assert report['summary']['breaking'] == 1 and not report['warnings']
    change, = report['changes']
    assert change['model'] == 'model.shop.orders' and change['column'] == 'payload'
    nullable, decimal = change['details']
    leaf = [{'kind': 'field', 'name': 'items'}, {'kind': 'array_element'},
            {'kind': 'field', 'name': 'amount'}]
    assert nullable['path'] == 'type.items.element.amount.nullable'
    assert nullable['path_segments'] == leaf + [{'kind': 'nullability'}]
    assert nullable['rule'] == 'nullability_removed'
    assert nullable['before_type'] == 'Nullable(DECIMAL(10,2))'
    assert nullable['after_type'] == 'DECIMAL(10,2)'
    assert decimal['path'] == 'type.items.element.amount'
    assert decimal['path_segments'] == leaf
    assert (decimal['before_type'], decimal['after_type']) == ('DECIMAL(10,2)', 'DECIMAL(8,2)')
    assert decimal['rule'] == 'decimal_capacity'
    assert 'integer digits 8 -> 6' in decimal['reason']
    assert 'fractional digits 2 -> 2' in decimal['reason']
    for detail in (nullable, decimal):
        assert detail['breaking'] and detail['certainty'] == 'known'
        assert detail['reason'] and 'version' in detail['remediation']
    # Deterministic fields/order for CI consumers.
    assert result.stdout == compare_fixture('nested').stdout


def test_constraint_explanations_keep_exact_guarantees():
    result = compare_fixture('constraints')
    assert result.returncode == 1 and not result.stderr
    report = json.loads(result.stdout)
    assert report['summary']['changes'] == 3 and report['summary']['breaking'] == 2
    for change in report['changes']:
        detail, = change['details']
        removed = change['kind'] == 'constraint_removed'
        assert detail['rule'] == ('constraint_guarantee_removed' if removed else 'constraint_guarantee_added')
        assert detail['before'] == change['before'] and detail['after'] == change['after']
        assert detail['certainty'] == 'known' and detail['reason'] and detail['remediation']
        signature = json.loads(detail['path_segments'][0]['signature'])
        assert signature == (change['before'] if removed else change['after'])
        assert detail['path'] == 'constraints'
    removed_check = next(c for c in report['changes'] if c['before'] and c['before']['type'] == 'check')
    assert removed_check['column'] == 'payload'
    assert removed_check['before']['expression'] == 'payload.items[1].amount >= 0'
    removed_unique = next(c for c in report['changes'] if c['column'] is None)
    assert removed_unique['before'] == {'type': 'unique', 'columns': ['id', 'payload']}


@pytest.mark.parametrize('fmt', ['text', 'markdown', 'github'])
@pytest.mark.parametrize('fixture,rule', [('nested', 'decimal_capacity'), ('constraints', 'constraint_guarantee_removed')])
def test_human_formats_explain_rules_and_actions(fixture, rule, fmt):
    result = compare_fixture(fixture, fmt)
    assert result.returncode == 1 and not result.stderr
    assert rule in result.stdout and 'Action:' in result.stdout
    if fixture == 'nested':
        assert 'type.items.element.amount' in result.stdout
        assert 'DECIMAL(10,2)' in result.stdout and 'DECIMAL(8,2)' in result.stdout


@pytest.mark.parametrize('name,code', [('opaque-equal', 0), ('opaque-changed', 1), ('malformed', 1)])
def test_opaque_explanations_never_claim_safety(name, code):
    result = compare_fixture(name)
    assert result.returncode == code and not result.stderr
    report = json.loads(result.stdout)
    change, = report['changes']
    detail, = change['details']
    assert change['kind'] == 'type_unparseable' and report['warnings']
    assert detail['certainty'] == 'unknown' and detail['rule'] == 'unparseable_type'
    assert detail['path_segments'] == []  # No invented leaf inside an opaque type.
    assert 'cannot determine compatibility' in detail['reason']
    assert detail['remediation']
    assert compare_fixture(name, 'json', '--fail-on', 'any').returncode == 1


def test_quoted_field_path_is_unambiguous():
    result = compare_types('STRUCT("a.b" INT, a STRUCT(b INT))',
                           'STRUCT("a.b" VARCHAR, a STRUCT(b VARCHAR))')
    paths = [d['path_segments'] for d in result.to_dict()['details']]
    assert paths == [[{'kind': 'field', 'name': '"a.b"'}],
                     [{'kind': 'field', 'name': 'a'}, {'kind': 'field', 'name': 'b'}]]


def test_nested_and_strict_widening_rules_explain_breaking_override():
    nested = compare_types('STRUCT(a INT)', 'STRUCT(a BIGINT)').to_dict()['details'][0]
    assert nested['breaking'] and nested['rule'] == 'nested_type_invariant'
    assert 'integer range' in nested['reason']
    strict = compare_types('INT', 'BIGINT', strict_widening=True).to_dict()['details'][0]
    assert strict['breaking'] and strict['rule'] == 'strict_widening'


@pytest.mark.parametrize('before,after,dialect,rule,segments', [
    ('BIGINT', 'INT', 'duckdb', 'integer_range', []),
    ('VARCHAR(10)', 'VARCHAR(5)', 'duckdb', 'string_capacity', []),
    ('DECIMAL(10,2)', 'DECIMAL(10,4)', 'duckdb', 'decimal_capacity', []),
    ('INT', 'VARCHAR', 'duckdb', 'scalar_incompatible', []),
    ('STRUCT(a INT)', 'STRUCT(a INT, b INT)', 'duckdb', 'struct_field_added', [{'kind': 'field', 'name': 'b'}]),
    ('STRUCT(a INT, b INT)', 'STRUCT(a INT)', 'duckdb', 'struct_field_removed', [{'kind': 'field', 'name': 'b'}]),
    ('STRUCT(a INT, b INT)', 'STRUCT(b INT, a INT)', 'duckdb', 'struct_field_order', []),
    ('MAP(INT, INT)', 'MAP(BIGINT, INT)', 'duckdb', 'nested_type_invariant', [{'kind': 'map_key'}]),
    ('MAP(INT, INT)', 'MAP(INT, VARCHAR)', 'duckdb', 'scalar_incompatible', [{'kind': 'map_value'}]),
    ('Int32', 'Nullable(Int32)', 'clickhouse', 'nullability_added', [{'kind': 'nullability'}]),
    ('Tuple(a Int32)', 'Tuple(a Nullable(Int32))', 'clickhouse', 'nested_type_invariant', [{'kind': 'field', 'name': 'a'}, {'kind': 'nullability'}]),
])
def test_explanations_cover_policy_branches(before, after, dialect, rule, segments):
    result = compare_types(before, after, dialect)
    assert not result.warnings
    detail, = result.to_dict()['details']
    assert detail['rule'] == rule and detail['path_segments'] == segments
    assert detail['certainty'] == 'known' and detail['reason'] and detail['remediation']
    assert detail['before_type'] or detail['after_type']


def test_explanation_rendering_escapes_constraint_content():
    from .conftest import artifact, node
    from datawrench.contract_diff.cli import render
    from datawrench.contract_diff.diff import compare_manifests
    from datawrench.contract_diff.manifest import Manifest

    before, after = node(), node()
    before['constraints'] = [{'type': 'check', 'expression': "x\n::error::injected|<script>`\x1b[31m"}]
    report = compare_manifests(Manifest.from_dict(artifact(before)), Manifest.from_dict(artifact(after)))
    github = render(report, 'github')
    # The guarantee object renders its expression as a quoted, escaped value.
    assert len(github.splitlines()) == 2 and '\\n::error::injected' in github
    markdown = render(report, 'markdown')
    assert '<script>' not in markdown and '&#124;' in markdown and '&#96;' in markdown
    assert '\x1b' not in github + markdown + render(report, 'text')
