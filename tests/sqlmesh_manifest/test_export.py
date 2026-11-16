import copy

import pytest

# Optional `sqlmesh` extra; skip this module cleanly when it is not installed.
pytest.importorskip("sqlmesh")
pytest.importorskip("dbt_artifacts_parser")

from dbt_artifacts_parser.parser import parse_manifest

from datawrench.sqlmesh_manifest.exporter import build_manifest, materialization, serialize
from datawrench.sqlmesh_manifest.loading import ExportError, load_project
from datawrench.sqlmesh_manifest.validation import ManifestError, validate_manifest


@pytest.mark.parametrize(('kind', 'expected'), [
    ('FULL', 'table'), ('VIEW', 'view'), ('EMBEDDED', 'ephemeral'), ('SEED', 'seed'),
    ('INCREMENTAL_BY_TIME_RANGE', 'incremental'), ('INCREMENTAL_BY_UNIQUE_KEY', 'incremental'),
    ('INCREMENTAL_BY_PARTITION', 'incremental'), ('INCREMENTAL_UNMANAGED', 'incremental'),
    ('SCD_TYPE_2', 'incremental'), ('SCD_TYPE_2_BY_TIME', 'incremental'), ('SCD_TYPE_2_BY_COLUMN', 'incremental'),
])
def test_kind_mapping(kind, expected):
    assert materialization(kind) == expected


@pytest.mark.parametrize('kind', ['CUSTOM', 'MANAGED', 'EXTERNAL', 'MYSTERY'])
def test_unsupported_kind(kind):
    with pytest.raises(ExportError, match='Unsupported'):
        materialization(kind)


def test_independent_parser_and_official_schema(example):
    _, manifest, warnings = example
    validate_manifest(manifest)
    parsed = parse_manifest(manifest)
    assert len(parsed.nodes) == 6
    assert len(parsed.sources) == 1
    assert not warnings


def test_dag_equals_context(example):
    context, manifest, _ = example
    resources = {**manifest['nodes'], **manifest['sources']}
    ids = {n['meta']['sqlmesh_name']: uid for uid, n in resources.items() if 'sqlmesh_name' in n.get('meta', {})}
    expected = {ids[fqn]: sorted(ids[dep] for dep in deps) for fqn, deps in context.dag.graph.items()}
    assert {uid: manifest['parent_map'][uid] for uid in ids.values()} == expected
    for uid, deps in manifest['parent_map'].items():
        for dep in deps:
            assert uid in manifest['child_map'][dep]
    assert sum(map(len, manifest['parent_map'].values())) == 5


def test_mapping_columns_meta_and_sql(example):
    _, m, _ = example
    nodes = m['nodes']
    fct = nodes['model.mini_project.fct_orders']
    assert fct['config']['materialized'] == 'incremental'
    assert fct['depends_on']['nodes'] == ['model.mini_project.stg_orders']
    assert fct['meta']['grain'] == ['order_id']
    assert fct['meta']['owner'] == 'analytics-team'
    assert fct['meta']['cron'] == '@daily'
    assert fct['tags'] == ['finance', 'gold']
    assert 'MODEL (' in fct['raw_code']
    assert 'SELECT' in fct['compiled_code']
    assert nodes['model.mini_project.stg_orders']['columns']['order_id'] == {
        'name': 'order_id', 'description': 'Order identifier', 'data_type': 'INT', 'meta': {}, 'tags': []}
    seed = nodes['seed.mini_project.raw_orders']
    assert seed['meta']['seed_file'] == 'seeds/raw_orders.csv'
    assert 'compiled_code' not in seed  # dbt v12 seed schema forbids compiled_code.
    source = m['sources']['source.mini_project.raw.customers']
    assert source['identifier'] == 'customers'
    assert source['schema'] == 'raw'
    assert source['columns']['customer_id']['description'] == 'Synthetic customer identifier'
    tests = [n for n in nodes.values() if n['resource_type'] == 'test']
    assert {n['test_metadata']['name'] for n in tests} == {'not_null', 'unique'}
    assert all(n['attached_node'] == fct['unique_id'] for n in tests)
    assert all(n['depends_on']['nodes'] == [fct['unique_id']] for n in tests)
    assert all(n['compiled_code'] for n in tests)


def test_deterministic_independent_loads(project):
    exports = []
    for _ in range(2):
        with load_project(project) as context:
            manifest, _ = build_manifest(context, deterministic=True)
            exports.append(serialize(manifest))
    assert exports[0] == exports[1]
    assert '1970-01-01T00:00:00Z' in exports[0]
    assert '00000000-0000-0000-0000-000000000000' in exports[0]


