"""Streaming validator; storage scales with schemas, not record count."""
from __future__ import annotations

import gzip
import hashlib
import re
import stat
import zlib
from io import BufferedReader
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .io import ByteBudget, Diagnostic, InputError, MAX_LINE_BYTES, RawBudgetReader, dumps, identify, lines, open_beneath
from .model import MISSING, Stream, at_path, label, paths, stream_id, timestamp, instant
from .schema import compile_schema
from .quality import Quality, StreamQuality


class StopValidation(Exception):
    pass


class Validator:
    def __init__(self, *, protocol="auto", strict=False, check_order=False,
                 check_duplicates=False, max_errors=100, duplicate_limit=100_000,
                 batch_root=None, max_line_bytes=MAX_LINE_BYTES, max_streams=1000,
                 max_properties=10_000, quality=False, example_limit=5, key_example_limit=0,
                 max_input_bytes=None, max_batch_bytes=None, max_batch_raw_bytes=None,
                 max_batch_files=None, max_records=None, max_messages=None, max_declarations=None):
        if protocol not in {"auto", "singer", "airbyte"}: raise ValueError("invalid protocol")
        if min(max_errors, duplicate_limit, max_line_bytes, max_streams, max_properties) < 1:
            raise ValueError("limits must be positive")
        if type(example_limit) is not int or not 0 <= example_limit <= 100:
            raise ValueError("example_limit must be between 0 and 100")
        if type(key_example_limit) is not int or not 0 <= key_example_limit <= 100:
            raise ValueError("key_example_limit must be between 0 and 100")
        if max_input_bytes is not None and (type(max_input_bytes) is not int or not 0 <= max_input_bytes <= 2**63 - 1):
            raise ValueError("max_input_bytes must be between 0 and 9223372036854775807")
        self.max_input_bytes = max_input_bytes
        if max_batch_bytes is not None and (type(max_batch_bytes) is not int or not 0 <= max_batch_bytes <= 2**63 - 1):
            raise ValueError("max_batch_bytes must be between 0 and 9223372036854775807")
        self.batch_budget = None if max_batch_bytes is None else ByteBudget(max_batch_bytes)
        if max_batch_raw_bytes is not None and (type(max_batch_raw_bytes) is not int or not 0 <= max_batch_raw_bytes <= 2**63 - 1):
            raise ValueError("max_batch_raw_bytes must be between 0 and 9223372036854775807")
        self.batch_raw_budget = None if max_batch_raw_bytes is None else ByteBudget(max_batch_raw_bytes)
        if max_batch_files is not None and (type(max_batch_files) is not int or not 0 <= max_batch_files <= 2**63 - 1):
            raise ValueError("max_batch_files must be between 0 and 9223372036854775807")
        self.max_batch_files = max_batch_files
        self.batch_files = 0
        if max_records is not None and (type(max_records) is not int or not 0 <= max_records <= 2**63 - 1):
            raise ValueError("max_records must be between 0 and 9223372036854775807")
        self.max_records = max_records
        self.records = 0
        if max_messages is not None and (type(max_messages) is not int or not 0 <= max_messages <= 2**63 - 1):
            raise ValueError("max_messages must be between 0 and 9223372036854775807")
        self.max_messages = max_messages
        self.processed_messages = 0
        if max_declarations is not None and (type(max_declarations) is not int or not 0 <= max_declarations <= 2**63 - 1):
            raise ValueError("max_declarations must be between 0 and 9223372036854775807")
        self.max_declarations = max_declarations
        self.declarations = 0
        self._catalog_source = None
        self._catalog_path = None
        self.quality = Quality(example_limit, key_example_limit) if quality else None
        self.protocol = None if protocol == "auto" else protocol
        self.automatic = protocol == "auto"
        self.strict, self.check_order, self.check_duplicates = strict, check_order, check_duplicates
        self.max_errors, self.duplicate_limit = max_errors, duplicate_limit
        self.batch_root = Path(batch_root or ".").resolve()
        self.max_line_bytes, self.max_streams, self.max_properties = max_line_bytes, max_streams, max_properties
        self.streams = {}
        self.diagnostics = []
        self.messages = Counter()
        self.seen_keys = set()
        self.input_lines = 0
        self.seen_input = False
        self.operational = False
        self.stopped = False
        self.source = "<stdin>"
        self.line = 0

    def error(self, code, message, key=None, path=None, *, operational=False, source=None, line=None):
        if path is None and self._catalog_source is not None:
            path = self._catalog_path
        self.operational |= operational
        if self.quality is not None:
            self.quality.error(code, key, source or self.source, self.line if line is None else line, path)
            if operational:
                self.stopped = True
                raise StopValidation()
            return
        self.diagnostics.append(Diagnostic(code, message, self.line if line is None else line,
            label(key) if key else None, source or self.source, path))
        if len(self.diagnostics) >= self.max_errors or operational:
            self.stopped = True
            raise StopValidation()

    def register(self, name, namespace, schema, primary, cursor, *, declaration_path=None):
        key = stream_id(name, namespace)
        if key not in self.streams and len(self.streams) >= self.max_streams:
            self.error("TC020", "stream limit exceeded", operational=True)
        # Count attempts, not successful or unique contracts. Envelope/path and
        # name checks precede admission; even invalid schemas consume allowance.
        if self.max_declarations is not None and self.declarations >= self.max_declarations:
            self.error("TC020", f"schema registration attempt {self.declarations + 1} exceeds --max-declarations budget; remaining input and final STATE coverage not checked",
                       key, declaration_path, operational=True)
        self.declarations += 1
        validator = compile_schema(schema, self.strict)
        if isinstance(schema, dict) and len(schema.get("properties", {})) > self.max_properties:
            self.error("TC020", "schema property limit exceeded", key, operational=True)
        # Verify local references up front where possible; record-dependent references
        # remain guarded by record validation below.
        if any(not p for p in primary) or (cursor is not None and not cursor):
            raise InputError("empty key/cursor path", "TC003")
        import json
        signature = json.dumps([schema, primary, cursor], sort_keys=True, ensure_ascii=True)
        old = self.streams.get(key)
        if old:
            if old.signature != signature:
                self.error("TC009", "schema or key/cursor contract changed mid-stream; original contract retained", key)
            if self.quality is not None and old.signature == signature:
                self.quality.streams[key].schemas += 1
            return
        if self.quality is not None:
            self.quality.streams[key] = StreamQuality(schemas=1)
        self.streams[key] = Stream(name, namespace, schema, primary, cursor, validator, signature)

    def singer_schema(self, msg, *, declaration_path=None):
        if "schema" not in msg or "key_properties" not in msg:
            raise InputError("SCHEMA needs schema and key_properties", "TC003")
        pk = msg["key_properties"]
        if not isinstance(pk, list) or any(not isinstance(p, str) for p in pk):
            raise InputError("key_properties must be an array of strings", "TC003")
        bookmarks = msg.get("bookmark_properties", [])
        if not isinstance(bookmarks, list) or any(not isinstance(p, str) for p in bookmarks):
            raise InputError("bookmark_properties must be an array of strings", "TC003")
        cursor = msg.get("replication_key")
        if cursor is None and bookmarks:
            if len(bookmarks) > 1:
                raise InputError("only one Singer bookmark/replication key is supported", "TC003")
            cursor = bookmarks[0]
        if cursor is not None and not isinstance(cursor, str):
            raise InputError("replication_key must be a string", "TC003")
        self.register(msg.get("stream"), None, msg["schema"], [(p,) for p in pk], (cursor,) if cursor is not None else None,
                      declaration_path=declaration_path)

    def catalog(self, catalog):
        if not isinstance(catalog, dict) or not isinstance(catalog.get("streams"), list):
            raise InputError("catalog must contain a streams array", "TC003")
        for index, entry in enumerate(catalog["streams"]):
            if self._catalog_source is not None:
                self._catalog_path = ["streams", index]
            if not isinstance(entry, dict): raise InputError("catalog entry must be an object", "TC003")
            if self.protocol == "singer":
                schema_msg = dict(entry, stream=entry.get("tap_stream_id", entry.get("stream")))
                # Singer catalog replication keys may live in root metadata.
                if "replication_key" not in schema_msg:
                    for meta in entry.get("metadata", []) if isinstance(entry.get("metadata", []), list) else []:
                        if isinstance(meta, dict) and meta.get("breadcrumb") == []:
                            metadata = meta.get("metadata", {})
                            if isinstance(metadata, dict) and "replication-key" in metadata:
                                schema_msg["replication_key"] = metadata["replication-key"]
                self.singer_schema(schema_msg, declaration_path=["streams", index])
                continue
            stream = entry.get("stream", entry)
            if not isinstance(stream, dict) or "json_schema" not in stream:
                raise InputError("Airbyte catalog entry needs json_schema", "TC003")
            primary = entry.get("primary_key", stream.get("source_defined_primary_key", []))
            cursor = entry.get("cursor_field", stream.get("default_cursor_field", []))
            cursor_paths = paths([cursor], "cursor_field") if cursor else []
            self.register(stream.get("name"), stream.get("namespace"), stream["json_schema"],
                          paths(primary, "primary_key"), cursor_paths[0] if cursor_paths else None,
                          declaration_path=["streams", index])

    def load_catalog(self, catalog):
        if self.protocol is None:
            if not isinstance(catalog, dict) or not isinstance(catalog.get("streams"), list) or not catalog["streams"]:
                raise InputError("cannot infer protocol from catalog; use --protocol", "TC022")
            first = catalog["streams"][0]
            if not isinstance(first, dict):
                if self._catalog_source is not None:
                    self._catalog_path = ["streams", 0]
                raise InputError("invalid catalog entry", "TC003")
            inner = first.get("stream", first)
            self.protocol = "airbyte" if isinstance(inner, dict) and "json_schema" in inner else "singer"
        self.catalog(catalog)

    def record(self, key, data, extracted=None):
        stream = self.streams.get(key)
        if stream is None:
            self.error("TC005", "RECORD/BATCH for undeclared stream; supply SCHEMA/CATALOG or --catalog", key)
            return
        if not isinstance(data, dict):
            # Preserve the existing final-STATE requirement for rejected data.
            stream.dirty = True
            self.error("TC003", "record data must be an object", key)
            return
        # Admission follows decoding/stream/type checks, but precedes every
        # record evaluation and record-state mutation (including quality samples).
        if self.max_records is not None:
            if self.records >= self.max_records:
                self.error("TC020", f"accepted object record {self.records + 1} exceeds --max-records budget; remaining input and final STATE coverage not checked",
                           key, operational=True)
            self.records += 1
        stream.dirty = True
        stream.count += 1
        if self.quality is not None:
            self.quality.streams[key].record(stream, data, self.source, self.line, self.quality.key_example_limit)
        try:
            for err in stream.validator.iter_errors(data):
                code = "TC013" if err.validator == "format" and err.validator_value == "date-time" else "TC006"
                self.error(code, "record violates JSON Schema (" + str(err.validator) + ")", key, list(err.absolute_path))
        except StopValidation:
            raise
        except Exception as exc:
            # jsonschema wraps reference failures in version-specific exceptions.
            if isinstance(exc, (KeyboardInterrupt, SystemExit)): raise
            self.error("TC004", "schema could not be evaluated (unresolved reference, recursion or invalid regex)", key)
            return
        for path in stream.primary:
            val = at_path(data, path)
            if val is MISSING or val is None:
                self.error("TC010", "key property missing or null", key, list(path))
        if self.strict and isinstance(stream.schema, dict):
            properties = stream.schema.get("properties", {})
            patterns = stream.schema.get("patternProperties", {})
            for prop, prop_schema in properties.items():
                if prop not in data:
                    try:
                        nullable = stream.validator.evolve(schema=prop_schema).is_valid(None)
                    except Exception:
                        self.error("TC004", "cannot resolve property schema", key, [prop])
                        continue
                    if not nullable:
                        self.error("TC011", "non-nullable declared property absent", key, [prop])
            for prop in data:
                if prop not in properties and not any(re.search(p, prop) for p in patterns):
                    self.error("TC012", "undeclared top-level property", key, [prop])
        for prop, val in data.items():
            if prop not in stream.present and len(stream.present) >= self.max_properties:
                self.error("TC020", "statistics property limit exceeded", key, operational=True)
            stream.present[prop] = stream.present.get(prop, 0) + 1
            if val is None: stream.nulls[prop] = stream.nulls.get(prop, 0) + 1
        if stream.cursor:
            val = at_path(data, stream.cursor)
            if val is MISSING or val is None or type(val) not in (str, int, float):
                if self.check_order:
                    self.error("TC014", "cursor must be present and a non-null string or number", key, list(stream.cursor))
            else:
                def comparable(a, b):
                    return (type(a) in (int, float) and type(b) in (int, float)) or type(a) is type(b)
                # date-time cursors compare instants, numeric/string cursors compare naturally.
                compare_val = self.cursor_value(stream, val, diagnose=True)
                if compare_val is not None:
                    if stream.last_cursor is not MISSING and not comparable(compare_val, stream.last_cursor):
                        if self.check_order: self.error("TC014", "cursor type changed", key, list(stream.cursor))
                    else:
                        if self.check_order and stream.last_cursor is not MISSING and compare_val < stream.last_cursor:
                            self.error("TC015", "replication key moved backwards", key, list(stream.cursor))
                        if stream.minimum is None or (comparable(compare_val, self.cursor_value(stream, stream.minimum)) and compare_val < self.cursor_value(stream, stream.minimum)):
                            stream.minimum = val
                        if stream.maximum is None or (comparable(compare_val, self.cursor_value(stream, stream.maximum)) and compare_val > self.cursor_value(stream, stream.maximum)):
                            stream.maximum = val
                    stream.last_cursor = compare_val
        if self.check_duplicates and stream.primary and all(at_path(data, p) is not MISSING and at_path(data, p) is not None for p in stream.primary):
            values = [at_path(data, p) for p in stream.primary]
            # Normalize numeric equality (1 and 1.0); JSON object key order is irrelevant.
            def normalize(v):
                if type(v) is float and v.is_integer(): return int(v)
                if isinstance(v, list): return [normalize(x) for x in v]
                if isinstance(v, dict): return {k: normalize(x) for k, x in v.items()}
                return v
            import json
            digest = hashlib.sha256(json.dumps([key, normalize(values)], sort_keys=True, ensure_ascii=True).encode()).digest()
            if digest in self.seen_keys:
                self.error("TC016", "duplicate primary key", key)
            else:
                if len(self.seen_keys) >= self.duplicate_limit:
                    self.error("TC018", "duplicate tracking capacity exceeded; raise --duplicate-limit", key, operational=True)
                self.seen_keys.add(digest)
            if self.quality is not None:
                self.quality.streams[key].duplicate_records_evaluated += 1
        if extracted is not None or self.protocol == "airbyte":
            try:
                value = timestamp(extracted, self.protocol)
                if stream.first_time is None: stream.first_time = value
                stream.last_time = value
            except InputError as exc:
                self.error(exc.code, str(exc), key)

    def cursor_value(self, stream, val, diagnose=False):
        schema = stream.schema
        for part in stream.cursor:
            schema = schema.get("properties", {}).get(part, {}) if isinstance(schema, dict) else {}
        if isinstance(schema, dict) and schema.get("format") == "date-time" and isinstance(val, str):
            try: return instant(val)
            except InputError as exc:
                if diagnose and self.check_order:
                    self.error("TC014", str(exc), (stream.name, stream.namespace), list(stream.cursor))
                    return None
                # Without order checking, invalid format annotations are strings.
                pass
        return val

    def airbyte_state(self, state):
        if not isinstance(state, dict): raise InputError("Airbyte state must be an object", "TC007")
        kind = state.get("type", "LEGACY")
        if not isinstance(kind, str) or kind not in {"LEGACY", "STREAM", "GLOBAL"}: raise InputError("unknown Airbyte state type", "TC007")
        active = {"LEGACY": "data", "STREAM": "stream", "GLOBAL": "global"}[kind]
        if active not in state or any(state.get(k) is not None for k in {"data", "stream", "global"} - {active}):
            raise InputError("state payload is missing or conflicts with state type", "TC007")
        def blob(value):
            if value is not None and not isinstance(value, dict): raise InputError("state blob must be object or null", "TC007")
        def stream_state(value):
            if not isinstance(value, dict) or not isinstance(value.get("stream_descriptor"), dict):
                raise InputError("STREAM state needs stream_descriptor", "TC007")
            descriptor = value["stream_descriptor"]
            try: key = stream_id(descriptor.get("name"), descriptor.get("namespace"))
            except InputError: raise InputError("invalid stream descriptor", "TC007") from None
            blob(value.get("stream_state"))
            return key
        if kind == "LEGACY":
            blob(state["data"])
            keys = list(self.streams)
        elif kind == "STREAM": keys = [stream_state(state["stream"])]
        else:
            value = state["global"]
            if not isinstance(value, dict) or not isinstance(value.get("stream_states"), list):
                raise InputError("GLOBAL state needs stream_states array", "TC007")
            blob(value.get("shared_state"))
            keys = [stream_state(v) for v in value["stream_states"]]
        self.cover_state(keys)

    def cover_state(self, keys):
        for key in set(keys):
            if key in self.streams:
                self.streams[key].dirty = False
                if self.quality is not None:
                    self.quality.streams[key].states += 1

    def batch(self, msg):
        key = stream_id(msg.get("stream"))
        if key not in self.streams:
            self.error("TC005", "BATCH for undeclared stream", key)
            return
        encoding, manifest = msg.get("encoding"), msg.get("manifest")
        if not isinstance(encoding, dict) or encoding.get("format") != "jsonl" or encoding.get("compression") not in (None, "none", "gzip"):
            raise InputError("BATCH supports only local jsonl and gzip jsonl", "TC017")
        if not isinstance(manifest, list) or not manifest or any(not isinstance(p, str) for p in manifest):
            raise InputError("BATCH manifest must be a nonempty array of paths", "TC017")
        for index, raw in enumerate(manifest):
            # Charge occurrences, including invalid paths, before any path work.
            # Envelope validation above retains its existing error priority.
            if self.max_batch_files is not None:
                if self.batch_files >= self.max_batch_files:
                    self.error("TC020", f"BATCH manifest occurrence {self.batch_files + 1} exceeds --max-batch-files budget; remaining input and final STATE coverage not checked",
                               key, ["manifest", index], operational=True)
                self.batch_files += 1
            try:
                parsed = urlsplit(raw)
                if parsed.scheme and parsed.scheme != "file": raise ValueError()
                if parsed.query or parsed.fragment or "\x00" in raw: raise ValueError()
                # file://relative/path is the Meltano SDK spelling of a relative path.
                if parsed.scheme == "file":
                    path_text = unquote((parsed.netloc + parsed.path) if parsed.netloc else parsed.path)
                else: path_text = raw
                path = (self.batch_root / path_text).resolve()
                if not path.is_relative_to(self.batch_root): raise ValueError()
                if not stat.S_ISREG(path.stat().st_mode): raise ValueError()
            except (ValueError, OSError, RuntimeError):
                self.error("TC017", "BATCH path is missing, non-regular, remote or outside --batch-root", key)
                continue
            try:
                from contextlib import ExitStack
                batch_source = str(path.relative_to(self.batch_root))
                with ExitStack() as stack:
                    handle = stack.enter_context(open_beneath(self.batch_root, path.relative_to(self.batch_root)))
                    if self.batch_raw_budget is not None:
                        handle = stack.enter_context(RawBudgetReader(handle, self.batch_raw_budget,
                            f"BATCH {batch_source!r} exceeds --max-batch-raw-bytes budget; remaining input and final STATE coverage not checked"))
                    if encoding.get("compression") == "gzip":
                        handle = stack.enter_context(gzip.GzipFile(fileobj=handle, mode="rb"))
                    elif self.batch_raw_budget is not None:
                        handle = stack.enter_context(BufferedReader(handle))
                    for number, value in lines(handle, batch_source, self.max_line_bytes,
                            byte_budget=self.batch_budget,
                            budget_message=f"BATCH {batch_source!r} exceeds --max-batch-bytes budget; remaining input and final STATE coverage not checked"):
                        if isinstance(value, Diagnostic):
                            self.error(value.code, value.message, key, source=value.source, line=value.line)
                        else:
                            previous_source, previous_line = self.source, self.line
                            try:
                                self.source, self.line = str(path.relative_to(self.batch_root)), number
                                self.record(key, value)
                            finally: self.source, self.line = previous_source, previous_line
            except InputError as exc:
                self.error(exc.code, str(exc), key, operational=True, source=batch_source, line=exc.line)
            except (OSError, EOFError, zlib.error):
                self.error("TC017", "cannot read BATCH file or corrupt gzip data", key)

    def consume(self, msg):
        if not isinstance(msg, dict): raise InputError("message must be a JSON object", "TC003")
        if self.automatic:
            detected = identify(msg)
            if detected and self.protocol and detected != self.protocol:
                self.error("TC022", "mixed protocols; select --protocol and provide a single-protocol stream", operational=True)
            if detected: self.protocol = detected
        kind = msg.get("type")
        if not isinstance(kind, str): raise InputError("message needs string type", "TC003")
        if self.protocol == "singer": kind = kind.upper()
        # In full-scan quality mode, arbitrary unsupported type names must not
        # accumulate an unbounded message Counter.
        counted_kind = kind
        if self.quality is not None and kind not in {
                "SCHEMA", "CATALOG", "RECORD", "STATE", "BATCH", "ACTIVATE_VERSION", "LOG", "TRACE", "CONTROL"}:
            counted_kind = "UNSUPPORTED"
        self.messages[counted_kind] += 1
        if self.protocol == "singer":
            if kind == "SCHEMA": self.singer_schema(msg)
            elif kind == "RECORD": self.record(stream_id(msg.get("stream")), msg.get("record"), msg.get("time_extracted"))
            elif kind == "STATE":
                if not isinstance(msg.get("value"), dict): raise InputError("Singer STATE value must be an object", "TC007")
                self.cover_state(self.streams)
            elif kind == "BATCH": self.batch(msg)
            elif kind == "ACTIVATE_VERSION":
                key = stream_id(msg.get("stream"))
                if type(msg.get("version")) is not int: raise InputError("ACTIVATE_VERSION needs integer version", "TC003")
                if key not in self.streams: self.error("TC005", "ACTIVATE_VERSION for undeclared stream", key)
            else: self.error("TC002", "unknown Singer message type")
        elif self.protocol == "airbyte":
            if kind == "CATALOG": self.catalog(msg.get("catalog"))
            elif kind == "RECORD":
                rec = msg.get("record")
                if not isinstance(rec, dict): raise InputError("Airbyte RECORD needs record object", "TC003")
                self.record(stream_id(rec.get("stream"), rec.get("namespace")), rec.get("data"), rec.get("emitted_at"))
            elif kind == "STATE": self.airbyte_state(msg.get("state"))
            elif kind in {"LOG", "TRACE", "CONTROL"}:
                if not isinstance(msg.get(kind.lower()), dict): raise InputError("passthrough message needs matching object payload", "TC003")
            else: self.error("TC002", "unknown or unsupported Airbyte message type")
        else:
            self.error("TC022", "cannot infer protocol; use --protocol singer or airbyte", operational=True)

    def run(self, handle, *, source="<stdin>", catalog=None, catalog_source="<catalog>"):
        self.source = source
        try:
            if catalog is not None:
                main_source, main_line = self.source, self.line
                self._catalog_source = catalog_source
                # Predecoded catalogs have entry paths, but no physical lines.
                self.source, self.line = catalog_source, 0
                try:
                    try: self.load_catalog(catalog)
                    except InputError as exc: self.error(exc.code, str(exc), operational=True)
                finally:
                    self.source, self.line = main_source, main_line
                    self._catalog_source = self._catalog_path = None
            for number, msg in lines(handle, source, self.max_line_bytes, max_bytes=self.max_input_bytes):
                # Reader events include parse diagnostics, but exclude ordinary
                # blanks and BATCH payload rows. Reading/decoding precedes this
                # gate; all dispatch and checked-prefix counters follow it.
                if self.max_messages is not None and self.processed_messages >= self.max_messages:
                    self.error("TC020", f"main input event {self.processed_messages + 1} exceeds --max-messages budget; remaining input and final STATE coverage not checked",
                               operational=True, line=number)
                self.processed_messages += 1
                self.line = number
                self.input_lines = number
                self.seen_input = True
                if isinstance(msg, Diagnostic):
                    self.error(msg.code, msg.message)
                    continue
                try: self.consume(msg)
                except InputError as exc:
                    self.error(exc.code, str(exc), operational=exc.code == "TC022")
            if not self.seen_input: self.error("TC021", "empty input: nothing validated", operational=True)
            if self.protocol is None: self.error("TC022", "no identifiable protocol; use --protocol", operational=True)
            for key, stream in self.streams.items():
                if stream.dirty: self.error("TC008", "missing final STATE covering records for stream", key)
        except StopValidation:
            pass
        except InputError as exc:
            # Reader failures stop before EOF: preserve checked-prefix counts
            # and do not issue final-state/empty-input verdicts on the tail.
            try: self.error(exc.code, str(exc), operational=True, line=exc.line)
            except StopValidation: pass
        except (OSError, EOFError):
            try: self.error("TC021", "failed to read input", operational=True)
            except StopValidation: pass
        return self.report()

    def report(self):
        if self.quality is not None:
            return self.quality.report(self)
        exit_code = 2 if self.operational else 1 if self.diagnostics else 0
        return {"report_version": 1, "protocol": self.protocol, "exit_code": exit_code,
                "valid": exit_code == 0, "errors": len(self.diagnostics), "stopped_early": self.stopped,
                "input_lines": self.input_lines, "messages": dict(self.messages),
                "diagnostics": [d.as_dict() for d in self.diagnostics],
                "streams": [s.report() for s in self.streams.values()]}
