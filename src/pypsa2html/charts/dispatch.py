"""Dispatch time-series charts: stacked weekly supply/demand."""

from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go

from ..extract.balance import dispatch_window, energy_balance
from .base import CHART_HEIGHT, FONT_SIZE, combine_charts, strip_markup, tech_color_map

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


def _dispatch_chart(ctx, node: str, section, *, carrier: str, season: str) -> go.Figure | None:
    window = dispatch_window(ctx, season)
    if window is None:
        logger.warning("could not determine %s dispatch window", season)
        return None
    start, stop = window

    figures: list[tuple[str, go.Figure]] = []
    for horizon in ctx.horizons:
        balance = energy_balance(ctx, node, carrier, horizon, start=start, stop=stop)
        fig = _stacked_area(balance, unit=section.unit, title=section.title, horizon=horizon)
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
) -> go.Figure | None:
    if balance is None or balance.empty:
        return None

    data = balance.astype(float) * _MW_TO_GW
    data = data.where(data.abs() >= _GW_THRESHOLD, 0.0)
    data = data.loc[:, (data != 0).any()]
    if data.empty or not len(data.columns):
        return None

    positive = data.clip(lower=0)
    negative = data.clip(upper=0)
    colors = tech_color_map()
    fig = go.Figure()
    legend_shown: set[str] = set()

    for col in data.columns:
        pos = positive[col]
        if (pos != 0).any():
            fig.add_trace(
                go.Scatter(
                    x=data.index,
                    y=pos,
                    name=col,
                    mode="lines",
                    line={"width": 0.5, "color": colors.get(col, "black")},
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
                    line={"width": 0.5, "color": colors.get(col, "black")},
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
