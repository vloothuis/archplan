"""Application-facing project facade, including optional model enrichment."""

from __future__ import annotations

from pathlib import Path

from .core.project import *  # noqa: F401,F403
from .core.project import Project as _Project
from .core.project import init as _init


class Project(_Project):
    """Compatibility project that enriches exports with optional analyses."""

    def export(self, timeout: float = 600, require_trust: bool = False) -> dict:
        counts = super().export(timeout=timeout, require_trust=require_trust)
        if not read_json(self.model_path, {}).get("vocabulary"):
            from .vocab import enrich as vocabulary_enrich
            vocabulary_enrich(self)
        from . import schema
        try:
            schema.enrich(self)
        except Exception as e:  # noqa: BLE001 - schema is optional enrichment
            import sys
            sys.stderr.write(f"archplan: schema step failed: {e!r}\n")
        return counts


def init(root: Path, export: str | None = None, port: int | None = None, force: bool = False) -> Project:
    """Initialize through core, then return the application-facing facade."""
    initialized = _init(root, export=export, port=port, force=force)
    project = Project(initialized.root)
    project.found = initialized.found
    return project
