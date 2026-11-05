"""Generate the Repo-Pulse favicon: the "pulse" mark, light and dark.

The favicon is a resolution-bound asset (16/32/48/180 px) and it has to be
emitted in two color schemes. Hand-maintaining the files means several
chances to drift; here the geometry lives once and the script renders it.

Run:  python assets/favicon/build_favicons.py
Out:  assets/favicon/svg/favicon.svg        light scheme (default slot)
      assets/favicon/svg/favicon-dark.svg   dark scheme
      assets/favicon/png/{light,dark}/<size>.png
      assets/favicon/ico/favicon.ico
      assets/favicon/preview.png            contact sheet
      assets/favicon/preview.html           the same sheet in a browser

The web module (ticket 13) will serve svg/ + png/ from /static/favicon/.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# --- palette -----------------------------------------------------------------
# A mint/teal signal color, deliberately not GitHub green: this is our own
# instrument, and the color must read on both a white and a near-black tab.
INK = "#0B1220"          # dark tile
INK_SOFT = "rgba(255,255,255,0.14)"
PAPER = "#F5F7FA"        # light tile
PAPER_SOFT = "rgba(11,18,32,0.16)"
MINT_DARK = "#0F9E7E"    # accent on light ground (contrast 3.15:1 vs paper)
MINT_LIGHT = "#22D3A6"   # accent on dark ground (contrast 9.75:1 vs ink)

PNG_SIZES = (16, 32, 48, 64, 180)
ICO_SIZES = (16, 32, 48)

# The mark: an ECG trace. One connected silhouette that survives 16px — a
# separated arrowhead or a second element dissolved at small sizes.
GLYPH = """
  <path class="mark" d="M9 34 L21 34 L27 18 L33 48 L39 34 L55 34"
        fill="none" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"/>
"""

SCHEMES = {
    "light": (MINT_DARK, PAPER, PAPER_SOFT),
    "dark": (MINT_LIGHT, INK, INK_SOFT),
}


def _style(accent: str, tile: str, edge: str) -> str:
    """Shared rules for one fixed color scheme."""
    return f"""
    .tile {{ fill: {tile}; stroke: {edge}; stroke-width: 1; }}
    .mark {{ stroke: {accent}; }}"""


def svg_document(accent: str, tile: str, edge: str) -> str:
    """A fixed-scheme SVG — used both as a deliverable and as the raster source."""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <title>Repo-Pulse</title>
  <style>{_style(accent, tile, edge)}</style>
  <rect class="tile" x="2.5" y="2.5" width="59" height="59" rx="15"/>
{GLYPH}</svg>
"""


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"failed: {' '.join(cmd)}\n{result.stderr}")


def main() -> None:
    magick = shutil.which("magick")
    if magick is None:
        sys.exit("ImageMagick 'magick' not found on PATH")

    svg_dir = ROOT / "svg"
    png_dir = ROOT / "png"
    ico_dir = ROOT / "ico"
    for d in (svg_dir, png_dir, ico_dir):
        d.mkdir(parents=True, exist_ok=True)

    for scheme, (accent, tile, edge) in SCHEMES.items():
        # The default slot (favicon.svg) is the light scheme; the dark one is
        # linked separately with media="(prefers-color-scheme: dark)".
        name = "favicon.svg" if scheme == "light" else "favicon-dark.svg"
        src = svg_dir / name
        src.write_text(svg_document(accent, tile, edge), encoding="utf-8")
        out_dir = png_dir / scheme
        out_dir.mkdir(parents=True, exist_ok=True)
        for size in PNG_SIZES:
            run([
                magick, "-background", "none", "-density", "600",
                str(src), "-resize", f"{size}x{size}",
                str(out_dir / f"{size}.png"),
            ])

    ico_srcs = [str(png_dir / "light" / f"{s}.png") for s in ICO_SIZES]
    run([magick, *ico_srcs, str(ico_dir / "favicon.ico")])

    build_preview(magick)
    build_preview_html()
    print("built favicon: pulse")


def build_preview(magick: str) -> None:
    """Contact sheet: rows are sizes, columns are the two schemes.

    Composited on an explicit canvas rather than via ``montage``: montage
    equalizes its cells to the largest input and stretches the small ones,
    which would misrepresent a 16px favicon.
    """
    sizes = (16, 32, 48)
    cell = 72
    gap = 16
    rows = len(sizes)
    width = gap + 2 * (cell + gap)
    height = gap + rows * (cell + gap)
    sheet = ROOT / "preview.png"

    run([magick, "-size", f"{width}x{height}", "xc:#9AA4B2", str(sheet)])
    for row, size in enumerate(sizes):
        for col, scheme in enumerate(("light", "dark")):
            x = gap + col * (cell + gap)
            y = gap + row * (cell + gap)
            run([
                magick, str(sheet),
                str(ROOT / "png" / scheme / f"{size}.png"),
                "-gravity", "NorthWest",
                "-geometry", f"+{x}+{y}",
                "-composite", str(sheet),
            ])


def build_preview_html() -> None:
    """A browser view of the same sheet, at the sizes a browser really uses."""
    cells = []
    for scheme in ("light", "dark"):
        for size in (16, 32, 48, 180):
            cells.append(
                f'<figure><img src="png/{scheme}/{size}.png" '
                f'alt="favicon {scheme} {size}" width="{size}">'
                f"<figcaption>{scheme} {size}</figcaption></figure>"
            )
    html = f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Repo-Pulse favicon</title>
<style>
  body {{ font: 14px/1.4 system-ui, sans-serif; background: #9AA4B2; margin: 24px; }}
  .row {{ display: flex; gap: 18px; align-items: flex-end; }}
  figure {{ margin: 0; text-align: center; background: #fff; padding: 8px; border-radius: 6px; }}
  figcaption {{ font-size: 11px; color: #55606E; margin-top: 6px; }}
</style>
<div class="row">
{"".join(cells)}
</div>
</html>
"""
    (ROOT / "preview.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
