from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_reading_javascript_contracts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    test_file = Path(__file__).parent / "js" / "reading_modules.test.cjs"

    completed = subprocess.run(
        [node, str(test_file)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "JavaScript tests passed" in completed.stdout
