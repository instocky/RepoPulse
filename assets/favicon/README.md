# Repo-Pulse favicon

The mark: **an ECG trace**. The product is a heartbeat for a starred set —
one reading per day, rhythm over time.

Rendered in a **light** and a **dark** scheme, because the dashboard is
reached from both a white and a near-black browser tab.

## Palette

| Role | Light scheme | Dark scheme |
|---|---|---|
| Tile | `#F5F7FA` | `#0B1220` |
| Accent (mint) | `#0F9E7E` | `#22D3A6` |
| Tile edge | `rgba(11,18,32,0.16)` | `rgba(255,255,255,0.14)` |

The accent is deliberately **not** GitHub green — this is our own instrument.
Contrast against its own tile: **3.15:1** (light), **9.75:1** (dark), so the
glyph clears the 3:1 non-text minimum in both schemes.

## Files

```
svg/favicon.svg           light scheme (default favicon slot)
svg/favicon-dark.svg      dark scheme
png/{light,dark}/         16 32 48 64 180 .png
ico/favicon.ico           16/32/48 multi-resolution
preview.png               contact sheet (sizes × schemes)
preview.html              the same sheet in a browser
```

## Rebuilding

```bash
python assets/favicon/build_favicons.py
```

Geometry lives in `build_favicons.py`; the PNG/ICO files are build output, not
sources. `tests/test_favicon_assets.py` fails if an asset goes missing, if a
PNG does not match its declared size, if the two SVGs are identical, or if a
stale SVG lingers.

Requires ImageMagick (`magick`) on PATH.

## Wiring into the dashboard (ticket 13)

Serve `svg/` and `png/` from `/static/favicon/`, then in `base.html`:

```html
<link rel="icon" href="/static/favicon/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/static/favicon/favicon-dark.svg"
      media="(prefers-color-scheme: dark)" type="image/svg+xml">
<link rel="alternate icon" href="/static/favicon/ico/favicon.ico">
```

The two schemes are **separate files** rather than a `prefers-color-scheme`
media query inside one SVG: browsers disagree on whether they honour CSS
media queries in a favicon SVG, and a favicon that renders as a blank tile on
a dark tab is worse than two files.

## Design notes

- The glyph is drawn inside a `59×59` rounded tile (`rx=15`) inside a `64×64`
  viewBox, so it keeps its own contrast against any tab colour.
- The trace is **one connected silhouette**. Earlier candidates that paired a
  separate arrowhead or a second element with the star dissolved below 32px:
  only the second element survived.
- Two alternatives were explored and dropped: a radar sweep (a swept sector
  reads as a pie chart below 32px) and concentric sonar rings (they read as a
  Wi-Fi badge, not as monitoring).
