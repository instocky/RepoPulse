"""Repo-Pulse: a personal daily pulse monitor for starred GitHub repositories.

See ``docs/SPEC.md`` for the full specification. This package is split into
the layers described in ``.scratch/repo-pulse/issues/00-architecture.md``:
``collector``, ``db``, ``analytics``, ``charts``, ``web``, plus the leaf
primitives ``config``, ``lock``, and ``gh``. The boundary rules between
those layers are enforced by ``tests/test_architecture.py``.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
