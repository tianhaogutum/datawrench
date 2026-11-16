"""Offline structural validation using the packaged current diff-v1 schema."""
from functools import lru_cache
from importlib.resources import files
import json
from pathlib import Path
import sys

from jsonschema import Draft7Validator

from .validation import ManifestError, read_json_artifact, read_json_stream


@lru_cache(maxsize=1)
def diff_schema_validator() -> Draft7Validator:
    schema = json.loads(files('datawrench.sqlmesh_manifest').joinpath('diff-v1.json').read_text(encoding='utf-8'))
    return Draft7Validator(schema)


def validate_diff(path: str | Path) -> None:
    """Check a saved report (literal string '-' reads stdin); shape only."""
    # Test before Path normalization: './-' explicitly names a regular file.
    report, _ = (read_json_stream(sys.stdin.buffer, 'diff report') if path == '-'
                 else read_json_artifact(path, 'diff report'))
    if not diff_schema_validator().is_valid(report):
        raise ManifestError('Invalid or unsupported semantic diff report')


def diff_validation_result(path: str | Path) -> dict:
    """Return a fixed result without exposing input or exception details.

    Like text mode, all failures share one diagnostic, including unexpected
    failures. No counts or input-derived fields imply semantic validation.
    """
    try:
        validate_diff(path)
    except Exception:
        return {'diff_validation_version': 1, 'valid': False,
                'diagnostics': [{'path': '$', 'rule': 'invalid_diff'}]}
    return {'diff_validation_version': 1, 'valid': True, 'diagnostics': []}
