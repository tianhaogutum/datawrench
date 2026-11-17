import pytest


# Optional `pandera` extra (pandas, Pandera and dfshrink, which needs Python 3.12+);
# skip this module cleanly when it is not installed.
pytest.importorskip("pandas")
pytest.importorskip("pandera")
pytest.importorskip("dfshrink")

from datawrench.failurepack.cli import main
from datawrench.failurepack.validator import read_options, validate_options


def test_deep_parser_json_is_reported_without_a_traceback(tmp_path, capsys):
    options = tmp_path / "deep.json"
    options.write_text("[" * 1500 + "0" + "]" * 1500)
    assert (
        main(["list", "missing.csv", "--schema", "missing.yaml", "--read-options", str(options)])
        == 2
    )
    assert "failurepack:" in capsys.readouterr().err


def test_oversized_parser_config_is_rejected(tmp_path):
    options = tmp_path / "huge.json"
    options.write_text('{"dtype": "' + "x" * (1024 * 1024) + '"}')
    with pytest.raises(ValueError, match="1 MiB"):
        read_options(options)


def test_non_json_api_options_are_clean_errors():
    with pytest.raises(ValueError):
        validate_options({"dtype": object()})
    with pytest.raises(ValueError):
        validate_options({"decimal": float("nan")})
    nested = []
    nested.append(nested)
    with pytest.raises(ValueError):
        validate_options({"na_values": nested})
