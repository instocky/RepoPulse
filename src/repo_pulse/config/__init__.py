"""Configuration loading for Repo-Pulse.

The contract lives in ``.scratch/repo-pulse/issues/03-config.md`` and
``docs/SPEC.md §Configurability``. This is a leaf layer: it reads
``os.environ`` + ``config.toml`` and produces a typed ``Config``; it
never imports any other ``repo_pulse`` submodule.
"""
from __future__ import annotations

from repo_pulse.config.loader import Config, ConfigError, load_config

__all__ = ["Config", "ConfigError", "load_config"]
