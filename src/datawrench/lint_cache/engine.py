"""Per-file orchestration; SQLFluff owns discovery, rendering, rules and records."""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

from sqlfluff.core import FluffConfig, Linter
from sqlfluff.core.config import clear_config_caches
from sqlfluff.core.dialects import dialect_readout
from sqlfluff.core.errors import SQLFluffSkipFile
from sqlfluff.core.linter.discovery import paths_from_path
from sqlfluff.core.linter.linted_dir import LintedDir

from .cache import Cache
from .fingerprint import (Fingerprinter, InputError, config_dump, dependency_stamp,
                          digest, engine_stamp)


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


@dataclass
class Stats:
    files: int = 0
    hits: int = 0
    misses: int = 0
    bypassed: int = 0
    skipped: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    records: list[dict]
    stats: Stats
    explanations: list[dict]

    @property
    def exit_code(self) -> int:
        return int(any(not v["warning"] for r in self.records for v in r["violations"]))


@dataclass
class Prepared:
    before: str
    raw: str
    config: FluffConfig
    encoding: str
    key: str
    components: dict[str, str]
    dependency_components: dict[str, str]
    bypass_reasons: list[str]


def _unknown_dialect(exc: KeyError) -> InputError:
    """SQLFluff signals a bad dialect name with a bare KeyError; report it as input error."""
    names = ", ".join(sorted(d.label for d in dialect_readout()))
    return InputError("Unknown dialect (from --dialect or a .sqlfluff `dialect` setting); "
                      f"available dialects: {names}")


def lint(paths: Sequence[str] = (), *, dialect: str | None = None,
         config_path: str | None = None, cache_dir: str | Path = ".sqlfluff_cache",
         no_cache: bool = False, dbt_project_hash: bool = False,
         trust_template_code: bool = False,
         explain_dependencies: bool = False,
         warn: Callable[[str], None] = lambda msg: None) -> Result:
    """Lint stable local inputs. Fatal/unsupported inputs raise, never become a hit."""
    clear_config_caches()  # also permits multiple calls after editing config in-process
    overrides = {"dialect": dialect} if dialect else None
    try:
        root = FluffConfig.from_path(os.getcwd(), extra_config_path=config_path,
                                     overrides=overrides, require_dialect=False)
        linter = Linter(config=root)
    except KeyError as exc:
        if exc.args != ("Unknown dialect",):
            raise
        raise _unknown_dialect(exc) from None
    names: list[str] = []
    extensions = root.get("sql_file_exts", default=".sql").lower().split(",")
    for path in paths or (os.getcwd(),):
        names.extend(paths_from_path(str(path), working_path=os.getcwd(),
                                     target_file_exts=extensions))
    # Overlapping inputs should not lint the same source twice.
    names = list(dict.fromkeys(names))
    if any(not Path(name).is_file() for name in names):
        raise InputError("All discovered SQL inputs must be regular files")
    cache = Cache(Path(cache_dir), warn)
    fp = Fingerprinter(cache.directory)
    stamp = engine_stamp()
    dependencies = dependency_stamp(stamp)
    stats = Stats()
    prepared: dict[str, Prepared] = {}
    explanations = []
    notices: set[str] = set()
    actual_templater = linter.templater.name
    for name in names:
        before = file_hash(Path(name))
        try:
            raw, cfg, encoding = linter.load_raw_file_and_config(name, root)
        except KeyError as exc:  # nested .sqlfluff may name an unknown dialect
            if exc.args != ("Unknown dialect",):
                raise
            raise _unknown_dialect(exc) from None
        except SQLFluffSkipFile as exc:
            warn(str(exc))
            stats.skipped += 1
            explanations.append({"filepath": name, "action": "skipped",
                                 "reasons": ["large_file_skipped"],
                                 **({"dependency_changes": []} if explain_dependencies else {})})
            continue
        if file_hash(Path(name)) != before:
            raise InputError(f"SQL input changed while reading: {name}")
        template = fp.template_inputs(
            cfg, actual_templater, trust_code=trust_template_code,
            dbt_project_hash=dbt_project_hash)
        can_cache, notice = template.cacheable, template.notice
        if actual_templater == "jinja" and fp.nondeterministic_jinja(cfg, raw, linter.templater):
            can_cache = False
            notice = "Jinja random input: cache bypassed to preserve result equivalence"
        if notice and notice not in notices:
            warn(notice)
            notices.add(notice)
        settings = config_dump(cfg)
        components = {"content": before, "settings": digest(settings),
                      "sqlfluff_version": digest(stamp["sqlfluff"]),
                      "dependencies": digest({"engine": dependencies,
                                              "templater": template.dependencies})}
        dependency_components = {"template_files": template.template_files,
                                 "python_plugins": digest(dependencies),
                                 "environment": template.environment}
        key = digest({"file": before,
                      "path": str(Path(name).absolute()), "cwd": os.getcwd(),
                      "config": settings, "engine": stamp,
                      "templater": template.fingerprint})
        bypass_reasons = (["cache_disabled"] if no_cache else [])
        if not can_cache:
            bypass_reasons.append("template_cache_bypassed")
        prepared[name] = Prepared(before, raw, cfg, encoding, key, components,
                                  dependency_components, bypass_reasons)
    if any(not item.bypass_reasons for item in prepared.values()):
        cache.prepare(create=True)
    records = []
    # Important for templaters such as dbt which control model sequencing.
    for name in linter.templater.sequence_files(list(prepared), config=root, formatter=None):
        if name not in prepared:
            raise InputError(f"Templater returned an undeclared source: {name}")
        item = prepared[name]
        before, raw, cfg, encoding, key = (item.before, item.raw, item.config,
                                           item.encoding, item.key)
        use_cache = not item.bypass_reasons
        stats.files += 1
        record, lookup_reason = cache.lookup(key, Path(name)) if use_cache else (None, "")
        dependency_changes = []
        if record is not None:
            if file_hash(Path(name)) != before:
                raise InputError(f"SQL input changed during cache lookup: {name}")
            stats.hits += 1
            action, reasons = "replayed", ["cache_hit"]
            cache.remember(key, Path(name), item.components, item.dependency_components)
        else:
            stats.misses += 1
            if not use_cache:
                stats.bypassed += 1
            action = "linted"
            if item.bypass_reasons:
                reasons = item.bypass_reasons
            else:
                reasons, dependency_changes = cache.miss_explanation(
                    Path(name), item.components, lookup_reason,
                    item.dependency_components if explain_dependencies else None)
            rendered = linter.render_string(raw, name, cfg, encoding)
            linted = linter.lint_rendered(rendered, linter.get_rulepack(config=cfg))
            directory = LintedDir("")
            directory.add(linted)
            record = dict(directory.as_records()[0])
            del record["filepath"]
            if file_hash(Path(name)) != before:
                raise InputError(f"SQL input changed while linting: {name}")
            if use_cache and cache.put(key, Path(name), record):
                cache.remember(key, Path(name), item.components, item.dependency_components)
        explanations.append({"filepath": name, "action": action, "reasons": reasons,
                             **({"dependency_changes": dependency_changes}
                                if explain_dependencies else {})})
        records.append({"filepath": name, **record})
    if stats.files != len(prepared):
        raise InputError("Templater omitted sources; no complete lint result is available")
    return Result(sorted(records, key=lambda r: r["filepath"]), stats,
                  sorted(explanations, key=lambda r: r["filepath"]))
