"""Electric-vehicle charging charts: natural vs optimised, V2G, state of charge.

Three sections, all on the dispatch page:

``ev_charging_winter`` / ``ev_charging_summer``
    The same two representative weeks the power and heat dispatch charts use,
    with the grid draw split into the part the optimiser cannot move (natural
    charging) and the part it can (smart charging), V2G returns below the axis,
    a dashed counterfactual showing the same energy charged uncontrolled, and
    the fleet-battery state of charge on the right-hand axis.

``ev_energy_by_mode``
    The annual version of the same split, per planning horizon, with the energy
    that actually reaches the vehicles marked on each bar -- the gap to the
    stacked total is the charging and V2G round-trip loss.

The dashed counterfactual is energy-neutral by construction (see
:func:`pypsa2html.extract.ev._counterfactual`), so the two curves enclose the
same area and their difference is purely a shift in time.
"""

from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go

from ..extract.balance import dispatch_window
from ..extract.ev import (
    COUNTERFACTUAL,
    DELIVERED,
    MODES,
    NET,
    V2G,
    EVCharging,
    annual_energy,
    charging_profiles,
)
from .base import (
    CHART_HEIGHT,
    FONT_SIZE,
    add_chart_data,
    combine_charts,
    resolve_tech_color,
    stacked_bar,
    strip_markup,
    tech_color_map,
)
from .dispatch import slim_dispatch_frame

logger = logging.getLogger(__name__)

#: MW → GW for the y-axis, and MWh → TWh for the annual chart.
_MW_TO_GW = 1e-3
_MWH_TO_TWH = 1e-6

#: Drop a series whose whole window stays below this (GW), so the legend only
#: lists things that are actually drawn.
_GW_THRESHOLD = 1e-4

#: ``model.dispatch_step_hours`` is applied only above this many points, so a
#: normal week keeps every snapshot: hourly it is 168 points and 6-hourly only
#: 28, where halving would visibly flatten the charging peaks the chart exists
#: to show, in exchange for a few kilobytes.  Values are still rounded, and a
#: deliberately longer configured window is still thinned.
_MIN_POINTS_TO_DOWNSAMPLE = 200

#: Right-hand axis for the state of charge, in % of installed fleet capacity.
_SOC_LABEL = "Fleet battery state of charge"


def charging_winter(ctx, node: str, section) -> go.Figure | None:
    return _charging_chart(ctx, node, section, season="winter")


def charging_summer(ctx, node: str, section) -> go.Figure | None:
    return _charging_chart(ctx, node, section, season="summer")


def energy_by_mode(ctx, node: str, section) -> go.Figure | None:
    """Annual EV grid energy by charging mode, per horizon."""
    table = annual_energy(ctx, node)
    if table is None:
        return None
    table = table * _MWH_TO_TWH
    add_chart_data(ctx, node, section.title, section.unit, table.T)

    delivered = table.loc[DELIVERED] if DELIVERED in table.index else None
    stack = table.loc[[m for m in MODES if m in table.index]]
    fig = stacked_bar(
        stack,
        tech_color_map(),
        unit=section.unit,
        title="",
        signed=True,
    )
    if fig is None:
        return None
    if delivered is not None and (delivered != 0).any():
        fig.add_trace(
            go.Scatter(
                x=[str(c) for c in table.columns],
                y=delivered.to_numpy(),
                name=DELIVERED,
                mode="markers",
                marker={
                    "symbol": "line-ew",
                    "size": 44,
                    "line": {"color": _color(DELIVERED), "width": 3},
                },
                hovertemplate="%{y:.3g}",
            )
        )
    fig.update_layout(hovermode="x unified", legend_title_text="")
    return fig


# ---------------------------------------------------------------------------
# The weekly charts
# ---------------------------------------------------------------------------

def _charging_chart(ctx, node: str, section, *, season: str) -> go.Figure | None:
    window = dispatch_window(ctx, season)
    if window is None:
        logger.warning("could not determine %s dispatch window", season)
        return None
    start, stop = window

    model = getattr(getattr(ctx, "config", None), "model", None)
    step_hours = int(getattr(model, "dispatch_step_hours", 2) or 1)
    decimals = int(getattr(model, "dispatch_value_decimals", 3) or 0)

    figures: list[tuple[str, go.Figure]] = []
    for horizon in ctx.horizons:
        data = charging_profiles(ctx, node, horizon, start=start, stop=stop)
        if data is None:
            continue
        fig = _profile_figure(
            data,
            unit=section.unit,
            title=section.title,
            horizon=horizon,
            step_hours=step_hours,
            decimals=decimals,
        )
        if fig is not None:
            figures.append((str(horizon), fig))

    if not figures:
        logger.warning("no EV charging data for node %s season %s", node, season)
        return None
    return combine_charts(figures, title=section.title, menu_title="Horizon")


