"""District-heating charts: the network over a year, its store, and electric heat.

The weekly dispatch charts show how a heat network is balanced in one week and
say nothing about three things that decide its design:

* the **thermal store** fills and empties over weeks to months, so a winter and
  a summer week miss most of what it does;
* **electric heat** (boilers, heat pumps) runs in the hours when electricity is
  nearly free, which is a statement about the *year*, not about a week;
* a **heat pump** that would save most of that electricity is not built when
  the hours that would pay for it are too few — an investment question that
  one number per technology answers better than any dispatch.

So the four sections here are annual: :func:`year_view`, :func:`storage_year`,
:func:`by_price` and :func:`electric_heat_value`.  The numbers come from
:mod:`pypsa2html.extract.district_heating`.
"""

from __future__ import annotations

import html
import logging

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..extract.district_heating import (
    CHARGING,
    DISCHARGING,
    DistrictHeat,
    ElectricHeat,
    PriceGroups,
    band_labels,
    by_price_group,
    daily_mean,
    district_heat_year,
    electric_heat_value as electric_heat_extract,
)
from ..extract.heat import COLOR_KEYS, heat_color
from ..report.render import figure_to_html
from .base import FONT_SIZE, HEADER_MARGIN_TOP, Html, add_chart_data, combine_charts, tech_color_map

logger = logging.getLogger(__name__)

#: Colours of the thermal store: heat going in (light) and coming out (dark).
STORE_IN = COLOR_KEYS.get(CHARGING, "#9ecae1")
STORE_OUT = COLOR_KEYS.get(DISCHARGING, "#3182bd")
#: Labels of the two store bands in the year chart (net of the day).
STORE_FILLING = "Thermal store: filling"
STORE_RELEASING = "Thermal store: releasing"

#: Heat-price classes, light (heat is nearly free) to dark (heat is scarce).
BAND_COLORS = ("#ffe9a8", "#ffc15e", "#ee8a2c", "#b2301f")

#: Source order, bottom of the stack first: the plants that run for another
#: reason (CHP), then electric heat, then fuel boilers.
_SUPPLY_ORDER = ("CHP", "Geothermal", "Waste", "Power-to-X", "Fuel cell", "heat pump",
                 "Electric boiler", "boiler", "Solar thermal")

#: One colour per planning horizon, oldest first (the light end of the shared
#: palette is unreadable as a thin line).
_HORIZON_LINES = ("#9ecae1", "#4292c6", "#2171b5", "#08306b", "#d95f0e", "#993404")

#: Months that count as the heating season in the store chart.
_HEATING_SEASON = ((1, 3), (11, 12))

_NOTE = "<p class='p2h-note'>{}</p>"


def _rank(label: str) -> int:
    for position, fragment in enumerate(_SUPPLY_ORDER):
        if fragment in label:
            return position
    return len(_SUPPLY_ORDER)


def _ordered(labels) -> list[str]:
    return sorted(labels, key=lambda lbl: (_rank(str(lbl)), str(lbl)))


def _rgba(color: str, alpha: float) -> str:
    color = color.lstrip("#")
    if len(color) != 6:
        return f"rgba(120,120,120,{alpha})"
    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _horizons(ctx, node: str) -> list[tuple[int, DistrictHeat]]:
    out = []
    for horizon in getattr(ctx, "horizons", None) or []:
        dh = district_heat_year(ctx, node, horizon)
        if dh is not None and (dh.annual_demand_twh > 0 or not dh.producers.empty):
            out.append((int(horizon), dh))
    return out


def _initial(found: list[tuple[int, DistrictHeat]]) -> str:
    """The horizon the page opens on: the last one, where the network is largest."""
    return str(found[-1][0])


# ---------------------------------------------------------------------------
# 1. the year
# ---------------------------------------------------------------------------

