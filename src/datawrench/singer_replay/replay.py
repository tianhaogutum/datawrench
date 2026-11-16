"""Catalog projection, source-time checkpoints, timestamp shifting and replay."""
from __future__ import annotations

import copy
import sys
import time
from datetime import datetime, timezone

from .cassette import catalog_entry, replication_key, validate_state
from .common import compare, render_time, require, timestamp


class InjectedFailure(Exception):
    def __init__(self, code):
        self.code = code


def notice(message):
    print(f"datawrench singer-replay tap: {message}", file=sys.stderr)


def schema_paths(schema, path=()):
    yield path
    for key, child in schema.get("properties", {}).items():
        yield from schema_paths(child, path + ("properties", key))
    if "items" in schema:
        yield from schema_paths(schema["items"], path + ("items",))


def selection(cassette, catalog):
    entries = cassette.catalog["streams"] if catalog is None else catalog.get("streams")
    require(isinstance(entries, list), "catalog requires a streams array")
    result, seen = {}, set()
    for entry in entries:
        require(isinstance(entry, dict), "catalog stream must be an object")
        stream = entry.get("tap_stream_id", entry.get("stream"))
        require(isinstance(stream, str) and stream not in seen, "missing or duplicate catalog stream id")
        seen.add(stream)
        require(entry.get("stream", stream) == stream, "stream renaming is unsupported")
        if stream not in cassette.validator.schemas:
            notice("unknown catalog stream skipped: " + repr(stream))
            continue
        original = cassette.validator.schemas[stream]
        require("schema" not in entry or entry["schema"] == original["schema"],
                "catalog may select fields but may not replace schemas")
        require("replication_key" not in entry or entry["replication_key"] == replication_key(original),
                "catalog may not change replication keys")
        require("key_properties" not in entry or entry["key_properties"] == original["key_properties"],
                "catalog may not change primary keys")
        metadata = entry.get("metadata", [])
        require(isinstance(metadata, list), "catalog metadata must be an array")
        flags, used = {}, set()
        valid = set(schema_paths(original["schema"]))
        discovered = {tuple(m["breadcrumb"]): m["metadata"]
                      for m in catalog_entry(original)["metadata"]}
        for item in metadata:
            require(isinstance(item, dict) and isinstance(item.get("breadcrumb"), list) and
                    all(isinstance(x, str) for x in item["breadcrumb"]) and
                    isinstance(item.get("metadata"), dict), "invalid catalog metadata entry")
            path = tuple(item["breadcrumb"])
            require(path in valid and path not in used, "unknown/duplicate metadata breadcrumb")
            used.add(path)
            meta = item["metadata"]
            for name in ("replication-method", "replication-key", "table-key-properties", "inclusion"):
                if name in meta:
                    expected = discovered.get(path, {}).get(name, "available" if name == "inclusion" and path else None)
                    require(meta[name] == expected, "catalog may not change replication or inclusion metadata")
            for name in ("selected", "selected-by-default"):
                require(name not in meta or type(meta[name]) is bool, "selected flags must be booleans")
            if "selected" in meta or "selected-by-default" in meta:
                flags[path] = meta.get("selected", meta.get("selected-by-default"))
        require("selected" not in entry or type(entry["selected"]) is bool, "selected must be boolean")
        if not flags.get((), entry.get("selected", True)):
            continue
        forced = set(original["key_properties"])
        key = replication_key(original)
        if key:
            forced.add(key)
        for field in forced:
            path = ("properties", field)
            if flags.get(path) is False:
                notice(f"retaining automatic key property {stream!r}.{field!r}")
            flags[path] = True
        result[stream] = flags
    return result


def project_schema(schema, flags, path=()):
    result = copy.deepcopy(schema)
    if "properties" in schema:
        result["properties"] = {
            key: project_schema(child, flags, path + ("properties", key))
            for key, child in schema["properties"].items()
            if flags.get(path + ("properties", key), True)}
        if "required" in result:
            result["required"] = [key for key in result["required"] if key in result["properties"]]
    if "items" in schema:
        require(flags.get(path + ("items",), True), "deselect the array property, not its items")
        result["items"] = project_schema(schema["items"], flags, path + ("items",))
    return result


def project_record(record, schema):
    if record is None:
        return None
    if isinstance(record, dict):
        return {key: project_record(value, schema["properties"][key])
                for key, value in record.items() if key in schema.get("properties", {})}
    if isinstance(record, list):
        return [project_record(value, schema["items"]) for value in record]
    return record


