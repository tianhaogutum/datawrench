from pathlib import Path
import shutil

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def project(tmp_path, monkeypatch):
    target = tmp_path / "project"
    shutil.copytree(FIXTURES / "ansi", target)
    monkeypatch.chdir(target)
    return target
