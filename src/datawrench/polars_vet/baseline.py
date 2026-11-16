"""Versioned, location-independent diagnostic identities and fail-closed comparison."""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

from .analysis import Diagnostic, RULES, Source


@dataclass(frozen=True)
class Classified:
    diagnostic: Diagnostic
    status: str
    reason: str = ''

    def to_dict(self):
        data = {**self.diagnostic.to_dict(), 'baselineStatus': self.status}
        if self.reason:
            data['baselineReason'] = self.reason
        return data


def relative(path: str | Path, root: Path) -> str:
    try:
        return Path(os.path.abspath(path)).relative_to(root).as_posix()
    except ValueError:
        raise ValueError('Baseline inputs must be inside the baseline file directory') from None


def policy(paths: list[str], root: Path, selected: set[str], unused: bool) -> dict:
    targets = {(relative(path, root), 'directory' if Path(path).is_dir() else 'file') for path in paths}
    return {'targets': [{'path': path, 'kind': kind} for path, kind in sorted(targets)],
            'selected': sorted(selected), 'reportUnusedSuppressions': unused}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def identities(text: str, diagnostics: list[Diagnostic], root: Path) -> list[dict | None]:
    """Hash the diagnostic node, statement header and named scope, never line numbers.

    Compound statement bodies are excluded: changes to unrelated loop/function
    statements must not invalidate a diagnostic in the header. Identical repeated
    statements in the same named scope intentionally remain ambiguous.
    """
    if not any(d.code != 'PLV000' for d in diagnostics):
        return [None] * len(diagnostics)
    source = Source(text, '')
    nodes = {}
    wanted = {(d.line, d.column, d.end_line, d.end_column) for d in diagnostics if d.code != 'PLV000'}

    def visit(node, scopes=(), statement=None):
        if isinstance(node, ast.stmt):
            statement = node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scopes = (*scopes, (type(node).__name__, node.name))
        if (isinstance(node, (ast.Call, ast.Subscript))
                or isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)) and statement is not None:
            start, end = source.span(node)
            position = (node.lineno, start - source.starts[node.lineno - 1] + 1,
                        node.end_lineno, end - source.starts[node.end_lineno - 1] + 1)
            if position in wanted:
                header = type(statement)(**{key: value for key, value in ast.iter_fields(statement)
                                           if key not in {'body', 'orelse', 'finalbody', 'handlers', 'cases'}})
                nodes[position] = (digest(scopes), digest([ast.dump(header), ast.dump(node)]))
        for child in ast.iter_child_nodes(node):
            visit(child, scopes, statement)

    try:
        visit(ast.parse(text))
    except (SyntaxError, ValueError, RecursionError):
        return [None] * len(diagnostics)
    result = []
    for d in diagnostics:
        identity = nodes.get((d.line, d.column, d.end_line, d.end_column)) if d.code != 'PLV000' else None
        result.append({'path': relative(d.path, root), 'code': d.code,
                       'scope': identity[0], 'anchor': identity[1],
                       'line': d.line, 'column': d.column, 'end_line': d.end_line,
                       'end_column': d.end_column} if identity else None)
    return result


def key(entry: dict) -> tuple:
    return tuple(entry[field] for field in ('path', 'code', 'scope', 'anchor'))


def valid_path(value):
    if not isinstance(value, str):
        return False
    try:
        os.fsencode(value)
    except UnicodeEncodeError:
        return False
    return (bool(value) and '\x00' not in value
            and not PurePosixPath(value).is_absolute() and '..' not in PurePosixPath(value).parts
            and PurePosixPath(value).as_posix() == value and value != '.')


def path_maps(values: list[str]) -> dict[str, str]:
    """Parse exact, simultaneous file renames; never normalize ambiguous input."""
    result = {}
    destinations = set()
    for value in values:
        parts = value.split('=')
        if len(parts) != 2 or not all(valid_path(p) for p in parts):
            raise ValueError('Invalid baseline path map: expected canonical relative OLD=NEW file paths')
        old, new = parts
        if old == new or old in result or new in destinations:
            raise ValueError('Invalid baseline path map: duplicate, colliding, or self-mapped paths')
        result[old] = new
        destinations.add(new)
    if result.keys() & destinations:
        raise ValueError('Invalid baseline path map: chains and cycles are ambiguous')
    return result


def prior_policy(current_policy: dict, mapping: dict[str, str]) -> dict:
    """Recover the saved policy: only explicit file targets may be renamed.

    Loading against this policy still validates the original snapshot and every
    original entry before applying any map. Directory and rule policies stay exact.
    """
    reverse = {new: old for old, new in mapping.items()}
    targets = [{'path': reverse.get(t['path'], t['path']) if t['kind'] == 'file' else t['path'],
                'kind': t['kind']} for t in current_policy['targets']]
    return {**current_policy, 'targets': sorted(targets, key=lambda t: (t['path'], t['kind']))}


