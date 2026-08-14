from __future__ import annotations

import subprocess
import sys


def test_python_module_cli_help_is_available():
    result = subprocess.run(
        [sys.executable, "-m", "zoofan", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "build-acceptance-report" in result.stdout
