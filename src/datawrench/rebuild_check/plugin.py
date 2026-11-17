"""``sqlite-utils rebuild-check``: the same check as a sqlite-utils plugin command.

Registered through the ``sqlite_utils`` entry point and loaded only by
sqlite-utils itself; nothing else in datawrench imports this module. Install
the ``sqlite`` extra (``pip install 'datawrench[sqlite]'``) into the
environment that runs sqlite-utils.
"""
from __future__ import annotations

from pathlib import Path

import click
import sqlite_utils

from .cli import DESCRIPTION, render, run


@click.command(help=DESCRIPTION)
@click.argument("database", type=click.Path(path_type=Path))
@click.argument("migrations", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text", show_default=True)
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=30.0, show_default=True,
              help="Total deadline in seconds, including the backup.")
@click.option("--foreign-keys", type=click.Choice(["on", "off"]), default="on", show_default=True,
              help="Initial migration connection setting. SQL may change it.")
@click.option("--count-rows/--no-count-rows", default=True, show_default=True)
@click.option("--max-violations", type=click.IntRange(min=1), default=100, show_default=True)
@click.option("--policy", type=click.Path(path_type=Path), help="JSON allowlist with exact findings and reasons.")
@click.option("--temp-dir", type=click.Path(path_type=Path, file_okay=False),
              help="Parent directory for the disposable backup.")
@click.option("--connection-plugins", is_flag=True,
              help="Run installed sqlite-utils prepare_connection hooks on the replica.")
def rebuild_check(database, migrations, output_format, timeout, foreign_keys, count_rows,
                  max_violations, policy, temp_dir, connection_plugins):
    report = run(database, migrations, timeout=timeout, foreign_keys=foreign_keys, count_rows=count_rows,
                 max_violations=max_violations, policy=policy, temp_dir=temp_dir,
                 connection_plugins=connection_plugins)
    click.echo(render(report, output_format), nl=False)
    raise click.exceptions.Exit(report["exit_code"])


@sqlite_utils.hookimpl
def register_commands(cli):
    cli.add_command(rebuild_check)
