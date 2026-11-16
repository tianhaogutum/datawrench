#!/usr/bin/env python3
"""Opt-in regeneration from a local, synthetic dbt project. Never used by tests."""
import json
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent  # tests/contract_diff
BUILD = HERE / '.fixture-build'


def main():
    if BUILD.exists():
        raise SystemExit('Remove .fixture-build before regeneration (refusing to overwrite).')
    shutil.copytree(HERE / 'fixture_project', BUILD, ignore=shutil.ignore_patterns('target', 'logs'))
    env = {**os.environ, 'DBT_SEND_ANONYMOUS_USAGE_STATS': 'false', 'DBT_USE_COLORS': 'false'}
    dbt = Path(os.sys.executable).parent / 'dbt'
    schema = BUILD / 'models/schema.yml'
    for label in ('base', 'head'):
        if label == 'head':
            schema.write_text(schema.read_text().replace('amount DECIMAL(10,2)', 'amount VARCHAR'))
        subprocess.run([str(dbt), 'parse', '--no-partial-parse', '--project-dir', str(BUILD),
                        '--profiles-dir', str(BUILD)], env=env, cwd=BUILD, check=True)
        artifact = json.loads((BUILD / 'target/manifest.json').read_text())
        # A genuine generated model-node slice, excluding dependency macros and
        # unrelated dbt resources. No upstream source code is redistributed.
        artifact = {key: artifact[key] for key in ('metadata', 'nodes', 'disabled')}
        for field in ('generated_at', 'invocation_started_at', 'run_started_at'):
            if field in artifact['metadata']:
                artifact['metadata'][field] = '2026-11-11T00:00:00Z'
        artifact['metadata']['invocation_id'] = '00000000-0000-0000-0000-000000000000'
        artifact['metadata']['user_id'] = None
        artifact['metadata']['env'] = {}
        for n in artifact['nodes'].values():
            n.pop('created_at', None)
        out = HERE / 'fixtures/real' / f'{label}.json'
        out.write_text(json.dumps(artifact, indent=2) + '\n')
    print('Generated tests/contract_diff/fixtures/real/{base,head}.json; dbt parse only, no warehouse access.')


if __name__ == '__main__':
    main()