def year_view(ctx, node: str, section) -> Html | None:
    """Supply, thermal store and prices of the district-heating network, daily."""
    found = _horizons(ctx, node)
    if not found:
        return None
    palette = tech_color_map()
    # The dropdown swaps the main axes, not the subplot grid: every horizon has to
    # share one set of panels, so the grid is decided here, once.
    show_store = any(dh.soc is not None for _, dh in found)
    show_prices = any(dh.heat_price is not None and dh.electricity_price is not None
                      for _, dh in found)
    figures = [(str(h), _year_figure(dh, palette, show_store, show_prices)) for h, dh in found]
    fig = combine_charts(figures, menu_title="Horizon", initial=_initial(found))
    if fig is None:
        return None
    note = _NOTE.format(
        "<b>How to read this.</b> <i>Top:</i> the heat delivered to the district-heating "
        "network by each source, stacked (daily mean power). The black line is the heat "
        "the network's users take. Where production is above the line the surplus goes "
        "into the thermal store (mirrored below zero, light blue); where it is below, "
        "the store releases heat (dark blue, on top of the stack). <i>Middle:</i> the "
        "energy held in the store. <i>Bottom:</i> the electricity price paid by the "
        "electric heating plant and the <b>heat price</b>, the cost of one more MWh of "
        "heat in the network that day. Both are shadow prices of the optimisation, not "
        "tariffs. They are shown for single regions only: the prices of different "
        "regions are not averaged."
    )
    return Html(figure_to_html(fig, plotly=ctx.config.output.plotly) + note)


def _year_figure(dh: DistrictHeat, palette: dict[str, str],
                 show_store: bool, show_prices: bool) -> go.Figure:
    weights = dh.weights
    has_store = dh.soc is not None
    has_prices = dh.heat_price is not None and dh.electricity_price is not None
    panels = ["supply"] + (["store"] if show_store else []) + (["prices"] if show_prices else [])
    heights = {"supply": 0.52, "store": 0.24, "prices": 0.24}
    row_heights = [heights[p] for p in panels]
    scale = sum(row_heights)
    total = dh.producers.mul(weights, axis=0).sum().sum() * 1e-6
    titles = [f"Heat delivered to the network: {total:.2f} TWh a year (GW, daily mean)"]
    if show_store:
        titles.append("Energy in the thermal store (GWh)")
    if show_prices:
        titles.append("Prices of the day (EUR/MWh)")
    fig = make_subplots(
        rows=len(panels), cols=1, shared_xaxes=True, vertical_spacing=0.07,
        row_heights=[h / scale for h in row_heights], subplot_titles=titles,
    )

    gw = daily_mean(dh.producers, weights) * 1e-3
    for label in _ordered(gw.columns):
        color = heat_color(label, palette)
        fig.add_trace(go.Scatter(
            x=gw.index, y=gw[label].round(4), name=label, mode="lines",
            line={"width": 0.6, "color": color}, fillcolor=_rgba(color, 0.9),
            stackgroup="supply", legendgroup=label, hovertemplate="%{y:.3f} GW",
        ), row=1, col=1)
    net = daily_mean(dh.discharging - dh.charging, weights) * 1e-3
    if float(net.abs().max() or 0.0) > 0:
        fig.add_trace(go.Scatter(
            x=net.index, y=net.clip(lower=0).round(4), name=STORE_RELEASING, mode="lines",
            line={"width": 0.6, "color": STORE_OUT}, fillcolor=_rgba(STORE_OUT, 0.9),
            stackgroup="supply", legendgroup=STORE_RELEASING, hovertemplate="%{y:.3f} GW",
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=net.index, y=net.clip(upper=0).round(4), name=STORE_FILLING, mode="lines",
            line={"width": 0.6, "color": STORE_IN}, fillcolor=_rgba(STORE_IN, 0.9),
            stackgroup="filling", legendgroup=STORE_FILLING, hovertemplate="%{y:.3f} GW",
        ), row=1, col=1)
    demand = daily_mean(dh.demand, weights) * 1e-3
    fig.add_trace(go.Scatter(
        x=demand.index, y=demand.round(4), name="Heat demand", mode="lines",
        line={"width": 1.6, "color": "#111111"}, hovertemplate="%{y:.3f} GW",
    ), row=1, col=1)
    fig.update_yaxes(title_text="GW", row=1, col=1)

    row = 2
    if show_store and has_store:
        soc = dh.soc.resample("D").mean() * 1e-3
        fig.add_trace(go.Scatter(
            x=soc.index, y=soc.round(3), name="Energy in the thermal store", mode="lines",
            line={"width": 1.2, "color": STORE_OUT}, fill="tozeroy",
            fillcolor=_rgba(STORE_OUT, 0.35), hovertemplate="%{y:.1f} GWh",
        ), row=row, col=1)
        if dh.soc_capacity:
            cap = dh.soc_capacity * 1e-3
            fig.add_trace(go.Scatter(
                x=[soc.index[0], soc.index[-1]], y=[cap, cap], mode="lines",
                name=f"Store capacity ({cap:.0f} GWh)",
                line={"width": 1, "color": "#555555", "dash": "dash"}, hoverinfo="skip",
            ), row=row, col=1)
    if show_store:
        fig.update_yaxes(title_text="GWh", rangemode="tozero", row=row, col=1)
        row += 1
    if show_prices and has_prices:
        pe = daily_mean(dh.electricity_price, weights)
        ph = daily_mean(dh.heat_price, weights)
        fig.add_trace(go.Scatter(
            x=pe.index, y=pe.round(1), name="Electricity price", mode="lines",
            line={"width": 1.3, "color": "#1f4e79"}, hovertemplate="%{y:.0f} EUR/MWh",
        ), row=row, col=1)
        fig.add_trace(go.Scatter(
            x=ph.index, y=ph.round(1), name="Heat price (cost of one more MWh of heat)",
            mode="lines", line={"width": 1.3, "color": "#c0392b"},
            hovertemplate="%{y:.0f} EUR/MWh",
        ), row=row, col=1)
    if show_prices:
        fig.update_yaxes(title_text="EUR/MWh", rangemode="tozero", row=row, col=1)

    fig.update_xaxes(tickformat="%b", dtick="M1", row=len(panels), col=1)
    for annotation in fig.layout.annotations:
        annotation.font.size = 14
        annotation.xanchor = "left"
        annotation.x = 0.0
    fig.update_layout(
        height=330 + 230 * len(panels), hovermode="x unified", font={"size": FONT_SIZE},
        legend={"orientation": "h", "yanchor": "top", "y": -0.08 if len(panels) > 1 else -0.12,
                "x": 0.0, "font": {"size": 13}},
        margin={"l": 60, "r": 30, "t": HEADER_MARGIN_TOP, "b": 130},
    )
    return fig


