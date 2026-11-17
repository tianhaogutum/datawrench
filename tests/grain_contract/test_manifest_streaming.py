import json
import tracemalloc

import pytest

from datawrench.grain_contract import ContractError
from datawrench.grain_contract.cli import main
from datawrench.grain_contract.dbt import from_manifest, read_manifest

from .test_dbt import FIXTURE, direct_contract


def test_selective_reader_matches_actual_dbt_artifact():
    full = json.loads(FIXTURE.read_text())
    small = read_manifest(FIXTURE, "order_enriched", direct_contract())
    assert len(small["nodes"]) == 3
    assert from_manifest(full, "order_enriched", direct_contract()) == from_manifest(
        small, "order_enriched", direct_contract()
    )


def test_large_unrelated_nodes_are_not_retained(tmp_path):
    path = tmp_path / "large.json"
    fixture = json.loads(FIXTURE.read_text())
    # Stream a 5MB artifact out; do not assemble it in the test process either.
    with path.open("w") as stream:
        stream.write('{"metadata":' + json.dumps(fixture["metadata"]) + ',"nodes":{')
        for key, node in fixture["nodes"].items():
            stream.write(json.dumps(key) + ":" + json.dumps(node) + ",")
        payload = "x" * 10000
        for index in range(500):
            if index:
                stream.write(",")
            stream.write(
                json.dumps(f"model.unrelated.m{index}")
                + ":"
                + json.dumps({"name": f"m{index}", "raw_code": payload})
            )
        stream.write("}}")
    tracemalloc.start()
    try:
        selected = read_manifest(path, "order_enriched", direct_contract())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(selected["nodes"]) == 3
    assert peak < 3_000_000


@pytest.mark.parametrize(
    "text,match",
    [
        ('{"nodes":{},"nodes":{}}', "duplicate JSON key"),
        ('{"nodes":{"n":{"name":"a","name":"b"}}}', "duplicate JSON key"),
        ('{"nodes":[]}', "must be an object"),
        ("[]", "root must be an object"),
        ('{"nodes":{}} garbage', "invalid dbt manifest JSON"),
        ('{"nodes":{', "invalid dbt manifest JSON"),
        ('{"nodes":{},"ignored":{"a":1,"a":2}}', "duplicate JSON key"),
    ],
)
def test_streaming_invalid_json_is_actionable(tmp_path, text, match):
    path = tmp_path / "manifest.json"
    path.write_text(text)
    with pytest.raises(ContractError, match=match):
        read_manifest(path, "order_enriched", direct_contract())


def test_invalid_utf8_and_nonstandard_json_constants_do_not_traceback(tmp_path, capsys):
    sql = tmp_path / "model.sql"
    sql.write_bytes(b"\xff")
    contract = tmp_path / "contract.json"
    contract.write_text("{}")
    assert main(["compile", str(sql), str(contract)]) == 2
    assert "Traceback" not in capsys.readouterr().err
    sql.write_text("SELECT 1")
    contract.write_text('{"version":NaN}')
    assert main(["compile", str(sql), str(contract)]) == 2
    assert "not a valid JSON number" in capsys.readouterr().err
