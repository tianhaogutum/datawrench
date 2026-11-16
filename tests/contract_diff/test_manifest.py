import json

import pytest

from datawrench.contract_diff.manifest import InputError, Manifest, load_manifest
from .conftest import artifact, node


@pytest.mark.parametrize('data', [{}, [], {'nodes': []}, {'nodes': {'x': []}}, {'nodes': {}, 'disabled': []},
                                  {'nodes': {}, 'metadata': []}, {'nodes': {}, 'disabled': {'x': {}}}])
def test_bad_shapes(data):
    with pytest.raises(InputError):
        Manifest.from_dict(data)


@pytest.mark.parametrize('edit', [
    lambda n: n.update(unique_id='wrong'), lambda n: n.update(name=None),
    lambda n: n.update(columns=[]), lambda n: n.update(config={'enabled': 'false'}),
    lambda n: n.update(contract={'enforced': 'false'}), lambda n: n.update(version=[]),
    lambda n: n['columns']['id'].update(name='wrong'), lambda n: n['columns']['id'].update(data_type=42),
    lambda n: n.update(constraints=[{'type': 'custom'}]),
    lambda n: n.update(constraints=[{'type': 'unique', 'new_semantics': True}]),
    lambda n: n.update(constraints=[{'type': 'unique', 'columns': 'id'}]),
    lambda n: n.update(constraints=[{'type': 'check', 'expression': {}}]),
    lambda n: n['config'].update(contract={'enforced': False}),
])
def test_invalid_model_fields(edit):
    n = node()
    edit(n)
    with pytest.raises(InputError):
        Manifest.from_dict(artifact(n, nodes={'model.shop.orders': n}))


@pytest.mark.parametrize('content', ['{', '{}', '{"nodes": {}, "nodes": {}}', '{"nodes": {}, "x": NaN}', '\ud800'])
def test_invalid_file(tmp_path, content):
    path = tmp_path / 'manifest.json'
    path.write_bytes(content.encode('utf-8', errors='surrogatepass'))
    with pytest.raises(InputError):
        load_manifest(path)


def test_missing_and_oversize(tmp_path, monkeypatch):
    import datawrench.contract_diff.manifest as module
    with pytest.raises(InputError):
        load_manifest(tmp_path / 'missing.json')
    monkeypatch.setattr(module, 'MAX_MANIFEST_BYTES', 10)
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(artifact()))
    with pytest.raises(InputError, match='64 MiB'):
        load_manifest(path)


def test_non_model_nodes_ignored_and_disabled_conflict():
    data = artifact(nodes={'source.shop.x': {'resource_type': 'source'}})
    assert not Manifest.from_dict(data).models
    data = artifact(disabled={'model.shop.orders': [node()]})
    with pytest.raises(InputError, match='both active and disabled'):
        Manifest.from_dict(data)


def test_nonregular_input_rejected_without_blocking(tmp_path):
    import os
    path = tmp_path / 'pipe'
    os.mkfifo(path)
    with pytest.raises(InputError, match='regular file'):
        load_manifest(path)


@pytest.mark.parametrize('constraint_type', [None, [], {}, 1, True])
def test_constraint_type_must_be_string(constraint_type):
    n = node()
    n['constraints'] = [{'type': constraint_type}]
    with pytest.raises(InputError):
        Manifest.from_dict(artifact(n))


def test_missing_resource_type_and_infinite_version_rejected():
    n = node()
    n.pop('resource_type')
    with pytest.raises(InputError):
        Manifest.from_dict(artifact(n))
    n = node(version=float('inf'))
    with pytest.raises(InputError):
        Manifest.from_dict(artifact(n))


@pytest.mark.parametrize('raw', [b'{"nodes": {}, "x": ' + b'9' * 5000 + b'}', '{"nodes": {}}'.encode('utf-16')])
def test_unrepresentable_number_and_wrong_encoding(tmp_path, raw):
    path = tmp_path / 'bad.json'
    path.write_bytes(raw)
    with pytest.raises(InputError):
        load_manifest(path)
