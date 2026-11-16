"""Loss-aware conversion of the shared JSONL subset."""
from __future__ import annotations

import base64
from datetime import datetime
import json
import re

from .engine import Validator
from .io import Diagnostic, InputError, decode, identify, lines
from .model import timestamp

PREFIX = "tapcheck:ab:"
STATE_KEY = "__tapcheck_airbyte_state__"


def singer_name(name, namespace=None):
    if namespace is None and not name.startswith(PREFIX): return name
    token = base64.urlsafe_b64encode(json.dumps([namespace, name], ensure_ascii=True, separators=(",", ":")).encode()).decode().rstrip("=")
    return PREFIX + token


def airbyte_name(name):
    if not name.startswith(PREFIX): return name, None
    try:
        token = name[len(PREFIX):]
        value = decode(base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, list) or len(value) != 2: raise ValueError()
        ns, stream = value
        if not isinstance(stream, str) or (ns is not None and not isinstance(ns, str)): raise ValueError()
        if singer_name(stream, ns) != name: raise ValueError()
        return stream, ns
    except (ValueError, UnicodeError):
        raise InputError("invalid reserved tapcheck:ab: stream name", "TC023") from None



def check_subset(msg, source):
    """Refuse fields with no mapping instead of silently dropping metadata."""
    def fields(obj, allowed):
        if set(obj) - allowed:
            raise InputError("field outside conversion subset; remove protocol-specific metadata explicitly", "TC023")
    kind = msg["type"].upper() if source == "singer" else msg["type"]
    if source == "singer":
        allowed = {
            "SCHEMA": {"type", "stream", "schema", "key_properties", "bookmark_properties", "replication_key"},
            "RECORD": {"type", "stream", "record", "time_extracted"},
            "STATE": {"type", "value"},
        }
        if kind in allowed: fields(msg, allowed[kind])
        if kind == "SCHEMA" and msg.get("replication_key") is not None and msg.get("bookmark_properties", [msg["replication_key"]]) != [msg["replication_key"]]:
            raise InputError("conflicting Singer bookmark and replication key", "TC023")
    else:
        allowed = {"CATALOG": {"type", "catalog"}, "RECORD": {"type", "record"}, "STATE": {"type", "state"}}
        if kind in allowed: fields(msg, allowed[kind])
        if kind == "RECORD": fields(msg["record"], {"stream", "namespace", "data", "emitted_at"})
        if kind == "CATALOG":
            fields(msg["catalog"], {"streams"})
            for entry in msg["catalog"]["streams"]:
                stream = entry.get("stream", entry)
                if "stream" in entry: fields(entry, {"stream", "primary_key", "cursor_field"})
                fields(stream, {"name", "namespace", "json_schema", "source_defined_primary_key", "default_cursor_field", "supported_sync_modes"})