# ---------------------------------------------------------------------------
# 2. the store, all horizons
# ---------------------------------------------------------------------------

def storage_year(ctx, node: str, section) -> Html | None:
    """Energy in the thermal store through the year, one panel per horizon."""
    found = [(h, dh) for h, dh in _horizons(ctx, node) if dh.soc is not None and dh.soc_capacity]
    if not found:
        return None
    titles, rows = [], []
    for horizon, dh in found:
        cap = dh.soc_capacity * 1e-3
        cycles = dh.store_cycles or 0.0
        titles.append(f"{horizon}: store of {cap:.0f} GWh, {cycles:.1f} full cycles a year")
        released = float(dh.discharging.mul(dh.weights).sum()) * 1e-3
        rows.append((horizon, cap, released, cycles, dh.annual_demand_twh * 1e3))
    fig = make_subplots(
        rows=len(found), cols=1, shared_xaxes=True, vertical_spacing=0.04, subplot_titles=titles,
    )
    for i, (horizon, dh) in enumerate(found, start=1):
        soc = dh.soc.resample("D").mean()
        pct = soc / dh.soc_capacity * 100.0
        fig.add_trace(go.Scatter(
            x=pct.index, y=pct.round(1), mode="lines", showlegend=False,
            line={"width": 1.3, "color": STORE_OUT}, fill="tozeroy", fillcolor=_rgba(STORE_OUT, 0.4),
            customdata=(soc * 1e-3).round(2), name=str(horizon),
            hovertemplate="%{y:.0f} % full (%{customdata:.1f} GWh)",
        ), row=i, col=1)
        fig.update_yaxes(range=[0, 105], tickvals=[0, 50, 100], row=i, col=1)
    year = int(pd.DatetimeIndex(found[0][1].index)[0].year)
    for first, last in _HEATING_SEASON:
        end = pd.Timestamp(year=year, month=last, day=1) + pd.offsets.MonthEnd(0)
        fig.add_vrect(
            x0=pd.Timestamp(year=year, month=first, day=1), x1=end, layer="below",
            fillcolor="rgba(120,140,200,0.12)", line_width=0, row="all", col=1,
        )
    for month in (2, 12):          # above the first panel's title, clear of it
        fig.add_annotation(
            x=pd.Timestamp(year=year, month=month, day=1 if month == 12 else 15), y=1.0,
            yref="paper", yanchor="bottom", yshift=24, text="heating season",
            showarrow=False, font={"size": 12, "color": "#5c5c5c"},
        )
    fig.add_annotation(
        x=-0.055, y=0.5, xref="paper", yref="paper", textangle=-90, showarrow=False,
        text="% of the store's capacity", font={"size": 14},
    )
    for annotation in fig.layout.annotations[: len(found)]:
        annotation.font.size = 14
        annotation.xanchor = "left"
        annotation.x = 0.0
    fig.update_xaxes(tickformat="%b", dtick="M1", row=len(found), col=1)
    fig.update_layout(
        height=140 + 150 * len(found), hovermode="x unified", font={"size": FONT_SIZE},
        margin={"l": 70, "r": 30, "t": 75, "b": 50},
    )
    note = _NOTE.format(
        "<b>How to read this.</b> Energy held in the district-heating thermal store as a "
        "share of its capacity (daily mean), one panel per planning horizon; the panel "
        "titles give the capacity and the number of full cycles per year (energy released "
        "over capacity). A store that fills in summer and empties in winter is "
        "<i>seasonal</i> and cycles about once a year. One that fills and empties several "
        "times a year is a buffer for spells of weeks (cold, still weather against windy, "
        "sunny weeks): a few cycles a year already mean it is used far beyond a daily "
        "buffer. Shaded: January to March and November to December."
    )
    table = _store_table(rows)
    return Html(figure_to_html(fig, plotly=ctx.config.output.plotly) + note + table)


