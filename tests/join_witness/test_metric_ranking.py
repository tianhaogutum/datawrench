from decimal import Decimal, localcontext
from fractions import Fraction

import pytest
from tableproof import JoinSpec, TableProofError

from datawrench.join_witness import analyze
from .test_adversarial import write_csv


def test_metric_ranking_finds_large_value_key_hidden_by_row_ranking(tmp_path):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    write_csv(left, ["id", "amount"], [["A", "1.00"] for _ in range(100)] + [["B", "1000.00"]])
    write_csv(right, ["id"], [["A"], ["A"], ["B"], ["B"]])
    spec = JoinSpec("metric", left, right, ("id",), ("id",), "many-to-one")
    rows = analyze(spec, top=1)
    assert rows["top_keys"][0]["key"] == ["A"]
    value = analyze(spec, top=1, rank_by="left:amount")
    assert value["top_keys"][0]["key"] == ["B"]
    assert Decimal(value["top_keys"][0]["ranking_measure_added"]) == Decimal("1000")
    assert "amount" in value["measures"]["left"]


def test_signed_metric_ranking_uses_absolute_extra_value(tmp_path):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    write_csv(left, ["id", "amount"], [["A", "100"], ["B", "-2000"]])
    write_csv(right, ["id"], [["A"], ["A"], ["B"], ["B"]])
    spec = JoinSpec("signed", left, right, ("id",), ("id",), "many-to-one")
    report = analyze(spec, rank_by="left:amount", top=1)
    assert report["top_keys"][0]["key"] == ["B"]
    assert report["top_keys"][0]["ranking_measure_added"] == "-2000"


def test_metric_rank_comparison_never_rounds_long_decimal_values(tmp_path):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    write_csv(left, ["id", "amount"], [["A", "1000000000000000000000000000000"],
                                      ["B", "1000000000000000000000000000001"]])
    write_csv(right, ["id"], [["A"], ["A"], ["B"], ["B"]])
    spec = JoinSpec("precise", left, right, ("id",), ("id",), "many-to-one")
    assert analyze(spec, rank_by="left:amount", top=1)["top_keys"][0]["key"] == ["B"]


def test_right_side_metric_rank_uses_left_multiplicity(tmp_path):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    write_csv(left, ["id"], [["A"] for _ in range(10)] + [["B"], ["B"]])
    write_csv(right, ["id", "price:usd"], [["A", "2"], ["B", "100"]])
    spec = JoinSpec("right", left, right, ("id",), ("id",), "many-to-one")
    report = analyze(spec, rank_by="right:price:usd", top=1)
    assert report["top_keys"][0]["key"] == ["B"]
    assert report["top_keys"][0]["ranking_measure_added"] == "100"
    assert report["measures"]["right"]["price:usd"]["predicted_join_sum"] == "220"


@pytest.mark.parametrize("rank_by", ["amount", "left:", "rows:amount", 42])
def test_invalid_rank_modes_are_normal_input_errors(tmp_path, rank_by):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    write_csv(left, ["id"], [["A"]])
    write_csv(right, ["id"], [["A"]])
    with pytest.raises(TableProofError, match="rank_by"):
        analyze(JoinSpec("bad", left, right, ("id",), ("id",), "many-to-one"), rank_by=rank_by)


def test_library_arithmetic_is_independent_of_callers_decimal_context(tmp_path):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    values = ["1e1000", "1e-1000", "-123456789012345678901234567890.123456789"]
    write_csv(left, ["id", "amount"], [["A", value] for value in values])
    write_csv(right, ["id"], [["A"], ["A"], ["A"]])
    spec = JoinSpec("context", left, right, ("id",), ("id",), "many-to-one")
    with localcontext() as context:
        context.prec, context.Emin, context.Emax = 2, -9, 9
        report = analyze(spec, rank_by="left:amount")
        metric = report["measures"]["left"]["amount"]
        expected = sum(Fraction(Decimal(value)) for value in values)
        assert Fraction(Decimal(metric["source_sum"])) == expected
        assert Fraction(Decimal(metric["predicted_join_sum"])) == expected * 3
        assert Fraction(Decimal(metric["added_by_repetition"])) == expected * 2
        assert Fraction(Decimal(report["top_keys"][0]["ranking_measure_added"])) == expected * 2
