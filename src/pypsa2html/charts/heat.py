"""Heat supply of buildings: a pie of pies, one per planning horizon.

The left pie answers "how is the residential and tertiary heat demand met",
with district heating as one slice; that slice is pulled out and expanded
into the right pie, "what feeds the district-heating network".  The numbers
come from :func:`pypsa2html.extract.heat.heat_split`; the table under the
chart carries them in TWh for every horizon.

Plotly has no pie-of-pie, so the two pies sit side by side in a fixed-size
figure and the two connector lines are drawn on a hidden ``[0, 1]`` axis.  The
size is fixed because a pie's radius depends on the pixel size of its domain:
in a responsive figure the connectors would miss the slice they point at.
"""

from __future__ import annotations

import html
import math

import pandas as pd
import plotly.graph_objects as go

from ..extract.heat import HeatSplit, heat_color, heat_split
from ..report.render import figure_to_html
from .base import FONT_SIZE, HEADER_MARGIN_TOP, Html, add_chart_data, combine_charts, tech_color_map

#: Label of the district-heating slice in the left pie.
DISTRICT = "District heating"

#: Figure geometry (px).  See the module docstring for why it is fixed.
WIDTH, HEIGHT = 960, 600
#: ``t`` matches :data:`~pypsa2html.charts.base.HEADER_MARGIN_TOP`, which
#: :func:`~pypsa2html.charts.base.combine_charts` enforces for its menu: a
#: smaller value would be raised there and move every pie off its connectors.
MARGIN = {"l": 20, "r": 20, "t": HEADER_MARGIN_TOP, "b": 140}
#: Pie domains, as fractions of the plot area.
LEFT_DOMAIN = {"x": [0.0, 0.47], "y": [0.0, 1.0]}
RIGHT_DOMAIN = {"x": [0.66, 0.98], "y": [0.14, 0.86]}
#: How far the district-heating slice is pulled out of the left pie.
PULL = 0.08


def supply_mix(ctx, node: str, section) -> Html | None:
    """Pie of pies of the building heat supply, one view per horizon."""
    splits: dict[int, HeatSplit] = {}
    for horizon in getattr(ctx, "horizons", None) or []:
        split = heat_split(ctx, node, horizon)
        if split is not None and split.total > 0:
            splits[horizon] = split
    if not splits:
        return None
    palette = tech_color_map()
    figures = [(str(h), _pie_of_pie(s, palette)) for h, s in splits.items()]
    fig = combine_charts(figures, menu_title="Horizon")
    table = _table_frame(splits)
    add_chart_data(ctx, node, section.title, "TWh/year", table.T)
    note = (
        "<p class='p2h-note'><b>How to read this.</b> Left: the heat used by "
        "homes and tertiary buildings (space heating and hot water), by the "
        "technology that delivers it. Right: the district-heating slice, "
        "split by what feeds the network. Agriculture and industrial heat are "
        "not counted. Where a heat network or bus also serves other users, "
        "its production is shared out in proportion to what each user takes, "
        "and heat storage is left out: it only moves heat in time.</p>"
    )
    return Html(figure_to_html(fig, plotly=ctx.config.output.plotly) + note
                + _table_html(table, splits))


def _color(label: str, palette: dict[str, str]) -> str:
    return heat_color(label, palette)


