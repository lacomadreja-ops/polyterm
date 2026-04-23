"""Centralized paths for PolyTerm runtime files.

Default runtime directory is `./logs` (project-local) for convenience, but we
fall back to `/logs` and finally the user's home directory if needed.

Override with env var: POLYTERM_DIR
"""

from __future__ import annotations

import os
from pathlib import Path


def get_polyterm_dir() -> Path:
    """Return writable runtime directory for logs/db/config."""
    override = os.getenv("POLYTERM_DIR", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))

    # Default: project-local logs/ (what most users expect)
    candidates.append(Path.cwd() / "logs")

    # Alternative: system-wide /logs (requires permissions)
    candidates.append(Path("/logs"))

    # Safe fallback
    candidates.append(Path.home() / ".polyterm")

    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            test = p / ".write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
            return p
        except Exception:
            continue

    # Last resort: current working directory
    p = Path.cwd() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def polyterm_path(*parts: str) -> Path:
    """Convenience to build a path under runtime directory."""
    return get_polyterm_dir().joinpath(*parts)