def test_normal_timestamps_and_project_name(example):
    c, _, _ = example
    m, _ = build_manifest(c, project_name='renamed')
    assert m['metadata']['generated_at'] != '1970-01-01T00:00:00Z'
    assert m['metadata']['project_name'] == 'renamed'
    assert all('.renamed.' in uid for uid in m['nodes'])
    with pytest.raises(ExportError, match='Project name'):
        build_manifest(c, project_name='../invalid')


def test_multicolumn_and_accepted_values(project):
    fct = project / 'models/fct_orders.sql'
    fct.write_text(fct.read_text().replace('not_null(columns := (order_id))', 'not_null(columns := (order_id, customer_id))').replace(
        'unique_values(columns := (order_id))', "accepted_values(column := customer_id, is_in := (10, 20))"))
    with load_project(project) as c:
        m, w = build_manifest(c, deterministic=True)
    validate_manifest(m)
    tests = [n for n in m['nodes'].values() if n['resource_type'] == 'test']
    assert len(tests) == 3
    assert {n['column_name'] for n in tests if n['test_metadata']['name'] == 'not_null'} == {'order_id', 'customer_id'}
    accepted = next(n for n in tests if n['test_metadata']['name'] == 'accepted_values')
    assert accepted['test_metadata']['kwargs']['values'] == [10, 20]
    assert accepted['test_metadata']['kwargs']['quote'] is False
    assert not w


def test_custom_and_standalone_audits(project):
    (project / 'audits').mkdir()
    (project / 'audits/checks.sql').write_text('''
AUDIT (name positive_amount);
SELECT * FROM @this_model WHERE amount < 0;
AUDIT (name standalone_check, standalone true);
SELECT * FROM analytics.dim_customers WHERE customer_id IS NULL;
''')
    fct = project / 'models/fct_orders.sql'
    fct.write_text(fct.read_text().replace('not_null(columns := (order_id))', 'positive_amount'))
    with load_project(project) as c:
        m, warnings = build_manifest(c, deterministic=True)
    validate_manifest(m)
    singular = next(n for n in m['nodes'].values() if n.get('meta', {}).get('sqlmesh_audit') == 'positive_amount')
    assert 'test_metadata' not in singular
    assert singular['meta']['attached_node'] == 'model.mini_project.fct_orders'
    assert 'amount' in singular['compiled_code']
    assert m['parent_map']['test.mini_project.standalone_check'] == ['model.mini_project.dim_customers']
    assert not warnings


def test_implicit_source_and_same_leaf_names(project):
    (project / 'models/other.sql').write_text('''MODEL (name other.stg_orders, kind VIEW); SELECT x FROM unknown.upstream;''')
    with load_project(project) as c:
        m, w = build_manifest(c, deterministic=True)
    validate_manifest(m)
    stg = [n for n in m['nodes'].values() if n['alias'] == 'stg_orders']
    assert len(stg) == 2 and stg[0]['unique_id'] != stg[1]['unique_id']
    assert 'source.mini_project.unknown.upstream' in m['sources']
    assert any('implicit source' in warning for warning in w)


def test_python_model_not_executed(project):
    (project / 'models/python_model.py').write_text('''
from sqlmesh import model
@model("analytics.python_model", columns={"value": "INT"}, depends_on=["analytics.stg_orders"])
def execute(context, **kwargs):
    raise RuntimeError("MODEL ENTRYPOINT MUST NOT RUN")
''')
    with pytest.raises(ExportError, match='trust-project'):
        with load_project(project):
            pass
    with load_project(project, trusted=True) as c:
        m, w = build_manifest(c, deterministic=True)
    validate_manifest(m)
    node = m['nodes']['model.mini_project.python_model']
    assert node['language'] == 'python' and node['compiled_code'] is None
    assert node['depends_on']['nodes'] == ['model.mini_project.stg_orders']
    assert any('Python execution' in warning for warning in w)


def test_runtime_only_query(project):
    (project / 'models/runtime.sql').write_text("""
MODEL (name analytics.runtime, kind FULL, columns (value INT), depends_on (analytics.stg_orders));
JINJA_QUERY_BEGIN;
{% if runtime_stage == 'evaluating' %}SELECT 1 AS value{% endif %}
JINJA_END;
""")
    with load_project(project, trusted=True) as c:
        m, w = build_manifest(c, deterministic=True)
    validate_manifest(m)
    assert m['nodes']['model.mini_project.runtime']['compiled_code'] is None
    assert any('offline rendering unavailable' in warning for warning in w)


