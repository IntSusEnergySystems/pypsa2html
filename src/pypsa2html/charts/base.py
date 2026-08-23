"""Shared chart machinery: figures, the variant dropdown, the data workbook.

This merges four near-identical legacy helpers into one implementation:
the legacy ``create_node_chart`` (area and line),
the legacy ``create_ghg_chart`` (area with signed
stackgroups) and the two ``combine_charts`` functions -- which had *different
signatures*, ``sf``'s being ``(combinations, main_params, description, title,
chart_type, xls_writer, unit, ...)`` and ``saf``'s
``(combinations, main_params, description, title, chart_type, xls_writer,
interval_year, unit, ...)``.  Eight call sites in the legacy code passed the
unit as the seventh positional argument, which reached ``saf`` as
``interval_year``: the four emission charts were labelled ``TWh/year`` in the
shipped ``ChartData_*.xlsx`` and their Sankey-style slider stepped every
``'MtCO<sub>2</sub>eq'`` years.  Everything here is keyword-only past the
frame, and the unit comes from ``section.unit``.

Other differences from the original:

* No mutable default arguments.  ``targets={}``, ``description=pd.DataFrame()``
  and three more were shared across every call in the process.
* No fixed ``width=1400``.  The report is responsive; a hardcoded width made
  every chart overflow on a laptop.
* No year relabelling.  ``create_node_chart`` rewrote the 2020 tick as "2019"
  and the pypsa-wal fork rewrote the same tick as "2025"; the axis now shows
  the horizons the model was actually solved for.
* :func:`combine_charts` counts the traces it merged instead of predicting
  them from ``len(df.columns)`` plus ad-hoc ``+1``s, which silently mis-toggled
  any chart whose frame had an all-zero column dropped.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from pypsa2html.datafiles import load_tech_colors, load_tech_groups

logger = logging.getLogger(__name__)

#: Height of every chart, in pixels.  Width is left to the container.
CHART_HEIGHT = 620

#: Base font size, matching the legacy output.
FONT_SIZE = 15


class Html:
    """A ready-made HTML fragment returned in place of a plotly figure.

    Maps and dispatch tabs are rendered elsewhere and arrive as markup; the
    page assembler needs to tell them apart from a :class:`~plotly.graph_objects.Figure`
    without ``isinstance`` checks against every possible producer.
    """

    __slots__ = ("fragment",)

    def __init__(self, fragment: str):
        if not isinstance(fragment, str):
            raise TypeError(f"Html() takes a string, got {type(fragment).__name__}")
        self.fragment = fragment

    def __str__(self) -> str:
        return self.fragment

    def __html__(self) -> str:  # Jinja / markupsafe protocol
        return self.fragment

    def _repr_html_(self) -> str:  # notebooks
        return self.fragment

    def __repr__(self) -> str:
        return f"Html({self.fragment[:40]!r}...)"


# ---------------------------------------------------------------------------
# Labels and colours
# ---------------------------------------------------------------------------

# Prefixes stripped by the legacy ``rename_techs`` (PyPSA-Eur ``plot_summary``).
_CARRIER_PREFIXES = (
    "residential ",
    "services ",
    "urban ",
    "rural ",
    "central ",
    "decentral ",
)

# Substrings that collapse the whole carrier label (``rename_if_contains``).
_CARRIER_CONTAINS = (
    "CHP",
    "gas boiler",
    "biogas",
    "solar thermal",
    "air heat pump",
    "ground heat pump",
    "resistive heater",
    "Fischer-Tropsch",
)

_CARRIER_CONTAINS_RENAME = {
    "water tanks": "hot water storage",
    "retrofitting": "building retrofitting",
    "H2 for industry": "H2 for industry",
    "land transport fuel cell": "land transport fuel cell",
    "land transport oil": "land transport oil",
    "oil shipping": "shipping oil",
}

_CARRIER_EXACT_RENAME = {
    "solar": "solar PV",
    "Sabatier": "methanation",
    "offwind": "offshore wind",
    "offwind-ac": "offshore wind (AC)",
    "offwind-dc": "offshore wind (DC)",
    "onwind": "onshore wind",
    "ror": "hydroelectricity",
    "hydro": "hydroelectricity",
    "PHS": "hydroelectricity",
    "NH3": "ammonia",
    "co2 Store": "DAC",
    "co2 stored": "CO2 sequestration",
    "AC": "transmission lines",
    "DC": "transmission lines",
    "B2B": "transmission lines",
}


def normalize_carrier(label: str) -> str:
    """Normalise a PyPSA carrier label (legacy ``rename_techs``)."""
    name = str(label)
    for prefix in _CARRIER_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
    for fragment in _CARRIER_CONTAINS:
        if fragment in name:
            return fragment
    for old, new in _CARRIER_CONTAINS_RENAME.items():
        if old in name:
            return new
    return _CARRIER_EXACT_RENAME.get(name, name)


#: Reserved ``tech_groups.csv`` group: drop the carrier from that view.
#: Matched against the *raw* carrier in :func:`omit_carriers` (before
#: :func:`normalize_carrier`), so ``^biogas$`` does not also remove
#: ``biogas to gas``.
OMIT_GROUP = "__omit__"


def _pattern_matches(pattern: str, name: str) -> bool:
    if pattern.startswith("^") and pattern.endswith("$"):
        return name == pattern[1:-1]
    return pattern in name


def omit_carriers(
    carriers: pd.Series,
    view: str,
    *,
    tech_groups: pd.DataFrame | None = None,
) -> pd.Series:
    """Boolean mask: ``True`` where the *raw* carrier is omitted for ``view``."""
    if tech_groups is None:
        tech_groups = load_tech_groups()
    rules = tech_groups.loc[
        (tech_groups["view"] == view) & (tech_groups["group"] == OMIT_GROUP)
    ]
    if rules.empty:
        return pd.Series(False, index=carriers.index)

    patterns = [str(p) for p in rules["pattern"]]

    def omitted(raw: object) -> bool:
        name = str(raw)
        return any(_pattern_matches(p, name) for p in patterns)

    return carriers.map(omitted)


def apply_tech_map(
    series: pd.Series,
    view: str,
    *,
    tech_groups: pd.DataFrame | None = None,
) -> pd.Series:
    """Map carrier names to display groups for ``view``.

    Unknown names pass through unchanged (after :func:`normalize_carrier`).
    ``tech_groups`` is read from ``data/tech_groups.csv`` when omitted; the
    table itself is never mutated.  Rows with group :data:`OMIT_GROUP` are
    ignored here — use :func:`omit_carriers` to filter them on raw names.
    """
    if tech_groups is None:
        tech_groups = load_tech_groups()
    rules = tech_groups.loc[
        (tech_groups["view"] == view) & (tech_groups["group"] != OMIT_GROUP)
    ].copy()
    rules["_plen"] = rules["pattern"].astype(str).str.len()
    rules = rules.sort_values(["order", "_plen"], ascending=[True, False])

    def map_one(raw: object) -> str:
        name = normalize_carrier(raw)
        for row in rules.itertuples(index=False):
            if _pattern_matches(str(row.pattern), name):
                return str(row.group)
        return name

    indexed = series.copy()
    return indexed.map(map_one)


def tech_color_map() -> dict[str, str]:
    """Return a copy of ``tech_colors.csv`` as a name -> #rrggbb dict."""
    table = load_tech_colors()
    return dict(zip(table["name"], table["color"], strict=True))


