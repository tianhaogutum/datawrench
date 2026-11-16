"""Regenerate our synthetic, redistributable test inputs. No network calls."""
from pathlib import Path
import gzip
import json

ROOT = Path(__file__).resolve().parents[2]  # repository root (tests/tapcheck/make_fixtures.py)


def write(path, messages):
    (ROOT / path).write_text("".join(json.dumps(m, separators=(",", ":")) + "\n" for m in messages), encoding="utf-8")


def main():
    schema = {"type": "SCHEMA", "stream": "users", "schema": {"type": "object", "properties": {
        "id": {"type": "integer"}, "updated": {"type": "integer"}, "name": {"type": ["string", "null"]}}},
        "key_properties": ["id"], "bookmark_properties": ["updated"]}
    record = lambda i: {"type": "RECORD", "stream": "users", "record": {"id": i, "updated": i, "name": None if i == 1 else "Ada"}, "time_extracted": f"2026-01-01T00:00:0{i}.000Z"}
    state = {"type": "STATE", "value": {"bookmarks": {"users": 2}}}
    write("examples/tapcheck/good_singer.jsonl", [schema, record(1), record(2), state])
    issue = {"type": "SCHEMA", "stream": "stream", "schema": {"type": "object", "properties": {"id": {"type": "integer"}}}, "key_properties": ["id"]}
    write("tests/tapcheck/fixtures/singer_tools_issue_19.jsonl", [issue, {"type": "RECORD", "stream": "stream", "record": {}}, {"type": "STATE", "value": {}}])
    ab_schema = {"type": "CATALOG", "catalog": {"streams": [{"stream": {"name": "users", "namespace": "public", "json_schema": schema["schema"], "supported_sync_modes": ["incremental"]}, "primary_key": [["id"]], "cursor_field": ["updated"]}]}}
    ab_record = {"type": "RECORD", "record": {"stream": "users", "namespace": "public", "data": record(1)["record"], "emitted_at": 1767225601000}}
    ab_state = {"type": "STATE", "state": {"type": "STREAM", "stream": {"stream_descriptor": {"name": "users", "namespace": "public"}, "stream_state": {"updated": 1}}}}
    write("examples/tapcheck/good_airbyte.jsonl", [ab_schema, ab_record, ab_state])
    write("tests/tapcheck/fixtures/airbyte_bad_state.jsonl", [ab_schema, ab_record, {"type": "STATE", "state": {"type": "STREAM", "stream": {"stream_state": {}}}}])
    data = b'{"id":1,"updated":1,"name":null}\n{"id":2,"updated":2,"name":"Ada"}\n'
    (ROOT / "tests/tapcheck/fixtures/batch.jsonl").write_bytes(data)
    (ROOT / "tests/tapcheck/fixtures/batch.jsonl.gz").write_bytes(gzip.compress(data, mtime=0))
    for extension, compression in [("jsonl", None), ("jsonl.gz", "gzip")]:
        batch = {"type": "BATCH", "stream": "users", "encoding": {"format": "jsonl", "compression": compression}, "manifest": [f"file://batch.{extension}"]}
        write(f"tests/tapcheck/fixtures/batch_{'gzip' if compression else 'plain'}.jsonl", [schema, batch, state])


if __name__ == "__main__": main()
