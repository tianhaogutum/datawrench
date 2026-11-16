"""Deterministic synthetic manifests; no production data or dbt execution."""
import json
from pathlib import Path

ROOT = Path(__file__).parent / 'fixtures'


def model(columns, **extra):
    return {'unique_id': 'model.shop.orders', 'resource_type': 'model', 'package_name': 'shop', 'name': 'orders',
            'config': {'enabled': True}, 'contract': {'enforced': True},
            'columns': {k: {'name': k, 'data_type': v, 'constraints': []} for k, v in columns.items()}, 'constraints': [], **extra}


def generate():
    base = model({'id': 'INTEGER', 'payload': 'STRUCT(id INTEGER, amount DECIMAL(10,2))',
                  'amount': 'DECIMAL(10,2)', 'label': 'VARCHAR(10)', 'score': 'FLOAT',
                  'tags': 'ARRAY(STRUCT(code INTEGER))', 'lookup': 'MAP(VARCHAR, INTEGER)', 'old_name': 'DATE'})
    head = model({'id': 'BIGINT', 'payload': 'STRUCT(id INTEGER, amount VARCHAR)',
                  'amount': 'DECIMAL(8,2)', 'label': 'VARCHAR(20)', 'score': 'DOUBLE',
                  'tags': 'ARRAY(STRUCT(code VARCHAR))', 'lookup': 'MAP(VARCHAR, BIGINT)', 'new_name': 'DATE'})
    base['columns']['id']['constraints'] = [{'type': 'not_null'}]
    head['constraints'] = [{'type': 'unique', 'columns': ['id']}]
    for name, node in [('base', base), ('head', head)]:
        data = {'metadata': {'dbt_schema_version': 'https://schemas.getdbt.com/dbt/manifest/v12.json',
                             'dbt_version': '1.11.1', 'project_name': 'shop', 'project_id': 'synthetic-shop', 'adapter_type': 'duckdb'},
                'nodes': {node['unique_id']: node}, 'disabled': {}}
        (ROOT / name / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')


def generate_explanations():
    """Local synthetic integration inputs for stable leaf/constraint explanations."""
    root = ROOT / 'explanations'
    root.mkdir(exist_ok=True)
    cases = {
        'nested': (
            model({'payload': 'Tuple(items Array(Tuple(amount Nullable(Decimal(10,2)))))'}),
            model({'payload': 'Tuple(items Array(Tuple(amount Decimal(8,2))))'}), 'clickhouse'),
        'constraints': (model({'id': 'INT', 'payload': 'STRUCT(items ARRAY(STRUCT(amount INT)))'}),
                        model({'id': 'INT', 'payload': 'STRUCT(items ARRAY(STRUCT(amount INT)))'}), 'duckdb'),
        'opaque-equal': (model({'payload': 'STRUCT(items ARRAY(custom_type))'}),
                         model({'payload': 'STRUCT(items ARRAY(custom_type))'}), 'duckdb'),
        'opaque-changed': (model({'payload': 'STRUCT(items ARRAY(custom_type))'}),
                           model({'payload': 'STRUCT(items ARRAY(other_type))'}), 'duckdb'),
        'malformed': (model({'payload': 'STRUCT(items ARRAY(INT))'}),
                      model({'payload': 'STRUCT(items ARRAY(INT)'}), 'duckdb'),
        'column-removed': (
            model({'payload': 'STRUCT(items INTEGER)',
                   'payload.items': 'STRUCT("a.b" INTEGER, amounts ARRAY(DECIMAL(10,2)))'}, version=1),
            model({'payload': 'STRUCT(items INTEGER)'}, version=1), 'duckdb'),
        'model-removed': (model({'id': 'INT'}), None, 'duckdb'),
        'enforcement-added': (
            model({'id': 'INT'}, contract={'enforced': False}),
            model({'id': 'INT'}), 'duckdb'),
        'enforcement-removed': (
            model({'id': 'INT'}),
            model({'id': 'INT'}, contract={'enforced': False}), 'duckdb'),
        'column-added': (
            model({'id': 'INT'}, version=1),
            model({'id': 'INT', 'payload.items': 'STRUCT(amount DECIMAL(10,2))'}, version=1), 'duckdb'),
    }
    base, head, _ = cases['constraints']
    base['columns']['payload']['constraints'] = [
        {'type': 'check', 'expression': 'payload.items[1].amount >= 0'}]
    head['columns']['payload']['constraints'] = [
        {'type': 'check', 'expression': 'payload.items[1].amount > 0'}]
    base['constraints'] = [{'type': 'unique', 'columns': ['id', 'payload']}]
    for name, (base, head, adapter) in cases.items():
        for side, node in [('base', base), ('head', head)]:
            data = {'metadata': {
                'dbt_schema_version': 'https://schemas.getdbt.com/dbt/manifest/v12.json',
                'project_id': 'synthetic-shop', 'adapter_type': adapter},
                'nodes': {node['unique_id']: node} if node is not None else {}, 'disabled': {}}
            (root / f'{name}-{side}.json').write_text(json.dumps(data, indent=2) + '\n')


if __name__ == '__main__':
    generate()
    generate_explanations()
