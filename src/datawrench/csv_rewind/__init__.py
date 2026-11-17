"""Check what a proposed DuckDB CSV schema would do to the original cell text.

A thin, local-only reversibility audit around DuckDB's CSV importer
(``datawrench csv-rewind``). Library use::

    from datawrench.csv_rewind import AuditOptions, audit, load_config
"""

from datawrench import __version__

try:
    from .audit import AuditError, AuditOptions, audit
    from .config import load_config
except ModuleNotFoundError as exc:  # DuckDB (the `duckdb` extra) is not installed
    if (exc.name or "").partition(".")[0] != "duckdb":
        raise
    # `python -m datawrench.csv_rewind` still reaches the dispatcher's install hint.

__all__ = ["AuditError", "AuditOptions", "__version__", "audit", "load_config"]
