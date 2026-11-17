"""Bounded, terminal-safe evidence using Python's Unicode database and diff."""

from __future__ import annotations

import difflib
import json
import unicodedata

TEXT_LIMIT = 160
DIFF_LIMIT = 512
CODEPOINT_LIMIT = 32
EDIT_LIMIT = 32
DETAIL_BUDGET_BYTES = 4 * 1024 * 1024


class EvidenceBudget:
    """Bound the complete sampled report, including user-raised sample limits."""

    def __init__(self, limit: int = DETAIL_BUDGET_BYTES):
        self.limit = limit
        self.used = 0
        self.exhausted = False

    def include(self, value: dict) -> bool:
        size = len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
        if self.used + size > self.limit:
            self.exhausted = True
            return False
        self.used += size
        return True


def describe_text(value: str) -> dict:
    return {
        "text": value[:TEXT_LIMIT],
        "escaped": value[:TEXT_LIMIT].encode("unicode_escape").decode("ascii"),
        "length": len(value),
        "omitted_characters": max(0, len(value) - TEXT_LIMIT),
    }


def codepoints(value: str, start: int = 0) -> list[dict]:
    return [
        {
            "index": start + i,
            "codepoint": f"U+{ord(c):04X}",
            "name": unicodedata.name(c, "<unnamed>"),
            "category": unicodedata.category(c),
        }
        for i, c in enumerate(value[:CODEPOINT_LIMIT])
    ]


def describe_change(before: str, after: str) -> dict:
    result = {"before": describe_text(before), "after": describe_text(after)}
    prefix = 0
    common_limit = min(len(before), len(after))
    while prefix < common_limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < common_limit - prefix
        and before[len(before) - suffix - 1] == after[len(after) - suffix - 1]
    ):
        suffix += 1
    before_end, after_end = len(before) - suffix, len(after) - suffix
    if max(before_end - prefix, after_end - prefix) > DIFF_LIMIT:
        result["diff_omitted"] = (
            "unmatched span exceeds 512 code points; avoids quadratic diff work"
        )
        return result
    # Leave diffing to difflib, but remove known equal context first. Repetitive
    # long identifiers otherwise trigger quadratic work for a one-letter edit.
    matcher = difflib.SequenceMatcher(
        None, before[prefix:before_end], after[prefix:after_end], autojunk=False
    )
    edits = []
    omitted = 0
    for tag, i, j, a, b in matcher.get_opcodes():
        if tag != "equal":
            i, j, a, b = i + prefix, j + prefix, a + prefix, b + prefix
            if len(edits) == EDIT_LIMIT:
                omitted += 1
                continue
            edits.append(
                {
                    "operation": tag,
                    "before_range": [i, j],
                    "after_range": [a, b],
                    "removed": codepoints(before[i:j], i),
                    "inserted": codepoints(after[a:b], a),
                    "omitted_removed_codepoints": max(0, j - i - CODEPOINT_LIMIT),
                    "omitted_inserted_codepoints": max(0, b - a - CODEPOINT_LIMIT),
                }
            )
    result["edits"] = edits
    result["omitted_edit_operations"] = omitted
    return result


def describe_key(key: list[str] | tuple[str, ...]) -> list[dict]:
    return [describe_text(v) for v in key]


def key_changes(before: list[str], after: list[str], columns: list[str]) -> list[dict]:
    return [
        {"column": name, **describe_change(old, new)}
        for name, old, new in zip(columns, before, after)
        if old != new
    ]
