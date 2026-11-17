from __future__ import annotations

import csv
import errno
import io
import os
import random
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from datawrench.key_audit.audit import Audit
from datawrench.key_audit.cli import main
from datawrench.key_audit.csvio import AuditError, CSVOptions, new_atomic_file, snapshot_source
from datawrench.key_audit.rules import transform_key

ROOT = Path(__file__).resolve().parents[2]


class FailureTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / ".tmp" / "key-audit"
        work.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.input = self.directory / "input.csv"
        self.input.write_text("id,note\nx ,payload\n")

    def test_disk_full_during_fsync_does_not_publish_or_leave_temp(self):
        output = self.directory / "output.csv"
        with (
            self.assertRaises(OSError),
            patch("datawrench.key_audit.csvio.os.fsync", side_effect=OSError(errno.ENOSPC, "disk full")),
        ):
            with new_atomic_file(output) as (target, _):
                target.write("complete-looking data")
        self.assertFalse(output.exists())
        self.assertFalse(list(self.directory.glob(".keyaudit-*")))

    def test_output_created_by_another_writer_is_not_overwritten(self):
        output = self.directory / "output.csv"

        def race(temporary, destination):
            destination.write_text("other writer's data")
            raise FileExistsError(errno.EEXIST, "already exists")

        with self.assertRaises(AuditError), patch("datawrench.key_audit.csvio.os.link", side_effect=race):
            with new_atomic_file(output) as (target, _):
                target.write("our data")
        self.assertEqual(output.read_text(), "other writer's data")
        self.assertFalse(list(self.directory.glob(".keyaudit-*")))

    def test_writer_failure_after_header_removes_partial_csv(self):
        class BrokenWriter:
            def __init__(self, target):
                self.target = target
                self.rows = 0

            def writerow(self, row):
                self.rows += 1
                if self.rows > 1:
                    raise csv.Error("simulated writer failure")
                self.target.write("header\r\n")

        output = self.directory / "output.csv"
        with Audit(self.input, ["id"], ["strip"], work_dir=self.directory) as a:
            with (
                self.assertRaises(csv.Error),
                patch(
                    "datawrench.key_audit.audit.csv.writer",
                    side_effect=lambda target, **kwargs: BrokenWriter(target),
                ),
            ):
                a.export(output)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.directory.glob(".keyaudit-*")))
        self.assertFalse(list(self.directory.glob("keyaudit-*")))

    def test_interrupt_during_report_write_cleans_both_temporary_layers(self):
        output = self.directory / "review.json"
        with (
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
            patch("datawrench.key_audit.cli.json.dump", side_effect=KeyboardInterrupt),
        ):
            code = main(
                [
                    "audit",
                    str(self.input),
                    "--key",
                    "id",
                    "--rule",
                    "strip",
                    "--report",
                    str(output),
                    "--work-dir",
                    str(self.directory),
                ]
            )
        self.assertEqual(code, 130)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.directory.glob("*keyaudit-*")))

    def test_detect_source_mutation_during_snapshot(self):
        original = os.fstat
        calls = 0

        def changed(fd):
            nonlocal calls
            calls += 1
            value = original(fd)
            return SimpleNamespace(
                st_mode=value.st_mode,
                st_size=value.st_size,
                st_mtime_ns=value.st_mtime_ns + (calls > 1),
                st_ctime_ns=value.st_ctime_ns,
            )

        with (
            self.assertRaisesRegex(AuditError, "changed during snapshot"),
            patch("datawrench.key_audit.csvio.os.fstat", side_effect=changed),
        ):
            snapshot_source(self.input, self.directory / "snapshot.csv")

    def test_appending_producer_is_rejected_after_one_bounded_copy(self):
        original_open = Path.open
        snapshot = self.directory / "snapshot.csv"
        source = self.input
        writes = []

        class AppendingTarget:
            def __init__(self, target):
                self.target = target

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.target.close()

            def write(self, data):
                writes.append(len(data))
                if len(writes) > 2:
                    raise AssertionError("snapshot chased an indefinitely appending producer")
                self.target.write(data)
                with original_open(source, "ab") as producer:
                    producer.write(b"x" * 64)

        def opened(path, *args, **kwargs):
            result = original_open(path, *args, **kwargs)
            return AppendingTarget(result) if path == snapshot else result

        with (
            self.assertRaisesRegex(AuditError, "changed during snapshot"),
            patch.object(Path, "open", opened),
        ):
            snapshot_source(self.input, snapshot)
        self.assertEqual(len(writes), 1)

    def test_key_expansion_obeys_the_configured_key_resource_limit(self):
        self.input.write_text("id\nﬃﬃ\n", encoding="utf-8")
        options = CSVOptions(max_field_chars=4, max_key_chars=4)
        with self.assertRaisesRegex(AuditError, "produces a key beyond"):
            with Audit(self.input, ["id"], ["nfkc"], options, work_dir=self.directory):
                pass
        with Audit(
            self.input,
            ["id"],
            ["nfkc"],
            CSVOptions(max_field_chars=6, max_key_chars=6),
            work_dir=self.directory,
        ) as a:
            self.assertEqual(a.summary()["changed_rows"], 1)

    def test_inherently_ambiguous_escape_dialects_are_rejected(self):
        for escape in (",", '"'):
            with self.subTest(escape=escape), self.assertRaisesRegex(AuditError, "must differ"):
                CSVOptions(escapechar=escape)

    def test_surrogate_dialect_characters_cannot_produce_unencodable_exports(self):
        for field in ("quotechar", "delimiter", "escapechar"):
            with self.subTest(field=field), self.assertRaisesRegex(AuditError, "Unicode scalar"):
                CSVOptions(**{field: "\ud800"})

    @unittest.skipUnless(os.name == "posix", "POSIX pipe exit semantics")
    def test_closed_output_consumer_is_quiet_for_commands_and_help(self):
        for arguments in (["rules"], ["--help"]):
            with self.subTest(arguments=arguments):
                env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
                process = subprocess.Popen(
                    [sys.executable, "-m", "datawrench", "key-audit", *arguments],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                try:
                    process.stdout.close()
                    process.stdout = None
                    _, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 141, stderr.decode())
                    self.assertEqual(stderr, b"")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)

    def test_random_dialects_roundtrip_all_nonkey_values(self):
        rng = random.Random(2306)
        alphabet = ["a", "é", "漢", ",", ";", '"', "'", "\\", "\r", "\n", "\t", "\0", "\u200b"]
        rows = [["id", "payload"]] + [
            [f"{n:03d} ", "".join(rng.choices(alphabet, k=40))] for n in range(30)
        ]
        for delimiter, quotechar, escapechar, doublequote in (
            (",", '"', None, True),
            (";", "'", "\\", False),
            ("\t", '"', "\\", True),
            ("|", "'", None, True),
        ):
            with self.subTest(delimiter=delimiter):
                options = CSVOptions(
                    delimiter=delimiter,
                    quotechar=quotechar,
                    escapechar=escapechar,
                    doublequote=doublequote,
                )
                with self.input.open("w", encoding="utf-8", newline="") as target:
                    csv.writer(target, **options.dialect()).writerows(rows)
                output = self.directory / (str(ord(delimiter)) + ".csv")
                with Audit(self.input, ["id"], ["strip"], options, work_dir=self.directory) as a:
                    a.export(output)
                with output.open(encoding="utf-8", newline="") as source:
                    actual = list(csv.reader(source, strict=True, **options.dialect()))
                self.assertEqual(
                    actual,
                    [rows[0], *[[transform_key([r[0]], ["strip"])[0], r[1]] for r in rows[1:]]],
                )

    @unittest.skipUnless(os.name == "posix", "POSIX signal semantics")
    def test_sigterm_and_sigint_cleanup_live_cli(self):
        code = """
import signal, sys
from datawrench.key_audit.audit import Audit
from datawrench.key_audit.cli import main
def pause(self):
    print("ready", flush=True)
    signal.pause()
Audit._scan = pause
raise SystemExit(main(sys.argv[1:]))
"""
        for signum, expected in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with self.subTest(signal=signum):
                output = self.directory / "review.json"
                env = dict(
                    os.environ,
                    PYTHONPATH=str(ROOT / "src"),
                    TMPDIR=str(self.directory),
                    SQLITE_TMPDIR=str(self.directory),
                )
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        code,
                        "audit",
                        str(self.input),
                        "--key",
                        "id",
                        "--rule",
                        "strip",
                        "--report",
                        str(output),
                        "--work-dir",
                        str(self.directory),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                try:
                    ready, _, _ = select.select([process.stdout], [], [], 5)
                    self.assertTrue(ready, "child did not reach scan within five seconds")
                    self.assertEqual(process.stdout.readline().strip(), "ready")
                    process.send_signal(signum)
                    _, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, expected, stderr)
                    self.assertFalse(output.exists())
                    self.assertFalse(list(self.directory.glob("*keyaudit-*")))
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
