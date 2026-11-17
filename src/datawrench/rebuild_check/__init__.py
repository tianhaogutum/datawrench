"""Rehearse SQLite migrations on a disposable backup and check that table rebuilds dropped nothing.

Library use::

    from datawrench.rebuild_check import check_migration
    report = check_migration("copy.db", ["001.sql", "002.sql"], timeout=60)
"""
from datawrench import __version__

from .core import check_migration

__all__ = ["__version__", "check_migration"]