def _profile_figure(
    data: EVCharging,
    *,
    unit: str,
    title: str,
    horizon: int,
    step_hours: int = 2,
    decimals: int = 3,
) -> go.Figure | None:
    frame = data.frame.astype(float) * _MW_TO_GW
    soc = data.soc
    if len(frame) >= _MIN_POINTS_TO_DOWNSAMPLE:
        frame = slim_dispatch_frame(frame, step_hours=step_hours, decimals=decimals)
        if soc is not None:
            soc = slim_dispatch_frame(soc.to_frame("soc"), step_hours=step_hours,
                                      decimals=1)["soc"]
    elif decimals >= 0:
        frame = frame.round(decimals)

    drawn = [c for c in MODES if c in frame and frame[c].abs().max() > _GW_THRESHOLD]
    if not drawn:
        return None

    fig = go.Figure()
    for column in drawn:
        _add_area(fig, frame[column], name=column)

    # The stack top is the net draw only when nothing is returned; with V2G the
    # net line is the number that matters and it is not visible in the stack.
    if V2G in drawn and NET in frame:
        fig.add_trace(
            go.Scatter(
                x=frame.index,
                y=frame[NET],
                name=NET,
                mode="lines",
                line={"width": 2, "color": _color(NET)},
                hovertemplate="%{y:.2f}",
            )
        )

    if COUNTERFACTUAL in frame:
        fig.add_trace(
            go.Scatter(
                x=frame.index,
                y=frame[COUNTERFACTUAL],
                name=f"{COUNTERFACTUAL} ({data.shape_source})",
                mode="lines",
                line={"width": 2, "color": _color(COUNTERFACTUAL), "dash": "dash"},
                hovertemplate="%{y:.2f}",
            )
        )

    if soc is not None and float(soc.abs().max()) > 0:
        fig.add_trace(
            go.Scatter(
                x=soc.index,
                y=soc,
                name=_SOC_LABEL,
                mode="lines",
                yaxis="y2",
                line={"width": 1.2, "color": _color(_SOC_LABEL), "dash": "dot"},
                opacity=0.6,
                hovertemplate="%{y:.0f}%",
            )
        )

    _layout(fig, data=data, frame=frame, unit=unit, title=title, horizon=horizon,
            with_soc=soc is not None)
    return fig


def _add_area(fig: go.Figure, series: pd.Series, *, name: str) -> None:
    """Add ``series`` as a stacked area, draws above the axis and returns below.

    Splitting on sign rather than trusting one stackgroup per series is what
    ``charts.dispatch`` does, and for the same reason: a series that crossed
    zero inside a single stackgroup would be drawn as a hole in the stack.
    """
    color = _color(name)
    shown = False
    for part, group in ((series.clip(lower=0), "draw"), (series.clip(upper=0), "return")):
        if not (part != 0).any():
            continue
        fig.add_trace(
            go.Scatter(
                x=series.index,
                y=part,
                name=name,
                mode="lines",
                line={"width": 0.5, "color": color},
                fillcolor=_translucent(color),
                stackgroup=group,
                legendgroup=name,
                showlegend=not shown,
                hovertemplate="%{y:.2f}",
            )
        )
        shown = True


def _layout(
    fig: go.Figure,
    *,
    data: EVCharging,
    frame: pd.DataFrame,
    unit: str,
    title: str,
    horizon: int,
    with_soc: bool,
) -> None:
    fig.update_layout(
        title=f"{title} — {horizon}" if title else str(horizon),
        hovermode="x unified",
        height=CHART_HEIGHT,
        font={"size": FONT_SIZE},
        legend_title_text="",
        legend={
            "orientation": "h",
            "yanchor": "top",
            "y": -0.16,
            "xanchor": "left",
            "x": 0,
        },
        margin={"l": 60, "r": 70 if with_soc else 30, "t": 90, "b": 90},
        xaxis={"title": "", "tickformat": "%a %d %b"},
        yaxis={
            "title": strip_markup(unit),
            "zeroline": True,
            "zerolinecolor": "#999999",
            "zerolinewidth": 1,
        },
    )
    if with_soc:
        fig.update_layout(
            yaxis2={
                "title": "State of charge (%)",
                "overlaying": "y",
                "side": "right",
                "range": [0, 105],
                "showgrid": False,
            }
        )
    caption = _caption(data, frame)
    if caption:
        fig.add_annotation(
            text=caption,
            xref="paper",
            yref="paper",
            x=0,
            y=1.10,
            xanchor="left",
            showarrow=False,
            align="left",
            font={"size": FONT_SIZE - 3, "color": "#555555"},
        )


def _caption(data: EVCharging, frame: pd.DataFrame) -> str:
    """One line of numbers the chart cannot show: peak shaving and V2G volume."""
    parts: list[str] = []
    if NET in frame and COUNTERFACTUAL in frame:
        peak = float(frame[NET].max())
        natural_peak = float(frame[COUNTERFACTUAL].max())
        if natural_peak > 0:
            change = peak / natural_peak - 1.0
            parts.append(
                f"peak {peak:.2f} GW vs {natural_peak:.2f} GW uncontrolled "
                f"({change:+.0%})"
            )
    returned = -data.energy_mwh(V2G) / 1e3
    if returned > 0:
        parts.append(f"V2G returned {returned:.1f} GWh")
    drawn = sum(data.energy_mwh(mode) for mode in MODES) / 1e3
    delivered = data.delivered_mwh / 1e3
    if drawn > 0 and delivered > 0:
        parts.append(f"{delivered:.1f} of {drawn:.1f} GWh drawn reached the vehicles")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

def _color(name: str) -> str:
    """Palette colour for one of the EV series names (``data/tech_colors.csv``)."""
    return resolve_tech_color(name, tech_color_map())


def _translucent(color: str, alpha: float = 0.75) -> str:
    """``#rrggbb`` → ``rgba(...)``, so stacked areas keep the grid readable."""
    text = str(color).lstrip("#")
    if len(text) != 6:
        return color
    try:
        r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return color
    return f"rgba({r},{g},{b},{alpha})"