def _pie_of_pie(split: HeatSplit, palette: dict[str, str]) -> go.Figure:
    main = split.decentral.copy()
    has_dh = split.district > 0 and not split.district_mix.empty
    if split.district > 0:
        main = pd.concat([pd.Series({DISTRICT: split.district}), main])
    total = float(main.sum())
    share = split.district / total if total else 0.0

    # Plotly starts the first slice at 12 o'clock + ``rotation`` and, with
    # ``direction='clockwise'``, sweeps clockwise.  Centring the
    # district-heating slice (first) on 3 o'clock points it at the right pie.
    rotation = 90.0 - 180.0 * share
    fig = go.Figure()
    fig.add_trace(go.Pie(
        labels=list(main.index),
        values=[float(v) for v in main.to_numpy()],
        marker={"colors": [_color(lbl, palette) for lbl in main.index],
                "line": {"color": "#ffffff", "width": 1}},
        pull=[PULL if (lbl == DISTRICT and has_dh) else 0.0 for lbl in main.index],
        sort=False, direction="clockwise", rotation=rotation,
        domain=LEFT_DOMAIN, textinfo="percent", textposition="inside",
        insidetextorientation="horizontal",
        hovertemplate="%{label}<br>%{value:.2f} TWh (%{percent})<extra></extra>",
        name="Buildings", scalegroup=None,
    ))
    annotations = [_title(
        sum(LEFT_DOMAIN["x"]) / 2, "Residential and tertiary heat", f"{total:.1f} TWh",
    )]
    if has_dh:
        sub = split.district_mix
        fig.add_trace(go.Pie(
            labels=list(sub.index),
            values=[float(v) for v in sub.to_numpy()],
            marker={"colors": [_color(lbl, palette) for lbl in sub.index],
                    "line": {"color": "#ffffff", "width": 1}},
            sort=False, direction="clockwise", rotation=0,
            domain=RIGHT_DOMAIN, textinfo="percent", textposition="inside",
            insidetextorientation="horizontal",
            hovertemplate="%{label}<br>%{value:.2f} TWh (%{percent} of district heating)<extra></extra>",
            name=DISTRICT,
        ))
        annotations.append(_title(
            sum(RIGHT_DOMAIN["x"]) / 2, "District heating",
            f"{split.district:.2f} TWh ({100 * share:.1f} % of the total)",
        ))
        for x, y in _connectors(share):
            fig.add_trace(go.Scatter(
                x=x, y=y, mode="lines", line={"color": "#8c8c8c", "width": 1, "dash": "dot"},
                hoverinfo="skip", showlegend=False, xaxis="x", yaxis="y",
            ))
    if split.district_other > 0.005:
        annotations.append({
            "x": sum(RIGHT_DOMAIN["x"]) / 2, "y": -0.04, "xref": "paper", "yref": "paper",
            "text": f"District heating also delivers {split.district_other:.2f} TWh to other users (not shown)",
            "showarrow": False, "font": {"size": 12, "color": "#5c5c5c"},
        })
    fig.update_layout(
        width=WIDTH, height=HEIGHT, autosize=False, margin=MARGIN,
        font={"size": FONT_SIZE},
        annotations=annotations,
        legend={"orientation": "h", "y": -0.06, "x": 0.5, "xanchor": "center", "yanchor": "top",
                "font": {"size": 13}},
        xaxis={"range": [0, 1], "visible": False, "fixedrange": True},
        yaxis={"range": [0, 1], "visible": False, "fixedrange": True},
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def _title(x: float, heading: str, detail: str) -> dict:
    """Two-line title above a pie: the two sit side by side in a narrow figure."""
    return {"x": x, "y": 1.02, "xref": "paper", "yref": "paper", "yanchor": "bottom",
            "text": f"<b>{html.escape(heading)}</b><br>{html.escape(detail)}",
            "showarrow": False, "font": {"size": 14}}


def _connectors(share: float) -> list[tuple[list[float], list[float]]]:
    """Two lines from the edges of the pulled slice to the right pie."""
    width = WIDTH - MARGIN["l"] - MARGIN["r"]
    height = HEIGHT - MARGIN["t"] - MARGIN["b"]

    def circle(domain):
        w = (domain["x"][1] - domain["x"][0]) * width
        h = (domain["y"][1] - domain["y"][0]) * height
        cx = (domain["x"][0] + domain["x"][1]) / 2 * width
        cy = (domain["y"][0] + domain["y"][1]) / 2 * height
        return cx, cy, min(w, h) / 2

    cx1, cy1, r1 = circle(LEFT_DOMAIN)
    r1 = r1 / (1 + PULL)  # plotly shrinks the pie to make room for the pull
    cx1 += PULL * r1       # the slice moves out along its bisector (3 o'clock)
    cx2, cy2, r2 = circle(RIGHT_DOMAIN)
    half = math.pi * min(share, 0.5)
    lines = []
    for sign in (1, -1):
        x0, y0 = cx1 + r1 * math.cos(half), cy1 + sign * r1 * math.sin(half)
        x1, y1 = cx2, cy2 + sign * r2
        lines.append(([x0 / width, x1 / width], [y0 / height, y1 / height]))
    return lines


def _table_frame(splits: dict[int, HeatSplit]) -> pd.DataFrame:
    rows: dict[str, dict[str, float]] = {}
    for horizon, split in splits.items():
        year = str(horizon)
        for label, value in split.decentral.items():
            rows.setdefault(label, {})[year] = float(value)
        rows.setdefault(DISTRICT, {})[year] = float(split.district)
        for label, value in split.district_mix.items():
            rows.setdefault(f"{DISTRICT}: {label}", {})[year] = float(value)
    frame = pd.DataFrame(rows).T.fillna(0.0)
    return frame.reindex(columns=[str(h) for h in splits])


def _table_html(frame: pd.DataFrame, splits: dict[int, HeatSplit]) -> str:
    years = list(frame.columns)
    head = "<th>Technology</th>" + "".join(f"<th class='num'>{y}</th>" for y in years)
    body = []
    decentral = [i for i in frame.index if not i.startswith(f"{DISTRICT}")]
    district_rows = [i for i in frame.index if i.startswith(f"{DISTRICT}: ")]
    for label in decentral:
        body.append(_tr(label, frame.loc[label], years))
    body.append(_tr(DISTRICT, frame.loc[DISTRICT], years, css="p2h-subtotal"))
    for label in district_rows:
        body.append(_tr(" " + label.split(": ", 1)[1], frame.loc[label], years))
    totals = pd.Series({str(h): s.total for h, s in splits.items()}).reindex(years)
    body.append(_tr("Total residential and tertiary heat", totals, years, css="p2h-total"))
    return ("<table class='p2h-table'><thead><tr>" + head + "</tr></thead><tbody>"
            + "".join(body) + "</tbody></table><p class='p2h-unit'>TWh per year.</p>")


def _tr(label: str, values: pd.Series, years: list[str], css: str = "") -> str:
    cls = f" class='{css}'" if css else ""
    cells = "".join(f"<td class='num'>{float(values.get(y, 0.0)):.2f}</td>" for y in years)
    return f"<tr{cls}><td>{html.escape(label)}</td>{cells}</tr>"
