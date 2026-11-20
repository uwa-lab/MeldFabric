from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_default_install_runs_agents_without_optional_adapters(tmp_path) -> None:
    script = Path(__file__).parent / "support" / "core_install_smoke.py"
    environment = dict(os.environ)
    environment.pop("MELDFABRIC_MASTER_KEY", None)
    result = subprocess.run(
        [sys.executable, str(script.resolve())],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "CORE_INSTALL_OK" in result.stdout
