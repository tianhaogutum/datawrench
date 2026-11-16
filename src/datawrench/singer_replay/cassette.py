"""Cassette validation, atomic metadata, and streaming recording."""
from __future__ import annotations

import copy
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .common import (MAX_JSON, MAX_LINE, MAX_MESSAGES_BYTES, ReplayError,
                     check_schema, compare, decode, encode, lines, order_value,
                     require, validate)

FILES = ("messages.jsonl.gz", "catalog.json")


def replication_key(message):
    keys = message.get("bookmark_properties", [])
    require(isinstance(keys, list) and len(keys) <= 1 and all(isinstance(k, str) for k in keys),
            "only one bookmark property is supported")
    key = message.get("replication_key")
    require(key is None or isinstance(key, str), "replication_key must be a string")
    require(not (key and keys and keys != [key]), "conflicting replication keys")
    return key or (keys[0] if keys else None)


def catalog_entry(message):
    key = replication_key(message)
    props = message["schema"]["properties"]
    automatic = set(message["key_properties"]) | ({key} if key else set())
    metadata = [{"breadcrumb": [], "metadata": {
        "selected": True, "table-key-properties": message["key_properties"],
        "replication-method": "INCREMENTAL" if key else "FULL_TABLE",
        **({"replication-key": key} if key else {})}}]
    metadata.extend({"breadcrumb": ["properties", name], "metadata": {
        "selected": True, "inclusion": "automatic" if name in automatic else "available"}}
        for name in props)
    return {"tap_stream_id": message["stream"], "stream": message["stream"],
            "schema": message["schema"], "key_properties": message["key_properties"],
            **({"replication_key": key} if key else {}), "metadata": metadata}


def validate_state(value, schemas, *, external=False):
    require(isinstance(value, dict) and set(value) <= {"bookmarks"},
            "state supports only a bookmarks object; opaque source state is unsupported")
    bookmarks = value.get("bookmarks", {})
    require(isinstance(bookmarks, dict), "bookmarks must be an object")
    for stream, bookmark in bookmarks.items():
        require(isinstance(bookmark, dict) and set(bookmark) == {
            "replication_key", "replication_key_value"},
            "bookmark requires only replication_key and replication_key_value")
        if stream not in schemas:
            require(external, "recorded state references an unknown stream")
            continue
        key = replication_key(schemas[stream])
        if not key and external:
            continue
        require(key is not None and bookmark["replication_key"] == key,
                "bookmark replication key differs from cassette")
        prop = schemas[stream]["schema"]["properties"][key]
        validate(bookmark["replication_key_value"], prop)
        order_value(bookmark["replication_key_value"], prop)
    return bookmarks


