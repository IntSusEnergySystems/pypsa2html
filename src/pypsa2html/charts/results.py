"""Cost, capacity and demand chart builders named in ``data/pages.yaml``.

Each builder has the ``(ctx, node, section) -> Figure | None`` signature from
``docs/INTERNALS.md`` §4.  Data comes from :mod:`pypsa2html.extract.tables`.

Capacity pages expose both a full stacked bar (all technologies) and the
legacy faceted panels (curated technology groups).
"""

from __future__ import annotations

import logging
from collections.abc import Collection

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..carriers import ccs_parent_carrier
from ..extract.tables import (
    UTILISATION_FOLD,
    capacity_table,
    cost_table,
    demand_table,
    fold_utilisation_index,
    utilisation_table,
)
from .base import (
    FONT_SIZE,
    add_chart_data,
    grouped_bar,
    resolve_tech_color,
    stacked_bar,
    tech_color_map,
)

logger = logging.getLogger(__name__)


def _study_wide_panels(ctx, node: str) -> bool:
    """Faceted CHP/PtL swap is for the study-wide sum, not a group aggregate."""
    if hasattr(ctx, "is_study_wide"):
        return ctx.is_study_wide(node)
    return hasattr(ctx, "is_aggregate") and ctx.is_aggregate(node)


#: MW -> GW / MWh -> GWh for capacity charts.
_CAPACITY_SCALE = 1e-3

#: The three power-to-fuel conversion routes share one panel: they compete for
#: the same electricity and each other's output (H2 feeds Fischer-Tropsch and
#: methanation), so the interesting comparison is between them, not between
#: each one and a blank neighbour.  They used to occupy three separate panels.
_POWER_TO_FUEL: list[str] = ["electrolysis", "methanation", "Fischer-Tropsch"]

#: The three PV carriers are reported separately in the *capacity* charts.
#: They are not interchangeable — ground-mounted and tracking PV compete for
#: land, rooftop PV does not, they carry different capital costs (79.1 / 80.9 /
#: 97.3 kEUR/MW/a in 2050) and only rooftop is exempt from the
#: ``electricity grid connection`` adder — and in the Walloon runs the split
#: moves while the total barely does: ground PV falls to zero by 2050 while
#: tracking PV appears, which a single "solar" bar hides completely.
#: Their *capacity factors* are near-identical (11.1 / 11.1 / 12.9 %), so the
#: utilisation charts fold them back — see
#: :data:`pypsa2html.extract.tables.UTILISATION_FOLD`.
_SOLAR_PV: list[str] = [
    "solar PV (rooftop)",
    "solar PV (ground)",
    "solar PV (tracking)",
]

#: Faceted capacity panels — port of ``create_capacity_chart`` groups.
_POWER_GROUPS: list[list[str]] = [
    _SOLAR_PV,
    ["onshore wind", "offshore wind"],
    ["power-to-heat"],
    _POWER_TO_FUEL,
    ["transmission lines"],
    ["nuclear"],
    ["CCGT"],
    ["CHP"],
]

#: Aggregate-node panels swap CHP for power-to-liquid (legacy ``groupss``).
_POWER_GROUPS_AGGREGATE: list[list[str]] = [
    _SOLAR_PV,
    ["onshore wind", "offshore wind"],
    ["power-to-heat"],
    _POWER_TO_FUEL,
    ["transmission lines"],
    ["CCGT"],
    ["nuclear"],
]

_STORAGE_GROUPS: list[list[str]] = [
    ["Grid-scale battery"],
    ["Thermal Energy Storage"],
    ["Gas storage"],
]


def _bar(ctx, node, section, table, *, signed=True, scale=1.0) -> go.Figure | None:
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    return stacked_bar(
        table,
        tech_color_map(),
        unit=section.unit,
        title="",
        signed=signed,
        scale=scale,
    )


def annual_costs(ctx, node: str, section) -> go.Figure | None:
    return _bar(ctx, node, section, cost_table(ctx, node, "total"), signed=True)


def clustered_costs(ctx, node: str, section) -> go.Figure | None:
    return _bar(ctx, node, section, cost_table(ctx, node, "clustered"), signed=False)


def investment_costs(ctx, node: str, section) -> go.Figure | None:
    return _bar(ctx, node, section, cost_table(ctx, node, "capital"), signed=True)


