"""Offline, scoped semantic differences for validated dbt manifest v12 files."""
from __future__ import annotations

import copy
import json

from .exporter import SCHEMA_URL, serialize
from .validation import ManifestError, validate_manifest

KINDS = ('model_added', 'model_removed', 'dependencies_changed',
         'column_added', 'column_removed', 'column_changed',
         'source_added', 'source_removed')


def _resources(manifest: dict) -> dict:
    resources = {}
    for group in ('nodes', 'sources', 'exposures', 'metrics', 'semantic_models', 'saved_queries', 'unit_tests'):
        resources.update(manifest.get(group) or {})
    return resources


def _file(node: dict | None) -> str | None:
    if node is None or node.get('meta', {}).get('implicit'):
        return None
    return node.get('original_file_path')


def _column(column: dict) -> dict:
    # Defaults match the vendored v12 ColumnInfo contract. Preserve extra
    # metadata too; normalize only known optional defaults and set-like tags.
    result = {'description': '', 'meta': {}, 'data_type': None, 'constraints': [],
              'quote': None, 'tags': [], '_extra': {}, 'granularity': None}
    result.update(copy.deepcopy(column))
    result['tags'] = sorted(set(result['tags']))
    config = {'meta': {}, 'tags': [], '_extra': {}}
    config.update(result.get('config', {}))
    config['tags'] = sorted(set(config['tags']))
    result['config'] = config
    return result


def _column_changes(before: dict, after: dict, origin: dict) -> list[dict]:
    changes = []
    old_columns, new_columns = before.get('columns', {}), after.get('columns', {})
    for name in sorted(old_columns.keys() | new_columns.keys()):
        previous = _column(old_columns[name]) if name in old_columns else None
        current = _column(new_columns[name]) if name in new_columns else None
        # JSON equality distinguishes metadata booleans from numbers, unlike
        # Python's dict equality (True == 1).
        if serialize(previous) == serialize(current):
            continue
        kind = 'column_added' if previous is None else 'column_removed' if current is None else 'column_changed'
        changes.append({'kind': kind, **origin, 'column': name, 'before': previous, 'after': current})
    return changes


def _description_changes(before: dict, after: dict, origin: dict) -> list[dict]:
    # Validated v12 model/source descriptions are optional strings, default ''.
    # The enabled resource maps normally exclude disabled nodes; also respect
    # an explicit config.enabled=false on either surviving resource.
    if not all(node.get('config', {}).get('enabled', True) for node in (before, after)):
        return []
    previous, current = before.get('description', ''), after.get('description', '')
    if previous == current:
        return []
    return [{'kind': 'description_changed', **origin, 'before': previous, 'after': current}]


def compare_manifests(before: dict, after: dict, *, include_descriptions: bool = False) -> dict:
    """Compare lifecycle, model edges, columns, and opt-in resource descriptions.

    IDs define identity: renamed IDs are removals/additions. Source paths are
    provenance, not identity. No code, config, manifest metadata or graph-index
    serialization is compared. Inputs are never mutated. Order within a surviving
    node is dependencies, description (if selected), then columns by exact key.
    """
    for side, manifest in (('before', before), ('after', after)):
        try:
            validate_manifest(manifest)
            if manifest['metadata']['dbt_schema_version'] != SCHEMA_URL:
                raise ManifestError('Semantic diff supports dbt manifest v12 inputs only')
        except ManifestError as exc:
            raise ManifestError(f'{side} manifest: {exc}') from None
    old_resources, new_resources = _resources(before), _resources(after)
    old = {uid: n for uid, n in before['nodes'].items() if n['resource_type'] == 'model'}
    new = {uid: n for uid, n in after['nodes'].items() if n['resource_type'] == 'model'}
    changes = []
    for uid in sorted(old.keys() | new.keys()):
        origin = {'node_id': uid, 'before_file': _file(old.get(uid)), 'after_file': _file(new.get(uid))}
        if uid not in old:
            changes.append({'kind': 'model_added', **origin})
            continue
        if uid not in new:
            changes.append({'kind': 'model_removed', **origin})
            continue
        # Declared edges remain authoritative with absent/null optional indexes.
        old_deps = set(old[uid].get('depends_on', {}).get('nodes', []))
        new_deps = set(new[uid].get('depends_on', {}).get('nodes', []))
        if old_deps != new_deps:
            changes.append({
                'kind': 'dependencies_changed', **origin,
                'removed': [{'node_id': dep, 'file': _file(old_resources[dep])} for dep in sorted(old_deps - new_deps)],
                'added': [{'node_id': dep, 'file': _file(new_resources[dep])} for dep in sorted(new_deps - old_deps)],
            })
        if include_descriptions:
            changes.extend(_description_changes(old[uid], new[uid], origin))
        changes.extend(_column_changes(old[uid], new[uid], origin))
    # Source identity is independent of consumers and YAML paths. As with
    # models, lifecycle events do not expand into contained column changes.
    for uid in sorted(before['sources'].keys() | after['sources'].keys()):
        previous, current = before['sources'].get(uid), after['sources'].get(uid)
        origin = {'node_id': uid, 'before_file': _file(previous), 'after_file': _file(current)}
        if previous is None:
            changes.append({'kind': 'source_added', **origin})
            continue
        if current is None:
            changes.append({'kind': 'source_removed', **origin})
            continue
        if include_descriptions:
            changes.extend(_description_changes(previous, current, origin))
        changes.extend(_column_changes(previous, current, origin))
    changes.sort(key=lambda change: change['node_id'])
    summary = {kind: 0 for kind in KINDS}
    if include_descriptions:
        summary['description_changed'] = 0
    for change in changes:
        summary[change['kind']] += 1
    return {'diff_version': 1, 'has_changes': bool(changes), 'summary': summary, 'changes': changes}


def render_diff(report: dict) -> str:
    """Render the exact same change records used by JSON output."""
    if not report['has_changes']:
        return 'No semantic changes.\n'
    lines = [f"Semantic changes: {len(report['changes'])}"]
    for change in report['changes']:
        # Escape untrusted IDs/paths/metadata, including embedded newlines.
        def display(value):
            return json.dumps(value, ensure_ascii=True, sort_keys=True)
        lines.append(f"{change['kind']} {display(change['node_id'])} "
                     f"(before={display(change['before_file'])}; after={display(change['after_file'])})")
        if change['kind'] == 'dependencies_changed':
            for sign, key in (('-', 'removed'), ('+', 'added')):
                for dep in change[key]:
                    lines.append(f"  {sign} {display(dep['node_id'])} ({display(dep['file'])})")
        elif change['kind'] == 'description_changed' or change['kind'].startswith('column_'):
            if change['kind'].startswith('column_'):
                lines.append(f"  column {display(change['column'])}")
            lines.append(f"  before: {display(change['before'])}")
            lines.append(f"  after: {display(change['after'])}")
    return '\n'.join(lines) + '\n'
