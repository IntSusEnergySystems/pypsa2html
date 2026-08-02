"""Dispatch time-series charts: stacked weekly supply/demand."""

from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go

from ..extract.balance import dispatch_window, energy_balance
from .base import (
    CHART_HEIGHT,
    FONT_SIZE,
    combine_charts,
    resolve_tech_color,
    strip_markup,
    tech_color_map,
)

logger = logging.getLogger(__name__)

#: MW → GW for the y-axis.
_MW_TO_GW = 1e-3

#: Point filter applied after GW conversion (matches legacy display threshold).
_GW_THRESHOLD = 0.1


def power_winter(ctx, node: str, section) -> go.Figure | None:
    return _dispatch_chart(ctx, node, section, carrier="AC", season="winter")


def power_summer(ctx, node: str, section) -> go.Figure | None:
    return _dispatch_chart(ctx, node, section, carrier="AC", season="summer")


def heat_winter(ctx, node: str, section) -> go.Figure | None:
    return _dispatch_chart(ctx, node, section, carrier="heat", season="winter")


def heat_summer(ctx, node: str, section) -> go.Figure | None:
    return _dispatch_chart(ctx, node, section, carrier="heat", season="summer")


def slim_dispatch_frame(
    data: pd.DataFrame,
    *,
    step_hours: int = 2,
    decimals: int = 3,
) -> pd.DataFrame:
    """Downsample and round a dispatch frame for lighter plotly serialisation.

    Keeps every ``step_hours``-th row (``1`` = no downsampling) and rounds
    values so the embedded JSON is not full float64 noise.  Chart *values* at
    retained timestamps stay faithful; only on-page density changes.
    """
    out = data
    step = max(int(step_hours), 1)
    if step > 1 and len(out) > 1:
        out = out.iloc[::step]
    if decimals is not None and decimals >= 0:
        out = out.round(int(decimals))
    return out


def _dispatch_chart(ctx, node: str, section, *, carrier: str, season: str) -> go.Figure | None:
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
        balance = energy_balance(ctx, node, carrier, horizon, start=start, stop=stop)
        fig = _stacked_area(
            balance,
            unit=section.unit,
            title=section.title,
            horizon=horizon,
            step_hours=step_hours,
            decimals=decimals,
        )
        if fig is not None:
            figures.append((str(horizon), fig))

    if not figures:
        logger.warning(
            "no dispatch data for node %s carrier %r season %s", node, carrier, season
        )
        return None
    return combine_charts(figures, title=section.title, menu_title="Horizon")


def _stacked_area(
    balance: pd.DataFrame | None,
    *,
    unit: str,
    title: str,
    horizon: int,
    step_hours: int = 2,
    decimals: int = 3,
) -> go.Figure | None:
    if balance is None or balance.empty:
        return None

    data = balance.astype(float) * _MW_TO_GW
    data = data.where(data.abs() >= _GW_THRESHOLD, 0.0)
    data = data.loc[:, (data != 0).any()]
    if data.empty or not len(data.columns):
        return None
    data = slim_dispatch_frame(data, step_hours=step_hours, decimals=decimals)

    positive = data.clip(lower=0)
    negative = data.clip(upper=0)
    colors = tech_color_map()
    fig = go.Figure()
    legend_shown: set[str] = set()

    for col in data.columns:
        color = resolve_tech_color(str(col), colors)
        pos = positive[col]
        if (pos != 0).any():
            fig.add_trace(
                go.Scatter(
                    x=data.index,
                    y=pos,
                    name=col,
                    mode="lines",
                    line={"width": 0.5, "color": color},
                    stackgroup="positive",
                    legendgroup=col,
                    showlegend=col not in legend_shown,
                    hovertemplate="%{y:.2f}",
                )
            )
            legend_shown.add(col)
        neg = negative[col]
        if (neg != 0).any():
            fig.add_trace(
                go.Scatter(
                    x=data.index,
                    y=neg,
                    name=col,
                    mode="lines",
                    line={"width": 0.5, "color": color},
                    stackgroup="negative",
                    legendgroup=col,
                    showlegend=col not in legend_shown,
                    hovertemplate="%{y:.2f}",
                )
            )
            legend_shown.add(col)

    fig.update_layout(
        title=f"{title} — {horizon}" if title else str(horizon),
        hovermode="x",
        height=CHART_HEIGHT,
        yaxis_title=strip_markup(unit),
        font={"size": FONT_SIZE},
        legend_title_text="",
        margin={"l": 60, "r": 30, "t": 60, "b": 50},
        xaxis={"title": "Time", "tickformat": "%m-%d"},
    )
    return fig
