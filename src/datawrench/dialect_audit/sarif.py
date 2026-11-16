"""SARIF presentation of current-run evidence; no source reads or inferred spans."""
from __future__ import annotations

import json
from pathlib import Path

from . import __version__

SCHEMA_URI = 'https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json'
MESSAGE_LIMIT = 4096
LEVELS = {'error': 'error', 'warn': 'warning'}


def _message(text: str) -> dict:
    if len(text) > MESSAGE_LIMIT:
        text = text[:MESSAGE_LIMIT - 1] + '…'
    return {'text': text}


def _artifact_location(file: str) -> dict:
    # absolute/as_uri escape URI metacharacters without reading/resolving the file.
    return {'uri': Path(file).absolute().as_uri()}


def _location(file: str, span: dict | None) -> dict:
    physical = {'artifactLocation': _artifact_location(file)}
    if span is not None:
        physical['region'] = {
            'startLine': span['line_start'], 'startColumn': span['column_start'],
            'endLine': span['line_end'], 'endColumn': span['column_end'] + 1,
        }
    return {'physicalLocation': physical}


def render_sarif(report: dict, policy: str) -> str:
    items = [(stmt['file'], stmt['index'], diagnostic) for stmt in report['statements']
             for diagnostic in stmt['diagnostics']]
    items.extend((notice['file'], None, notice) for notice in report['notices'])
    codes = sorted({item['category'] for _, _, item in items})
    indices = {code: index for index, code in enumerate(codes)}
    results = []
    for file, statement_index, item in items:
        result = {
            'ruleId': item['category'], 'ruleIndex': indices[item['category']],
            'level': LEVELS[item['severity']], 'message': _message(item['message']),
            'locations': [_location(file, item.get('location'))],
        }
        if statement_index is not None:
            result['properties'] = {'statementIndex': statement_index}
        if item.get('related_locations'):
            related = []
            for index, entry in enumerate(item['related_locations'], 1):
                location = _location(file, entry['location'])
                location.update(id=index, message=_message(entry['description'] or 'description unavailable'))
                related.append(location)
            result['relatedLocations'] = related
        results.append(result)
    run = {
        'tool': {'driver': {'name': 'dialect-audit', 'version': __version__,
                            'rules': [{'id': code} for code in codes]}},
        'columnKind': 'unicodeCodePoints', 'results': results,
        'artifacts': [{'location': _artifact_location(file)} for file in sorted(report['file_summaries'])],
        'properties': {
            'read': report['read'], 'write': report['write'], 'binding': report['binding'],
            'failOn': policy,
            'summary': {key: report['summary'][key] for key in (
                'files', 'statements', 'ok', 'flagged', 'errors', 'warnings', 'skipped_files')},
        },
    }
    return json.dumps({'$schema': SCHEMA_URI, 'version': '2.1.0', 'runs': [run]},
                      indent=2, ensure_ascii=True) + '\n'
