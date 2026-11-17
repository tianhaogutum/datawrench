import csv
from pathlib import Path

import duckdb
import pytest
from tableproof import JoinSpec

from datawrench.join_witness import analyze
from tableproof import TableProofError
from datawrench.join_witness import engine


def write_csv(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def test_case_distinct_headers_assert_actual_key_column(tmp_path, db):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    for path in (left, right):
        write_csv(path, ["id", "ID"], [["A", "x"], ["B", "x"]])
    report = analyze(JoinSpec("case", left, right, ("ID",), ("ID",), "many-to-one"))
    assertion = next(item for item in report["assertions"] if item["name"] == "right_unique")
    with pytest.raises(duckdb.InvalidInputException, match="must be unique"):
        db.execute(assertion["sql"])


def test_literal_glob_path_does_not_read_a_nearby_file(tmp_path, db):
    left, right = tmp_path / "left.csv", tmp_path / "right*.csv"
    write_csv(left, ["id"], [["A"]])
    write_csv(right, ["id"], [["A"], ["B"]])
    write_csv(tmp_path / "right-extra.csv", ["id"], [["A"], ["A"]])
    report = analyze(JoinSpec("glob", left, right, ("id",), ("id",), "many-to-one"))
    for assertion in report["assertions"]:
        assert db.execute(assertion["sql"]).fetchone() == (True,)


def test_selected_all_null_measure_still_has_explicit_group_summary(tmp_path):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    write_csv(left, ["id", "amount"], [["A", ""], ["A", ""]])
    write_csv(right, ["id"], [["A"], ["A"]])
    report = analyze(JoinSpec("nulls", left, right, ("id",), ("id",), "many-to-many"), left_measures=("amount",))
    values = report["top_keys"][0]["left_measures"]["amount"]
    assert values["source_nonnull_rows"] == 0
    assert values["predicted_join_sum"] == "0"


def test_change_while_generating_assertions_is_detected(tmp_path, monkeypatch):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    for path in (left, right):
        write_csv(path, ["id"], [["A"]])
    original = engine.assertions

    def mutate_then_generate(spec):
        write_csv(right, ["id"], [["A"], ["A"]])
        return original(spec)

    monkeypatch.setattr(engine, "assertions", mutate_then_generate)
    with pytest.raises(TableProofError, match="Input changed"):
        analyze(JoinSpec("race", left, right, ("id",), ("id",), "many-to-one"))


def test_saved_assertion_rejects_reordered_headers_instead_of_checking_wrong_column(tmp_path, db):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    write_csv(left, ["id"], [["A"]])
    write_csv(right, ["id", "other"], [["A", "one"], ["A", "two"]])
    report = analyze(JoinSpec("schema", left, right, ("id",), ("id",), "many-to-one"))
    query = next(item["sql"] for item in report["assertions"] if item["name"] == "right_unique")
    write_csv(right, ["other", "id"], [["one", "A"], ["two", "A"]])
    with pytest.raises(duckdb.InvalidInputException, match="header changed"):
        db.execute(query)


@pytest.mark.parametrize("folder_name", ["plain", "space and ' quote", "brackets[?]star*", "line\nbreak", "Unicode-東京"])
def test_csv_assertion_matrix_with_literal_paths_and_composite_headers(tmp_path, db, folder_name):
    folder = tmp_path / folder_name
    folder.mkdir()
    left, right = folder / "left.csv", folder / "right.csv"
    header = ["id", "ID", "comma,key", "multi\nline", 'quote"key']
    rows = [["one", "A", "x", "l1", "z"], ["two", "A", "x", "l2", "z"], ["three", "", "x", "l3", "z"]]
    for path in (left, right):
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
    report = analyze(JoinSpec("matrix", left, right, ("ID", "comma,key"), ("ID", "comma,key"), "one-to-one"))
    for assertion in report["assertions"]:
        reason = "must be unique" if assertion["name"].endswith("unique") else "must be nonblank"
        with pytest.raises(duckdb.InvalidInputException, match=reason):
            db.execute(assertion["sql"])


@pytest.mark.parametrize("terminator", ["\n", "\r\n", "\r"])
def test_explicit_record_terminators_with_multiline_data(tmp_path, db, terminator):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    for path in (left, right):
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator=terminator)
            writer.writerow(["id", "note"])
            writer.writerows([["A", "a\nb"], ["B", "x\r\ny"]])
    report = analyze(JoinSpec("terminators", left, right, ("id",), ("id",), "one-to-one"))
    for item in report["assertions"]:
        assert db.execute(item["sql"]).fetchone() == (True,)


