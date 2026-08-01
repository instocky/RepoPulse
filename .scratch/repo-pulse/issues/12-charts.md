# 11 — Charts

**What to build:** The Chart spec builder and the Plotly HTML renderer. The `Chart` object is the public contract — typed, library-agnostic. The renderer turns it into HTML. Future renderers (PNG, SVG, PDF) plug in without touching Analytics.

**Blocked by:** 08 — Analytics + DTOs

**Status:** ready-for-agent

- [ ] `Chart` is a frozen dataclass with `title: str`, `kind: Literal["line","bar","heatmap"]`, `data: ChartData`, `layout: ChartLayout`
- [ ] `build_line(series: list[TimeSeries], title: str) -> Chart`
- [ ] `build_bar(categories: list[BarItem], title: str) -> Chart`
- [ ] `build_heatmap(matrix: list[list[int]], row_labels: list[str], col_labels: list[str], title: str) -> Chart`
- [ ] `PlotlyRenderer` takes a `Chart` and returns an HTML string (uses `include_plotlyjs='cdn'`)
- [ ] The HTML is self-contained except for the Plotly CDN script tag
- [ ] `Chart` shape does NOT leak Plotly concepts (no `figure`, no `go.Scatter`, no `pio`)
- [ ] Golden-file tests assert on key substrings of the rendered HTML: title appears, data values appear, no Plotly-specific terms leak
- [ ] Tests cover: each chart kind with known data, empty data (edge case), single-point series
- [ ] No IO; no file writes


Respects 00-architecture doctrine
