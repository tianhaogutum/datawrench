from copy import deepcopy

import pytest

from datawrench.contract_diff.manifest import Manifest


def node(dtype='INT', *, enforced=True, version=None, latest=None):
    return {'resource_type': 'model', 'unique_id': 'model.shop.orders', 'name': 'orders', 'package_name': 'shop',
            'version': version, 'latest_version': latest, 'config': {'enabled': True},
            'contract': {'enforced': enforced}, 'columns': {'id': {'name': 'id', 'data_type': dtype, 'constraints': []}},
            'constraints': []}


def artifact(n=None, *, nodes=None, adapter='duckdb', **kwargs):
    if nodes is None:
        n = node() if n is None else n
        nodes = {n['unique_id']: n}
    return {'metadata': {'adapter_type': adapter, 'project_id': 'shop'}, 'nodes': deepcopy(nodes), **kwargs}


@pytest.fixture
def base_node():
    return node()


@pytest.fixture
def manifest():
    return lambda n=None, **kwargs: Manifest.from_dict(artifact(n, **kwargs))
