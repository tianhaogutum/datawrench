from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import tempfile
import unicodedata
import unittest
from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from datawrench.key_audit.audit import Audit, DetailLimits, review_id
from datawrench.key_audit.cli import main
from datawrench.key_audit.csvio import AuditError, CSVOptions
from datawrench.key_audit.evidence import EvidenceBudget, describe_change
from datawrench.key_audit.review import load_review
from datawrench.key_audit.rules import transform_key

ROOT = Path(__file__).resolve().parents[2]
# Scratch space inside the repository (ignored by .gitignore).
WORK = ROOT / ".tmp" / "key-audit"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class AuditTests(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.input = self.directory / "input.csv"

    def write(self, rows, *, encoding="utf-8", **dialect):
        with self.input.open("w", encoding=encoding, newline="") as f:
            csv.writer(f, **dialect).writerows(rows)

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def audit_command(self, *rules, **kwargs):
        path = self.directory / kwargs.pop("name", "review.json")
        args = ["audit", self.input, "--key", "id", "--report", path, "--work-dir", self.directory]
        for rule in rules:
            args.extend(["--rule", rule])
        for key, value in kwargs.items():
            args.extend(["--" + key.replace("_", "-"), value])
        result = self.command(*args)
        return path, result

    def test_exact_duplicate_is_not_a_new_collision(self):
        self.write([["id", "note"], ["001", "one"], ["001", "two"], ["002 ", "three"]])
        with Audit(self.input, ["id"], ["strip"], work_dir=self.directory) as a:
            s = a.summary()
            self.assertEqual(
                (s["baseline_duplicate_groups"], s["baseline_duplicate_excess_rows"]), (1, 1)
            )
            self.assertEqual(s["new_collision_groups"], 0)
            self.assertEqual(s["changed_rows"], 1)
            self.assertEqual(a.report()["baseline_duplicates"]["groups"][0]["row_count"], 2)

    def test_new_collision_extends_existing_duplicate(self):
        self.write([["id"], ["001"], ["001"], ["001\u3000"], ["Cafe\u0301"], ["Café"]])
        with Audit(self.input, ["id"], ["nfc", "strip"], work_dir=self.directory) as a:
            report = a.report()
        self.assertEqual(report["summary"]["new_collision_groups"], 2)
        self.assertEqual(report["summary"]["new_collision_rows"], 5)
        self.assertEqual(report["summary"]["original_keys_merged"], 2)
        self.assertEqual([s["additional_keys_merged"] for s in report["stages"]], [0, 1, 1])
        group = report["new_collisions"]["groups"][0]
        self.assertEqual((group["row_count"], group["variant_count"]), (3, 2))
        removed = group["variants"][1]["changes"][0]["edits"][0]["removed"]
        self.assertEqual(removed[0]["codepoint"], "U+3000")

    def test_composite_keys_are_tuples_not_concatenation(self):
        self.write([["tenant", "id"], ["a", "bc"], ["ab", "c"], ["a", "bc "], ["ab", "bc"]])
        with Audit(self.input, ["tenant", "id"], ["strip"], work_dir=self.directory) as a:
            self.assertEqual(a.summary()["new_collision_groups"], 1)
            self.assertEqual(a.summary()["normalized_distinct_keys"], 3)

    def test_only_confirmed_rules_and_all_nonkey_values_preserved(self):
        self.write(
            [["id", "note"], ["Cafe\u0301 ", "\ufeffA\u200bB,\n001"], ["ß", "👩\u200d💻"]],
            encoding="utf-8-sig",
        )
        review, (code, _, err) = self.audit_command("nfc", "strip", "casefold")
        self.assertEqual((code, err), (0, ""))
        output = self.directory / "clean.csv"
        code, out, err = self.command(
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "nfc",
            "--output",
            output,
            "--work-dir",
            self.directory,
        )
        self.assertEqual((code, err), (0, ""))
        with output.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(
            rows, [["id", "note"], ["Café ", "\ufeffA\u200bB,\n001"], ["ß", "👩\u200d💻"]]
        )
        receipt = json.loads(out)
        self.assertEqual(receipt["applied_rules"], ["nfc"])
        self.assertEqual(receipt["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())

    def test_confirmations_keep_reviewed_order(self):
        self.write([["id"], ["x \u200b"]])
        review, _ = self.audit_command("remove-zwsp", "strip")
        output = self.directory / "clean.csv"
        code, out, err = self.command(
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "strip",
            "--confirm",
            "remove-zwsp",
            "--output",
            output,
            "--work-dir",
            self.directory,
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["applied_rules"], ["remove-zwsp", "strip"])
        self.assertEqual(output.read_text(), "id\nx\n")

    def test_collision_export_guard_and_explicit_accept_flag(self):
        self.write([["id"], ["x "], ["x"], ["x"]])
        review, (code, _, _) = self.audit_command("strip")
        self.assertEqual(code, 1)
        output = self.directory / "clean.csv"
        args = [
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "strip",
            "--output",
            output,
            "--work-dir",
            self.directory,
        ]
        code, _, err = self.command(*args)
        self.assertEqual(code, 1)
        self.assertIn("--accept-collisions", err)
        self.assertFalse(output.exists())
        code, out, err = self.command(*args, "--accept-collisions")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["summary"]["rows"], 3)
        self.assertEqual(output.read_text(), "id\nx\nx\nx\n")

    def test_new_empty_key_guard_is_independent_of_collision(self):
        self.write([["id", "note"], ["\u200b", "one"]])
        review, (code, _, _) = self.audit_command("remove-zwsp")
        self.assertEqual(code, 1)
        output = self.directory / "clean.csv"
        args = [
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "remove-zwsp",
            "--output",
            output,
            "--work-dir",
            self.directory,
        ]
        code, _, err = self.command(*args)
        self.assertEqual(code, 1)
        self.assertIn("--accept-empty", err)
        self.assertFalse(output.exists())
        code, _, err = self.command(*args, "--accept-empty")
        self.assertEqual((code, err), (0, ""))

    def test_changed_source_and_changed_review_are_rejected(self):
        self.write([["id"], ["x "]])
        review, _ = self.audit_command("strip")
        original = self.input.read_bytes()
        self.input.write_bytes(original + b"y\r\n")
        args = [
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "strip",
            "--output",
            self.directory / "out.csv",
            "--work-dir",
            self.directory,
        ]
        code, _, err = self.command(*args)
        self.assertEqual(code, 2)
        self.assertIn("input bytes differ", err)
        self.input.write_bytes(original)
        report = json.loads(review.read_text())
        report["plan"]["rules"] = ["casefold"]
        review.write_text(json.dumps(report))
        code, _, err = self.command(*args)
        self.assertEqual(code, 2)
        self.assertIn("contract changed", err)

    def test_no_overwrite_and_temporary_cleanup(self):
        self.write([["id"], ["x "]])
        review, _ = self.audit_command("strip")
        before = self.input.read_bytes()
        code, _, err = self.command(
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "strip",
            "--output",
            self.input,
            "--work-dir",
            self.directory,
        )
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(self.input.read_bytes(), before)
        self.assertFalse(list(self.directory.glob("keyaudit-*")))
        self.assertFalse(list(self.directory.glob(".keyaudit-*")))

    def test_sampling_never_changes_counts_or_guard(self):
        rows = [["id"]] + [["x" + "\u200b" * i] for i in range(8)] + [["x"]] * 9
        self.write(rows)
        with Audit(
            self.input,
            ["id"],
            ["remove-zwsp"],
            limits=DetailLimits(1, 2, 1),
            work_dir=self.directory,
        ) as a:
            report = a.report()
        group = report["new_collisions"]["groups"][0]
        self.assertEqual(
            (group["row_count"], group["variant_count"], group["omitted_variants"]), (17, 8, 6)
        )
        self.assertEqual(group["variants"][0]["omitted_row_samples"], 9)

    def test_real_upstream_csv_fixtures_roundtrip(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text())
        for fixture in manifest:
            with self.subTest(fixture=fixture["file"]):
                path = FIXTURES / fixture["file"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), fixture["sha256"])
                options = CSVOptions(**fixture.get("csv_options", {}))
                with path.open(encoding=options.encoding, newline="") as f:
                    original = list(csv.reader(f, **options.dialect()))
                output = self.directory / (path.name + ".out")
                with Audit(path, [original[0][0]], ["nfc"], options, work_dir=self.directory) as a:
                    self.assertEqual(a.summary()["new_collision_groups"], 0)
                    a.export(output)
                with output.open(encoding="utf-8", newline="") as f:
                    self.assertEqual(list(csv.reader(f, **options.dialect())), original)

    def test_nfd_variant_derived_from_real_upstream_name(self):
        with (FIXTURES / "upstream/miller/utf8-1.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        name = rows[1][1]
        self.assertNotEqual(name, unicodedata.normalize("NFD", name))
        self.write([["id"], [name], [unicodedata.normalize("NFD", name)]])
        with Audit(self.input, ["id"], ["nfc"], work_dir=self.directory) as a:
            self.assertEqual(a.summary()["new_collision_groups"], 1)

    def test_upstream_export_encodings_and_dialects(self):
        for encoding, delimiter in (("utf-16", ";"), ("cp1252", ";"), ("utf-8-sig", "\t")):
            with self.subTest(encoding=encoding):
                rows = [["id", "notes"], ["é ", 'comma,quote"\r\nline'], ["é", "002"]]
                self.write(rows, encoding=encoding, delimiter=delimiter)
                options = CSVOptions(encoding=encoding, delimiter=delimiter)
                with Audit(self.input, ["id"], ["strip"], options, work_dir=self.directory) as a:
                    self.assertEqual(a.summary()["new_collision_groups"], 1)
                    output = self.directory / (encoding + ".csv")
                    a.export(output)
                with output.open(encoding="utf-8", newline="") as f:
                    result = list(csv.reader(f, delimiter=delimiter))
                self.assertEqual(result, [rows[0], ["é", rows[1][1]], rows[2]])

    def test_invalid_csv_headers_encoding_width_and_quotes(self):
        cases = [
            b"",
            b"id,id\na,b\n",
            b"id,\na,b\n",
            b"id,n\na\n",
            b'id,n\n"unclosed,b\n',
            b"id\n\xff\n",
            b"id\n\n",
        ]
        for data in cases:
            with self.subTest(data=data):
                self.input.write_bytes(data)
                with self.assertRaises(AuditError):
                    with Audit(self.input, ["id"], ["nfc"], work_dir=self.directory):
                        pass
                self.assertFalse(list(self.directory.glob("keyaudit-*")))

    def test_record_field_and_key_limits(self):
        self.write([["id", "n"], ["abcd", "payload"]])
        for options in (
            CSVOptions(max_key_chars=3),
            CSVOptions(max_field_chars=3, max_key_chars=3),
            CSVOptions(max_record_chars=8),
        ):
            with self.subTest(options=options), self.assertRaises(AuditError):
                with Audit(self.input, ["id"], ["strip"], options, work_dir=self.directory):
                    pass

    def test_null_bytes_zwj_and_bom_in_data_are_literal(self):
        self.write([["id"], ["A\0B"], ["AB"], ["👩\u200d💻"], ["\ufeffAB"], ["A\u200bB"]])
        with Audit(
            self.input, ["id"], ["nfc", "remove-zwsp", "remove-bom"], work_dir=self.directory
        ) as a:
            self.assertEqual(a.summary()["normalized_distinct_keys"], 3)
            report = a.report()
        self.assertEqual(report["new_collisions"]["groups"][0]["variant_count"], 3)

    def test_deterministic_randomized_grouping_against_counter_oracle(self):
        rng = random.Random(306)
        base = [
            "001",
            "A",
            "a",
            "ß",
            "ss",
            "Café",
            "Cafe\u0301",
            "Ａ",
            "",
            " ",
            "\u200b",
            "a\0b",
            "👩\u200d💻",
        ]
        keys = [
            (
                rng.choice(["eu", "us", "us "]),
                rng.choice(base) + rng.choice(["", "", "\u3000", "\u200b"]),
            )
            for _ in range(240)
        ]
        self.write([["tenant", "id"], *keys])
        for rules in (
            ["nfc"],
            ["width", "strip"],
            ["remove-zwsp", "strip", "casefold"],
            ["nfkc", "casefold"],
        ):
            with self.subTest(rules=rules):
                original = Counter(keys)
                groups = defaultdict(set)
                for key in keys:
                    groups[transform_key(key, rules)].add(key)
                collisions = [values for values in groups.values() if len(values) > 1]
                expected_rows = sum(original[k] for group in collisions for k in group)
                with Audit(self.input, ["tenant", "id"], rules, work_dir=self.directory) as a:
                    s = a.summary()
                self.assertEqual(s["original_distinct_keys"], len(original))
                self.assertEqual(s["normalized_distinct_keys"], len(groups))
                self.assertEqual(s["new_collision_groups"], len(collisions))
                self.assertEqual(s["new_collision_rows"], expected_rows)
                self.assertEqual(
                    s["baseline_duplicate_groups"], sum(n > 1 for n in original.values())
                )

    def test_header_only_is_valid(self):
        self.write([["id", "note"]])
        with Audit(self.input, ["id"], ["nfc"], work_dir=self.directory) as a:
            self.assertEqual(a.summary()["rows"], 0)
            self.assertEqual(a.summary()["new_collision_groups"], 0)

    def test_changed_source_is_rejected_before_attempting_to_parse_it(self):
        self.write([["id"], ["x "]])
        review, _ = self.audit_command("strip")
        self.input.write_bytes(b'bad header\n"unterminated')
        code, _, err = self.command(
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "strip",
            "--output",
            self.directory / "out.csv",
            "--work-dir",
            self.directory,
        )
        self.assertEqual(code, 2)
        self.assertIn("input bytes differ", err)

    def test_quote_introduced_by_nfkc_is_exportable_with_explicit_escape(self):
        self.write([["id"], ["＂"]], doublequote=False, escapechar="\\")
        review, (code, _, err) = self.audit_command("nfkc", escapechar="\\")
        self.assertEqual((code, err), (0, ""))
        output = self.directory / "clean.csv"
        code, _, err = self.command(
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "nfkc",
            "--output",
            output,
            "--work-dir",
            self.directory,
        )
        self.assertEqual((code, err), (0, ""))
        with output.open(encoding="utf-8", newline="") as f:
            self.assertEqual(list(csv.reader(f, escapechar="\\")), [["id"], ['"']])

    def test_no_doublequote_without_escape_is_rejected_before_work(self):
        with self.assertRaisesRegex(AuditError, "requires --escapechar"):
            CSVOptions(doublequote=False)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX named pipes only")
    def test_input_and_review_fifos_do_not_block(self):
        fifo = self.directory / "pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(AuditError, "regular file"):
            with Audit(fifo, ["id"], ["strip"], work_dir=self.directory):
                pass
        with self.assertRaisesRegex(AuditError, "regular file"):
            load_review(fifo)

    def test_malformed_review_is_actionable(self):
        for content in ('{"schema":"x","schema":"y"}', '{"a":NaN}', "[1]", "{", "null"):
            path = self.directory / "bad.json"
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(AuditError):
                load_review(path)

    def test_report_budget_truncation_is_explicit_and_does_not_change_mapping(self):
        self.write([["id"], *[[f"{g}" + "\u200b" * i] for g in range(3) for i in range(5)]])
        with Audit(self.input, ["id"], ["remove-zwsp"], work_dir=self.directory) as a:
            with patch("datawrench.key_audit.audit.EvidenceBudget", lambda: EvidenceBudget(1200)):
                report = a.report()
            self.assertTrue(report["evidence_budget"]["exhausted"])
            self.assertLessEqual(report["evidence_budget"]["compact_bytes_used"], 1200)
            self.assertEqual(report["summary"]["new_collision_groups"], 3)
            mapping = self.directory / "map.csv"
            receipt = a.export_map(mapping)
        with mapping.open(newline="") as f:
            entries = list(csv.DictReader(f))
        self.assertEqual(len(entries), 15)
        self.assertEqual(receipt["mapping_rows"], 15)
        self.assertEqual(len({e["collision_id"] for e in entries}), 3)
        self.assertEqual(
            [json.loads(e["original_key_json"]) for e in entries[:5]],
            [["0" + "\u200b" * i] for i in range(5)],
        )

    def test_dense_diffs_are_bounded_with_omission_counts(self):
        before = "a\u200b" * 120
        change = describe_change(before, "a" * 120)
        self.assertLessEqual(len(change["edits"]), 32)
        self.assertGreater(change["omitted_edit_operations"], 0)
        change = describe_change("\u200b" * 100, "")
        self.assertEqual(len(change["edits"][0]["removed"]), 32)
        self.assertEqual(change["edits"][0]["omitted_removed_codepoints"], 68)

    def test_long_repetitive_key_diff_keeps_absolute_character_indices(self):
        before = "a" * 3000 + "A" + "a" * 1000
        change = describe_change(before, before.casefold())
        self.assertNotIn("diff_omitted", change)
        self.assertEqual(len(change["edits"]), 1)
        edit = change["edits"][0]
        self.assertEqual(edit["before_range"], [3000, 3001])
        self.assertEqual(edit["after_range"], [3000, 3001])
        self.assertEqual(edit["removed"][0]["index"], 3000)
        self.assertEqual(edit["removed"][0]["codepoint"], "U+0041")
        self.assertEqual(edit["inserted"][0]["codepoint"], "U+0061")

    def test_trimmed_context_handles_prefix_insertions_deletions_and_large_changes(self):
        for before, after, operation, old_range, new_range in (
            ("abc", "abcd", "insert", [3, 3], [3, 4]),
            ("abcd", "abc", "delete", [3, 4], [3, 3]),
            ("aXz", "aYz", "replace", [1, 2], [1, 2]),
        ):
            with self.subTest(before=before):
                edit = describe_change(before, after)["edits"][0]
                self.assertEqual(
                    (edit["operation"], edit["before_range"], edit["after_range"]),
                    (operation, old_range, new_range),
                )
        self.assertIn("diff_omitted", describe_change("a" * 600, "b" * 600))

    def test_export_map_complete_keys_and_scopes(self):
        rows = [
            ["id", "note"],
            ["x", "a"],
            ["x", "b"],
            ["x\u200b", "c"],
            ["long" * 200 + " ", "d"],
            ["other", "e"],
        ]
        self.write(rows)
        review, _ = self.audit_command(
            "strip", "remove-zwsp", groups="1", variants="1", samples="1"
        )
        for scope, expected in (("collisions", 2), ("changed", 2), ("all", 4)):
            output = self.directory / (scope + ".csv")
            code, out, err = self.command(
                "export-map",
                self.input,
                "--review",
                review,
                "--output",
                output,
                "--scope",
                scope,
                "--work-dir",
                self.directory,
            )
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out)["mapping_rows"], expected)
            with output.open(newline="") as f:
                mapping = list(csv.DictReader(f))
            self.assertEqual(len(mapping), expected)
            if scope == "all":
                self.assertEqual(json.loads(mapping[2]["original_key_json"])[0], rows[4][0])
                self.assertEqual(mapping[0]["original_rows"], "2")

    def test_newly_empty_examples_include_later_changed_keys(self):
        self.write([["id"], ["a "], ["\u200b"]])
        with Audit(
            self.input,
            ["id"],
            ["strip", "remove-zwsp"],
            limits=DetailLimits(1, 1, 1),
            work_dir=self.directory,
        ) as a:
            report = a.report()
        self.assertEqual(report["newly_empty_keys"]["keys"][0]["original_key"][0]["text"], "\u200b")
        self.assertEqual(report["summary"]["newly_empty_distinct_keys"], 1)

    def test_subset_can_introduce_a_collision_absent_from_full_plan(self):
        self.write([["id"], ["x \u200b"], ["x "]])
        review, (code, _, err) = self.audit_command("strip", "remove-zwsp")
        self.assertEqual((code, err), (0, ""))
        code, out, err = self.command(
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "remove-zwsp",
            "--output",
            self.directory / "out.csv",
            "--work-dir",
            self.directory,
        )
        self.assertEqual(code, 1)
        self.assertIn("1 new collision", err)
        self.assertFalse((self.directory / "out.csv").exists())

    def test_show_works_after_engine_change_but_export_does_not(self):
        self.write([["id"], ["x "]])
        review, _ = self.audit_command("strip")
        report = json.loads(review.read_text())
        report["engine"]["unicode"] = "0.0.0"
        report["review_id"] = review_id(report)
        review.write_text(json.dumps(report))
        code, out, err = self.command("show", review)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("U+0020 SPACE", out)
        code, _, err = self.command(
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "strip",
            "--output",
            self.directory / "out.csv",
            "--work-dir",
            self.directory,
        )
        self.assertEqual(code, 2)
        self.assertIn("engine differs", err)

    def test_show_escapes_terminal_controls_in_tampered_evidence(self):
        self.write([["id"], ["x "]])
        review, _ = self.audit_command("strip")
        report = json.loads(review.read_text())
        report["summary"]["rows"] = "\x1b[2J\u202e"
        report["changed_keys"]["keys"][0]["changes"][0]["edits"][0]["removed"][0]["name"] = (
            "\x1b[31m"
        )
        review.write_text(json.dumps(report))
        code, out, err = self.command("show", review)
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)
        self.assertIn("\\x1b", out)

    def test_normative_unicode_fixture_grouping_and_csv_replay(self):
        forms = []
        with (FIXTURES / "upstream/unicode/NormalizationTest-sample.txt").open(
            encoding="utf-8"
        ) as source:
            for line in source:
                line = line.split("#", 1)[0].strip()
                if line:
                    forms.append(
                        [
                            "".join(chr(int(cp, 16)) for cp in field.split())
                            for field in next(csv.reader([line], delimiter=";"))[:5]
                        ]
                    )
        self.assertEqual(len(forms), 15)
        for rule, count, target_index in (("nfc", 3, 1), ("nfkc", 5, 3)):
            rows = [["case", "id", "expected"]]
            expected_new_groups = 0
            expected_original_keys = 0
            for n, case in enumerate(forms):
                values = case[:count]
                expected_new_groups += len(set(values)) > 1
                expected_original_keys += len(set(values))
                rows.extend([[str(n), value, case[target_index]] for value in values])
            self.write(rows)
            output = self.directory / (rule + "-normative.csv")
            with Audit(self.input, ["case", "id"], [rule], work_dir=self.directory) as a:
                summary = a.summary()
                self.assertEqual(summary["new_collision_groups"], expected_new_groups)
                self.assertEqual(summary["original_distinct_keys"], expected_original_keys)
                self.assertEqual(summary["normalized_distinct_keys"], len(forms))
                a.export(output)
            with output.open(encoding="utf-8", newline="") as source:
                actual = list(csv.reader(source))
            self.assertEqual(actual, [rows[0], *[[row[0], row[2], row[2]] for row in rows[1:]]])

    def test_width_and_nfc_order_for_real_halfwidth_katakana(self):
        self.write([["id"], ["ｶﾞ"], ["ガ"]])
        with Audit(self.input, ["id"], ["nfc", "width"], work_dir=self.directory) as a:
            self.assertEqual(a.summary()["new_collision_groups"], 0)
        with Audit(self.input, ["id"], ["width", "nfc"], work_dir=self.directory) as a:
            self.assertEqual(a.summary()["new_collision_groups"], 1)
            self.assertEqual(a.report()["stages"][2]["additional_keys_merged"], 1)

    def test_forged_finding_counts_cannot_bypass_fresh_export_guard(self):
        self.write([["id"], ["x "], ["x"]])
        review, _ = self.audit_command("strip")
        report = json.loads(review.read_text())
        report["summary"]["new_collision_groups"] = 0
        report["new_collisions"]["groups"] = []
        review.write_text(json.dumps(report))
        code, _, err = self.command(
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "strip",
            "--output",
            self.directory / "out.csv",
            "--work-dir",
            self.directory,
        )
        self.assertEqual(code, 1)
        self.assertIn("1 new collision", err)

    def test_byte_identical_source_can_move_and_nonproposed_confirmations_fail(self):
        self.write([["id"], ["x "]])
        review, _ = self.audit_command("strip")
        moved = self.directory / "moved.csv"
        self.input.rename(moved)
        output = self.directory / "out.csv"
        code, _, err = self.command(
            "clean",
            moved,
            "--review",
            review,
            "--confirm",
            "nfc",
            "--output",
            output,
            "--work-dir",
            self.directory,
        )
        self.assertEqual(code, 2)
        self.assertIn("not in the review", err)
        code, _, err = self.command(
            "clean",
            moved,
            "--review",
            review,
            "--confirm",
            "strip",
            "--output",
            output,
            "--work-dir",
            self.directory,
        )
        self.assertEqual((code, err), (0, ""))

    def test_physical_lines_in_real_multiline_upstream_csv(self):
        source = FIXTURES / "upstream/miller/quoted-crlf.csv"
        output = self.directory / "lines.csv"
        with Audit(source, ["a"], ["nfc"], work_dir=self.directory) as a:
            a.export_map(output, scope="all")
        with output.open(newline="") as f:
            mapping = list(csv.DictReader(f))
        self.assertEqual(
            [(row["first_record"], row["first_line"]) for row in mapping], [("1", "2"), ("2", "4")]
        )

    def test_existing_empty_keys_are_preserved_without_accept_empty(self):
        self.write([["id"], [""], [""]])
        review, (code, _, err) = self.audit_command("strip")
        self.assertEqual((code, err), (0, ""))
        output = self.directory / "empty.csv"
        code, out, err = self.command(
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "strip",
            "--output",
            output,
            "--work-dir",
            self.directory,
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["summary"]["baseline_duplicate_groups"], 1)
        with output.open(newline="") as f:
            self.assertEqual(list(csv.reader(f)), [["id"], [""], [""]])

    def test_collision_and_new_empty_need_both_explicit_accept_flags(self):
        self.write([["id"], [""], [" "]])
        review, _ = self.audit_command("strip")
        output = self.directory / "empty-collision.csv"
        args = [
            "clean",
            self.input,
            "--review",
            review,
            "--confirm",
            "strip",
            "--output",
            output,
            "--work-dir",
            self.directory,
        ]
        code, _, err = self.command(*args, "--accept-collisions")
        self.assertEqual(code, 1)
        self.assertIn("--accept-empty", err)
        self.assertFalse(output.exists())
        code, _, err = self.command(*args, "--accept-collisions", "--accept-empty")
        self.assertEqual((code, err), (0, ""))

    def test_missing_key_diagnostics_bound_very_large_header_names(self):
        self.write([["h" * 20000], ["value"]])
        _, (code, _, err) = self.audit_command("strip")
        self.assertEqual(code, 2)
        self.assertLess(len(err), 2000)
        self.assertIn("key columns not found", err)

    def test_existing_output_is_rejected_before_reading_invalid_input(self):
        self.input.write_bytes(b"\xff invalid encoding")
        output = self.directory / "existing.json"
        output.write_text("keep me")
        code, _, err = self.command(
            "audit",
            self.input,
            "--key",
            "id",
            "--rule",
            "strip",
            "--report",
            output,
            "--work-dir",
            self.directory,
        )
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(output.read_text(), "keep me")
        self.assertFalse(list(self.directory.glob("*keyaudit-*")))

    def test_readable_preview_marks_clipped_keys_and_omitted_changes(self):
        self.write([["id"], ["a" * 200 + "A"], ["b" * 200 + "B"]])
        review, (code, _, err) = self.audit_command("casefold", groups="1")
        self.assertEqual((code, err), (0, ""))
        code, out, err = self.command("show", review)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("+41 code points", out)
        self.assertIn("1 more changed original keys", out)
        self.assertIn("U+0041", out)


if __name__ == "__main__":
    unittest.main()