def _store_table(rows: list[tuple]) -> str:
    head = "<th>Horizon</th>" + "".join(
        f"<th class='num'>{h}</th>" for h in
        ("Capacity (GWh)", "Released (GWh)", "Full cycles a year", "Annual heat demand (GWh)"))
    body = []
    for horizon, cap, released, cycles, demand in rows:
        body.append(
            f"<tr><td>{horizon}</td><td class='num'>{cap:.1f}</td>"
            f"<td class='num'>{released:.0f}</td><td class='num'>{cycles:.1f}</td>"
            f"<td class='num'>{demand:.0f}</td></tr>"
        )
    return ("<table class='p2h-table'><thead><tr>" + head + "</tr></thead><tbody>"
            + "".join(body) + "</tbody></table>")


# ---------------------------------------------------------------------------
# 3. heat supplied, by the price of electricity
# ---------------------------------------------------------------------------

def by_price(ctx, node: str, section) -> Html | None:
    """Heat delivered in the cheapest to the dearest tenth of the hours."""
    palette = tech_color_map()
    figures = []
    last = None
    for horizon in getattr(ctx, "horizons", None) or []:
        groups = by_price_group(ctx, node, horizon)
        if groups is None or float(groups.demand.sum()) <= 0:
            continue
        figures.append((str(horizon), _price_figure(groups, palette)))
        last = str(horizon)
        add_chart_data(ctx, node, f"{section.title} {horizon}", section.unit,
                       _price_frame(groups))
    if not figures:
        return None
    fig = combine_charts(figures, menu_title="Horizon", initial=last)
    note = _NOTE.format(
        "<b>How to read this.</b> The hours of the year are sorted by the price of "
        "electricity and cut into ten groups of equal length, from the cheapest tenth "
        "(left) to the dearest (right); the labels give the price range in EUR/MWh. Each "
        "bar adds up the heat each source delivered in those hours; the light blue bar "
        "below zero is heat going into the thermal store and the dark blue part is heat "
        "coming out of it. The dash is the heat the network's users took in those hours. "
        "The red line (right axis) is the mean <b>heat price</b>: what one more MWh of "
        "heat was worth. <i>What to look for:</i> electric boilers and heat pumps in the "
        "cheap groups, where heat is nearly worthless, and CHP plants, whose heat is a "
        "by-product of electricity, in the dear ones."
    )
    return Html(figure_to_html(fig, plotly=ctx.config.output.plotly) + note)


def _price_labels(groups: PriceGroups) -> list[str]:
    """Price range of each group; the dearest one reads ``> x`` (its top is a spike)."""
    out = []
    last = len(groups.low) - 1
    for k, (lo, hi) in enumerate(zip(groups.low, groups.high)):
        if round(lo) == round(hi):
            out.append(f"{hi:.0f}")
        elif k == last:
            out.append(f"> {lo:.0f}")
        else:
            out.append(f"{lo:.0f}–{hi:.0f}")
    return out