def remap(previous: list[dict], mapping: dict[str, str]) -> list[dict]:
    saved_paths = {e['path'] for e in previous}
    if mapping.keys() - saved_paths:
        raise ValueError('Invalid baseline path map: OLD must have saved findings')
    if saved_paths & set(mapping.values()):
        raise ValueError('Invalid baseline path map: destination already has saved findings')
    return [{**e, 'path': mapping.get(e['path'], e['path'])} for e in previous]


def load(text: str, expected_policy: dict) -> list[dict]:
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError('Invalid baseline: duplicate JSON key')
            result[name] = value
        return result

    try:
        data = json.loads(text, object_pairs_hook=unique)
    except (ValueError, RecursionError):
        raise ValueError('Invalid baseline JSON') from None
    if (not isinstance(data, dict) or set(data) != {'version', 'policy', 'entries'}
            or type(data['version']) is not int or data['version'] != 1):
        raise ValueError('Invalid baseline schema or unsupported version')
    # Compare canonical JSON as Python equality considers True == 1.
    if json.dumps(data['policy'], sort_keys=True) != json.dumps(expected_policy, sort_keys=True):
        raise ValueError('Baseline scan scope or rule policy changed; use the saved inputs or explicitly update the baseline')
    entries = data['entries']
    if not isinstance(entries, list):
        raise ValueError('Invalid baseline entries')
    fields = {'path', 'code', 'scope', 'anchor', 'line', 'column', 'end_line', 'end_column'}
    seen = set()
    for e in entries:
        if (not isinstance(e, dict) or set(e) != fields or not valid_path(e['path'])
                or not isinstance(e['code'], str) or e['code'] not in RULES
                or any(not isinstance(e[f], str) or not re.fullmatch('[0-9a-f]{64}', e[f]) for f in ('scope', 'anchor'))
                or any(type(e[f]) is not int or e[f] < 1 for f in ('line', 'column', 'end_line', 'end_column'))
                or (e['end_line'], e['end_column']) <= (e['line'], e['column'])):
            raise ValueError('Invalid baseline diagnostic entry')
        targets = expected_policy['targets']
        if e['code'] not in expected_policy['selected'] or not any(
            e['path'] == t['path'] or (t['kind'] == 'directory' and
                                      (t['path'] == '.' or e['path'].startswith(t['path'] + '/')))
            for t in targets
        ):
            raise ValueError('Baseline entry is outside the saved scope or rule policy')
        record = json.dumps(e, sort_keys=True)
        if record in seen:
            raise ValueError('Invalid baseline: duplicate diagnostic entry')
        seen.add(record)
    return entries


def compare(diagnostics: list[Diagnostic], entries: list[dict | None], previous: list[dict],
            root: Path, *, discovery_failed: bool = False, scanned_paths: set[str] | None = None) -> list[Classified]:
    old = defaultdict(list)
    current = defaultdict(list)
    for entry in previous:
        old[key(entry)].append(entry)
    for i, entry in enumerate(entries):
        if entry:
            current[key(entry)].append(i)
    result = []
    failed_paths = {os.path.abspath(d.path) for d, e in zip(diagnostics, entries) if d.code == 'PLV000' or e is None}
    for d, entry in zip(diagnostics, entries):
        if d.code == 'PLV000':
            result.append(Classified(d, 'error'))
        elif entry is None:
            result.append(Classified(d, 'uncertain', 'Cannot build a stable AST identity'))
        elif len(current[key(entry)]) > 1 or len(old.get(key(entry), ())) > 1:
            result.append(Classified(d, 'uncertain', 'Ambiguous repeated diagnostic in the same scope'))
        else:
            result.append(Classified(d, 'existing' if key(entry) in old else 'new'))
    for identity, group in old.items():
        if identity in current:
            continue
        for e in group:
            path = root / e['path']
            # Display like scanned diagnostics (relative to the working directory), not as an absolute checkout path.
            d = Diagnostic(os.path.relpath(path), e['line'], e['column'], e['end_line'], e['end_column'], e['code'],
                           RULES[e['code']])
            skipped = (scanned_paths is not None and str(path) not in scanned_paths
                       and (path.exists() or any(p.is_symlink() for p in (path, *path.parents))))
            if discovery_failed or str(path) in failed_paths or skipped:
                result.append(Classified(d, 'uncertain', 'Source could not be fully checked; previous finding is unverified'))
            else:
                result.append(Classified(d, 'resolved'))
    return sorted(result, key=lambda r: (os.path.abspath(r.diagnostic.path), r.diagnostic.line, r.diagnostic.column,
                                         r.diagnostic.code, r.status))


def snapshot(entries: list[dict | None], current_policy: dict) -> str:
    records = sorted((e for e in entries if e), key=lambda e: (e['path'], e['line'], e['column'], e['code']))
    return json.dumps({'version': 1, 'policy': current_policy, 'entries': records}, indent=2, ensure_ascii=True) + '\n'