def operational_costs(ctx, node: str, section) -> go.Figure | None:
    return _bar(ctx, node, section, cost_table(ctx, node, "marginal"), signed=True)


def _smart_capitalize(phrase: str) -> str:
    if not phrase or phrase[0].isupper():
        return phrase
    return phrase[0].upper() + phrase[1:]


#: Panels whose capacity is not a plain electrical rating.
#:
#: Every power plant is restated to **GW_e** by
#: :func:`pypsa2html.extract.capacity_filter.electric_output_scaling`, so CCGT,
#: CHP, OCGT and nuclear share an axis with wind and solar Generators.  Two
#: groups keep another convention and say so in their title:
#:
#: * ``power-to-heat`` is restated on the *heat* side by
#:   :func:`pypsa2html.extract.capacity_filter.heat_output_scaling` — heat-pump
#:   ``p_nom`` is already MW_th and resistive heaters are scaled by their
#:   efficiency — so the panel reads GW_th;
#: * the power-to-fuel routes are rated at their **input**: electrolysis in
#:   GW_e of electricity drawn, Fischer-Tropsch and methanation in GW of H2
#:   consumed.  Their fuel *output* is smaller by the conversion efficiency.
_PANEL_UNIT_NOTE = {
    "power-to-heat": "thermal",
    "electrolysis": "input-rated",
    "methanation": "input-rated",
    "Fischer-Tropsch": "input-rated",
}


#: Panels whose member list is longer than a useful heading.  Subplot titles
#: are centred over columns barely 150 px wide, so a long one runs into its
#: neighbour; the members are named in the legend either way.
_PANEL_TITLES: dict[tuple[str, ...], str] = {
    tuple(_POWER_TO_FUEL): "Power-to-fuel",
    tuple(_SOLAR_PV): "Solar PV",
    ("onshore wind", "offshore wind"): "Wind",
}

#: Capacity panels drawn as one stacked bar per year rather than side-by-side
#: bars.  Only PV: its three carriers are *additive* — they are the same fleet
#: split by mounting, and the reader's first question is the Walloon PV total,
#: which three separate bars force them to add by eye.  The other panels hold
#: technologies that are alternatives to each other (onshore vs offshore wind,
#: the three power-to-fuel routes competing for the same electricity), where a
#: stack would invent a meaningless total.
#:
#: Capacity charts only.  Capacity *factors* are intensive: stacking three
#: percentages would produce a number no technology has.  The utilisation
#: panels also fold PV to a single ``solar`` bar (:data:`UTILISATION_FOLD`),
#: so there is nothing to stack there in the first place.
_STACKED_CAPACITY_PANELS: tuple[str, ...] = (_PANEL_TITLES[tuple(_SOLAR_PV)],)


def _panel_title(techs: list[str]) -> str:
    title = _PANEL_TITLES.get(
        tuple(techs), ", ".join(_smart_capitalize(t) for t in techs)
    )
    notes = sorted({_PANEL_UNIT_NOTE[t] for t in techs if t in _PANEL_UNIT_NOTE})
    if notes:
        title += f" ({', '.join(notes)})"
    return title


def _expand_capacity_group(group: list[str], index) -> list[str]:
    """Keep curated panel members, and append CCS siblings present in ``index``.

    ``CCGT`` therefore also shows ``CCGT CC`` when that carrier exists, without
    listing every capture-equipped plant in :data:`_POWER_GROUPS`.
    """
    names = list(group)
    seen = set(names)
    for extra in index:
        extra_s = str(extra)
        if extra_s in seen:
            continue
        parent = ccs_parent_carrier(extra_s)
        if parent in seen:
            names.append(extra_s)
            seen.add(extra_s)
    return names