class Validator:
    def __init__(self):
        self.schemas = {}
        self.counts = Counter()
        self.latest = {}
        self.messages = 0

    def accept(self, msg):
        kind = msg.get("type")
        require(kind in ("SCHEMA", "RECORD", "STATE"),
                "unsupported message type (only uppercase SCHEMA/RECORD/STATE)")
        if kind == "STATE":
            require(set(msg) == {"type", "value"}, "unsupported STATE attributes")
            bookmarks = validate_state(msg["value"], self.schemas)
            for stream, bookmark in bookmarks.items():
                key = replication_key(self.schemas[stream])
                require(stream in self.latest, "STATE precedes records for its stream")
                require(compare(bookmark["replication_key_value"], self.latest[stream],
                                self.schemas[stream]["schema"]["properties"][key]) <= 0,
                        "STATE bookmark is ahead of recorded data")
        else:
            stream = msg.get("stream")
            require(isinstance(stream, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", stream),
                    "stream must be a safe identifier: [A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
            if kind == "SCHEMA":
                require(set(msg) <= {"type", "stream", "schema", "key_properties",
                                     "bookmark_properties", "replication_key"},
                        "unsupported SCHEMA attributes")
                require("schema" in msg and "key_properties" in msg, "SCHEMA missing required attributes")
                check_schema(msg["schema"])
                require(msg["schema"]["type"] == "object" and "properties" in msg["schema"],
                        "stream schema must be an object with properties")
                props = msg["schema"]["properties"]
                keys = msg["key_properties"]
                require(isinstance(keys, list) and all(isinstance(k, str) and k in props for k in keys)
                        and len(set(keys)) == len(keys), "invalid key_properties")
                for pk in keys:
                    kinds = props[pk]["type"]
                    kinds = kinds if isinstance(kinds, list) else [kinds]
                    require(not (set(kinds) & {"object", "array"}), "primary keys must be scalar")
                key = replication_key(msg)
                require(key is None or key in props, "replication key is absent from schema")
                if key:
                    kinds = props[key]["type"]
                    kinds = kinds if isinstance(kinds, list) else [kinds]
                    require(set(kinds) <= {"string", "integer", "number", "null"},
                            "replication key must be a string or number")
                require(stream not in self.schemas or self.schemas[stream] == msg,
                        "schema evolution within a cassette is unsupported")
                self.schemas[stream] = copy.deepcopy(msg)
                self.counts.setdefault(stream, 0)
            else:
                require(set(msg) <= {"type", "stream", "record", "time_extracted"},
                        "unsupported RECORD attributes")
                require(stream in self.schemas, "RECORD precedes its SCHEMA")
                require(isinstance(msg.get("record"), dict), "RECORD requires an object")
                schema = self.schemas[stream]
                validate(msg["record"], schema["schema"])
                for key in schema["key_properties"]:
                    require(key in msg["record"] and msg["record"][key] is not None,
                            "RECORD missing/non-null primary key")
                if "time_extracted" in msg:
                    from .common import timestamp
                    timestamp(msg["time_extracted"])
                key = replication_key(schema)
                if key:
                    require(key in msg["record"], "RECORD missing replication key")
                    current = msg["record"][key]
                    order_value(current, schema["schema"]["properties"][key])
                    if stream in self.latest:
                        require(compare(current, self.latest[stream], schema["schema"]["properties"][key]) > 0,
                                "replication values must be strictly increasing per stream (ties unsupported)")
                    self.latest[stream] = current
                self.counts[stream] += 1
        self.messages += 1

    def catalog(self):
        return {"streams": [catalog_entry(m) for m in self.schemas.values()]}


def scrub_rules(values):
    result = {}
    for rule in values:
        try:
            target, mode = rule.rsplit("=", 1)
            if ":" in target:
                stream, path = target.split(":", 1)
                if path.startswith("/"):
                    parts = path[1:].split("/")
                    require(1 <= len(parts) <= 32 and all(parts),
                            "scrub JSON Pointer requires 1..32 nonempty property names")
                    require(not any(re.search(r"~(?:[^01]|$)", part) for part in parts),
                            "scrub JSON Pointer escapes must be ~0 or ~1")
                    # Decode once: ~01 names the literal '~1', not '/'.
                    field = tuple(part.replace("~1", "/").replace("~0", "~") for part in parts)
                else:
                    field = tuple(path.split("."))
                    require(1 <= len(field) <= 32 and all(
                        re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in field),
                        "scrub path requires 1..32 dot-separated names ([A-Za-z0-9_-]+)")
            else:
                stream, field = target.rsplit(".", 1)
        except ValueError as exc:
            raise ReplayError("scrub syntax is STREAM.FIELD=MODE, STREAM:OBJECT.FIELD=MODE "
                              "or STREAM:/JSON/Pointer=MODE") from exc
        require(stream and field and mode in {"hash", "mask", "drop"}, "invalid scrub rule")
        path = field if isinstance(field, tuple) else (field,)
        for name, previous in result:
            if name == stream:
                previous = previous if isinstance(previous, tuple) else (previous,)
                require(path != previous, "duplicate scrub rule")
                require(path[:len(previous)] != previous and previous[:len(path)] != path,
                        "overlapping scrub paths are unsupported")
        result[stream, field] = mode
    return result


def scrub_schema_parent(schema, path, *, explicit):
    """Locate only declared object properties; never expand arrays or expressions."""
    for index, field in enumerate(path):
        kinds = schema["type"]
        kinds = kinds if isinstance(kinds, list) else [kinds]
        require("object" in kinds and set(kinds) <= {"object", "null"},
                "scrub path traverses a non-object schema")
        require(field in schema.get("properties", {}), "scrub path is absent from stream schema")
        if explicit:
            # Object examples/defaults can contain raw values for the targeted leaf.
            schema.pop("examples", None)
            schema.pop("default", None)
        if index == len(path) - 1:
            return schema
        schema = schema["properties"][field]


def scrub_message(msg, rules, salt):
    msg = copy.deepcopy(msg)
    stream = msg.get("stream")
    for (name, target), mode in rules.items():
        if name != stream:
            continue
        explicit = isinstance(target, tuple)
        path = target if explicit else (target,)
        field = path[-1]
        if msg["type"] == "SCHEMA":
            require(path[0] not in msg["key_properties"] and path[0] != replication_key(msg),
                    "scrubbing primary/replication keys is unsupported")
            parent = scrub_schema_parent(msg["schema"], path, explicit=explicit)
            props = parent["properties"]
            if mode == "drop":
                del props[field]
                if "required" in parent:
                    parent["required"] = [k for k in parent["required"] if k != field]
            else:
                props[field] = {"type": ["string", "null"]}
        elif msg["type"] == "RECORD":
            parent = msg["record"]
            if not explicit and field not in parent:
                continue  # Preserve legacy behavior for absent optional top-level fields.
            for index, part in enumerate(path):
                require(isinstance(parent, dict), "scrub path traverses a non-object record value")
                require(part in parent, "scrub path is absent from record")
                if index < len(path) - 1:
                    parent = parent[part]
            value = parent[field]
            if mode == "drop":
                del parent[field]
            elif value is not None:
                domain = path[0] if len(path) == 1 else list(path)
                parent[field] = ("***" if mode == "mask" else
                    hmac.new(salt, json.dumps([stream, domain, value], sort_keys=True,
                             ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(),
                             hashlib.sha256).hexdigest())
    return msg


def write_private(path, data):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as dst:
        dst.write(data)


def _metadata_data(value, *, reserved_bytes=0):
    """Encoded JSON framing, UTF-8 and newline count toward every metadata bound."""
    data = (encode(value) + "\n").encode()
    require(len(data) + reserved_bytes <= MAX_JSON, "cassette metadata exceeds 16 MiB")
    return data


def write_metadata(directory, name, value):
    temp = directory / (name + ".tmp")
    data = _metadata_data(value)
    write_private(temp, data)
    os.replace(temp, directory / name)


def _scrub_descriptions(rules):
    return [{"stream": s, **({"path": list(f)} if isinstance(f, tuple) else {"field": f}),
             "mode": mode} for (s, f), mode in rules.items()]


def _initial_manifest(rules, metadata):
    manifest = {"format_version": 1, "complete": False, "recorded_at":
                datetime.now(timezone.utc).isoformat(), "scrub": _scrub_descriptions(rules)}
    manifest.update(metadata or {})
    return manifest


def _final_manifest(manifest, validator, *, complete, truncated, exit_code, checksums):
    return {**manifest, "complete": complete, "truncated": truncated,
            "source_exit_code": exit_code, "counts": dict(validator.counts),
            "message_count": validator.messages, "sha256": checksums}


def record(source, directory, *, rules=None, salt=None, limit=None, finish=None):
    """finish(truncated) returns the source exit code, after the pipe is closed."""
    return _record_messages(lines(source), directory, rules=rules, salt=salt,
                            limit=limit, finish=finish)


def _output_data(message, output_bytes):
    """The writer and read-only planner enforce the same encoded message bounds."""
    data = (encode(message) + "\n").encode()
    output_bytes += len(data)
    require(len(data) <= MAX_LINE, "output message exceeds 16 MiB")
    require(output_bytes <= MAX_MESSAGES_BYTES, "output messages exceed 1 GiB uncompressed")
    return data, output_bytes


def _record_messages(messages, directory, *, rules=None, salt=None, limit=None,
                     finish=None, metadata=None, truncated=False):
    rules = rules or {}
    salt = salt or secrets.token_bytes(32)
    directory = Path(directory)
    directory.mkdir(mode=0o700)  # Exclusive: never overwrite or merge with input-controlled files.
    validator, raw_validator = Validator(), Validator()
    manifest = _initial_manifest(rules, metadata)
    write_metadata(directory, "manifest.json", manifest)
    error = None
    exit_code = None
    output_bytes = 0
    try:
        with os.fdopen(os.open(directory / FILES[0], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as dst:
                for message in messages:
                    raw_validator.accept(message)
                    cleaned = scrub_message(message, rules, salt)
                    data, output_bytes = _output_data(cleaned, output_bytes)
                    validator.accept(cleaned)
                    dst.write(data)
                    if limit is not None and sum(validator.counts.values()) >= limit:
                        truncated = True
                        break
        require(validator.schemas, "no SCHEMA messages recorded")
        require(all(s in raw_validator.schemas for s, _ in rules), "scrub rule names an absent stream")
    except BaseException as exc:
        error = exc
    finally:
        if finish:
            try:
                exit_code = finish(truncated or error is not None)
            except BaseException as exc:
                error = error or exc
        write_metadata(directory, "catalog.json", validator.catalog())
        checksums = {name: digest_file(directory / name) for name in FILES if (directory / name).exists()}
        manifest = _final_manifest(manifest, validator,
                                   complete=error is None and (truncated or exit_code in (None, 0)),
                                   truncated=truncated, exit_code=exit_code, checksums=checksums)
        write_metadata(directory, "manifest.json", manifest)
    if error:
        raise error
    require(manifest["complete"], "tap command exited non-zero; cassette marked incomplete")
    return manifest


def digest_file(path):
    with open_regular(path) as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024 * 1024:
        os.close(fd)
        raise ReplayError("cassette members must be regular, non-symlink files <=1 GiB")
    return os.fdopen(fd, "rb")


class Cassette:
    """Keep the verified message fd open across validation and replay."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.raw = None

    def __enter__(self):
        try:
            with open_regular(self.directory / "manifest.json") as src:
                data = src.read(MAX_JSON + 1)
                require(len(data) <= MAX_JSON, "manifest exceeds size limit")
                self.manifest = decode(data)
            m = self.manifest
            require(isinstance(m, dict) and m.get("format_version") == 1 and m.get("complete") is True,
                    "unsupported or incomplete cassette")
            require(isinstance(m.get("sha256"), dict) and set(m["sha256"]) == set(FILES),
                    "manifest must checksum messages and catalog")
            self.raw = open_regular(self.directory / FILES[0])
            require(hashlib.file_digest(self.raw, "sha256").hexdigest() == m["sha256"][FILES[0]],
                    "messages checksum mismatch")
            with open_regular(self.directory / FILES[1]) as src:
                require(hashlib.file_digest(src, "sha256").hexdigest() == m["sha256"][FILES[1]],
                        "catalog checksum mismatch")
                src.seek(0)
                data = src.read(MAX_JSON + 1)
                require(len(data) <= MAX_JSON, "catalog exceeds size limit")
                self.catalog = decode(data)
            self.validator = Validator()
            for msg in self.messages():
                self.validator.accept(msg)
            require(self.validator.schemas, "cassette contains no schemas")
            require(self.catalog == self.validator.catalog(), "catalog differs from recorded schemas")
            require(m.get("counts") == dict(self.validator.counts) and
                    m.get("message_count") == self.validator.messages, "manifest counts mismatch")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def messages(self):
        self.raw.seek(0)
        with gzip.GzipFile(fileobj=self.raw, mode="rb") as src:
            yield from lines(src)

    def __exit__(self, *args):
        if self.raw:
            self.raw.close()


def verify(source):
    """Validate the entire source and summarize its actual messages, read-only."""
    with Cassette(source) as cassette:
        validator = cassette.validator
        summary = {"counts": dict(validator.counts), "message_count": validator.messages,
                   "truncated": bool(cassette.manifest.get("truncated", False)),
                   "streams": list(validator.schemas)}
        _metadata_data(summary)
        return summary


def scrub(source, directory, *, rules):
    """Re-scrub an existing cassette without changing its message boundaries."""
    require(isinstance(rules, (list, tuple)) and rules and all(isinstance(r, str) for r in rules),
            "scrub requires a nonempty list of rule strings")
    rules = scrub_rules(rules)
    require(all(mode in {"mask", "drop"} for mode in rules.values()),
            "offline scrub supports only mask and drop")
    destination = Path(directory)
    require(not destination.exists() and not destination.is_symlink(),
            "output directory already exists; refusing to overwrite")
    try:
        nested = destination.resolve().is_relative_to(Path(source).resolve())
    except (OSError, RuntimeError) as exc:  # Python 3.11 reports symlink loops as RuntimeError.
        raise ReplayError("output path cannot be resolved safely") from exc
    require(not nested, "output directory must be outside the source cassette")
    with Cassette(source) as cassette:
        require(all(s in cassette.validator.schemas for s, _ in rules),
                "scrub rule names an absent stream")
        # Validate schema paths and protected keys before creating any output.
        for schema in cassette.validator.schemas.values():
            scrub_message(schema, rules, None)
        inherited = cassette.manifest.get("scrub", [])
        require(isinstance(inherited, list), "source scrub provenance must be a list")
        descriptions = _scrub_descriptions(rules)
        metadata = {"recorded_at": cassette.manifest.get("recorded_at"),
                    "scrub": inherited + descriptions,
                    "rescrub": {"rules": descriptions,
                                "source_sha256": cassette.manifest["sha256"]}}
        return _record_messages(cassette.messages(), directory, rules=rules, metadata=metadata,
                                truncated=bool(cassette.manifest.get("truncated", False)))


def trim_limits(values):
    """Parse repeatable CLI overrides without silently replacing duplicate IDs."""
    limits = {}
    for value in values:
        match = re.fullmatch(r"([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})=([0-9]+)", value)
        require(match is not None,
                "stream-limit must be STREAM=N with a safe stream ID and nonnegative decimal integer")
        stream, count = match.groups()
        require(stream not in limits, "duplicate stream-limit for the same stream ID")
        limits[stream] = int(count)
    return limits


def _trim_messages(cassette, limits, *, compact_states=False, max_records=None,
                   final_state_only=False, state_every=None, schema_once=False):
    counts, bookmarks = Counter(), {}
    seen_schemas = set()
    total = 0
    last_kind, last_state = None, None
    for message in cassette.messages():
        kind = message["type"]
        if kind == "STATE":
            if final_state_only or state_every is not None:
                continue
            # Source bookmarks may refer to records removed by this trim.
            message = {"type": "STATE", "value": {"bookmarks": copy.deepcopy(bookmarks)}}
        elif message["stream"] not in limits:
            continue
        elif kind == "SCHEMA" and schema_once:
            # Whole-source preflight has already rejected nonidentical repeats.
            if message["stream"] in seen_schemas:
                continue
            seen_schemas.add(message["stream"])
        elif kind == "RECORD":
            stream = message["stream"]
            if counts[stream] >= limits[stream] or (max_records is not None and total >= max_records):
                continue
            counts[stream] += 1
            total += 1
            key = replication_key(cassette.validator.schemas[stream])
            if key:
                bookmarks[stream] = {"replication_key": key,
                                     "replication_key_value": message["record"][key]}
        # Adjacency is measured after filtering/capping/schema suppression.
        if compact_states and kind == "STATE" and last_kind == "STATE" and message == last_state:
            continue
        yield message
        last_kind = kind
        last_state = message if kind == "STATE" else None
        if state_every is not None and kind == "RECORD" and total % state_every == 0:
            yield {"type": "STATE", "value": {"bookmarks": copy.deepcopy(bookmarks)}}
            last_kind = "STATE"
    if final_state_only or last_kind != "STATE":
        yield {"type": "STATE", "value": {"bookmarks": copy.deepcopy(bookmarks)}}


def _trim_summary(messages, truncated, metadata):
    """Consume the actual selector without buffering records or creating artifacts."""
    manifest = _initial_manifest({}, metadata)
    _metadata_data(manifest)
    validator, output_bytes = Validator(), 0
    for message in messages:
        _, output_bytes = _output_data(message, output_bytes)
        validator.accept(message)
    summary = {"counts": dict(validator.counts), "message_count": validator.messages,
               "truncated": truncated, "streams": list(validator.schemas)}
    for value in (validator.catalog(), summary):
        _metadata_data(value)
    # Empty slots encode only the checksum map's JSON framing. Each real SHA256
    # adds exactly 64 ASCII hex bytes; no output hashing or pretend digests.
    manifest = _final_manifest(manifest, validator, complete=True, truncated=truncated,
                               exit_code=None, checksums=dict.fromkeys(FILES, ""))
    _metadata_data(manifest, reserved_bytes=64 * len(FILES))
    return summary


def trim(source, directory=None, *, per_stream, stream_limits=None, include_streams=None,
         exclude_streams=None, compact_states=False, max_records=None, drop_empty_streams=False,
         final_state_only=False, state_every=None, schema_once=False, dry_run=False):
    """Write a prefix cassette, or return its bounded read-only metadata plan."""
    require(type(dry_run) is bool, "dry-run must be boolean")
    require(directory is None if dry_run else directory is not None,
            "dry-run cannot have an output path" if dry_run else "output path is required")
    require(type(per_stream) is int and per_stream >= 0,
            "per-stream limit must be a nonnegative integer")
    require(type(compact_states) is bool, "compact-states must be boolean")
    require(type(drop_empty_streams) is bool, "drop-empty-streams must be boolean")
    require(type(final_state_only) is bool, "final-state-only must be boolean")
    require(type(schema_once) is bool, "schema-once must be boolean")
    require(state_every is None or (type(state_every) is int and state_every > 0),
            "state-every must be a positive integer")
    require(state_every is None or not final_state_only,
            "state-every and final-state-only cannot be combined")
    require(max_records is None or (type(max_records) is int and max_records >= 0),
            "max-records must be a nonnegative integer")
    if stream_limits is None:
        stream_limits = {}
    require(isinstance(stream_limits, dict) and all(
        isinstance(s, str) and type(n) is int and n >= 0 for s, n in stream_limits.items()),
        "stream-limits must map stream IDs to nonnegative integers")
    require(include_streams is None or exclude_streams is None,
            "include-stream and exclude-stream cannot be combined")
    for option, streams in (("include-stream", include_streams), ("exclude-stream", exclude_streams)):
        if streams is not None:
            require(isinstance(streams, list) and streams and all(
                isinstance(s, str) and s for s in streams),
                f"{option} requires a nonempty list of nonempty stream IDs")
            require(len(set(streams)) == len(streams), f"duplicate {option} ID")
    if not dry_run:
        require(not Path(directory).exists(), "output directory already exists; refusing to overwrite")
        require(not Path(directory).resolve().is_relative_to(Path(source).resolve()),
                "output directory must be outside the source cassette")
    # Even N=0 preflights the entire source before creating a destination.
    with Cassette(source) as cassette:
        require(not (stream_limits.keys() - cassette.validator.schemas.keys()),
                "stream-limit names an unknown stream")
        included = set(cassette.validator.schemas) if include_streams is None else set(include_streams)
        require(not (included - cassette.validator.schemas.keys()),
                "include-stream names an unknown stream")
        if exclude_streams is not None:
            require(not (set(exclude_streams) - cassette.validator.schemas.keys()),
                    "exclude-stream names an unknown stream")
            included.difference_update(exclude_streams)
            require(included, "cannot exclude every stream; at least one schema must remain")
        require(not (stream_limits.keys() - included), "stream-limit names an excluded stream")
        limits = {s: stream_limits.get(s, per_stream) for s in cassette.validator.schemas if s in included}
        metadata = {"recorded_at": cassette.manifest.get("recorded_at"),
                    "scrub": cassette.manifest.get("scrub", []),
                    "trim": {"per_stream": per_stream,
                             "source_sha256": cassette.manifest["sha256"]}}
        if stream_limits:
            metadata["trim"]["stream_limits"] = dict(sorted(stream_limits.items()))
        if include_streams is not None:
            metadata["trim"]["include_streams"] = sorted(include_streams)
        if exclude_streams is not None:
            metadata["trim"]["exclude_streams"] = sorted(exclude_streams)
        if compact_states:
            metadata["trim"]["compact_states"] = True
        if schema_once:
            metadata["trim"]["schema_once"] = True
        if final_state_only:
            metadata["trim"]["final_state_only"] = True
        if state_every is not None:
            metadata["trim"]["state_every"] = state_every
        if max_records is not None:
            metadata["trim"]["max_records"] = max_records
        truncated = bool(cassette.manifest.get("truncated")) or any(
            count > limits.get(stream, 0) for stream, count in cassette.validator.counts.items())
        candidates = sum(min(count, limits.get(stream, 0))
                         for stream, count in cassette.validator.counts.items())
        truncated = truncated or (max_records is not None and candidates > max_records)
        if drop_empty_streams:
            # Decide schema eligibility before writing, using the same source-order
            # record selection as output. Keep only stream IDs in memory, not records.
            nonempty = {m["stream"] for m in _trim_messages(cassette, limits, max_records=max_records)
                        if m["type"] == "RECORD"}
            require(nonempty, "cannot drop every stream; at least one schema must remain")
            limits = {s: n for s, n in limits.items() if s in nonempty}
            metadata["trim"]["drop_empty_streams"] = True
        messages = _trim_messages(cassette, limits, compact_states=compact_states,
                                  max_records=max_records, final_state_only=final_state_only,
                                  state_every=state_every, schema_once=schema_once)
        if dry_run:
            return _trim_summary(messages, truncated, metadata)
        return _record_messages(messages, directory, metadata=metadata, truncated=truncated)
