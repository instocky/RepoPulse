"""The favicon assets must exist, be valid, and stay in sync with the generator.

The favicon set is generated (assets/favicon/build_favicons.py). These tests
are the cheap net that catches the two ways that set rots: a file goes
missing (someone deleted it, or a rename landed halfway) and a PNG does not
match its declared size (a hand-dropped replacement at the wrong
resolution).

Run the generator first if a test fails:
    python assets/favicon/build_favicons.py
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

FAVICON_DIR = Path(__file__).resolve().parents[1] / "assets" / "favicon"
SCHEMES = ("light", "dark")
SIZES = (16, 32, 48, 64, 180)


def _png_size(path: Path) -> tuple[int, int]:
    """Read width/height straight out of the PNG IHDR chunk."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("size", SIZES)
def test_png_exists_at_declared_size(scheme: str, size: int) -> None:
    path = FAVICON_DIR / "png" / scheme / f"{size}.png"
    assert path.exists(), f"missing {path}; run build_favicons.py"
    assert _png_size(path) == (size, size)


@pytest.mark.parametrize(
    "name", ("favicon.svg", "favicon-dark.svg")
)
def test_svg_exists_and_is_wellformed(name: str) -> None:
    path = FAVICON_DIR / "svg" / name
    assert path.exists(), f"missing {path}; run build_favicons.py"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<svg"), "SVG must start with the root element"
    assert text.rstrip().endswith("</svg>"), "SVG must be closed"
    assert "viewBox=" in text, "SVG needs a viewBox to scale"


def test_ico_exists_and_is_a_real_ico() -> None:
    path = FAVICON_DIR / "ico" / "favicon.ico"
    assert path.exists(), f"missing {path}; run build_favicons.py"
    data = path.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00", "ICO must carry the 1.0 magic"


def test_dark_and_light_svg_differ() -> None:
    """The two schemes must not be the same file with a different name."""
    light = (FAVICON_DIR / "svg" / "favicon.svg").read_text(encoding="utf-8")
    dark = (FAVICON_DIR / "svg" / "favicon-dark.svg").read_text(encoding="utf-8")
    assert light != dark, "light and dark SVGs are identical"


def test_no_stale_svg_files() -> None:
    """Only the current naming scheme may live in svg/."""
    expected = {"favicon.svg", "favicon-dark.svg"}
    actual = {p.name for p in (FAVICON_DIR / "svg").glob("*.svg")}
    assert actual == expected, f"unexpected SVG set: {actual ^ expected}"
