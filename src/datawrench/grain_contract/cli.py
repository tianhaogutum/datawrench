"""``datawrench grain-contract``: CLI around the SQLGlot compiler and optional DuckDB executor."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__
from .compiler import ContractError, compile_contract
from .dbt import from_manifest, read_manifest
from .runner import run_bundle


def _json(path: Path):
    def object_pairs(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ContractError(f"{path}: duplicate JSON key {key!r}")
            obj[key] = value
        return obj

    def constant(value):
        raise ContractError(f"{path}: {value} is not a valid JSON number")

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=object_pairs,
        parse_constant=constant,
    )


def _test_files(bundle, dbt_model, dbt_package, provenance):
    prefix = "__".join(x for x in (dbt_package, dbt_model) if x)
    if prefix:
        prefix += "__"
    files = {}
    for check in bundle.checks:
        header = f"-- Generated {check.id}; zero rows means pass.\n"
        if dbt_model:
            ref_args = (
                f"'{dbt_package}', '{dbt_model}'" if dbt_package else f"'{dbt_model}'"
            )
            header += "-- depends_on: {{ ref(" + ref_args + ") }}\n"
        checksum = provenance.get("model_checksum") if provenance else None
        if isinstance(checksum, dict) and isinstance(checksum.get("checksum"), str):
            # graph is populated at execute time, not during dbt's parse phase.
            # JSON string literals are valid Jinja literals and safely quote names.
            guards = [
                {"unique_id": provenance["unique_id"], "checksum": checksum}
            ] + provenance.get("ephemeral_checksums", [])
            guards = [
                {"id": g["unique_id"], "checksum": g["checksum"]["checksum"]}
                for g in guards
            ]
            message = json.dumps(
                f"Stale SQL grain contract for {provenance['unique_id']}; run dbt compile and datawrench grain-contract from-dbt again."
            )
            header += (
                "{% if execute and flags.WHICH in ['test', 'build'] %}\n"
                "{% for gc_guard in " + json.dumps(guards) + " %}\n"
                "{% set gc_source = graph.nodes.get(gc_guard.id) %}\n"
                "{% if gc_source is none or gc_source.get('checksum', {}).get('checksum') != gc_guard.checksum %}\n"
                "{{ exceptions.raise_compiler_error(" + message + ") }}\n"
                "{% endif %}\n{% endfor %}\n{% endif %}\n"
            )
        # dbt wraps singular SQL tests in SELECT COUNT(*) FROM (...).
        files[f"{prefix}{check.id}.sql"] = (
            header + check.sql.rstrip().removesuffix(";") + "\n"
        )
    return files


def _current(bundle, target, dbt_model, dbt_package, provenance):
    if target.is_symlink() or not target.is_dir():
        raise ContractError(
            f"generated output does not exist or is a symlink: {target}"
        )
    old = _json(target / "manifest.json")
    if not isinstance(old, dict) or old.get("generator") != "sql-grain-contract":
        raise ContractError(f"unrecognized generated output: {target}")
    fields = ("source_sha256", "contract_sha256", "sqlglot_version")
    if (
        any(old.get(field) != getattr(bundle, field) for field in fields)
        or old.get("provenance") != provenance
    ):
        return False
    files = _test_files(bundle, dbt_model, dbt_package, provenance)
    if set(files) | {"manifest.json"} != {p.name for p in target.iterdir()}:
        return False
    return all(
        not (target / name).is_symlink()
        and (target / name).is_file()
        and (target / name).read_text(encoding="utf-8") == body
        for name, body in files.items()
    )


def _export(
    bundle,
    target: Path,
    dbt_model: str | None,
    *,
    dbt_package: str | None = None,
    provenance: dict | None = None,
    replace: bool = False,
):
    """Stage complete output; replace only directories owned by this generator."""
    existed = target.exists() or target.is_symlink()
    if existed and not replace:
        raise ContractError(
            f"output path already exists: {target}; choose a new directory"
        )
    if existed:
        if target.is_symlink() or not target.is_dir():
            raise ContractError(
                "--replace requires a generated directory, not a file or symlink"
            )
        try:
            old = _json(target / "manifest.json")
        except (OSError, ValueError) as exc:
            raise ContractError(
                "--replace requires an existing generated manifest.json"
            ) from exc
        files = old.get("output_files") if isinstance(old, dict) else None
        if (
            not isinstance(old, dict)
            or old.get("generator") != "sql-grain-contract"
            or not isinstance(files, list)
            or any(not isinstance(f, str) or Path(f).name != f for f in files)
            or set(files) | {"manifest.json"} != {p.name for p in target.iterdir()}
            or any(p.is_symlink() or not p.is_file() for p in target.iterdir())
        ):
            raise ContractError(
                "--replace refuses a directory containing untracked files or an unrecognized manifest"
            )
    if dbt_model and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dbt_model):
        raise ContractError("--dbt-model must be a simple dbt model name")
    if dbt_package and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dbt_package):
        raise ContractError("dbt package must be a simple package name")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".grain-contract-", dir=target.parent))
    backup = None
    committed = False
    try:
        manifest = bundle.to_dict()
        manifest["generator"] = "sql-grain-contract"
        manifest["dbt_model"] = dbt_model
        if provenance:
            manifest["provenance"] = provenance
        files = _test_files(bundle, dbt_model, dbt_package, provenance)
        manifest["output_files"] = list(files)
        for filename, body in files.items():
            (staging / filename).write_text(body, encoding="utf-8")
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        if existed:
            backup = Path(
                tempfile.mkdtemp(prefix=".grain-contract-backup-", dir=target.parent)
            )
            backup.rmdir()
            target.rename(backup)
        try:
            staging.rename(target)
            committed = True
        except OSError:
            if backup:
                try:
                    backup.rename(target)
                    backup = None
                except OSError as exc:
                    raise ContractError(
                        f"export failed; previous output remains safe in {backup}"
                    ) from exc
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if committed and backup and backup.exists():
            shutil.rmtree(backup)


def build_parser():
    p = argparse.ArgumentParser(
        prog="datawrench grain-contract",
        description="Compile explicit SQL joins into persistent grain checks.",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("compile", "check"):
        cmd = sub.add_parser(
            name,
            help="emit SQL tests"
            if name == "compile"
            else "compile and run against a local DuckDB",
        )
        cmd.add_argument(
            "sql", type=Path, help="one SELECT, using already compiled relation names"
        )
        cmd.add_argument("contract", type=Path, help="version-1 JSON declarations")
        if name == "compile":
            cmd.add_argument(
                "--out",
                type=Path,
                help="new directory for manifest + individual singular SQL tests; otherwise stdout JSON",
            )
            cmd.add_argument(
                "--dbt-model", help="add a dbt depends_on ref to every emitted test"
            )
            cmd.add_argument(
                "--replace",
                action="store_true",
                help="replace a previously generated directory (refuses unrelated files)",
            )
        else:
            cmd.add_argument("--database", type=Path, required=True)
            cmd.add_argument(
                "--timeout",
                type=float,
                default=30,
                help="per-check timeout in seconds (default 30)",
            )
            cmd.add_argument("--sample-limit", type=int, default=10)
            cmd.add_argument(
                "--fail-fast",
                action="store_true",
                help="stop after the first data violation to avoid unnecessary join scans",
            )
            cmd.add_argument(
                "--json", action="store_true", help="machine-readable results on stdout"
            )
    dbt = sub.add_parser(
        "from-dbt", help="generate singular tests from dbt's compiled manifest"
    )
    dbt.add_argument("--manifest", type=Path, required=True)
    dbt.add_argument(
        "--model", required=True, help="unambiguous model name or dbt unique_id"
    )
    dbt.add_argument("--contract", type=Path, required=True)
    dbt.add_argument(
        "--out",
        type=Path,
        required=True,
        help="new directory, usually under dbt tests/",
    )
    dbt.add_argument("--replace", action="store_true")
    dbt.add_argument(
        "--check",
        action="store_true",
        help="read-only CI freshness check; exit 1 when generated tests are stale",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "from-dbt":
            declaration = _json(args.contract)
            dbt_input = from_manifest(
                read_manifest(args.manifest, args.model, declaration),
                args.model,
                declaration,
            )
            bundle = compile_contract(dbt_input.sql, dbt_input.contract)
            if args.check:
                if args.replace:
                    raise ContractError("--check and --replace cannot be combined")
                current = _current(
                    bundle,
                    args.out,
                    dbt_input.model,
                    dbt_input.package,
                    dbt_input.provenance,
                )
                print(
                    "Current: generated contracts match the compiled model"
                    if current
                    else "Stale: regenerate with datawrench grain-contract from-dbt --replace"
                )
                return 0 if current else 1
            _export(
                bundle,
                args.out,
                dbt_input.model,
                dbt_package=dbt_input.package,
                provenance=dbt_input.provenance,
                replace=args.replace,
            )
            print(f"Wrote {len(bundle.checks)} dbt singular tests to {args.out}")
            return 0
        bundle = compile_contract(
            args.sql.read_text(encoding="utf-8"), _json(args.contract)
        )
        if args.command == "compile":
            if args.out:
                _export(bundle, args.out, args.dbt_model, replace=args.replace)
                print(f"Wrote {len(bundle.checks)} SQL checks to {args.out}")
            else:
                if args.dbt_model:
                    raise ContractError("--dbt-model requires --out")
                print(json.dumps(bundle.to_dict(), indent=2))
            return 0
        results = run_bundle(
            bundle,
            args.database,
            timeout=args.timeout,
            sample_limit=args.sample_limit,
            fail_fast=args.fail_fast,
        )
        status = (
            "error"
            if any(r.status == "error" for r in results)
            else "fail"
            if any(r.status == "fail" for r in results)
            else "pass"
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "status": status,
                        "total_checks": len(bundle.checks),
                        "executed_checks": len(results),
                        "results": [r.to_dict() for r in results],
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            for result in results:
                context = result.scope + (f" / {result.join}" if result.join else "")
                print(
                    f"{result.status.upper():5} {result.id} [{context}] {result.description}"
                )
                if result.sample:
                    if result.key_columns:
                        print(
                            "      keys: "
                            + ", ".join(
                                f"k{i}={key}"
                                for i, key in enumerate(result.key_columns)
                            )
                        )
                    print(f"      {', '.join(result.columns)}: {result.sample!r}")
                if result.error:
                    print(result.error, file=sys.stderr)
            print(
                f"{status.upper()}: {len(results)}/{len(bundle.checks)} checks executed"
            )
        return {"pass": 0, "fail": 1, "error": 2}[status]
    except (ContractError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"datawrench grain-contract: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