#: Distinct hues used when a technology has no palette entry.  Avoid the
#: previous ``lightgrey`` default that made storage / unknown techs unreadable.
_FALLBACK_COLORS = (
    "#e41a1c",
    "#377eb8",
    "#4daf4a",
    "#984ea3",
    "#ff7f00",
    "#a65628",
    "#f781bf",
    "#66c2a5",
    "#fc8d62",
    "#8da0cb",
    "#e78ac3",
    "#a6d854",
    "#ffd92f",
    "#e5c494",
    "#1b9e77",
    "#d95f02",
    "#7570b3",
    "#e7298a",
)


def resolve_tech_color(
    name: str,
    palette: dict[str, str] | None = None,
) -> str:
    """Look up a tech colour with case / rename fallbacks.

    Order: exact match → case-insensitive → :func:`normalize_carrier` → a
    stable distinct colour derived from the name (never flat grey).
    """
    palette = palette if palette is not None else tech_color_map()
    key = str(name)
    if key in palette:
        return palette[key]
    lower = {str(k).lower(): v for k, v in palette.items()}
    if key.lower() in lower:
        return lower[key.lower()]
    norm = normalize_carrier(key)
    if norm in palette:
        return palette[norm]
    if norm.lower() in lower:
        return lower[norm.lower()]
    # Stable per-name pick from the fallback palette.
    idx = sum(ord(c) for c in key) % len(_FALLBACK_COLORS)
    return _FALLBACK_COLORS[idx]


