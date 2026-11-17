import json
import errno
import os
import stat
import subprocess
import sys

import duckdb
import pytest

from datawrench.join_witness import analyze
from datawrench.join_witness.cli import main
from datawrench.join_witness import cli
from .test_engine import example_spec


def arguments():
    spec = example_spec()
    return ["--left", str(spec.left), "--right", str(spec.right), "--key", "customer_id", "--expect", "many-to-one"]


def test_generated_assertions_actually_fail_duplicate_keys(db):
    report = analyze(example_spec())
    for assertion in report["assertions"]:
        if assertion["name"] == "right_unique":
            with pytest.raises(duckdb.InvalidInputException, match="must be unique"):
                db.execute(assertion["sql"])
        else:
            assert db.execute(assertion["sql"]).fetchone() == (True,)


def test_cli_machine_report_and_sql_export(tmp_path):
    output, sql = tmp_path / "report.json", tmp_path / "assert.sql"
    assert main(arguments() + ["--left-measure", "amount", "--format", "json", "--output", str(output), "--assertions-output", str(sql)]) == 1
    report = json.loads(output.read_text())
    assert report["summary"]["predicted_join_rows"] == 6
    assert "error(" in sql.read_text()


def test_output_cannot_overwrite_input(capsys):
    spec = example_spec()
    before = spec.left.read_bytes()
    assert main(arguments() + ["--output", str(spec.left)]) == 2
    assert "overwrite an input" in capsys.readouterr().err
    assert spec.left.read_bytes() == before


def test_invalid_cli_does_not_leave_partial_report(tmp_path, capsys):
    output = tmp_path / "report.json"
    assert main(arguments() + ["--left-measure", "missing", "--output", str(output)]) == 2
    assert not output.exists()
    assert "Missing measure" in capsys.readouterr().err


def test_bad_report_destination_does_not_publish_sql(tmp_path, capsys):
    output = tmp_path / "missing-parent" / "report.json"
    sql = tmp_path / "assertions.sql"
    assert main(arguments() + ["--output", str(output), "--assertions-output", str(sql)]) == 2
    assert not sql.exists()


def test_broken_pipe_is_a_clean_cli_exit(monkeypatch):
    class ClosedPipe:
        def write(self, content):
            raise BrokenPipeError()

    monkeypatch.setattr(cli.sys, "stdout", ClosedPipe())
    assert main(arguments()) == 0


def test_staging_error_preserves_existing_outputs_and_cleans_temporary_files(tmp_path, monkeypatch, capsys):
    output, sql = tmp_path / "report.json", tmp_path / "assert.sql"
    output.write_text("old report")
    sql.write_text("old SQL")
    original = cli.tempfile.NamedTemporaryFile
    calls = 0

    def fail_second_stage(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated full disk")
        return original(*args, **kwargs)

    monkeypatch.setattr(cli.tempfile, "NamedTemporaryFile", fail_second_stage)
    assert main(arguments() + ["--output", str(output), "--assertions-output", str(sql)]) == 2
    assert output.read_text() == "old report"
    assert sql.read_text() == "old SQL"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["assert.sql", "report.json"]
    assert "full disk" in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipe")
def test_named_pipe_input_is_rejected_without_waiting_for_a_writer(tmp_path):
    source = tmp_path / "pipe.csv"
    os.mkfifo(source)
    args = arguments()
    args[1] = str(source)
    try:
        run = subprocess.run([sys.executable, "-m", "datawrench", "join-witness", *args], capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail("a local table path must not block forever on a FIFO")
    assert run.returncode == 2
    assert "regular file" in run.stderr


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipe")
def test_output_does_not_replace_a_named_pipe(tmp_path, capsys):
    output = tmp_path / "pipe.json"
    os.mkfifo(output)
    assert main(arguments() + ["--output", str(output)]) == 2
    assert "regular file" in capsys.readouterr().err
    assert stat.S_ISFIFO(output.stat().st_mode)


def test_symlink_loop_is_a_usage_error_and_preserves_existing_report(tmp_path, capsys):
    source, output = tmp_path / "loop.csv", tmp_path / "report.json"
    source.symlink_to(source.name)
    output.write_text("old report")
    args = arguments()
    args[1] = str(source)
    assert main(args + ["--output", str(output)]) == 2
    message = capsys.readouterr().err
    assert "resolve" in message or "symbolic" in message
    assert output.read_text() == "old report"


@pytest.mark.skipif(os.name != "posix", reason="POSIX undecodable filename")
def test_non_utf8_input_path_has_an_explicit_error_before_output(tmp_path, capsys):
    raw_path = os.fsencode(tmp_path) + b"/invalid-\xff.csv"
    try:
        descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        if exc.errno == errno.EILSEQ:
            pytest.skip("the host filesystem itself rejects non-UTF-8 filenames")
        raise
    try:
        os.write(descriptor, b"customer_id,amount\nC001,1.25\n")
    finally:
        os.close(descriptor)
    args = arguments()
    args[1] = os.fsdecode(raw_path)
    output = tmp_path / "report.json"
    assert main(args + ["--format", "json", "--output", str(output)]) == 2
    assert "UTF-8" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_input_aliases_cannot_be_overwritten(tmp_path, capsys, kind):
    source = example_spec().left
    before = source.read_bytes()
    output = tmp_path / "alias.csv"
    if kind == "symlink":
        output.symlink_to(source)
    else:
        os.link(source, output)
    assert main(arguments() + ["--output", str(output)]) == 2
    assert "overwrite an input" in capsys.readouterr().err
    assert source.read_bytes() == output.read_bytes() == before


def test_real_cli_closed_pipe_exits_cleanly_without_interpreter_traceback():
    process = subprocess.Popen([sys.executable, "-m", "datawrench", "join-witness", "--demo", "--top", "0"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.stdout.close()
    process.stdout = None
    _, error = process.communicate(timeout=5)
    assert process.returncode == 0, error.decode()
    assert not error


@pytest.mark.skipif(not hasattr(os, "pathconf"), reason="POSIX filename limit query")
def test_maximum_length_valid_output_filename_is_supported(tmp_path):
    maximum = os.pathconf(tmp_path, "PC_NAME_MAX")
    output = tmp_path / ("r" * (maximum - 5) + ".json")
    assert main(arguments() + ["--format", "json", "--output", str(output)]) == 1
    assert json.loads(output.read_text())["summary"]["predicted_join_rows"] == 6