def datetimes(record, schema):
    if isinstance(record, dict):
        for key, value in record.items():
            yield from datetimes(value, schema["properties"][key])
    elif isinstance(record, list):
        for value in record:
            yield from datetimes(value, schema["items"])
    elif record is not None and schema.get("format") == "date-time":
        yield timestamp(record)


def shift_record(record, schema, delta):
    if isinstance(record, dict):
        return {key: shift_record(value, schema["properties"][key], delta) for key, value in record.items()}
    if isinstance(record, list):
        return [shift_record(value, schema["items"], delta) for value in record]
    if record is not None and schema.get("format") == "date-time":
        return render_time(timestamp(record) + delta)
    return record


def replay(cassette, config, emit, *, catalog=None, state=None):
    chosen = selection(cassette, catalog)
    schemas = cassette.validator.schemas
    output_schemas = {s: project_schema(schemas[s]["schema"], flags) for s, flags in chosen.items()}
    initial = validate_state(state or {}, schemas, external=True)
    bookmarks = {}
    for stream, bookmark in initial.items():
        if stream not in chosen:
            if stream not in schemas:
                notice("unknown state stream ignored: " + repr(stream))
            continue
        if not replication_key(schemas[stream]):
            notice(f"state for {stream!r} ignored: no replication key; full replay")
        else:
            bookmarks[stream] = copy.deepcopy(bookmark)
    cutoff = copy.deepcopy(bookmarks)
    delta = None
    if config["time_shift"]:
        newest = None
        for msg in cassette.messages():
            if msg["type"] == "RECORD":
                for value in datetimes(msg["record"], schemas[msg["stream"]]["schema"]):
                    newest = max(newest, value) if newest else value
        require(newest is not None, "time_shift requires a schema-declared date-time record field")
        anchor = (datetime.now(timezone.utc) if config["time_shift"] == "now"
                  else timestamp(config["time_shift"]))
        delta = anchor - newest
        # Fail before writing stdout on timestamp overflow, including unselected streams.
        for msg in cassette.messages():
            if msg["type"] == "RECORD":
                shift_record(msg["record"], schemas[msg["stream"]]["schema"], delta)
                if "time_extracted" in msg:
                    render_time(timestamp(msg["time_extracted"]) + delta)
    last_state = None
    last_kind = None
    count = 0

    def send(msg):
        nonlocal last_kind
        emit(msg)
        last_kind = msg["type"]

    def checkpoint():
        nonlocal last_state
        value = {"bookmarks": copy.deepcopy(bookmarks)}
        send({"type": "STATE", "value": value})
        last_state = value

    if config["fail_after_records"] == 0:
        checkpoint()
        raise InjectedFailure(config["fail_exit_code"])
    for source in cassette.messages():
        kind = source["type"]
        if kind == "STATE":
            # Re-emit the boundary, never an unselected or un-emitted source bookmark.
            checkpoint()
            continue
        stream = source["stream"]
        if stream not in chosen:
            continue
        msg = copy.deepcopy(source)
        if kind == "SCHEMA":
            msg["schema"] = output_schemas[stream]
            send(msg)
            continue
        key = replication_key(schemas[stream])
        if key and stream in cutoff:
            if compare(source["record"][key], cutoff[stream]["replication_key_value"],
                       schemas[stream]["schema"]["properties"][key]) <= 0:
                continue
        msg["record"] = project_record(msg["record"], output_schemas[stream])
        if delta is not None:
            msg["record"] = shift_record(msg["record"], output_schemas[stream], delta)
            if "time_extracted" in msg:
                msg["time_extracted"] = render_time(timestamp(msg["time_extracted"]) + delta)
        if config["delay_ms"]:
            time.sleep(config["delay_ms"] / 1000)
        send(msg)
        count += 1
        if key:
            bookmarks[stream] = {"replication_key": key, "replication_key_value": source["record"][key]}
        if config["fail_after_records"] is not None and count == config["fail_after_records"]:
            checkpoint()
            raise InjectedFailure(config["fail_exit_code"])
        if config["state_message_frequency"] and count % config["state_message_frequency"] == 0:
            checkpoint()
    if last_kind != "STATE" or last_state != {"bookmarks": bookmarks}:
        checkpoint()
    return count
