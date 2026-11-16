"""Bounded, value-free aggregates for the quality command."""
from collections import Counter
from dataclasses import dataclass, field

from .model import MISSING, at_path


@dataclass
class Issues:
    errors: Counter = field(default_factory=Counter)
    examples: list = field(default_factory=list)

    def add(self, code, source, line, path, limit):
        self.errors[code] += 1
        if len(self.examples) < limit:
            self.examples.append({'code': code, 'source': source, 'line': line, 'path': path})

    def report(self):
        return {'errors_by_code': dict(sorted(self.errors.items())), 'examples': list(self.examples),
                'omitted_examples': sum(self.errors.values()) - len(self.examples)}


@dataclass
class StreamQuality:
    schemas: int = 0
    states: int = 0
    # Index by declaration occurrence so repeated paths retain existing totals.
    missing_by_path: Counter = field(default_factory=Counter)
    null_by_path: Counter = field(default_factory=Counter)
    examples_by_path: dict = field(default_factory=dict)
    incomplete_keys: int = 0
    duplicate_records_evaluated: int = 0
    issues: Issues = field(default_factory=Issues)

    def record(self, stream, data, source, line, limit):
        incomplete = False
        for index, path in enumerate(stream.primary):
            value = at_path(data, path)
            kind = None
            if value is MISSING:
                self.missing_by_path[index] += 1
                kind = "missing"
            elif value is None:
                self.null_by_path[index] += 1
                kind = "null"
            if kind is not None:
                incomplete = True
                if limit:
                    examples = self.examples_by_path.setdefault(index, [])
                    if len(examples) < limit:
                        examples.append({'kind': kind, 'source': source, 'line': line})
        self.incomplete_keys += incomplete

    def report(self, stream, check_duplicates):
        return {'name': stream.name, 'namespace': stream.namespace,
                'schemas': self.schemas, 'records': stream.count, 'states': self.states,
                'keys': {'paths': [list(p) for p in stream.primary],
                         'by_path': [{'path': list(path), 'missing_fields': self.missing_by_path[index],
                                      'null_fields': self.null_by_path[index],
                                      'examples': list(self.examples_by_path.get(index, [])),
                                      'omitted_examples': self.missing_by_path[index] + self.null_by_path[index]
                                                          - len(self.examples_by_path.get(index, []))}
                                     for index, path in enumerate(stream.primary)],
                         'missing_fields': sum(self.missing_by_path.values()),
                         'null_fields': sum(self.null_by_path.values()),
                         'records_with_missing_or_null_keys': self.incomplete_keys},
                'duplicates': {'enabled': check_duplicates, 'evaluated_records': self.duplicate_records_evaluated,
                               'duplicate_keys': self.issues.errors['TC016'] if check_duplicates and stream.primary else None},
                **self.issues.report()}


class Quality:
    def __init__(self, example_limit, key_example_limit):
        self.example_limit = example_limit
        self.key_example_limit = key_example_limit
        self.streams = {}
        self.unassigned = Issues()
        self.errors = Counter()

    def error(self, code, key, source, line, path):
        self.errors[code] += 1
        bucket = self.streams[key].issues if key in self.streams else self.unassigned
        bucket.add(code, source, line, path, self.example_limit)

    def report(self, engine):
        not_evaluated = ['full_protocol_certification', 'checkpoint_contents', 'version_delete_semantics',
                         'remote_or_non_jsonl_batches', 'partitioned_cursor_order']
        if not engine.check_duplicates: not_evaluated.append('duplicate_keys')
        if any(not stream.primary for stream in engine.streams.values()):
            not_evaluated.append('streams_without_primary_keys')
        if not engine.check_order: not_evaluated.append('cursor_order')
        if not engine.strict: not_evaluated.append('strict_presence_closure_and_datetime')
        if self.errors['TC004']: not_evaluated.append('checks_after_schema_evaluation_failure')
        if self.errors['TC005']: not_evaluated.append('records_for_undeclared_streams')
        if self.errors['TC017']: not_evaluated.append('unreadable_or_unsupported_batch_contents')
        if self.errors['TC001'] or self.errors['TC020']: not_evaluated.append('unparsed_or_oversized_input')
        if engine.stopped:
            not_evaluated.extend(['remaining_input_after_stop', 'final_state_coverage'])
        errors = sum(self.errors.values())
        exit_code = 2 if engine.operational else 1 if errors else 0
        keys = sorted(self.streams, key=lambda k: (k[0], k[1] is not None, k[1] or ''))
        return {'report_version': 1, 'report_kind': 'data_quality', 'protocol': engine.protocol,
                'exit_code': exit_code, 'valid': exit_code == 0, 'errors': errors,
                'stopped_early': engine.stopped, 'coverage': 'partial_input' if engine.stopped else 'complete_input',
                'input_lines': engine.input_lines, 'messages': dict(sorted(engine.messages.items())),
                'example_limit': self.example_limit, 'key_example_limit': self.key_example_limit, 'errors_by_code': dict(sorted(self.errors.items())),
                'unassigned': self.unassigned.report(),
                'streams': [self.streams[k].report(engine.streams[k], engine.check_duplicates) for k in keys],
                'not_evaluated': sorted(not_evaluated)}
