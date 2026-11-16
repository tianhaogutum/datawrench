"""Read-only checks of static dbt seed configuration. No dbt/Jinja execution."""
from __future__ import annotations

from pathlib import Path
import yaml

from .compare import compare
from .csvio import CSVSource, SeedtypesError, discover, validate_delimiter
from .validate import validate_columns, validate_requested_types


class StaticLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise SeedtypesError('YAML aliases are not supported')
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, (str, int, float, bool)) or key in result:
                raise SeedtypesError('YAML keys must be unique scalars')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def scalar_constructor(constructor, conversion_errors):
    def construct(loader, node):
        try:
            return constructor(loader, node)
        except conversion_errors as exc:
            # Pinned SafeConstructor can leak Python conversion/lookup errors.
            # Keep the boundary at the individual built-in scalar constructor;
            # parser, mapping and application errors must not be swallowed.
            raise yaml.constructor.ConstructorError(
                None, None, 'invalid YAML scalar', node.start_mark) from exc
    return construct


for scalar, errors in (
    ('bool', (KeyError,)),
    ('int', (ValueError, IndexError)),
    ('float', (ValueError, IndexError, OverflowError)),
    ('timestamp', (ValueError, AttributeError)),
):
    # KeyError: unsupported bool; IndexError: empty numeric; AttributeError:
    # explicit timestamp with no regex match. Binary errors are already YAMLError.
    StaticLoader.add_constructor(
        f'tag:yaml.org,2002:{scalar}',
        scalar_constructor(getattr(yaml.SafeLoader, f'construct_yaml_{scalar}'), errors))


def load_yaml(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise SeedtypesError(f'{path}: expected a regular YAML file of at most 2 MiB')
    try:
        text = path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError as exc:
        # Decoder messages expose byte values/offsets and omit the YAML origin.
        raise SeedtypesError(f'{path}: expected UTF-8 encoded YAML (optional BOM)') from exc
    if '{{' in text or '{%' in text or '{#' in text:
        raise SeedtypesError(f'{path}: Jinja is outside the static check boundary; nothing was evaluated')
    try:
        value = yaml.load(text, Loader=StaticLoader)
    except SeedtypesError as exc:
        raise SeedtypesError(f'{path}: {exc}') from exc
    except (yaml.YAMLError, RecursionError) as exc:
        # PyYAML messages and formatted marks may contain source text, tags or anchors.
        # Only the numeric problem position is safe to include in a diagnostic.
        mark = getattr(exc, 'problem_mark', None)
        location = f' at line {mark.line + 1}, column {mark.column + 1}' if mark is not None else ''
        raise SeedtypesError(f'{path}: invalid or excessively nested YAML{location}') from exc
    if not isinstance(value, dict):
        raise SeedtypesError(f'{path}: expected a YAML mapping')
    return value


def column_types(node, *, source, config_path):
    if not isinstance(node, dict):
        raise SeedtypesError(f'{source}: {config_path}: dbt seed configuration must be a mapping')
    # Preserve selection of the + spelling without validating a shadowed plain setting.
    key = '+column_types' if '+column_types' in node else 'column_types'
    value = node.get(key, {})
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                                           for k, v in value.items()):
        raise SeedtypesError(f'{source}: {config_path}[{key!r}]: '
                             'column_types must map exact column names to nonempty type strings')
    return value


def seed_delimiter(node, inherited=',', *, source, config_path):
    # An absent setting inherits; explicit null/unsupported values are errors.
    # Validate both spellings if present, with the + form taking precedence
    # as it does for column_types. YAML carries the literal character.
    for key in ('delimiter', '+delimiter'):
        if key in node:
            try:
                inherited = validate_delimiter(node[key])
            except SeedtypesError as exc:
                raise SeedtypesError(f'{source}: {config_path}[{key!r}]: {exc}') from exc
    return inherited


