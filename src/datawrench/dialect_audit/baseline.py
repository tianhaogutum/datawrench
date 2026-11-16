"""Portable audit snapshots and read-only comparisons (format version 1)."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict, deque
from copy import deepcopy
from itertools import accumulate
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile

import duckdb
import sqlglot
import yaml

from . import __version__
from .schema import InputError
from .report import diagnostic_label, format_location, related_details

MAX_BASELINE_BYTES = 64 * 1024 * 1024
COLUMN_FIELDS = {'column_start', 'column_end'}
STATEMENT_FIELDS = COLUMN_FIELDS | {'file', 'index', 'line_start', 'line_end', 'source_sql', 'target_sql',
                    'binding_status', 'functions', 'auto_stubs', 'diagnostics'}
CONFIG_FIELDS = {'read', 'write', 'schema_read', 'auto_stub', 'binding', 'schema'}
TOOLS = {'dialect-audit', 'sqlglot', 'duckdb', 'PyYAML'}


def snapshot(report: dict, root: str) -> dict:
    base = Path(root).resolve()
    if not base.is_dir():
        raise InputError('--root must be an existing directory')

    def relative(file):
        try:
            return Path(file).resolve().relative_to(base).as_posix()
        except ValueError as exc:
            raise InputError(f'{file}: input/schema must be inside --root {base}') from exc

    names = {file: relative(file) for file in report['file_summaries']}
    statements = []
    for stmt in report['statements']:
        item = {key: deepcopy(stmt[key]) for key in STATEMENT_FIELDS}
        item['file'] = names[stmt['file']]
        statements.append(item)
    notices = deepcopy(report['notices'])
    for item in notices:
        file = item['file']
        item['file'] = names[file]
        item['message'] = item['message'].replace(file, names[file])
    config = {key: report[key] for key in CONFIG_FIELDS}
    if report['schema']:
        path = Path(report['schema'])
        config['schema'] = {'file': relative(str(path)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return {'baseline_version': 1,
            'tool_versions': {'dialect-audit': __version__, 'sqlglot': sqlglot.__version__,
                              'duckdb': duckdb.__version__, 'PyYAML': yaml.__version__},
            'configuration': config, 'files': sorted(names.values()),
            'statements': sorted(statements, key=lambda s: (s['file'], s['index'])),
            'notices': sorted(notices, key=lambda n: (n['file'], n['category'], n['message']))}


def _validate(data: dict) -> None:
    """Fail closed on truncated, incompatible or ill-typed committed artifacts."""
    def require(condition):
        if not condition:
            raise InputError('invalid baseline version 1 structure; regenerate with the baseline command')

    def fields(value, keys):
        return isinstance(value, dict) and value.keys() == keys

    def strings(value):
        return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) and v for k, v in value.items())

    def name(value):
        return (isinstance(value, str) and value not in ('', '.') and
                not PurePosixPath(value).is_absolute() and '..' not in PurePosixPath(value).parts
                and PurePosixPath(value).as_posix() == value)

    def positive(value):
        return type(value) is int and value > 0

    def span(location, stmt):
        if location is not None:
            require(stmt is not None and fields(location, {'line_start', 'line_end'} | COLUMN_FIELDS))
            require(all(positive(v) for v in location.values()))
            first = (location['line_start'], location['column_start'])
            last = (location['line_end'], location['column_end'])
            require(first <= last)
            require(stmt['line_start'] <= first[0] <= last[0] <= stmt['line_end'])
            if stmt.get('column_start') is not None:
                require((stmt['line_start'], stmt['column_start']) <= first)
                require(last <= (stmt['line_end'], stmt['column_end']))

    def finding(value, stmt=None):
        keys = {'category', 'severity', 'message'}
        require(isinstance(value, dict) and keys <= value.keys() <= keys | {'location', 'related_locations'})
        span(value.get('location'), stmt)
        if 'related_locations' in value:
            related = value['related_locations']
            require(stmt is not None and value['category'] == 'PARSE_ERROR' and value.get('location') is None)
            require(isinstance(related, list) and len(related) >= 2)
            for entry in related:
                require(fields(entry, {'description', 'location'}))
                require(entry['description'] is None or isinstance(entry['description'], str))
                span(entry['location'], stmt)
        require(isinstance(value['category'], str) and bool(value['category']))
        require(value['severity'] in ('warn', 'error') and isinstance(value['message'], str))

    require(fields(data, {'baseline_version', 'tool_versions', 'configuration', 'files', 'statements', 'notices'}))
    require(type(data['baseline_version']) is int and data['baseline_version'] == 1)
    require(strings(data['tool_versions']) and TOOLS <= data['tool_versions'].keys())
    config = data['configuration']
    require(fields(config, CONFIG_FIELDS))
    require(all(isinstance(config[k], str) and config[k] for k in ('read', 'write', 'schema_read')))
    require(type(config['auto_stub']) is bool and config['binding'] in ('duckdb-explain', 'not-available'))
    schema = config['schema']
    if schema is not None:
        require(fields(schema, {'file', 'sha256'}))
        require(name(schema['file']) and isinstance(schema['sha256'], str) and
                len(schema['sha256']) == 64 and all(c in '0123456789abcdef' for c in schema['sha256']))
    require(isinstance(data['files'], list) and bool(data['files']) and all(name(f) for f in data['files']))
    require(len(set(data['files'])) == len(data['files']))
    require(isinstance(data['statements'], list) and isinstance(data['notices'], list))
    seen = set()
    for stmt in data['statements']:
        require(isinstance(stmt, dict) and stmt.keys() in (STATEMENT_FIELDS, STATEMENT_FIELDS - COLUMN_FIELDS))
        require(isinstance(stmt['file'], str) and stmt['file'] in data['files'])
        require(positive(stmt['index']) and positive(stmt['line_start']) and positive(stmt['line_end']))
        require(stmt['line_end'] >= stmt['line_start'])
        if COLUMN_FIELDS <= stmt.keys():
            start, end = stmt['column_start'], stmt['column_end']
            require((start is None and end is None) or (positive(start) and positive(end)))
            if start is not None and stmt['line_start'] == stmt['line_end']:
                require(end >= start)
        key = (stmt['file'], stmt['index'])
        require(key not in seen)
        seen.add(key)
        require(isinstance(stmt['source_sql'], str) and (stmt['target_sql'] is None or isinstance(stmt['target_sql'], str)))
        require(stmt['binding_status'] in ('bound', 'failed', 'not-attempted', 'static-only'))
        require(isinstance(stmt['functions'], dict) and all(
            isinstance(k, str) and positive(v) for k, v in stmt['functions'].items()))
        require(isinstance(stmt['auto_stubs'], list))
        for stub in stmt['auto_stubs']:
            require(fields(stub, {'table', 'columns', 'type'}))
            require(isinstance(stub['table'], str) and isinstance(stub['type'], str))
            require(isinstance(stub['columns'], list) and all(isinstance(c, str) for c in stub['columns']))
        require(isinstance(stmt['diagnostics'], list))
        for item in stmt['diagnostics']:
            finding(item, stmt)
    for item in data['notices']:
        require(fields(item, {'file', 'category', 'severity', 'message'}))
        require(isinstance(item['file'], str) and item['file'] in data['files'])
        finding({k: v for k, v in item.items() if k != 'file'})


def load_baseline(path: str) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise InputError(f'duplicate baseline key: {key}')
            value[key] = item
        return value

    try:
        if not Path(path).is_file():
            raise InputError('expected a regular baseline file')
        with Path(path).open('rb') as stream:
            payload = stream.read(MAX_BASELINE_BYTES + 1)
        if len(payload) > MAX_BASELINE_BYTES:
            raise InputError(f'baseline exceeds {MAX_BASELINE_BYTES} bytes')
        data = json.loads(payload.decode('utf-8'), object_pairs_hook=unique)
        _validate(data)
        return data
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise InputError(f'baseline {path}: {exc}') from exc


def save_baseline(data: dict, path: str, update: bool, protected: list[str]) -> None:
    target = Path(path).absolute()
    if target.is_symlink():
        raise InputError('baseline output must not be a symlink')
    for file in protected:
        source = Path(file)
        if target.resolve() == source.resolve() or (target.exists() and target.samefile(source)):
            raise InputError('baseline output must not overwrite an SQL input or schema fixture')
    if target.exists():
        if not update:
            raise InputError('baseline exists; use --update to explicitly replace it')
        load_baseline(str(target))  # --update never replaces an unrelated file.
    _validate(data)
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + '\n'
    if len(payload.encode('utf-8')) > MAX_BASELINE_BYTES:
        raise InputError(f'baseline exceeds {MAX_BASELINE_BYTES} bytes')
    fd, temporary = tempfile.mkstemp(prefix='.dialect-audit-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(payload)
        if update:
            os.replace(temporary, target)
        else:
            # Atomic no-clobber publication, even if another writer creates the name.
            os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _location(stmt: dict) -> dict:
    return {k: stmt.get(k) for k in ('file', 'index', 'line_start', 'line_end', 'column_start', 'column_end')}


def _anchor_gap_candidates(exact: list, old: dict, new: dict,
                           old_counts: Counter, new_counts: Counter) -> list:
    """Find singleton gaps before fallback can consume ambiguous candidates.

    Real, unique, consecutive exact matches must bound the gap. A boundary is
    stable only if no other exact match crosses it, including duplicate matches.
    This is positional evidence, never source similarity or target equivalence.
    """
    ordered = sorted(exact, key=lambda p: p[0]['index'])
    positions = [after['index'] for _, after in ordered]
    prefix_max = [0, *accumulate(positions, max)]
    suffix_min = [*reversed(list(accumulate(reversed(positions), min))), float('inf')]
    stable = [prefix_max[i] < after['index'] < suffix_min[i + 1]
              and old_counts[before['source_sql']] == new_counts[before['source_sql']] == 1
              for i, (before, after) in enumerate(ordered)]
    old_indices, new_indices = sorted(old), sorted(new)
    candidates = []
    for i, ((left_old, left_new), (right_old, right_new)) in enumerate(zip(ordered, ordered[1:])):
        if not (stable[i] and stable[i + 1]):
            continue
        old_start = bisect_right(old_indices, left_old['index'])
        old_end = bisect_left(old_indices, right_old['index'])
        new_start = bisect_right(new_indices, left_new['index'])
        new_end = bisect_left(new_indices, right_new['index'])
        if old_end - old_start == new_end - new_start == 1:
            candidates.append((old_indices[old_start], new_indices[new_start]))
    return candidates


def _pair_statements(baseline: dict, current: dict) -> tuple[list, list]:
    """Pair exact sources, same-index edits, then bounded singleton anchor gaps.

    The full source string is the dictionary key, so hash collisions cannot
    establish identity. Queues preserve occurrence order for duplicate SQL.
    """
    old_files, new_files = defaultdict(dict), defaultdict(dict)
    for statements, files in ((baseline['statements'], old_files), (current['statements'], new_files)):
        for stmt in statements:
            files[stmt['file']][stmt['index']] = stmt
    pairs, order_changes = [], []
    for file in sorted(old_files.keys() | new_files.keys()):
        old, new = old_files[file], new_files[file]
        old_counts = Counter(s['source_sql'] for s in old.values())
        new_counts = Counter(s['source_sql'] for s in new.values())
        sources = defaultdict(deque)
        for index in sorted(old):
            sources[old[index]['source_sql']].append(index)
        exact = []
        for index in sorted(new):
            candidates = sources.get(new[index]['source_sql'])
            if candidates:
                exact.append((old.pop(candidates.popleft()), new[index]))
        for _, after in exact:
            del new[after['index']]
        # A changed duplicate count makes occurrence identity ambiguous: an
        # inserted copy must not imply that another statement was reordered.
        ordered = [(before, after) for before, after in exact
                   if old_counts[before['source_sql']] == new_counts[before['source_sql']]]
        baseline_order = [before['index'] for before, _ in sorted(ordered, key=lambda p: p[0]['index'])]
        current_order = [before['index'] for before, _ in ordered]
        if baseline_order != current_order:
            order_changes.append({'file': file, 'baseline_indices': baseline_order, 'current_order': current_order,
                'matched_statements': [{'source_sql': before['source_sql'],
                                        'baseline_location': _location(before), 'current_location': _location(after)}
                                       for before, after in ordered]})
        candidates = _anchor_gap_candidates(exact, old, new, old_counts, new_counts)
        edits = [(old.pop(index), new.pop(index)) for index in sorted(old.keys() & new.keys())]
        edits.extend((old.pop(left), new.pop(right)) for left, right in candidates if left in old and right in new)
        remaining = [(stmt, None) for stmt in old.values()] + [(None, stmt) for stmt in new.values()]
        pairs.extend(sorted(exact + edits + remaining, key=lambda p: (p[1] or p[0])['index']))
    return pairs, order_changes


def compare(baseline: dict, current: dict) -> dict:
    result = {'diff_version': 1, 'changed': False, 'reasons': [],
              'current_binding_statuses': dict(sorted(Counter(s['binding_status'] for s in current['statements']).items()))}
    versions = result['version_changes'] = []
    for tool in sorted(baseline['tool_versions'].keys() | current['tool_versions'].keys()):
        before, after = baseline['tool_versions'].get(tool), current['tool_versions'].get(tool)
        if before != after:
            versions.append({'tool': tool, 'before': before, 'after': after})
    config = result['configuration_changes'] = []
    for key in sorted(CONFIG_FIELDS):
        before, after = baseline['configuration'][key], current['configuration'][key]
        if before != after:
            config.append({'option': key, 'before': before, 'after': after})
    files = result['file_changes'] = []
    old_files, new_files = set(baseline['files']), set(current['files'])
    for file in sorted(old_files ^ new_files):
        files.append({'file': file, 'change': 'added' if file in new_files else 'removed'})
    for key in ('statement_changes', 'source_changes', 'sql_changes', 'binding_changes',
                'analysis_changes', 'new_findings', 'resolved_findings'):
        result[key] = []
    pairs, result['statement_order_changes'] = _pair_statements(baseline, current)
    for before, after in pairs:
        if before is None or after is None:
            stmt = after if after is not None else before
            result['statement_changes'].append({**_location(stmt),
                'change': 'added' if after is not None else 'removed', 'statement': stmt})
        else:
            for output, fields in [('source_changes', ('source_sql', 'line_start', 'line_end')),
                                   ('sql_changes', ('target_sql',)), ('binding_changes', ('binding_status',)),
                                   ('analysis_changes', ('functions', 'auto_stubs'))]:
                # Index shifts from insertions/deletions are incidental positions,
                # not source edits. Relative reordering is recorded once per file.
                if (output == 'source_changes' and before['index'] != after['index']
                        and before['source_sql'] == after['source_sql']):
                    continue
                # Legacy v1 baselines lack columns. Compare columns only when both
                # snapshots know them; metadata enrichment alone is not SQL drift.
                if output == 'source_changes' and all(
                    s.get(k) is not None for s in (before, after) for k in COLUMN_FIELDS
                ):
                    fields += ('column_start', 'column_end')
                left, right = ({k: stmt[k] for k in fields} for stmt in (before, after))
                if left != right:
                    change = {**_location(after), 'baseline_location': _location(before),
                        'before': left[fields[0]] if len(fields) == 1 else left,
                        'after': right[fields[0]] if len(fields) == 1 else right}
                    if output in ('sql_changes', 'binding_changes', 'analysis_changes'):
                        change['source_sql'] = {'before': before['source_sql'], 'after': after['source_sql']}
                    result[output].append(change)
        left = before['diagnostics'] if before else []
        right = after['diagnostics'] if after else []
        _finding_changes(result, left, right,
                         {**_location(before), 'source_sql': before['source_sql']} if before else None,
                         {**_location(after), 'source_sql': after['source_sql']} if after else None)
    # File-level skipped/empty-input findings have no statement index or line range.
    for file in sorted(old_files | new_files):
        left = [{k: v for k, v in n.items() if k != 'file'} for n in baseline['notices'] if n['file'] == file]
        right = [{k: v for k, v in n.items() if k != 'file'} for n in current['notices'] if n['file'] == file]
        loc = {'file': file, 'index': None, 'line_start': None, 'line_end': None,
               'column_start': None, 'column_end': None}
        _finding_changes(result, left, right, loc, loc)
    for field, reason in [('version_changes', 'tool_versions_changed'), ('configuration_changes', 'configuration_changed'),
                          ('file_changes', 'files_changed'), ('statement_changes', 'statements_changed'),
                          ('statement_order_changes', 'statement_order_changed'),
                          ('source_changes', 'source_changed'), ('sql_changes', 'target_sql_changed'),
                          ('binding_changes', 'binding_changed'), ('analysis_changes', 'analysis_changed'),
                          ('new_findings', 'new_findings'), ('resolved_findings', 'resolved_findings')]:
        if result[field]:
            result['reasons'].append(reason)
    result['changed'] = bool(result['reasons'])
    return result


def _finding_changes(result, before, after, old_location, new_location):
    def counts(items):
        return Counter((d['category'], d['severity'], d['message']) for d in items)
    old, new = counts(before), counts(after)
    for output, items, loc in [('new_findings', new - old, new_location), ('resolved_findings', old - new, old_location)]:
        # Location is evidence, not identity: preserve it without creating
        # finding drift when older baselines omit it or a statement moves.
        candidates = after if output == 'new_findings' else before
        for identity, count in sorted(items.items()):
            matches = [d for d in candidates if (d['category'], d['severity'], d['message']) == identity]
            result[output].extend({**loc, **deepcopy(d)} for d in matches[:count])


def render_diff(result: dict, format: str) -> str:
    if format == 'json':
        return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True) + '\n'

    def location(item):
        if item['index'] is None:
            return item['file']
        return f"{item['file']}#{item['index']} {format_location(item)}"

    lines = ['dialect-audit diff: ' + (', '.join(result['reasons']) if result['changed'] else 'no changes')]
    for item in result['version_changes']:
        lines.append(f"TOOL_VERSION {item['tool']}: {item['before']} -> {item['after']}")
    for item in result['configuration_changes']:
        lines.append(f"CONFIGURATION {item['option']}: {json.dumps(item['before'])} -> {json.dumps(item['after'])}")
    for item in result['file_changes']:
        lines.append(f"FILE_{item['change'].upper()} {item['file']}")
    for item in result['statement_changes']:
        stmt = item['statement']
        lines.append(f"STATEMENT_{item['change'].upper()} {location(item)} (binding: {stmt['binding_status']})")
        lines.append('  source: ' + json.dumps(stmt['source_sql']))
        lines.append('  SQL: ' + json.dumps(stmt['target_sql']))
    for item in result['statement_order_changes']:
        lines.append(f"STATEMENT_ORDER_CHANGED {item['file']}: {item['baseline_indices']} -> {item['current_order']}")
        for match in item['matched_statements']:
            lines.append(f"  matched {location(match['current_location'])} (baseline: {location(match['baseline_location'])})")
            lines.append('    source: ' + json.dumps(match['source_sql']))
    for field, label in [('source_changes', 'SOURCE_CHANGED'), ('sql_changes', 'SQL_CHANGED'),
                         ('binding_changes', 'BINDING_CHANGED'), ('analysis_changes', 'ANALYSIS_CHANGED')]:
        for item in result[field]:
            heading = f"{label} {location(item)}"
            if item['baseline_location'] != _location(item):
                heading += f" (baseline: {location(item['baseline_location'])})"
            lines.append(heading)
            lines.extend(['  before: ' + json.dumps(item['before']), '  after: ' + json.dumps(item['after'])])
            if field in ('sql_changes', 'binding_changes', 'analysis_changes'):
                source = item['source_sql']
                if source['before'] == source['after']:
                    lines.append('  source: ' + json.dumps(source['after']))
                else:
                    lines.extend(['  source before: ' + json.dumps(source['before']),
                                  '  source after: ' + json.dumps(source['after'])])
    for field, label in [('new_findings', 'NEW'), ('resolved_findings', 'RESOLVED')]:
        for item in result[field]:
            lines.append(f"{label} {location(item)} {item['category']} ({item['severity']}){diagnostic_label(item)}: {item['message']}")
            lines.extend('  ' + line for line in related_details(item))
            if 'source_sql' in item:
                lines.append('  source: ' + json.dumps(item['source_sql']))
    lines.append('Current binding outcomes: ' + json.dumps(result['current_binding_statuses'], sort_keys=True))
    lines.append('A matching baseline acknowledges findings; it does not verify compatibility. Inferred stub types remain unverified.')
    return '\n'.join(lines) + '\n'
