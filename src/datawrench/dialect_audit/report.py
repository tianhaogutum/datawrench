from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

from .sarif import render_sarif
from .schema import InputError


def failing(items: list[dict], policy: str) -> bool:
    levels = {'never': set(), 'error': {'error'}, 'warn': {'error', 'warn'}}[policy]
    return any(item['severity'] in levels for item in items)


def exit_code(report: dict, policy: str) -> int:
    items = report['notices'] + [d for stmt in report['statements'] for d in stmt['diagnostics']]
    return int(failing(items, policy))


def _xml_text(text: str) -> str:
    # XML 1.0 cannot represent all characters legal in SQL string literals.
    return ''.join(c if ord(c) in (9, 10, 13) or 32 <= ord(c) <= 0xD7FF or
                   0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF else '\uFFFD' for c in text)


def format_location(stmt: dict) -> str:
    lines = f"L{stmt['line_start']}-{stmt['line_end']}"
    if stmt.get('column_start') is None or stmt.get('column_end') is None:
        return lines + ' (columns unavailable)'
    return lines + f" C{stmt['column_start']}-{stmt['column_end']}"


def diagnostic_label(item: dict) -> str:
    location = item.get('location')
    return f" [{format_location(location)}]" if location is not None else ''


def related_details(item: dict) -> list[str]:
    lines = []
    for index, related in enumerate(item.get('related_locations', []), 1):
        location = related['location']
        label = format_location(location) if location is not None else 'location unavailable'
        description = related['description']
        lines.append(f"related #{index} [{label}]: " + (
            description if description is not None else 'description unavailable'))
    return lines


def diagnostic_text(item: dict) -> str:
    return (f"{item['category']}{diagnostic_label(item)}: {item['message']}"
            + ''.join('\n  ' + line for line in related_details(item)))