def strip_markup(text: str) -> str:
    """Strip the ``<sub>`` markup the legacy unit strings carried."""
    return re.sub(r"</?[a-zA-Z][^>]*>", "", str(text or ""))


def describe(ctx, codes: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """Display labels for ``codes`` and a label -> colour map.

    The palette is read out of the taxonomy and copied; ``docs/INTERNALS.md``
    section 4 forbids mutating it, and ``create_sankey`` used to write a whole
    ``ColorOpacity`` column into the shared ``NODES`` frame on every call.
    """
    labels = ctx.taxonomy.labels
    colors = ctx.taxonomy.colors
    names = [str(labels.get(code, code)) for code in codes]
    palette: dict[str, str] = {}
    for code, name in zip(codes, names, strict=True):
        color = colors.get(code)
        if isinstance(color, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            palette.setdefault(name, color)
    return names, palette


def _prepare(ctx, frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    """Round, relabel and merge same-labelled columns of an indicator frame."""
    decimals = getattr(ctx.config.project, "decimals", 3)
    names, palette = describe(ctx, list(frame.columns))
    out = frame.round(decimals).copy()
    out.columns = names
    if out.columns.duplicated().any():
        # e.g. lqf_fe and lqf_se both read "Liquid motor fuels"
        out = out.T.groupby(level=0, sort=False).sum().T
    return out, palette


def _layout(fig: go.Figure, *, title: str, unit: str, years: Sequence) -> None:
    fig.update_layout(
        title=title or None,
        hovermode="x",
        height=CHART_HEIGHT,
        legend_title_text="",
        yaxis_title=strip_markup(unit),
        font={"size": FONT_SIZE},
        margin={"l": 60, "r": 30, "t": 60 if title else 30, "b": 50},
        xaxis={"tickmode": "array", "tickvals": list(years), "title": ""},
    )


def _add_targets(fig: go.Figure, targets: dict | None) -> None:
    """Overlay an external target trajectory, when the caller supplies one."""
    if not targets:
        return
    fig.add_scatter(
        x=targets["x"],
        y=targets["y"],
        mode=targets.get("mode", "markers"),
        name=targets.get("title", "Target"),
        marker_size=14,
        marker_color="black",
    )


def _empty(frame: pd.DataFrame | None) -> bool:
    return frame is None or frame.empty or not len(frame.columns)


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def area_chart(
    ctx,
    frame: pd.DataFrame,
    *,
    unit: str,
    title: str = "",
    total: bool = True,
    targets: dict | None = None,
) -> go.Figure | None:
    """Stacked area chart of one indicator frame (index: years, columns: codes)."""
    if _empty(frame):
        return None
    data, palette = _prepare(ctx, frame)
    fig = go.Figure()
    for name in data.columns:
        fig.add_trace(
            go.Scatter(
                x=data.index,
                y=data[name],
                name=name,
                mode="lines",
                line={"width": 1, "color": palette.get(name)},
                stackgroup="one",
                hovertemplate="%{y:.1f}",
            )
        )
    if total:
        fig.add_trace(
            go.Scatter(
                x=data.index,
                y=data.sum(axis=1),
                name="Total",
                mode="lines",
                line={"color": "black", "width": 2},
                hovertemplate="%{y:.1f}",
            )
        )
    _add_targets(fig, targets)
    _layout(fig, title=title, unit=unit, years=data.index)
    return fig


def ghg_area_chart(
    ctx,
    frame: pd.DataFrame,
    *,
    unit: str,
    title: str = "",
    total: bool = True,
    targets: dict | None = None,
) -> go.Figure | None:
    """Area chart with emissions stacked upwards and removals downwards.

    ``saf.create_ghg_chart`` split the *melted* rows by sign, so a series that
    crossed zero was drawn as two traces with a hole in the middle and no
    connecting segment.  Each series is instead split into its positive and
    negative parts, which stack correctly and stay continuous.
    """
    if _empty(frame):
        return None
    data, palette = _prepare(ctx, frame)
    fig = go.Figure()
    for name in data.columns:
        series = data[name]
        positive, negative = series.clip(lower=0), series.clip(upper=0)
        shown = False
        if (positive != 0).any():
            fig.add_trace(
                go.Scatter(
                    x=data.index, y=positive, name=name, mode="lines",
                    line={"width": 1, "color": palette.get(name)},
                    stackgroup="positive", legendgroup=name,
                    hovertemplate="%{y:.1f}",
                )
            )
            shown = True
        if (negative != 0).any():
            fig.add_trace(
                go.Scatter(
                    x=data.index, y=negative, name=name, mode="lines",
                    line={"width": 1, "color": palette.get(name)},
                    stackgroup="negative", legendgroup=name,
                    showlegend=not shown, hovertemplate="%{y:.1f}",
                )
            )
    if total:
        fig.add_trace(
            go.Scatter(
                x=data.index, y=data.sum(axis=1), name="Total", mode="lines",
                line={"color": "black", "width": 2}, hovertemplate="%{y:.1f}",
            )
        )
    _add_targets(fig, targets)
    _layout(fig, title=title, unit=unit, years=data.index)
    return fig


def line_chart(
    ctx,
    frame: pd.DataFrame,
    *,
    unit: str,
    title: str = "",
    targets: dict | None = None,
) -> go.Figure | None:
    """Line chart of one indicator frame -- ratios, shares, intensities."""
    if _empty(frame):
        return None
    data, palette = _prepare(ctx, frame)
    fig = go.Figure()
    for name in data.columns:
        fig.add_trace(
            go.Scatter(
                x=data.index, y=data[name], name=name, mode="lines+markers",
                line={"width": 2, "color": palette.get(name)},
                connectgaps=False, hovertemplate="%{y:.1f}",
            )
        )
    _add_targets(fig, targets)
    _layout(fig, title=title, unit=unit, years=data.index)
    return fig


def stacked_bar(
    df: pd.DataFrame,
    colors: dict[str, str] | None,
    unit: str,
    title: str = "",
    *,
    signed: bool = True,
    scale: float = 1.0,
) -> go.Figure | None:
    """Stacked (or relative) bar chart of a tech × year table.

    ``df`` has technologies as the index and year labels as columns -- the
    shape returned by :func:`pypsa2html.extract.tables.cost_table`.  Replaces
    the four near-identical legacy bar builders (``create_bar_chart``,
    ``create_clustered_costs``, ``create_investment_costs``,
    ``create_operational_costs``).

    When ``signed`` is true, positive and negative values are split so
    ``barmode='relative'`` stacks credits below the axis.  ``scale`` multiplies
    every value (e.g. ``1e-3`` to show MW as GW).
    """
    if df is None or df.empty or not len(df.columns):
        return None
    data = df.astype(float) * scale
    # Drop all-zero technologies so the legend stays readable.
    data = data.loc[~(data == 0).all(axis=1)]
    if data.empty:
        return None
    years = [str(c) for c in data.columns]
    transposed = data.T
    transposed.index = years
    palette = dict(colors or {})
    fig = go.Figure()
    for tech in transposed.columns:
        series = transposed[tech]
        color = resolve_tech_color(str(tech), palette)
        if signed:
            fig.add_trace(
                go.Bar(
                    x=years,
                    y=series.where(series > 0, 0.0),
                    name=tech,
                    marker_color=color,
                    legendgroup=tech,
                    hovertemplate="%{y:.3g}",
                )
            )
            if (series < 0).any():
                fig.add_trace(
                    go.Bar(
                        x=years,
                        y=series.where(series < 0, 0.0),
                        name=tech,
                        marker_color=color,
                        legendgroup=tech,
                        showlegend=False,
                        hovertemplate="%{y:.3g}",
                    )
                )
        else:
            fig.add_trace(
                go.Bar(
                    x=years,
                    y=series,
                    name=tech,
                    marker_color=color,
                    hovertemplate="%{y:.3g}",
                )
            )
    fig.update_layout(
        title=title or None,
        barmode="relative" if signed else "stack",
        height=CHART_HEIGHT,
        hovermode="x unified",
        yaxis_title=strip_markup(unit),
        font={"size": FONT_SIZE},
        legend_title_text="",
        margin={"l": 60, "r": 30, "t": 60 if title else 30, "b": 50},
        xaxis={"tickmode": "array", "tickvals": years, "title": ""},
    )
    return fig


# ---------------------------------------------------------------------------
# Combining variants
# ---------------------------------------------------------------------------

def combine_charts(
    figures: Sequence[tuple[str, go.Figure]],
    *,
    title: str = "",
    menu_title: str = "",
) -> go.Figure | None:
    """Merge per-variant figures into one, switched by a dropdown.

    ``figures`` is a sequence of ``(button label, figure)``.  Figures that
    carry a slider (the Sankeys) keep it, and the slider swaps with the
    variant, as in the original.  ``None`` entries are dropped rather than
    crashing the page.

    ``barmode`` is a *layout* property, so a single merged figure cannot hold
    two of them: each button carries its own variant's mode.  Without that, a
    comparison variant drawn with ``barmode='group'`` would flatten the
    stacked composition variants sharing the dropdown with it (and vice
    versa).  ``annotations`` are layout properties for the same reason and are
    swapped the same way, so a per-variant caption follows its own traces.
    """
    usable = [(str(label), fig) for label, fig in figures if fig is not None]
    if not usable:
        return None
    if len(usable) == 1:
        only = usable[0][1]
        if title:
            only.update_layout(title=title)
        return only

    # Per-variant trace visibility as the variant's own figure intended it
    # (Sankeys mark every step but the active one invisible).
    intent = [
        [True if trace.visible is None else bool(trace.visible) for trace in fig.data]
        for _, fig in usable
    ]
    offsets, running = [], 0
    for flags in intent:
        offsets.append(running)
        running += len(flags)
    n_traces = running

    merged = go.Figure()
    for i, (_, fig) in enumerate(usable):
        for j, trace in enumerate(fig.data):
            clone = copy.deepcopy(trace)
            clone.visible = bool(intent[i][j]) and i == 0
            merged.add_trace(clone)
    # Axes, height and legend come from the first variant; its slider (if any)
    # is replaced below, once the full trace count is known.
    merged.update_layout(usable[0][1].layout)

    # An annotation is a layout property too, so a variant's caption has to be
    # swapped by its own button -- otherwise variant 0's numbers stay on screen
    # while variant 2's traces are shown.  Only touched when some variant
    # actually carries one, so charts without captions are unaffected.
    any_annotations = any(bool(fig.layout.annotations) for _, fig in usable)

    buttons = []
    for i, (label, fig) in enumerate(usable):
        visible = [False] * n_traces
        for j, flag in enumerate(intent[i]):
            visible[offsets[i] + j] = flag
        args: list[dict] = [
            {"visible": visible},
            {"title": {"text": f"{title} {label}".strip()}},
        ]
        if fig.layout.barmode is not None:
            args[1]["barmode"] = fig.layout.barmode
        if any_annotations:
            args[1]["annotations"] = [
                copy.deepcopy(a) for a in (fig.layout.annotations or ())
            ]
        sliders = fig.layout.sliders or ()
        if sliders:
            slider = copy.deepcopy(sliders[0])
            for j, step in enumerate(slider.steps):
                step_visible = [False] * n_traces
                step_visible[offsets[i] + j] = True
                step.args[0]["visible"] = step_visible
            args[1]["sliders"] = [slider]
            if i == 0:
                merged.update_layout(sliders=[slider])
        buttons.append(
            {"args": args, "label": label[:1].upper() + label[1:], "method": "update"}
        )

    merged.update_layout(
        title=f"{title} {usable[0][0]}".strip() or None,
        updatemenus=[
            {
                "buttons": buttons,
                "direction": "down",
                "showactive": True,
                "x": 1.0,
                "xanchor": "right",
                "y": 1.15,
                "yanchor": "top",
                "name": menu_title or None,
            }
        ],
    )
    return merged


# ---------------------------------------------------------------------------
# The chart-data workbook
# ---------------------------------------------------------------------------

@dataclass
class ChartData:
    """The numbers behind the figures of one node, for ``ChartData_<node>.xlsx``.

    Builders call :func:`add_chart_data` as they go; the site builder flushes
    the book once per node with :meth:`write`.  The legacy code passed an open
    ``pd.ExcelWriter`` down through six layers of positional arguments and
    wrote to it as a side effect of building a figure -- including from the
    ``else:`` branches that threw the figure away.
    """

    sheets: list[tuple[str, str, pd.DataFrame]] = field(default_factory=list)

    def add(self, title: str, unit: str, frame: pd.DataFrame) -> str:
        name = f"Chart {len(self.sheets) + 1}"
        self.sheets.append((title, strip_markup(unit), frame.copy()))
        return name

    def write(self, path: str | Path) -> Path | None:
        """Write the workbook, with a table of contents.  ``None`` if empty."""
        if not self.sheets:
            return None
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            for i, (title, unit, frame) in enumerate(self.sheets, start=1):
                sheet = f"Chart {i}"
                frame.to_excel(writer, sheet_name=sheet, startrow=2)
                ws = writer.sheets[sheet]
                ws["A1"] = f"{title} ({unit})" if unit else title
                ws["A2"].value = "Back to table of contents"
                ws["A2"].hyperlink = "#TOC!A1"
                ws["A2"].style = "Hyperlink"
            toc = writer.book.create_sheet("TOC")
            for i, (title, unit, _) in enumerate(self.sheets, start=1):
                toc.cell(row=i, column=1, value=f"Chart {i}")
                cell = toc.cell(row=i, column=2, value=f"{title} ({unit})" if unit else title)
                cell.hyperlink = f"#'Chart {i}'!A1"
                cell.style = "Hyperlink"
            writer.book.active = toc
        logger.info("wrote %s (%d sheets)", path, len(self.sheets))
        return path


def chart_data(ctx, node: str) -> ChartData:
    """The workbook accumulator for ``node``, created on first use."""
    key = ("chartdata", str(node))
    book = ctx._files.get(key)
    if book is None:
        book = ChartData()
        ctx._files[key] = book
    return book


def add_chart_data(ctx, node: str, title: str, unit: str, frame: pd.DataFrame) -> None:
    """Record the numbers behind a chart, when ``output.chart_data`` is on."""
    if not getattr(ctx.config.output, "chart_data", True):
        return
    if _empty(frame):
        return
    labelled, _ = _prepare(ctx, frame)
    chart_data(ctx, node).add(title, unit, labelled)
