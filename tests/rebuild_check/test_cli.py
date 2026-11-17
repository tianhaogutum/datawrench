"""`datawrench rebuild-check` argparse front end and the sqlite-utils plugin wrapper."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("sqlite_utils", reason="rebuild-check needs the 'sqlite' extra")

import datawrench
from datawrench.rebuild_check.cli import main

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "rebuild_check"


def _main(argv, capsys):
    try:
        code = main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_help_uses_datawrench_prog(capsys):
    code, out, _ = _main(["--help"], capsys)
    assert code == 0
    assert out.startswith("usage: datawrench rebuild-check ")
    for option in ("--format", "--timeout", "--foreign-keys", "--count-rows", "--no-count-rows",
                   "--max-violations", "--policy", "--temp-dir", "--connection-plugins"):
        assert option in out


def test_version_is_package_version(capsys):
    code, out, _ = _main(["--version"], capsys)
    assert code == 0 and out == f"{datawrench.__version__}\n"


def test_text_report_for_broken_rebuild(ledger, capsys):
    code, out, _ = _main([str(ledger), str(EXAMPLES / "broken-rebuild.sql"), "--temp-dir", str(ledger.parent)], capsys)
    assert code == 1
    lines = out.splitlines()
    assert lines[0] == "FAILED: 2 finding(s)"
    assert any(line.startswith("ERROR object_missing trigger:account_audit: ") for line in lines)
    assert any(line.startswith("ERROR object_missing index:balance_idx: ") for line in lines)
    assert lines[-1].endswith("definition change(s); use --format json for before/after details.")


def test_json_report_for_good_rebuild(ledger, capsys):
    code, out, _ = _main([str(ledger), str(EXAMPLES / "good-rebuild.sql"), "--format", "json",
                          "--temp-dir", str(ledger.parent), "--no-count-rows"], capsys)
    report = json.loads(out)
    assert code == 0 == report["exit_code"] and report["schema_version"] == 1
    assert report["status"] == "passed"


@pytest.mark.parametrize("args", [["--timeout", "0"], ["--timeout", "nan"], ["--max-violations", "0"],
                                  ["--foreign-keys", "maybe"], ["--format", "yaml"]])
def test_invalid_options_are_usage_errors(ledger, sql_file, args, capsys):
    code, _, err = _main([str(ledger), str(sql_file("SELECT 1;")), *args], capsys)
    assert code == 2 and "usage: datawrench rebuild-check" in err


def test_migration_is_required(ledger, capsys):
    code, _, err = _main([str(ledger)], capsys)
    assert code == 2 and "usage: datawrench rebuild-check" in err


def test_temp_dir_must_not_be_a_file(ledger, sql_file, tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    code, _, err = _main([str(ledger), str(sql_file("SELECT 1;")), "--temp-dir", str(blocker)], capsys)
    assert code == 2 and "--temp-dir" in err


def test_module_entry_point_routes_through_dispatcher(ledger, sql_file):
    result = subprocess.run([sys.executable, "-m", "datawrench", "rebuild-check", str(ledger),
                             str(sql_file("SELECT 1;")), "--format", "json", "--temp-dir", str(ledger.parent)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "passed"


# --------------------------------------------------------------------------- sqlite-utils plugin

def test_plugin_registers_rebuild_check_command():
    click = pytest.importorskip("click")
    from datawrench.rebuild_check import plugin

    group = click.Group()
    plugin.register_commands(group)
    assert "rebuild-check" in group.commands


def test_plugin_command_matches_cli_output(ledger, capsys):
    from click.testing import CliRunner
    from datawrench.rebuild_check.plugin import rebuild_check

    args = [str(ledger), str(EXAMPLES / "broken-rebuild.sql"), "--temp-dir", str(ledger.parent)]
    result = CliRunner().invoke(rebuild_check, args)
    assert result.exit_code == 1
    code, out, _ = _main(args, capsys)
    assert code == 1 and result.output == out


def test_plugin_json_error_contract(ledger, sql_file):
    from click.testing import CliRunner
    from datawrench.rebuild_check.plugin import rebuild_check

    result = CliRunner().invoke(rebuild_check, [str(ledger.parent / "absent.db"), str(sql_file("SELECT 1;")),
                                                "--format", "json", "--temp-dir", str(ledger.parent)])
    assert result.exit_code == 2
    assert json.loads(result.output)["status"] == "error"


def test_datawrench_does_not_import_the_plugin():
    probe = subprocess.run([sys.executable, "-c",
                            "import sys, contextlib, io\nfrom datawrench.cli import main\n"
                            "with contextlib.redirect_stdout(io.StringIO()):\n"
                            "    try:\n        main(['rebuild-check', '--help'])\n    except SystemExit:\n        pass\n"
                            "print('datawrench.rebuild_check.plugin' in sys.modules, 'click' in sys.modules)"],
                           capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.split()[0] == "False", probe.stderr