def _faceted_capacity_chart(
    table: pd.DataFrame,
    groups: list[list[str]],
    *,
    unit: str,
    scale: float = _CAPACITY_SCALE,
    shared_y: bool = True,
    height: int = 800,
    fill_missing: bool = True,
    hover_fmt: str = "%{y:.3g}",
    stacked_groups: Collection[str] = (),
) -> go.Figure | None:
    """Legacy-style capacity panels: one subplot per technology group.

    Missing technologies still get a zero-height bar so the panel (e.g.
    Nuclear) is never a blank hole when the group is configured.

    ``fill_missing=False`` drops that filler instead.  Utilisation panels need
    it: a zero-height *capacity* bar honestly says "none installed", while a
    zero-height *capacity factor* says "installed and never used" — the
    opposite of the truth.

    ``stacked_groups`` names the panels — by their :func:`_panel_title` — whose
    members stack into one bar per year instead of standing side by side.
    Plotly's ``barmode`` is figure-wide, so the per-panel choice is made with
    ``offsetgroup``: under ``barmode="relative"`` traces sharing an offsetgroup
    stack, and traces with distinct offsetgroups each get their own slot, which
    reproduces ``barmode="group"`` for every panel not listed.  Capacities are
    non-negative, so ``relative`` and ``stack`` render identically here.
    """
    if table is None or table.empty:
        return None

    data = table.astype(float) * scale
    years = [str(c) for c in data.columns]
    n_groups = len(groups)
    if n_groups == 0:
        return None

    # Prefer a 2×N grid when there are several panels; storage uses 1×N.
    if n_groups <= 3:
        rows, cols = 1, n_groups
    else:
        cols = (n_groups + 1) // 2
        rows = 2

    titles = [_panel_title(g) for g in groups]
    fig = make_subplots(
        rows=rows,
        cols=cols,
        subplot_titles=titles,
        shared_yaxes=shared_y,
    )
    palette = tech_color_map()
    any_trace = False
    stacked = {str(name) for name in stacked_groups}

    for i, tech_group in enumerate(groups):
        if rows == 1:
            row_idx, col_idx = 1, i + 1
        else:
            row_idx = 1 if i < cols else 2
            col_idx = i + 1 if i < cols else i - cols + 1
        stack_panel = titles[i] in stacked

        for tech in _expand_capacity_group(tech_group, data.index):
            if tech in data.index:
                series = data.loc[tech, years]
            elif fill_missing:
                series = pd.Series(0.0, index=years)
            else:
                continue
            fig.add_trace(
                go.Bar(
                    x=years,
                    y=series.values,
                    name=_smart_capitalize(tech),
                    marker_color=resolve_tech_color(tech, palette),
                    hovertemplate=hover_fmt,
                    # One slot for the whole panel when it stacks, one slot per
                    # technology otherwise — see the docstring.
                    offsetgroup=f"p{i}" if stack_panel else f"p{i}-{tech}",
                ),
                row=row_idx,
                col=col_idx,
            )
            any_trace = True

    if not any_trace:
        return None

    for annotation in fig.layout.annotations:
        annotation.font.size = 14  # subplot titles: fit the column, see _PANEL_TITLES

    fig.update_layout(
        height=height,
        showlegend=True,
        font={"size": FONT_SIZE},
        legend={"font": {"size": FONT_SIZE}},
        margin={"l": 60, "r": 30, "t": 60, "b": 50},
        barmode="relative" if stacked else "group",
    )
    for r in range(1, rows + 1):
        for c in range(1, cols + 1):
            fig.update_xaxes(tickfont={"size": 13}, row=r, col=c)
            fig.update_yaxes(tickfont={"size": 13}, row=r, col=c)
    fig.update_yaxes(title_text=unit, row=rows, col=1, title_font={"size": 15})
    return fig


def capacities(ctx, node: str, section) -> go.Figure | None:
    """Stacked bar of all power capacities (full technology list)."""
    return _bar(
        ctx, node, section, capacity_table(ctx, node, "power"),
        signed=False, scale=_CAPACITY_SCALE,
    )


def capacities_by_tech(ctx, node: str, section) -> go.Figure | None:
    """Faceted panels for the curated technology groups (legacy layout)."""
    table = capacity_table(ctx, node, "power")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    groups = (
        _POWER_GROUPS_AGGREGATE
        if _study_wide_panels(ctx, node)
        else _POWER_GROUPS
    )
    return _faceted_capacity_chart(
        table, groups, unit=section.unit, stacked_groups=_STACKED_CAPACITY_PANELS
    )


def storage_capacities(ctx, node: str, section) -> go.Figure | None:
    """Stacked bar of all storage capacities."""
    return _bar(
        ctx, node, section, capacity_table(ctx, node, "storage"),
        signed=False, scale=_CAPACITY_SCALE,
    )


