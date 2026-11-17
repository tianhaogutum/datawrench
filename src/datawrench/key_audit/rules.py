"""Small, explicit transformations; no inferred policy or heuristic repairs."""

from __future__ import annotations

import importlib.metadata
import platform
import sys
import unicodedata
from dataclasses import dataclass
from typing import Callable, Sequence

from ftfy.fixes import fix_character_width


@dataclass(frozen=True)
class Rule:
    description: str
    transform: Callable[[str], str]


RULES = {
    "nfc": Rule("Canonical Unicode composition (NFC).", lambda s: unicodedata.normalize("NFC", s)),
    "nfkc": Rule(
        "Compatibility composition (NFKC); may erase meaningful distinctions.",
        lambda s: unicodedata.normalize("NFKC", s),
    ),
    "width": Rule(
        "ftfy character-width conversion, including U+3000 to ASCII space.", fix_character_width
    ),
    "strip": Rule("Remove leading and trailing Python Unicode whitespace.", str.strip),
    "collapse-whitespace": Rule(
        "Collapse Unicode whitespace to one ASCII space and trim its edges.",
        lambda s: " ".join(s.split()),
    ),
    "casefold": Rule("Unicode full case folding; locale independent.", str.casefold),
    "remove-zwsp": Rule("Remove only U+200B ZERO WIDTH SPACE.", lambda s: s.replace("\u200b", "")),
    "remove-bom": Rule(
        "Remove only U+FEFF anywhere inside a selected key.", lambda s: s.replace("\ufeff", "")
    ),
}


def validate_rules(names: Sequence[str], *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(names, (list, tuple)) or not all(isinstance(n, str) for n in names):
        raise ValueError("rules must be a list of rule names")
    if not names and not allow_empty:
        raise ValueError("select at least one explicit rule; use 'datawrench key-audit rules' to list them")
    if len(set(names)) != len(names):
        raise ValueError("rule names must not repeat")
    unknown = [n for n in names if n not in RULES]
    if unknown:
        raise ValueError(f"unknown rules: {unknown!r}")
    return tuple(names)


def transform_key(key: Sequence[str], rules: Sequence[str]) -> tuple[str, ...]:
    result = tuple(key)
    for name in rules:
        result = tuple(RULES[name].transform(s) for s in result)
    return result


def engine_identity() -> dict:
    # Python's string whitespace/casefold tables also depend on the interpreter.
    return {
        "rule_contract": 1,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "implementation": platform.python_implementation(),
        "unicode": unicodedata.unidata_version,
        "ftfy": importlib.metadata.version("ftfy"),
    }