def check_project(project_dir, *, show_examples=False, validate_column_types=False, null_tokens=None):
    if null_tokens and not validate_column_types:
        raise SeedtypesError('check null tokens require --validate-column-types')
    null_tokens = list(dict.fromkeys(null_tokens or []))
    if len(null_tokens) > 64 or any(len(t) > 1024 or '\x00' in t for t in null_tokens):
        raise SeedtypesError('at most 64 null tokens, each at most 1024 characters without NUL')

    def type_settings(node, *, source, config_path):
        types = column_types(node, source=source, config_path=config_path)
        key = '+column_types' if '+column_types' in node else 'column_types'
        origin = {'source': str(source), 'config_path': f'{config_path}[{key!r}]'}
        if validate_column_types:
            try:
                validate_requested_types(types, allow_empty=True)
            except SeedtypesError as exc:
                raise SeedtypesError(f'{source}: {origin["config_path"]}: {exc}') from exc
        return types, {col: origin for col in types}

    root = Path(project_dir).resolve()
    project_path = root / 'dbt_project.yml'
    project = load_yaml(project_path)
    name = project.get('name')
    if not isinstance(name, str) or not name:
        raise SeedtypesError(f'{project_path}: a static project name is required')
    seed_paths = project.get('seed-paths', ['seeds'])
    if not isinstance(seed_paths, list) or any(not isinstance(p, str) for p in seed_paths):
        raise SeedtypesError(f'{project_path}: seed-paths must be a list of local relative directories')
    roots = []
    for index, item in enumerate(seed_paths):
        raw = Path(item)
        candidate = (root / raw).resolve()
        if raw.is_absolute() or '..' in raw.parts or not candidate.is_relative_to(root):
            raise SeedtypesError(f'{project_path}: seed-paths[{index}]: escapes the project directory')
        if any((root / Path(*raw.parts[:i])).is_symlink() for i in range(1, len(raw.parts) + 1)):
            raise SeedtypesError(f'{project_path}: seed-paths[{index}]: symlinks are unsupported')
        if candidate.exists() and not candidate.is_dir():
            raise SeedtypesError(f'{project_path}: seed-paths[{index}]: expected a directory')
        if candidate.exists():
            if any(candidate.is_relative_to(p) or p.is_relative_to(candidate) for p in roots):
                raise SeedtypesError('overlapping or duplicate seed-paths are unsupported')
            roots.append(candidate)
    files = sorted({path for directory in roots for path in directory.rglob('*') if path.suffix.lower() == '.csv'})
    files = discover([str(p) for p in files]) if files else []
    files_by_name = {}
    for path in files:
        files_by_name.setdefault(path.stem, []).append(path)
    conflicts = [f'{name!r}: ' + ', '.join(repr(str(p)) for p in paths)
                 for name, paths in sorted(files_by_name.items()) if len(paths) > 1]
    if conflicts:
        raise SeedtypesError('duplicate dbt seed names across seed paths are unsupported: '
                             + '; '.join(conflicts))
    properties = {}
    documents = []
    for directory in roots:
        for path in sorted(directory.rglob('*')):
            if path.suffix.lower() not in ('.yml', '.yaml'):
                continue
            if not path.resolve().is_relative_to(root):
                raise SeedtypesError(f'{path}: YAML path escapes project')
            documents.append((path, load_yaml(path)))
    # Load complete YAML inputs before optional type grammar validation or casts.
    for path, doc in documents:
        seeds = doc.get('seeds', [])
        if not isinstance(seeds, list):
            raise SeedtypesError(f'{path}: seeds must be a list')
        for index, seed in enumerate(seeds):
            if not isinstance(seed, dict) or not isinstance(seed.get('name'), str):
                raise SeedtypesError(f'{path}: seed properties need a name')
            seed_name = seed['name']
            if seed_name in properties:
                raise SeedtypesError(f'{path}: duplicate properties for seed {seed_name}')
            config = seed.get('config', {})
            config_path = f'seed {seed_name!r}, seeds[{index}].config'
            types, origins = type_settings(config, source=path, config_path=config_path)
            properties[seed_name] = (types, origins,
                                    seed_delimiter(config, None, source=path, config_path=config_path))
    seeds_config = project.get('seeds', {})
    root_types, root_origins = type_settings(seeds_config, source=project_path, config_path='seeds')
    default_delimiter = seed_delimiter(seeds_config, source=project_path, config_path='seeds')
    issues, results, failures, plans = [], [], [], []
    for path in files:
        seed_root = max((p for p in roots if path.is_relative_to(p)), key=lambda p: len(p.parts))
        explicit, origins = dict(root_types), dict(root_origins)
        node = seeds_config.get(name, {})
        config_path = f'seeds[{name!r}]'
        types, sources = type_settings(node, source=project_path, config_path=config_path)
        explicit.update(types)
        origins.update(sources)
        delimiter = seed_delimiter(node, default_delimiter, source=project_path, config_path=config_path)
        relative = path.relative_to(seed_root).with_suffix('')
        for component in relative.parts:
            node = node.get(component, {})
            config_path += f'[{component!r}]'
            types, sources = type_settings(node, source=project_path, config_path=config_path)
            explicit.update(types)
            origins.update(sources)
            delimiter = seed_delimiter(node, delimiter, source=project_path, config_path=config_path)
        property_types, property_origins, property_delimiter = properties.get(path.stem, ({}, {}, None))
        explicit.update(property_types)
        origins.update(property_origins)
        if property_delimiter is not None:
            delimiter = property_delimiter
        plans.append((path, explicit, origins, delimiter))
    # Resolve and validate every encountered config before processing any seed.
    for path, explicit, origins, delimiter in plans:
        source = CSVSource(path, delimiter=delimiter) if validate_column_types else None
        comparison = compare(path, null_tokens, show_examples=show_examples, delimiter=delimiter)
        columns = [c['name'] for c in comparison['inference']['columns']]
        unknown = set(explicit) - set(columns)
        if unknown:
            raise SeedtypesError(f'{path}: column_types names not in CSV header: {", ".join(sorted(unknown))}')
        for engine, report in comparison['engines'].items():
            if report['status'] == 'ERROR':
                # A default parser refusing valid input is also an unpinned loading risk.
                for col in columns:
                    if col not in explicit:
                        issues.append({'path': str(path), 'column': col, 'engine': engine,
                                       'reason': 'default reader failed; pin a type', 'error': report['error']})
            for col, details in report.get('columns', {}).items():
                if details['status'] == 'LOSSY' and col not in explicit:
                    issues.append({'path': str(path), 'column': col, 'engine': engine,
                                   'reason': 'default inference alters text',
                                   'line': details['first_mismatch_line'], 'examples': details['examples']})
        result = {'path': str(path), 'explicit_columns': sorted(explicit), 'comparison': comparison}
        if validate_column_types:
            validation = validate_columns(path, explicit, null_tokens,
                                          show_examples=show_examples, delimiter=delimiter)
            source.unchanged()
            for col in validation['columns']:
                col.update(origins[col['name']])
                if not col['valid']:
                    failures.append({'path': str(path), 'column': col['name'],
                                     'requested_type': col['requested_type'], **origins[col['name']],
                                     'rejection': col['rejection']})
            result['column_type_validation'] = validation
        results.append(result)
    result = {'project': str(root), 'seeds_checked': len(results), 'issues': issues, 'seeds': results}
    if validate_column_types:
        result['column_type_failures'] = failures
    return result