def ccs_capacities(ctx, node: str, section) -> go.Figure | None:
    """Installed capture plant, rated on the fuel-input side (not MW_e, not Mt)."""
    return _bar(
        ctx, node, section, capacity_table(ctx, node, "ccs"),
        signed=False, scale=_CAPACITY_SCALE,
    )


def storage_capacities_by_tech(ctx, node: str, section) -> go.Figure | None:
    """Faceted panels for battery / TES / gas storage (legacy layout)."""
    table = capacity_table(ctx, node, "storage")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    return _faceted_capacity_chart(
        table,
        _STORAGE_GROUPS,
        unit=section.unit,
        shared_y=False,
        height=500,
    )


# ---------------------------------------------------------------------------
# Utilisation: capacity factors and storage cycles
# ---------------------------------------------------------------------------

#: p.u. -> % for capacity-factor charts.
_UTILISATION_SCALE = 100.0

#: Cache miss sentinel.  ``None`` is a real cached value here ("this node has
#: no utilisation table"), and a DataFrame cannot be compared to a default with
#: ``!=`` without raising, so the marker has to be a distinct object.
_NOT_CACHED = object()


def _aligned_utilisation(ctx, node: str, kind: str) -> pd.DataFrame | None:
    """Utilisation restricted to the technologies the capacity chart shows.

    The two charts are read together, so a factor for a technology with no bar
    above it is a puzzle rather than information — and that happens for real:
    PyPSA-Eur parks some Links (oil boilers, ``OCGT methanol``, the ammonia
    cracker) at the pseudo-location ``EU`` in ``nodal_capacities.csv``, so they
    are absent from every *regional* capacity chart, while
    :func:`~pypsa2html.extract.tables.utilisation_table` reads the network and
    attributes them to the region they serve.  Rows the capacity chart does not
    carry are dropped here and logged; the reverse case (a capacity bar with no
    factor) stays as a gap.

    The same alignment is applied *per year*: a technology the capacity chart
    draws at (effectively) zero for a horizon gets no factor for that horizon.
    ``utilisation_table`` already floors its own denominator, but the two charts
    read different sources — the chart reads ``csvs/nodal_capacities.csv``, the
    factor reads ``p_nom_opt`` on the solved network — so a technology can be
    empty in one and degenerate-but-nonzero in the other.  The chart is what the
    reader sees, so the chart decides.
    """
    # Two sections plot the same table; the network scan behind it is not free.
    cache_key = ("aligned_utilisation", node, kind)
    cached = getattr(ctx, "_files", {}).get(cache_key, _NOT_CACHED)
    if cached is not _NOT_CACHED:
        return None if cached is None else cached.copy()

    table = _compute_aligned_utilisation(ctx, node, kind)
    if hasattr(ctx, "_files"):
        ctx._files[cache_key] = table
    return None if table is None else table.copy()


def _compute_aligned_utilisation(ctx, node: str, kind: str) -> pd.DataFrame | None:
    table = utilisation_table(ctx, node, kind)
    if table is None or table.empty:
        return None
    capacities = capacity_table(ctx, node, kind)
    if capacities is None or capacities.empty:
        return table
    # The capacity chart splits PV into ground / rooftop / tracking and the
    # utilisation table folds them back, so align on the *folded* capacity
    # index — otherwise the single "solar" factor has no matching row and the
    # chart loses solar entirely.
    folded_capacities = capacities.groupby(
        fold_utilisation_index(capacities.index), sort=False
    ).sum()
    allowed = pd.Index(dict.fromkeys(fold_utilisation_index(capacities.index)))
    dropped = table.index.difference(allowed)
    if len(dropped):
        logger.info(
            "utilisation %s/%s: not on the capacity chart, dropped: %s",
            node,
            kind,
            ", ".join(str(d) for d in dropped),
        )
    aligned = table.reindex(index=allowed)
    aligned = _mask_invisible_capacity(ctx, node, kind, aligned, folded_capacities)
    aligned = aligned.loc[aligned.notna().any(axis=1)]
    return None if aligned.empty else aligned


