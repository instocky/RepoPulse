"""Enforce the 00-architecture doctrine.

Walks src/repo_pulse/ and asserts that the forbidden cross-layer imports
listed in `.scratch/repo-pulse/issues/00-architecture.md` are absent.

The test is the regression net for the doctrine: if a developer adds a
forbidden import, the test fails with a precise location and direction.
The regression tests at the bottom of this file are themselves a
regression guard — they prove the enforcer can actually catch a
violation, including the relative-import form `from ..db import X`.
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
# `lock` is a leaf primitive — only `os` and `pathlib` may be imported.
# `__future__` is a Python-language feature, not a runtime dependency.
# The doctrine documents the carve-out.
LOCK_ALLOWED: frozenset[str] = frozenset({"os", "pathlib", "__future__"})


def _resolve_target(module: str, name: str, level: int) -> str | None:
    """Resolve an import statement to its absolute target module name.

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
                    _check(out, module, layer, alias.name, 0)
            elif isinstance(node, ast.ImportFrom):
                _check(out, module, layer, node.module or "", node.level)
    return out


def _check(out: list[str], module: str, layer: str, name: str, level: int) -> None:
    if not name:
        return
    target = _resolve_target(module, name, level)
    if target is None:
        return
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


def test_enforcer_catches_lock_violation(tmp_path: Path) -> None:
    """Regression net: lock may not import arbitrary stdlib (e.g. sys)."""
    fake_pkg = tmp_path / "src" / "repo_pulse"
    violations = _planted_violation(fake_pkg, "lock", "import sys\n")
    assert any("lock" in v and "'sys'" in v for v in violations), (
        "regression net (lock) failed; got: " + repr(violations)
    )
