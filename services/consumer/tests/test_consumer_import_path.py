"""Regression test for direct consumer-script imports.

Revision history:
  2026-08-24  Verify the consumer can load its sibling insights package when
              PYTHONPATH is unset, matching the production systemd invocation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_consumer_script_imports_insights_without_pythonpath() -> None:
    """Load consumer.py as a direct script without the CI module path."""
    repo_root = Path(__file__).parents[3]
    script = repo_root / "services" / "consumer" / "consumer.py"
    consumer_dir = script.parent
    code = "\n".join(
        [
            "import runpy",
            "import sys",
            f"sys.path.insert(0, {str(consumer_dir)!r})",
            f"runpy.run_path({str(script)!r}, run_name='consumer_import_smoke')",
        ]
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
