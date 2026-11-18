from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("board_js_check.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не встановлено")
def test_board_page_script_works_on_fake_dom():
    result = subprocess.run(
        ["node", str(SCRIPT)], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
