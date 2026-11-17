"""``datawrench check``: one read-only pass over a data repository.

``check`` finds dbt projects and manifests, SQL files, Python files that use
Polars, CSV seeds, Parquet folders, dbmate-style DuckDB migrations, TableProof
join contracts, grain contracts, Singer / Airbyte output and catalogs and
singer-replay cassettes by their content, then runs the read-only analyses of
the other datawrench tools on them (contract-diff, sqlmesh-manifest,
lint-cache, dialect-audit, join-witness, grain-contract, polars-vet,
parquet-unify, seedtypes, duckmate, tapcheck, singer-replay). It never writes a
file, never runs a model, a tap or a query, and never opens a network
connection.

Library use::

    from datawrench.check import CheckOptions, discover, run_checks
    discovery = discover(["."])
    findings = run_checks(discovery, options=CheckOptions(dialect=("snowflake", "duckdb")))
"""
from __future__ import annotations

from datawrench import __version__
from .discover import Discovery, DiscoveryError, Source, discover
from .model import SEVERITIES, Finding
from .registry import AREAS, CHECKS, CheckOptions, run_checks, select

__all__ = [
    "AREAS",
    "CHECKS",
    "SEVERITIES",
    "CheckOptions",
    "Discovery",
    "DiscoveryError",
    "Finding",
    "Source",
    "__version__",
    "discover",
    "run_checks",
    "select",
]