def convert_message(msg, source, target):
    check_subset(msg, source)
    kind = msg["type"].upper() if source == "singer" else msg["type"]
    if source == target:
        raise InputError("input already uses target protocol", "TC023")
    if source == "singer":
        if kind == "SCHEMA":
            name, ns = airbyte_name(msg["stream"])
            stream = {"name": name, "json_schema": msg["schema"], "supported_sync_modes": ["full_refresh", "incremental"]}
            if ns is not None: stream["namespace"] = ns
            primary = [[p] for p in msg["key_properties"]]
            cursor = msg.get("replication_key")
            if cursor is None and msg.get("bookmark_properties"): cursor = msg["bookmark_properties"][0]
            stream["source_defined_primary_key"] = primary
            stream["default_cursor_field"] = [cursor] if cursor is not None else []
            return [{"type": "CATALOG", "catalog": {"streams": [stream]}}]
        if kind == "RECORD":
            if "time_extracted" not in msg or msg["time_extracted"] is None:
                raise InputError("conversion to Airbyte requires time_extracted; no timestamp is invented", "TC023")
            text = msg["time_extracted"]
            timestamp(text, "singer")
            # Avoid rounding away sub-millisecond data.
            dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
            fraction = re.search(r"\.(\d+)", text)
            if (fraction and len(fraction.group(1).rstrip("0")) > 3) or dt.microsecond % 1000:
                raise InputError("Airbyte conversion requires millisecond timestamp precision", "TC023")
            epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo)
            # timestamp() correctly accounts for source timezone; use UTC integer arithmetic.
            from datetime import timezone
            delta = dt.astimezone(timezone.utc) - epoch.replace(tzinfo=timezone.utc)
            emitted = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
            name, ns = airbyte_name(msg["stream"])
            rec = {"stream": name, "data": msg["record"], "emitted_at": emitted}
            if ns is not None: rec["namespace"] = ns
            return [{"type": "RECORD", "record": rec}]
        if kind == "STATE":
            value = msg["value"]
            if STATE_KEY in value:
                if set(value) != {STATE_KEY}: raise InputError("reserved state envelope has extra fields", "TC023")
                state = value[STATE_KEY]
                Validator(protocol="airbyte").airbyte_state(state)
            else: state = {"type": "LEGACY", "data": value}
            return [{"type": "STATE", "state": state}]
    else:
        if kind == "CATALOG":
            output = []
            for entry in msg["catalog"]["streams"]:
                stream = entry.get("stream", entry)
                primary = entry.get("primary_key", stream.get("source_defined_primary_key", []))
                cursor = entry.get("cursor_field", stream.get("default_cursor_field", []))
                if any(len(p) != 1 for p in primary) or len(cursor) > 1:
                    raise InputError("nested Airbyte primary/cursor paths cannot map to Singer", "TC023")
                schema = {"type": "SCHEMA", "stream": singer_name(stream["name"], stream.get("namespace")),
                          "schema": stream["json_schema"], "key_properties": [p[0] for p in primary]}
                if cursor: schema["bookmark_properties"] = cursor
                output.append(schema)
            return output
        if kind == "RECORD":
            rec = msg["record"]
            return [{"type": "RECORD", "stream": singer_name(rec["stream"], rec.get("namespace")),
                     "record": rec["data"], "time_extracted": timestamp(rec["emitted_at"], "airbyte")}]
        if kind == "STATE":
            state = msg["state"]
            if state.get("type", "LEGACY") == "LEGACY" and isinstance(state.get("data"), dict) and STATE_KEY not in state["data"]:
                value = state["data"]
            else: value = {STATE_KEY: state}
            return [{"type": "STATE", "value": value}]
    raise InputError("message type is outside conversion subset (SCHEMA/CATALOG, RECORD, STATE)", "TC023")


def convert(handle, target, *, protocol="auto", catalog=None, source_name="<stdin>", max_input_bytes=None):
    """Yield messages; raise on any validation/loss boundary. Output may be partial."""
    engine = Validator(protocol=protocol, max_errors=1)
    if catalog is not None:
        engine.load_catalog(catalog)
        if engine.protocol == "airbyte":
            check_subset({"type": "CATALOG", "catalog": catalog}, "airbyte")
        else:
            for entry in catalog["streams"]:
                if set(entry) - {"type", "stream", "tap_stream_id", "schema", "key_properties", "bookmark_properties", "replication_key"}:
                    raise InputError("Singer catalog metadata outside conversion subset", "TC023")
        if engine.protocol == target: raise InputError("catalog already uses target protocol", "TC023")
        # External catalog is emitted before records as an inline target catalog/schema.
        if engine.protocol == "airbyte":
            yield from convert_message({"type": "CATALOG", "catalog": catalog}, "airbyte", target)
        else:
            for s in engine.streams.values():
                message = {"type": "SCHEMA", "stream": s.name, "schema": s.schema, "key_properties": [p[0] for p in s.primary]}
                if s.cursor: message["bookmark_properties"] = list(s.cursor)
                yield from convert_message(message, "singer", target)
    had_input = False
    from .engine import StopValidation
    for number, msg in lines(handle, source_name, max_bytes=max_input_bytes):
        had_input = True
        engine.line, engine.source = number, source_name
        if isinstance(msg, Diagnostic): raise InputError(f"line {number}: {msg.message}", msg.code)
        try:
            engine.consume(msg)
            if not engine.protocol:
                raise InputError("cannot infer conversion input protocol", "TC022")
            yield from convert_message(msg, engine.protocol, target)
        except StopValidation:
            d = engine.diagnostics[-1]
            raise InputError(f"line {number}: {d.message}", d.code) from None
        except InputError as exc:
            raise InputError(f"line {number}: {exc}", exc.code) from None
    if not had_input: raise InputError("empty input: nothing converted", "TC021")
    if any(s.dirty for s in engine.streams.values()):
        raise InputError("missing final STATE; converted output is incomplete", "TC008")