def _mask_invisible_capacity(
    ctx, node: str, kind: str, aligned: pd.DataFrame, capacities: pd.DataFrame
) -> pd.DataFrame:
    """Blank the (technology, year) cells whose capacity bar is not there."""
    floor = float(getattr(ctx.config.model, "utilisation_capacity_floor", 0.0) or 0.0)
    if floor <= 0:
        return aligned
    installed = (
        capacities.reindex(index=aligned.index, columns=aligned.columns)
        .astype(float)
        .fillna(0.0)
        >= floor
    )
    hidden = aligned.notna() & ~installed
    if hidden.to_numpy().any():
        names = sorted({str(i) for i in aligned.index[hidden.any(axis=1)]})
        logger.info(
            "utilisation %s/%s: %d technology-year(s) under the %.3g MW capacity "
            "floor on the capacity chart, factor suppressed: %s",
            node,
            kind,
            int(hidden.to_numpy().sum()),
            floor,
            ", ".join(names),
        )
    return aligned.where(installed)


def capacity_factors(ctx, node: str, section) -> go.Figure | None:
    """Capacity factor of every power technology, one cluster per technology."""
    table = _aligned_utilisation(ctx, node, "power")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, (table * _UTILISATION_SCALE).T)
    return grouped_bar(
        table, unit=section.unit, title="", scale=_UTILISATION_SCALE,
        hover_fmt="%{y:.1f}",
    )


def _fold_groups(groups: list[list[str]]) -> list[list[str]]:
    """Panel membership after :data:`UTILISATION_FOLD`, order kept, deduplicated."""
    return [
        list(dict.fromkeys(UTILISATION_FOLD.get(tech, tech) for tech in group))
        for group in groups
    ]


def capacity_factors_by_tech(ctx, node: str, section) -> go.Figure | None:
    """Capacity factors in the same panels as ``capacities_by_tech``.

    The PV panel carries one folded ``solar`` bar rather than the three the
    capacity panel shows — see :data:`UTILISATION_FOLD`.
    """
    table = _aligned_utilisation(ctx, node, "power")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, (table * _UTILISATION_SCALE).T)
    groups = _fold_groups(
        _POWER_GROUPS_AGGREGATE
        if _study_wide_panels(ctx, node)
        else _POWER_GROUPS
    )
    return _faceted_capacity_chart(
        table,
        groups,
        unit=section.unit,
        scale=_UTILISATION_SCALE,
        shared_y=False,
        fill_missing=False,
        hover_fmt="%{y:.1f}",
    )


def storage_cycles(ctx, node: str, section) -> go.Figure | None:
    """Equivalent full cycles per year of every storage technology.

    Energy discharged over ``e_nom_opt`` — the storage analogue of a capacity
    factor, and the number that says whether a store is a daily buffer
    (hundreds of cycles) or a seasonal one (a handful).
    """
    table = _aligned_utilisation(ctx, node, "storage")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    return grouped_bar(table, unit=section.unit, title="", hover_fmt="%{y:.1f}")


def storage_cycles_by_tech(ctx, node: str, section) -> go.Figure | None:
    """Storage cycles in the same panels as ``storage_capacities_by_tech``."""
    table = _aligned_utilisation(ctx, node, "storage")
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    return _faceted_capacity_chart(
        table,
        _STORAGE_GROUPS,
        unit=section.unit,
        scale=1.0,
        shared_y=False,
        height=500,
        fill_missing=False,
        hover_fmt="%{y:.1f}",
    )


def sectoral_demands(ctx, node: str, section) -> go.Figure | None:
    table = demand_table(ctx, node)
    if table is None or table.empty:
        logger.warning("section %s: nothing to plot for node %s", section.id, node)
        return None
    add_chart_data(ctx, node, section.title, section.unit, table.T)
    groups = table.attrs.get("groups")
    if groups is None or groups.empty:
        return stacked_bar(
            table, tech_color_map(), unit=section.unit, title="", signed=False
        )

    figures: list[tuple[str, go.Figure]] = []
    colors = tech_color_map()
    for group in sorted(set(groups.dropna())):
        sectors = groups.index[groups == group]
        sub = table.loc[table.index.intersection(sectors)]
        if sub.empty:
            continue
        fig = stacked_bar(sub, colors, unit=section.unit, title=group, signed=False)
        if fig is not None:
            figures.append((group, fig))
    if not figures:
        return stacked_bar(
            table, colors, unit=section.unit, title="", signed=False
        )
    if len(figures) == 1:
        return figures[0][1]

    from .base import combine_charts

    return combine_charts(figures, menu_title="Demand")