def _price_figure(groups: PriceGroups, palette: dict[str, str]) -> go.Figure:
    labels = _price_labels(groups)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    for label in _ordered(groups.producers.columns):
        if float(groups.producers[label].abs().sum()) <= 0:
            continue
        fig.add_trace(go.Bar(
            x=labels, y=groups.producers[label].round(2).to_numpy(), name=label,
            marker={"color": heat_color(label, palette)}, legendgroup=label,
            hovertemplate="%{y:.1f} GWh",
        ), secondary_y=False)
    if float(groups.discharging.sum()) > 0:
        fig.add_trace(go.Bar(
            x=labels, y=np.round(groups.discharging, 2), name="Thermal store: releasing",
            marker={"color": STORE_OUT}, hovertemplate="%{y:.1f} GWh",
        ), secondary_y=False)
    if float(groups.charging.sum()) > 0:
        fig.add_trace(go.Bar(
            x=labels, y=-np.round(groups.charging, 2), name="Thermal store: filling",
            marker={"color": STORE_IN}, hovertemplate="%{y:.1f} GWh",
        ), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=labels, y=np.round(groups.demand, 2), mode="markers", name="Heat demand",
        marker={"symbol": "line-ew", "size": 30, "color": "#111111", "line": {"width": 3, "color": "#111111"}},
        hovertemplate="%{y:.1f} GWh",
    ), secondary_y=False)
    if groups.heat_price is not None:
        fig.add_trace(go.Scatter(
            x=labels, y=np.round(groups.heat_price, 1), mode="lines+markers",
            name="Heat price (right axis)", line={"width": 2, "color": "#c0392b"},
            marker={"size": 7}, hovertemplate="%{y:.0f} EUR/MWh",
        ), secondary_y=True)
    fig.update_layout(
        barmode="relative", height=600, font={"size": FONT_SIZE}, hovermode="x unified",
        xaxis={"title": "Electricity price of the hours, EUR/MWh: the year in ten equal groups "
                        "of hours, cheapest to dearest", "tickfont": {"size": 13}},
        legend={"orientation": "h", "yanchor": "top", "y": -0.2, "x": 0.0, "font": {"size": 13}},
        margin={"l": 60, "r": 60, "t": HEADER_MARGIN_TOP, "b": 150},
    )
    fig.update_yaxes(title_text="GWh of heat in the group", secondary_y=False)
    fig.update_yaxes(title_text="Heat price, EUR/MWh", rangemode="tozero", showgrid=False,
                     secondary_y=True)
    return fig


def _price_frame(groups: PriceGroups) -> pd.DataFrame:
    frame = groups.producers.copy()
    frame.index = _price_labels(groups)
    frame["Thermal store: releasing"] = groups.discharging
    frame["Thermal store: filling"] = -groups.charging
    frame["Heat demand"] = groups.demand
    if groups.heat_price is not None:
        frame["Heat price (EUR/MWh)"] = groups.heat_price
    return frame


# ---------------------------------------------------------------------------
# 4. is electric heat worth building?
# ---------------------------------------------------------------------------

def electric_heat_value(ctx, node: str, section) -> Html | None:
    """What one MW_th of each electric heating technology earns, against its cost."""
    found: list[tuple[int, list[ElectricHeat], dict]] = []
    for horizon in getattr(ctx, "horizons", None) or []:
        result = electric_heat_extract(ctx, node, horizon)
        if result is not None:
            found.append((int(horizon), result[0], result[1]))
    if not found:
        return None
    figures = [(str(h), _value_figure(techs, info)) for h, techs, info in found]
    fig = combine_charts(figures, menu_title="Horizon", initial=str(found[-1][0]))
    if fig is None:
        return None
    add_chart_data(ctx, node, section.title, section.unit, _value_frame(found))
    note = _NOTE.format(
        "<b>How to read this.</b> What would one more MW of heat output of each electric "
        "heating technology earn at the prices of this solution, and what does it cost? "
        "<i>Earnings</i> (stacked bars) are the heat price minus the cost of the "
        "electricity needed to make that heat, summed over the hours where this is "
        "positive, and split by how dear heat was in those hours. The <i>black bar</i> is "
        "the annualised capital cost. The optimiser adds capacity until the last MW "
        "earns exactly its cost, so a technology whose earnings fall short of the black "
        "bar is <b>not built</b>, and one that reaches it is built to the point where it "
        "just pays. These are marginal values: a large new plant would lower the heat "
        "price in the hours it serves, so they are an upper bound for any capacity "
        "actually added."
    )
    return Html(figure_to_html(fig, plotly=ctx.config.output.plotly) + note + _value_table(found))