def test_render_exception_retains_raw_and_hides_detail(example, monkeypatch):
    c, _, _ = example
    from sqlmesh.core.model.definition import SqlModel
    def fail(self, **kwargs):
        raise RuntimeError('secret-password')
    with monkeypatch.context() as patch:
        patch.setattr(SqlModel, 'render_query', fail)
        m, w = build_manifest(c, deterministic=True)
    assert m['nodes']['model.mini_project.stg_orders']['compiled_code'] is None
    assert m['nodes']['model.mini_project.stg_orders']['raw_code']
    assert 'secret-password' not in str(w)
    validate_manifest(m)


def test_graph_corruption_detected(example):
    m = copy.deepcopy(example[1])
    m['parent_map']['model.mini_project.stg_orders'] = []
    with pytest.raises(ManifestError, match='non-reciprocal|differs'):
        validate_manifest(m)


@pytest.mark.parametrize('indexes', ['empty', 'null'])
def test_dangling_declared_dependency_without_indexes(example, indexes):
    m = copy.deepcopy(example[1])
    m['nodes']['model.mini_project.stg_orders']['depends_on']['nodes'] = ['model.mini_project.missing']
    for key in ('parent_map', 'child_map'):
        m[key] = {} if indexes == 'empty' else None
    with pytest.raises(ManifestError, match='dangling'):
        validate_manifest(m)


@pytest.mark.parametrize('index', ['parent_map', 'child_map', 'both'])
def test_indexes_cannot_omit_declared_edges(example, index):
    m = copy.deepcopy(example[1])
    for key in ('parent_map', 'child_map'):
        m[key] = {} if index in (key, 'both') else None
    with pytest.raises(ManifestError, match='differs'):
        validate_manifest(m)


def test_valid_declarations_without_optional_indexes(example):
    m = copy.deepcopy(example[1])
    m['parent_map'] = m['child_map'] = None
    validate_manifest(m)


def test_duplicate_declared_dependency_without_indexes(example):
    m = copy.deepcopy(example[1])
    deps = m['nodes']['model.mini_project.stg_orders']['depends_on']['nodes']
    deps.append(deps[0])
    m['parent_map'] = m['child_map'] = None
    with pytest.raises(ManifestError, match='duplicate'):
        validate_manifest(m)


def test_real_embedded_and_scd_kinds(project):
    (project / 'models/embedded.sql').write_text('''MODEL (name analytics.embedded, kind EMBEDDED); SELECT 1 AS id;''')
    (project / 'models/history.sql').write_text('''
MODEL (name analytics.history, kind SCD_TYPE_2_BY_TIME (unique_key customer_id),
columns (customer_id INT, updated_at TIMESTAMP));
SELECT 1 AS customer_id, CAST('2024-01-01' AS TIMESTAMP) AS updated_at;
''')
    with load_project(project) as c:
        m, _ = build_manifest(c, deterministic=True)
    validate_manifest(m)
    assert m['nodes']['model.mini_project.embedded']['config']['materialized'] == 'ephemeral'
    history = m['nodes']['model.mini_project.history']
    assert history['config']['materialized'] == 'incremental'
    assert history['meta']['sqlmesh_kind'] == 'SCD_TYPE_2_BY_TIME'
    assert 'unique_key' in history['meta']['sqlmesh_kind_definition']


def test_builtin_override_stays_singular_and_nonblocking(project):
    (project / 'audits').mkdir()
    (project / 'audits/not_null.sql').write_text('''
AUDIT (name not_null, blocking false);
SELECT * FROM @this_model WHERE amount < 0;
''')
    fct = project / 'models/fct_orders.sql'
    fct.write_text(fct.read_text().replace('unique_values(columns := (order_id))', 'unique_values(columns := (order_id), blocking := false)'))
    with load_project(project) as c:
        m, _ = build_manifest(c, deterministic=True)
    validate_manifest(m)
    not_null = next(n for n in m['nodes'].values() if n.get('meta', {}).get('sqlmesh_audit') == 'not_null')
    assert 'test_metadata' not in not_null
    assert not_null['config']['severity'] == 'WARN'
    unique = next(n for n in m['nodes'].values() if n.get('meta', {}).get('sqlmesh_audit') == 'unique_values')
    assert unique['test_metadata']['name'] == 'unique'
    assert unique['config']['severity'] == 'WARN'


def test_root_seed_path_stable_across_checkouts(project, tmp_path):
    import shutil
    file = project / 'models/raw_orders.sql'
    file.write_text(file.read_text().replace('../seeds/raw_orders.csv', '$root/seeds/raw_orders.csv'))
    other = tmp_path / 'other-checkout'
    shutil.copytree(project, other)
    artifacts = []
    for root in (project, other):
        with load_project(root) as c:
            m, _ = build_manifest(c, deterministic=True)
            artifacts.append(serialize(m))
        assert str(root) not in artifacts[-1]
    assert artifacts[0] == artifacts[1]
