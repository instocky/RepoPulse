"""Enforce the 00-architecture doctrine.

Walks src/repo_pulse/ and asserts that the forbidden cross-layer imports
listed in `.scratch/repo-pulse/issues/00-architecture.md` are absent.

The test is the regression net for the doctrine: if a developer adds a
forbidden import, the test fails with a precise location and direction.
The regression tests at the bottom of this file are themselves a
regression guard — they prove the enforcer can actually catch a
violation, including the relative-import form `from ..db import X` and
the direct-submodule forms `from repo_pulse import db` and
`from .. import db`, both of which previously slipped past the enforcer
because only `node.module` was inspected, not `node.names`.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "repo_pulse"

# Forbidden (source_layer, target_layer) pairs. Mirror of the matrix in
# 00-architecture.md — if you change one, change the other.
FORBIDDEN_PAIRS: frozenset[tuple[str, str]] = frozenset({
    ("web", "db"),
    ("web", "gh"),
    ("analytics", "gh"),
    ("analytics", "web"),
    ("charts", "db"),
    ("charts", "web"),
    ("charts", "analytics"),
    ("collector", "analytics"),
    ("db", "gh"),
})
# All known top-level subpackages of `repo_pulse` (i.e. the layers). Used to
# detect direct-submodule imports like `from repo_pulse import db` and
# `from .. import db`, where the imported name is itself a layer.
LAYERS: frozenset[str] = frozenset({
    "collector", "db", "analytics", "charts", "web", "config", "lock", "gh",
})
# `lock` is a leaf primitive — only `os` and `pathlib` may be imported.
# `__future__` is a Python-language feature, not a runtime dependency.
# The doctrine documents the carve-out.
LOCK_ALLOWED: frozenset[str] = frozenset({"os", "pathlib", "__future__"})


def _resolve_target(module: str, name: str, level: int) -> str | None:
    """Resolve an `ast.ImportFrom`-style source module (node.module) to its
    absolute target name.

    For `from X import ...` we treat `X` as the module being imported from.
    Returns None for relative imports that walk above the top of the
    `repo_pulse` package (the runtime would error on those anyway).
    """
    if level == 0:
        return name
    parts = module.split(".")
    # `from .X` in `repo_pulse.web.routes` resolves to `repo_pulse.web.X`;
    # `from ..X` resolves to `repo_pulse.X`. Formula: base = parts[:len - level].
    base = parts[: len(parts) - level]
    if not base:
        return None
    return ".".join(base + name.split("."))


def _resolve_submodule(
    module: str, source: str | None, name: str, level: int
) -> str | None:
    """Resolve `from <source> import <name>` to the absolute module path
    of the imported submodule.

    For absolute imports (level=0): `from X import Y` resolves to `X.Y`.
    For relative imports (level>0): the base is `parts[:len(module) - level]`,
    then either `source` is appended (for `from .X import Y`) or `name` is
    appended directly (for `from . import Y`).

    Examples (current module = `repo_pulse.web.routes`):
        from .. import db           -> "repo_pulse.db"          (level=2, source=None)
        from . import db            -> "repo_pulse.web.db"      (level=1, source=None)
        from repo_pulse import db   -> "repo_pulse.db"          (level=0, source="repo_pulse")
        from .analytics import compute
                                    -> "repo_pulse.web.analytics.compute"
                                                                   (level=1, source="analytics")
    """
    if level == 0:
        if source:
            return f"{source}.{name}"
        return name
    parts = module.split(".")
    base = parts[: len(parts) - level]
    if not base:
        return None
    if source:
        return ".".join(base + source.split(".") + [name])
    return ".".join(base + [name])


def scan(src_root: Path) -> list[str]:
    """Return one human-readable violation per forbidden import, or []."""
    if not src_root.exists():
        return []
    out: list[str] = []
    for py in sorted(src_root.rglob("*.py")):
        rel = py.relative_to(src_root.parent)
        parts = rel.with_suffix("").parts  # ('repo_pulse', '<layer>', '<name>')
        if len(parts) < 3:
            continue
        layer = parts[1]
        module = (
            "repo_pulse."
            + ".".join(parts[1:]).removesuffix(".__init__")
        )
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _check_target(out, module, layer, alias.name)
            elif isinstance(node, ast.ImportFrom):
                _check_import_from(out, module, layer, node)
    return out


def _check_import_from(
    out: list[str], module: str, layer: str, node: ast.ImportFrom
) -> None:
    """Enforce the doctrine on a single `from X import Y[, Z, ...]` statement.

    Two independent detection paths:
      1. The source module (node.module) may itself point to a forbidden
         layer. This covers `from repo_pulse.db import conn` and the
         relative form `from ..db import conn`.
      2. An imported name (in node.names) may be a known layer, meaning
         `from X import <layer>` pulls the layer's submodule in directly.
         This covers `from repo_pulse import db` and `from .. import db`,
         which previously slipped past the enforcer.
    """
    # Path 1: the source module is the thing being imported from.
    if node.module:
        target = _resolve_target(module, node.module, node.level)
        if target is not None:
            _check_target(out, module, layer, target)
    # Path 2: an imported name is itself a known layer (direct submodule import).
    for alias in node.names:
        name = alias.name
        if name in LAYERS:
            target = _resolve_submodule(module, node.module, name, node.level)
            if target is not None:
                _check_target(out, module, layer, target)


def _check_target(out: list[str], module: str, layer: str, target: str) -> None:
    """If `target` represents a forbidden layer access from `layer`, append."""
    target_parts = target.split(".")
    top = target_parts[0]
    if layer == "lock":
        # lock is a leaf primitive: no other repo_pulse submodules, and only
        # the stdlib carve-out (plus __future__) may be imported.
        if top == "repo_pulse":
            out.append(f"{module}: lock may not import {target!r} (doctrine 00)")
        elif top not in LOCK_ALLOWED:
            out.append(f"{module}: lock may not import {top!r} (doctrine 00)")
        return
    if (
        top == "repo_pulse"
        and len(target_parts) >= 2
        and (layer, target_parts[1]) in FORBIDDEN_PAIRS
    ):
        out.append(
            f"{module}: forbidden {layer!r} -> {target_parts[1]!r} (doctrine 00)"
        )


def test_no_forbidden_layer_imports() -> None:
    """src/repo_pulse/ must respect 00-architecture.md."""
    if not SRC_ROOT.exists():
        pytest.skip("src/repo_pulse/ not scaffolded yet (ticket 01)")
    violations = scan(SRC_ROOT)
    if violations:
        pytest.fail(
            "Doctrine violations (see 00-architecture.md):\n  "
            + "\n  ".join(violations)
        )


def _planted_violation(
    src_root: Path, layer: str, code: str
) -> list[str]:
    """Plant `code` in src_root/<layer>/routes.py and run the enforcer."""
    layer_dir = src_root / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    (layer_dir / "__init__.py").write_text("", encoding="utf-8")
    (layer_dir / "routes.py").write_text(code, encoding="utf-8")
    return scan(src_root)


def test_enforcer_catches_absolute_violation(tmp_path: Path) -> None:
    """Regression net: an absolute `web -> db` import must trip the enforcer."""
    fake_pkg = tmp_path / "src" / "repo_pulse"
    violations = _planted_violation(
        fake_pkg, "web", "from repo_pulse.db import conn\n"
    )
    assert any("'web'" in v and "'db'" in v for v in violations), (
        "regression net (absolute) failed; got: " + repr(violations)
    )


def test_enforcer_catches_relative_violation(tmp_path: Path) -> None:
    """Regression net: `from ..db import conn` from `web/routes.py` must trip.

    The earlier enforcer early-returned on `level > 0` and missed this case.
    """
    fake_pkg = tmp_path / "src" / "repo_pulse"
    violations = _planted_violation(
        fake_pkg, "web", "from ..db import conn\n"
    )
    assert any("'web'" in v and "'db'" in v for v in violations), (
        "regression net (relative) failed; got: " + repr(violations)
    )


def test_enforcer_catches_from_pkg_import_layer(tmp_path: Path) -> None:
    """Regression net: `from repo_pulse import db` from `web/routes.py` must trip.

    The earlier enforcer only inspected `node.module` and never looked at
    `node.names`, so this direct-submodule import slipped through. The
    resolved target is `repo_pulse.db`, which is a forbidden layer from web.
    """
    fake_pkg = tmp_path / "src" / "repo_pulse"
    violations = _planted_violation(
        fake_pkg, "web", "from repo_pulse import db\n"
    )
    assert any("'web'" in v and "'db'" in v for v in violations), (
        "regression net (from-pkg-import-layer) failed; got: " + repr(violations)
    )


def test_enforcer_catches_relative_import_layer_only(tmp_path: Path) -> None:
    """Regression net: `from .. import db` from `web/routes.py` must trip.

    Combines a relative import (level=2) with a name-only submodule
    reference. The earlier enforcer never reached the name-level check
    because `node.module` was empty and the old `_check` early-returned on
    an empty name.
    """
    fake_pkg = tmp_path / "src" / "repo_pulse"
    violations = _planted_violation(
        fake_pkg, "web", "from .. import db\n"
    )
    assert any("'web'" in v and "'db'" in v for v in violations), (
        "regression net (relative-import-layer-only) failed; got: "
        + repr(violations)
    )


def test_enforcer_catches_lock_violation(tmp_path: Path) -> None:
    """Regression net: lock may not import arbitrary stdlib (e.g. sys)."""
    fake_pkg = tmp_path / "src" / "repo_pulse"
    violations = _planted_violation(fake_pkg, "lock", "import sys\n")
    assert any("lock" in v and "'sys'" in v for v in violations), (
        "regression net (lock) failed; got: " + repr(violations)
    )