def _value_figure(techs: list[ElectricHeat], info: dict) -> go.Figure:
    labels = [
        f"{t.label}<br>{t.installed_mw_th:,.0f} MW_th built" if t.installed_mw_th >= 1.0
        else f"{t.label}<br>not built"
        for t in techs
    ]
    fig = go.Figure()
    names = band_labels()
    hours = info.get("hours")
    for k, name in enumerate(names):
        values = [float(t.margin_bands.iloc[k]) * 1e-3 for t in techs]
        if not any(values):
            continue
        suffix = f" ({hours[k]:,.0f} h)" if hours is not None and hours[k] > 0 else ""
        fig.add_trace(go.Bar(
            x=labels, y=[round(v, 3) for v in values], name=f"Earned in hours when heat costs {name} EUR/MWh{suffix}",
            marker={"color": BAND_COLORS[min(k, len(BAND_COLORS) - 1)]},
            hovertemplate="%{y:.1f} EUR/kW a year",
        ))
    fig.add_trace(go.Scatter(
        x=labels, y=[round(t.capital_cost * 1e-3, 3) for t in techs], mode="markers",
        name="Annualised capital cost",
        marker={"symbol": "line-ew", "size": 90, "color": "#111111", "line": {"width": 4, "color": "#111111"}},
        hovertemplate="%{y:.1f} EUR/kW a year",
    ))
    top = max([t.capital_cost * 1e-3 for t in techs] + [t.margin * 1e-3 for t in techs]) or 1.0
    for t, label in zip(techs, labels):
        verdict = "built where it just pays" if t.coverage >= 0.995 else "not built"
        fig.add_annotation(
            x=label, y=max(t.capital_cost, t.margin) * 1e-3, yshift=26, showarrow=False,
            text=(f"earns {t.margin * 1e-3:,.0f} of {t.capital_cost * 1e-3:,.0f} EUR/kW a year "
                  f"({100 * t.coverage:.0f} %)<br><i>{verdict}</i>"),
            font={"size": 13},
        )
    fig.update_layout(
        barmode="stack", height=560, font={"size": FONT_SIZE},
        yaxis={"title": "EUR per kW of heat output and year", "range": [0, top * 1.28]},
        legend={"orientation": "h", "yanchor": "top", "y": -0.18, "x": 0.0, "font": {"size": 13}},
        margin={"l": 70, "r": 30, "t": HEADER_MARGIN_TOP, "b": 150},
    )
    return fig


def _value_frame(found) -> pd.DataFrame:
    rows = {}
    for horizon, techs, _ in found:
        for t in techs:
            rows[f"{t.label} {horizon}"] = {
                "Built (MW_th)": t.installed_mw_th, "Added (MW_th)": t.new_mw_th,
                "Heat (TWh_th)": t.heat_twh, "Full-load hours": t.full_load_hours,
                "Capital cost (EUR/kW_th/yr)": t.capital_cost * 1e-3,
                "Earnings (EUR/kW_th/yr)": t.margin * 1e-3, "Earnings / cost": t.coverage,
            }
    return pd.DataFrame(rows)


def _value_table(found) -> str:
    head = "<th>Horizon</th><th>Technology</th>" + "".join(
        f"<th class='num'>{h}</th>" for h in
        ("Built (MW_th)", "Heat (TWh_th)", "Full-load hours", "Capital cost (EUR/kW a year)",
         "Earnings (EUR/kW a year)", "Earnings / cost"))
    body = []
    for horizon, techs, _ in found:
        for t in techs:
            cop = f" (COP {t.cop:.1f})" if t.cop else ""
            body.append(
                f"<tr><td>{horizon}</td><td>{html.escape(t.label)}{cop}</td>"
                f"<td class='num'>{t.installed_mw_th:,.0f}</td><td class='num'>{t.heat_twh:.2f}</td>"
                f"<td class='num'>{t.full_load_hours:,.0f}</td>"
                f"<td class='num'>{t.capital_cost * 1e-3:,.1f}</td>"
                f"<td class='num'>{t.margin * 1e-3:,.1f}</td>"
                f"<td class='num'>{100 * t.coverage:.0f} %</td></tr>"
            )
    return ("<table class='p2h-table'><thead><tr>" + head + "</tr></thead><tbody>"
            + "".join(body) + "</tbody></table>"
            + _NOTE.format("Earnings / cost below 100 %: the technology would be built if its "
                           "annualised capital cost were lower by that shortfall (through its "
                           "investment cost or the discount rate)."))
