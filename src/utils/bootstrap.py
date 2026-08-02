"""Runtime helpers for script-style execution (``python src/.../*.py``).

Ensures the repository ``src/`` directory is on ``sys.path`` so modules can import
``utils.*`` and package siblings without installing the project as a package.
"""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_src_on_path(file: str) -> None:
    """Ensure repo src/ is on sys.path for script-style execution."""
    resolved = Path(file).resolve()
    src_dir = next(
        (
            parent / "src"
            for parent in resolved.parents
            if (parent / "src" / "utils" / "bootstrap.py").exists()
        ),
        resolved.parents[1],
    )
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