def test_assertions_keep_exact_keys_when_connection_collation_is_nocase(tmp_path, db):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    for path in (left, right):
        write_csv(path, ["id"], [["A"], ["a"]])
    report = analyze(JoinSpec("collation", left, right, ("id",), ("id",), "one-to-one"))
    db.execute("SET default_collation='nocase'")
    for item in report["assertions"]:
        assert db.execute(item["sql"]).fetchone() == (True,)


def test_wide_record_values_respect_byte_budget_without_losing_locators(tmp_path):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    for path in (left, right):
        write_csv(path, ["id", "note", "amount"], [["A", "x" * 60000, "1.25"], ["A", "y" * 60000, "2.75"]])
    report = analyze(JoinSpec("wide", left, right, ("id",), ("id",), "many-to-many"),
                     left_measures=("amount",), sample_bytes=1000)
    group = report["top_keys"][0]
    assert group["left_records"][0]["record_number"] == 1
    assert group["left_records"][0]["values_omitted"] == "sample_byte_limit"
    assert "values" not in group["left_records"][0]
    assert report["sampling"]["values_bytes"] <= 1000
    assert report["sampling"]["records_without_values"] == 4
    assert report["measures"]["left"]["amount"]["predicted_join_sum"] == "8.00"


def test_mixed_record_terminators_cannot_silently_generate_a_false_sql_guard(tmp_path):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    for path in (left, right):
        path.write_bytes(b"id,amount\r\nA,1\nA,2\r\n")
    with pytest.raises(TableProofError, match="Mixed record terminators"):
        analyze(JoinSpec("mixed", left, right, ("id",), ("id",), "many-to-one"), top=0)


def test_nul_and_unicode_keys_and_headers_are_not_sql_syntax(tmp_path, db):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    header = ["key\x00name", "東京"]
    for path in (left, right):
        write_csv(path, header, [["A\x00", "é"], ["A", "e\u0301"]])
    report = analyze(JoinSpec("bytes", left, right, (header[0],), (header[0],), "one-to-one"))
    for item in report["assertions"]:
        assert db.execute(item["sql"]).fetchone() == (True,)


def test_replacement_during_final_hash_is_detected_after_hash_finishes(tmp_path, monkeypatch):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    for path in (left, right):
        write_csv(path, ["id", "amount"], [["A", "1"]])
    original = engine._hash
    replaced = False

    def hash_then_replace(path):
        nonlocal replaced
        digest = original(path)
        if path == left and not replaced:
            replaced = True
            replacement = tmp_path / "replacement.csv"
            write_csv(replacement, ["id", "amount"], [["A", "1000"]])
            replacement.replace(left)
        return digest

    monkeypatch.setattr(engine, "_hash", hash_then_replace)
    with pytest.raises(TableProofError, match="Input changed"):
        analyze(JoinSpec("late-snapshot", left, right, ("id",), ("id",), "many-to-one"), left_measures=("amount",))


@pytest.mark.parametrize("filename", [r"back\slash*.csv", "café*.csv", "cafe\u0301*.csv", "[]mix?.csv", r"bracket\[x].csv"])
def test_literal_filename_edge_cases_are_read_as_one_source(tmp_path, db, filename):
    left, right = tmp_path / "left.csv", tmp_path / filename
    write_csv(left, ["id"], [["A"]])
    write_csv(right, ["id"], [["A"], ["A"]])
    spec = JoinSpec("filename", left, right, ("id",), ("id",), "many-to-one")
    if "\\" in filename:
        with pytest.raises(TableProofError, match="backslash and glob"):
            analyze(spec)
        return
    report = analyze(spec)
    for item in report["assertions"]:
        if item["name"] == "right_unique":
            with pytest.raises(duckdb.InvalidInputException, match="must be unique"):
                db.execute(item["sql"])
        else:
            assert db.execute(item["sql"]).fetchone() == (True,)