def render(report: dict, format: str, policy: str = 'error') -> str:
    if format == 'sarif':
        return render_sarif(report, policy)
    if format == 'json':
        return json.dumps(report, indent=2, ensure_ascii=True) + '\n'
    if format == 'junit':
        cases = []
        for stmt in report['statements']:
            cases.append((stmt['file'], f"statement-{stmt['index']}:{format_location(stmt)}", stmt['diagnostics'], stmt))
        for notice in report['notices']:
            cases.append((notice['file'], notice['category'], [notice], None))
        failures = sum(failing(items, policy) for _, _, items, _ in cases)
        skipped = sum(stmt is None and not failing(items, policy) for _, _, items, stmt in cases)
        suite = ET.Element('testsuite', name='dialect-audit', tests=str(len(cases)), failures=str(failures), errors='0', skipped=str(skipped))
        properties = ET.SubElement(suite, 'properties')
        for key in ('read', 'write', 'binding', 'function_inventory', 'file_function_inventory', 'file_summaries'):
            value = report[key]
            ET.SubElement(properties, 'property', name=key, value=_xml_text(
                json.dumps(value, sort_keys=True, ensure_ascii=True) if isinstance(value, dict) else value))
        for file, name, items, stmt in cases:
            case = ET.SubElement(suite, 'testcase', classname=_xml_text(file), name=_xml_text(name))
            detail = '\n'.join(diagnostic_text(d) for d in items)
            if failing(items, policy):
                ET.SubElement(case, 'failure', message=', '.join(d['category'] for d in items)).text = _xml_text(detail)
            elif stmt is None:
                ET.SubElement(case, 'skipped', message=_xml_text(detail))
            ET.SubElement(case, 'system-out').text = _xml_text(json.dumps(stmt or items, ensure_ascii=True))
        return ET.tostring(suite, encoding='unicode') + '\n'
    summary = report['summary']
    footer = (f"{summary['statements']} statements, {summary['ok']} OK, {summary['flagged']} flagged; "
              f"{summary['errors']} errors, {summary['warnings']} warnings, {summary['skipped_files']} skipped files")
    lines = [f"dialect-audit: {report['read']} → {report['write']} (binding: {report['binding']})", '']
    if format == 'md':
        lines += ['| Statement | Location | Categories | Binding | Details |', '| --- | --- | --- | --- | --- |']
        def cell(value):
            return html.escape(str(value)).replace('|', '\\|').replace('\n', '<br>')
        for stmt in report['statements']:
            detail = '; '.join(diagnostic_text(d) for d in stmt['diagnostics'])
            lines.append('| ' + ' | '.join(cell(x) for x in [f"{stmt['file']}#{stmt['index']}",
                         format_location(stmt), ', '.join(stmt['categories']),
                         f"binding: {stmt['binding_status']}", detail]) + ' |')
    else:
        for stmt in report['statements']:
            lines.append(f"{stmt['file']}#{stmt['index']} {format_location(stmt)} {', '.join(stmt['categories'])} "
                         f"(binding: {stmt['binding_status']})")
            lines.extend('  ' + diagnostic_text(d) for d in stmt['diagnostics'])
    for stmt in report['statements']:
        for stub in stmt['auto_stubs']:
            value = f"Auto-stub {stub['table']}: {', '.join(stub['columns'])} ({stub['type']}; inferred, unverified types)"
            lines.append(html.escape(value) if format == 'md' else value)
    for notice in report['notices']:
        value = f"{notice['file']}: {notice['category']}: {notice['message']}"
        lines.append(html.escape(value) if format == 'md' else value)
    lines += ['', 'Files:']
    if format == 'md':
        lines += ['', '| File | Status | Statements | OK | Flagged | Errors | Warnings | Skipped | Binding counts |',
                  '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for file, counts in report['file_summaries'].items():
        bindings = ', '.join(f'{key}={value}' for key, value in counts['binding_statuses'].items()) or 'none'
        if format == 'md':
            lines.append('| ' + ' | '.join(cell(x) for x in [
                file, counts['status'].upper(), counts['statements'], counts['ok'], counts['flagged'],
                counts['errors'], counts['warnings'], 'yes' if counts['skipped'] else 'no', bindings]) + ' |')
        else:
            lines.append(f"{file}: {counts['status'].upper()} — {counts['statements']} statements, "
                         f"{counts['ok']} OK, {counts['flagged']} flagged; {counts['errors']} errors, "
                         f"{counts['warnings']} warnings; skipped: {'yes' if counts['skipped'] else 'no'}; "
                         f"binding counts: {bindings}")
    overall = 'Functions overall: ' + json.dumps(report['function_inventory'], sort_keys=True)
    lines += ['', footer, '', html.escape(overall) if format == 'md' else overall]
    for file, inventory in report['file_function_inventory'].items():
        value = f'Functions {file}: ' + json.dumps(inventory, sort_keys=True)
        lines.append(html.escape(value) if format == 'md' else value)
    return '\n'.join(lines) + '\n'


def emit(report: dict, directory: str) -> None:
    root = Path(directory).absolute()
    if root.is_symlink():
        raise InputError('emit directory must not be a symlink')
    root.mkdir(parents=True, exist_ok=True)
    payloads = {}
    mapping = []
    for stmt in report['statements']:
        if stmt['target_sql'] is not None:
            digest = hashlib.sha256(stmt['file'].encode()).hexdigest()[:16]
            filename = f'{digest}-{stmt["index"]:04d}.sql'
            payloads[filename] = stmt['target_sql'] + ';\n'
            mapping.append({'output': filename, 'file': stmt['file'], 'index': stmt['index']})
    report['emitted_files'] = mapping
    payloads['report.json'] = render(report, 'json')
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in payloads:
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise InputError('emit directory contains a report/output with the same name; use a fresh directory')
        for name, content in payloads.items():
            # Bind creation to the open directory, including if its path is renamed.
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(content)
    finally:
        os.close(directory_fd)
