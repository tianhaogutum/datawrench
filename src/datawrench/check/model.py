"""Finding records shared by every check and every output format."""
from __future__ import annotations

from dataclasses import dataclass

ERROR = "error"
WARNING = "warning"
INFO = "info"
# Most severe first; the index doubles as the sort key.
SEVERITIES = (ERROR, WARNING, INFO)
RANK = {severity: index for index, severity in enumerate(SEVERITIES)}


@dataclass(frozen=True)
class Finding:
    """One problem (or notice) about one input.

    ``file`` is the display path of the input (a SQL or Python file, a dbt
    project folder, a Parquet dataset folder, a manifest); it is ``None`` only
    for run-level notices. ``line`` is a 1-based line number; ``pointer`` a
    location inside the input (a JSON Pointer such as ``/nodes/model.shop.orders``
    or a column path); either may be absent. ``subject`` names what the finding
    is about: a model, a seed column, a stream, a Parquet column. Messages never
    quote record values, CSV fields or credentials from the inputs.
    """

    check: str
    rule: str
    severity: str
    message: str
    file: str | None = None
    line: int | None = None
    pointer: str | None = None
    subject: str | None = None
    hint: str | None = None

    def __post_init__(self):
        if self.severity not in RANK:
            raise ValueError(f"unknown severity {self.severity!r}")

    @property
    def rule_id(self) -> str:
        """Stable, globally unique rule identifier, e.g. ``polars-vet/PLV004``."""
        return f"{self.check}/{self.rule}"

    def sort_key(self):
        return (self.file is None, self.file or "", RANK[self.severity], self.check, self.rule,
                self.line or 0, self.pointer or "", self.subject or "", self.message)

    def to_dict(self) -> dict:
        return {
            "check": self.check, "rule": self.rule, "ruleId": self.rule_id,
            "severity": self.severity, "message": self.message, "file": self.file,
            "line": self.line, "pointer": self.pointer, "subject": self.subject, "hint": self.hint,
        }


def counts(findings) -> dict[str, int]:
    result = dict.fromkeys(SEVERITIES, 0)
    for finding in findings:
        result[finding.severity] += 1
    return result


def at_or_above(findings, threshold: str | None) -> bool:
    """True if any finding is at least as severe as ``threshold`` (None: never)."""
    if threshold is None:
        return False
    limit = RANK[threshold]
    return any(RANK[finding.severity] <= limit for finding in findings)
