"""Network-cost sections: distribution and transmission, existing vs new.

Written in answer to a stakeholder review of the cost charts (Sept–Oct 2026),
which asked, in substance:

* is gas inside the *distribution* figure, and how does the figure compare
  with the regulator's allowed revenue?  -> electricity and gas are now two
  separate views, every layer of the regulated revenue is drawn (existing
  capital charges, OPEX, add-ons, losses) and the regulator's own total is
  marked on the chart when the project supplies it;
* what in *transmission* is existing and what is new, what is inside the
  region and what are the links with the neighbouring nodes?  -> the tariff
  view (what the region's users pay) and the branch view (the model cost of
  each link to a neighbour, existing and new) are separate, labelled views,
  and the table under the chart says for every line which of the four it is.

The numbers come from :mod:`pypsa2html.extract.network_costs`.  Both
builders return HTML (chart + table) and return ``None`` -- section not
rendered -- for a node with nothing to show.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go

from ..extract.network_costs import (
    BRANCH_ORDER,
    INTERNAL,
    NetworkCostsSpec,
    branch_costs,
    branch_wide,
    calibrated_table,
    wide,
)
from ..report.render import figure_to_html
from .base import CHART_HEIGHT, FONT_SIZE, Html, add_chart_data, combine_charts, strip_markup

logger = logging.getLogger(__name__)

#: Hatching for anything the model *built*; existing assets stay solid.
NEW_PATTERN = {"shape": "/", "fgcolor": "rgba(255,255,255,0.85)", "solidity": 0.35}

#: Status -> (legend group title, table wording).
STATUS = {
    "existing": ("Existing grid", "Existing"),
    "operation": ("Operation", "Existing (operation)"),
    "new": ("New: built by the model", "New"),
    "metering": ("Smart meters", "Existing (smart meters)"),
    "addon": ("Transfers: not a system cost", "Transfer"),
    "losses": ("Grid losses: in the energy costs", "Losses"),
    "memo": ("Memo (not added)", "Memo, not added"),
}

#: Layers that enter the total system cost (the cost-by-segment chart's
#: *Distribution* bar, ``network_cost_report.segments`` in pypsa-wal).
#: Transfers (public-service obligations, road-use fee, other) are not a cost
#: to the system, and losses are energy, already paid for in the production /
#: import costs, so neither is counted again.
COUNTED = frozenset({"existing", "operation", "new", "metering"})

#: Where the money comes from, by ``view`` of the calibrated table.
SOURCE = {
    "regulated": "Regulated accounts, 2025 (exogenous, not optimised)",
    "model": "Solved network",
    "tariff": "Belgian regulated revenue or increments x the node's share of Belgian offtake",
    "direct": "Solved network, branch cost split 50/50 between its two ends",
}

#: Display label, status and colour of each known row, keyed by
#: (network, layer, item keyword).  Unknown rows fall back to their own
#: ``item`` text and a status derived from the layer.
_ROWS: list[tuple[str, str, str, str, str, str]] = [
    # network, layer, keyword, label, status, colour
    ("electricity_distribution", "L1", "capital", "Existing grid: capital charges", "existing", "#1f4e79"),
    ("electricity_distribution", "L3", "opex", "Operating costs (assets and connections)", "operation", "#5b9bd5"),
    ("electricity_distribution", "L2", "reinforcement", "Reinforcement for peak-demand growth", "new", "#e46c0a"),
    ("electricity_distribution", "L2", "rooftop", "Hosting of new rooftop PV", "new", "#f6b26b"),
    ("electricity_distribution", "L4", "smart meters", "Smart meters", "metering", "#7f7f7f"),
    ("electricity_distribution", "L4", "public service", "Public-service obligations", "addon", "#a5a5a5"),
    ("electricity_distribution", "L4", "road-use", "Road-use fee", "addon", "#c9c9c9"),
    ("electricity_distribution", "L4", "other", "Other regulated items", "addon", "#e0e0e0"),
    ("electricity_distribution", "L4", "losses", "Grid losses (energy at the wholesale price)", "losses", "#d99694"),
    ("gas_distribution", "L1", "capital", "Existing grid: capital charges", "existing", "#7f3f00"),
    ("gas_distribution", "L3", "opex", "Operating costs", "operation", "#c55a11"),
    ("gas_distribution", "L4", "road-use", "Road-use fee", "addon", "#c9c9c9"),
    ("gas_distribution", "L4", "public service", "Public-service obligations", "addon", "#a5a5a5"),
    ("gas_distribution", "L4", "other", "Other regulated items", "addon", "#e0e0e0"),
]

_LAYER_STATUS = {"L1": "existing", "L1+L3": "existing", "L2": "new", "L3": "operation", "memo": "memo"}

#: Colour of each branch network (electricity, gas, H2, CO2).
NETWORK_COLORS = {
    "electricity": "#2f6f9f",
    "methane": "#d08b4f",
    "hydrogen": "#c45fb0",
    "CO2": "#7a7a7a",
}
NETWORK_LABELS = {
    "electricity": "Electricity",
    "methane": "Methane",
    "hydrogen": "Hydrogen",
    "CO2": "CO₂",
}

#: Calibrated-table network name -> branch network.
_TRANSMISSION_NETWORK = {
    "electricity_transmission": "electricity",
    "methane_transmission": "methane",
    "H2_transmission": "hydrogen",
    "CO2_transmission": "CO2",
}

#: Distinct colours for the neighbours of a node, in order of first use.
_NEIGHBOUR_COLORS = (
    "#1b9e77", "#d95f02", "#7570b3", "#e7298a", "#66a61e",
    "#e6ab02", "#a6761d", "#1f78b4", "#b2df8a", "#fb9a99",
)


@dataclass
class Row:
    """One stacked series: a labelled line of the table and a bar trace."""

    label: str
    status: str
    color: str
    values: pd.Series
    source: str = ""
    where: str = ""
    pattern: bool | None = None
    #: Branch network (``electricity``, ``methane``, ...) for transmission rows.
    network: str = ""


# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------

def distribution(ctx, node: str, section) -> Html | None:
    """Electricity and gas distribution costs, every regulated layer, separately."""
    table = calibrated_table(ctx, node)
    if table is None:
        return None
    years = _years(ctx, table)
    spec = NetworkCostsSpec.from_config(ctx)
    pivot = wide(table, years)

    figures: list[tuple[str, go.Figure]] = []
    blocks: list[str] = []
    for network, title in (
        ("electricity_distribution", "Electricity distribution"),
        ("gas_distribution", "Gas distribution"),
    ):
        rows, memos = _distribution_rows(pivot, network)
        if not rows:
            continue
        refs = spec.references.get(network) or []
        counted = _counted(rows, years)
        # Seven legend groups: the default height cuts the last ones off.
        fig = _stacked(rows, unit=section.unit, references=refs, years=years,
                       counted=counted, height=760)
        figures.append((title, fig))
        add_chart_data(ctx, node, f"{section.title}: {title}", section.unit,
                       pd.DataFrame({r.label: r.values for r in rows}))
        blocks.append(
            f"<h3>{html.escape(title)}</h3>"
            + _table(rows, years, memos=memos, total_label="Total (all layers)",
                     references=refs, show_where=False, counted=counted)
        )
    if not figures:
        return None
    fig = combine_charts(figures, menu_title="Network")
    segment = _segment_line(ctx, spec, "distr", years)
    notes = (
        "<p class='p2h-note'><b>How to read this.</b> Electricity and gas are "
        "shown separately (use the menu). Only the hatched "
        "<i>New</i> layers are decided by the model: the reinforcement the "
        "distribution grid needs when the hourly peak it carries grows, and "
        "the hosting cost of new rooftop PV. The existing grid, its operating "
        "costs and the regulated add-ons are taken from the regulator's 2025 "
        "accounts and held constant in real terms (OPEX grows with the number "
        "of connections). Diamonds mark the regulator's own total, which "
        "includes losses and transfers, so it compares with the full stack. "
        "The <b>red mark</b> is what enters the total system cost (the "
        "cost-by-segment chart): the existing grid, its operation, the new "
        "layers and smart meters. Transfers (public-service obligations, "
        "road-use fee) are not a cost to the system, and losses are energy "
        "already paid for in the production and import costs, so neither is "
        "counted twice.</p>"
    )
    return Html(figure_to_html(fig, plotly=ctx.config.output.plotly) + notes
                + "".join(blocks) + segment)


def _counted(rows: list[Row], years: list[str]) -> pd.Series:
    """Sum of the layers that enter the total system cost (:data:`COUNTED`)."""
    return sum((r.values.reindex(years).fillna(0.0) for r in rows if r.status in COUNTED),
               pd.Series(0.0, index=years))


def _distribution_rows(pivot: pd.DataFrame, network: str) -> tuple[list[Row], list[Row]]:
    rows: list[Row] = []
    memos: list[Row] = []
    if network not in pivot.index.get_level_values("network"):
        return rows, memos
    sub = pivot.xs(network, level="network")
    for (view, layer, item), values in sub.iterrows():
        label, status, color = _describe(network, str(layer), str(item))
        row = Row(label=label, status=status, color=color, values=values.astype(float),
                  source=SOURCE.get(str(view), str(view)))
        if status == "memo":
            memos.append(row)
        elif (values.abs() > 1e-9).any():
            rows.append(row)
    order = list(STATUS)
    rows.sort(key=lambda r: order.index(r.status))
    return rows, memos


def _describe(network: str, layer: str, item: str) -> tuple[str, str, str]:
    text = item.lower()
    for net, lay, key, label, status, color in _ROWS:
        if net == network and lay == layer and key in text:
            return label, status, color
    if layer == "L4":
        status = "losses" if "loss" in text else "addon"
    else:
        status = _LAYER_STATUS.get(layer, "addon")
    return item[:1].upper() + item[1:], status, "#999999"


# ---------------------------------------------------------------------------
# Transmission
# ---------------------------------------------------------------------------

def transmission(ctx, node: str, section) -> Html | None:
    """Transmission: what the node's users pay, and each link to a neighbour."""
    table = calibrated_table(ctx, node)
    branches = branch_costs(ctx, node)
    if table is None and (branches is None or branches.empty):
        return None
    years = _years(ctx, table) if table is not None else list(ctx.year_columns)
    spec = NetworkCostsSpec.from_config(ctx)
    label = _node_label(ctx, node)

    figures: list[tuple[str, go.Figure]] = []
    blocks: list[str] = []

    if table is not None:
        rows, info = _tariff_rows(wide(table, years), label)
        elec = [r for r in rows if r.network == "electricity"]
        other = [r for r in rows if r.network != "electricity"]
        if elec:
            figures.append((f"Electricity: what {label} pays", _stacked(
                elec, unit=section.unit, years=years,
                references=spec.references.get("electricity_transmission") or [])))
            add_chart_data(ctx, node, f"{section.title}: electricity, tariff view", section.unit,
                           pd.DataFrame({r.label: r.values for r in elec}))
            blocks.append(
                f"<h3>Electricity transmission: what {html.escape(label)} pays</h3>"
                + _table(elec, years, total_label="Total electricity transmission",
                         show_where=True, extra=_not_modelled_row(years, label))
                + "".join(f"<p class='p2h-note'>{html.escape(t)}</p>"
                          for t in info.get("electricity", []))
            )
        if other:
            blocks.append(
                "<details><summary><b>Other transmission networks charged to "
                f"{html.escape(label)}: gas transmission, H\u2082 and CO\u2082 pipelines"
                "</b></summary>"
                + _table(other, years, total_label="Total other networks", show_where=True)
                + "".join(f"<p class='p2h-note'>{html.escape(t)}</p>"
                          for t in info.get("methane", []))
                + "</details>"
            )

    bwide = branch_wide(branches, years) if branches is not None else pd.DataFrame()
    colors = _neighbour_colors(bwide)
    branch_blocks: list[str] = []
    for network in BRANCH_ORDER:
        if bwide.empty or network not in bwide.index.get_level_values("network"):
            continue
        rows = _branch_rows(ctx, bwide.xs(network, level="network"), colors, node)
        if not rows:
            continue
        name = NETWORK_LABELS[network]
        figures.append((f"{name}: links to neighbouring nodes", _stacked(
            rows, unit=section.unit, years=years)))
        add_chart_data(ctx, node, f"{section.title}: {name} branches", section.unit,
                       pd.DataFrame({r.label: r.values for r in rows}))
        branch_blocks.append(
            f"<h4>{html.escape(name)}</h4>"
            + _table(rows, years, total_label=f"Total {name.lower()} branches",
                     show_where=True)
        )
    if not figures:
        return None
    if branch_blocks:
        blocks.append(
            f"<details><summary><b>Links between {html.escape(label)} and its "
            "neighbouring nodes, by neighbour (model cost, 50/50)</b></summary>"
            + _branch_note(label, table is not None)
            + "".join(branch_blocks)
            + "</details>"
        )
    fig = combine_charts(figures, menu_title="View")
    notes = _transmission_note(label, table is not None)
    segment = _segment_line(ctx, spec, "tran", years) if table is not None else ""
    return Html(figure_to_html(fig, plotly=ctx.config.output.plotly) + notes
                + "".join(blocks) + segment)


def _tariff_rows(pivot: pd.DataFrame, label: str) -> tuple[list[Row], dict[str, list[str]]]:
    """The views the cost-segment chart sums: tariff for electricity and gas,
    direct (50/50) for the hydrogen and CO2 pipelines, which have no
    regulated owner."""
    rows: list[Row] = []
    info: dict[str, list[str]] = {}
    spec = [
        ("electricity_transmission", "tariff", "L1+L3", "existing",
         "Existing grid: inside the region and existing interconnectors",
         f"Inside {label} and the existing interconnectors: the whole regulated grid "
         "and its system services"),
        ("electricity_transmission", "tariff", "L2", "new",
         "New interconnections between nodes",
         "Interconnections between nodes"),
        ("methane_transmission", "tariff", "L1+L3", "existing",
         "Methane: existing transmission grid (share of the operator's revenue)",
         "Whole national gas transmission grid"),
        ("H2_transmission", "direct", "L1", "existing",
         "Hydrogen: existing pipelines (50/50)", "Links to neighbouring nodes"),
        ("H2_transmission", "direct", "L2", "new",
         "Hydrogen: new pipelines (50/50)", "Links to neighbouring nodes"),
        ("CO2_transmission", "direct", "L1", "existing",
         "CO\u2082: existing pipelines (50/50)", "Links to neighbouring nodes"),
        ("CO2_transmission", "direct", "L2", "new",
         "CO\u2082: new pipelines (50/50)", "Links to neighbouring nodes"),
    ]
    idx = pivot.index
    for network, view, layer, status, text, where in spec:
        mask = ((idx.get_level_values("network") == network)
                & (idx.get_level_values("view") == view)
                & (idx.get_level_values("layer") == layer))
        if not mask.any():
            continue
        values = pivot.loc[mask].sum()
        if not (values.abs() > 1e-9).any():
            continue
        net = _TRANSMISSION_NETWORK[network]
        # The table's own wording ("Elia 2025 allowed revenue x Walloon
        # offtake share") names the operator and the key, so it is the source.
        items = "; ".join(dict.fromkeys(pivot.loc[mask].index.get_level_values("item")))
        source = items if view == "tariff" else SOURCE[view]
        color = NETWORK_COLORS[net]
        if net == "electricity" and status == "new":
            color = "#e46c0a"  # the electricity view has two lines: keep them apart
        rows.append(Row(label=text, status=status, color=color,
                        values=values, source=source, where=where, network=net))
    for network, name in (("electricity_transmission", "electricity"),
                          ("methane_transmission", "gas")):
        mask = ((idx.get_level_values("network") == network)
                & (idx.get_level_values("view") == "share"))
        if mask.any():
            share = pivot.loc[mask].iloc[0]
            info.setdefault(_TRANSMISSION_NETWORK[network], []).append(
                f"{label}'s share of Belgian {name} offtake, used for the tariff view: "
                + ", ".join(f"{y}: {100 * v:.1f} %" for y, v in share.items())
                + "."
            )
    return rows, info


def _branch_rows(ctx, sub: pd.DataFrame, colors: dict[str, str], node: str) -> list[Row]:
    rows: list[Row] = []
    order = sorted(
        sub.index.get_level_values("counterpart").unique(),
        key=lambda c: (c == INTERNAL, -float(sub.xs(c, level="counterpart").to_numpy().sum())),
    )
    for counterpart in order:
        for status in ("existing", "new"):
            key = (counterpart, status)
            if key not in sub.index:
                continue
            values = sub.loc[key].astype(float)
            if not (values.abs() > 1e-9).any():
                continue
            if counterpart == INTERNAL:
                who = f"Inside {_node_label(ctx, node)}"
                where = "Branch with both ends inside the node (counted in full)"
            else:
                who = _node_label(ctx, counterpart)
                where = f"Interconnection with {who} (half of the branch)"
            rows.append(Row(
                label=f"{who}: {'existing' if status == 'existing' else 'new'}",
                status=status, color=colors.get(counterpart, "#999999"), values=values,
                source=SOURCE["direct"], where=where,
            ))
    return rows


def _neighbour_colors(bwide: pd.DataFrame) -> dict[str, str]:
    if bwide is None or bwide.empty:
        return {}
    seen = list(dict.fromkeys(bwide.index.get_level_values("counterpart")))
    seen = [c for c in seen if c != INTERNAL] + ([INTERNAL] if INTERNAL in seen else [])
    return {c: _NEIGHBOUR_COLORS[i % len(_NEIGHBOUR_COLORS)] for i, c in enumerate(seen)}


def _not_modelled_row(years: Sequence[str], label: str) -> str:
    cells = "".join("<td class='num'>not modelled</td>" for _ in years)
    return (
        "<tr class='p2h-muted-row'><td>Electricity: reinforcement of the grid inside "
        f"{html.escape(label)}</td><td>New</td><td>Inside {html.escape(label)}</td>"
        "<td>One bus per region: no internal grid in the model. The existing internal "
        "grid is in the first line, at its 2025 regulated cost.</td>"
        f"{cells}</tr>"
    )


def _transmission_note(label: str, has_tariff: bool) -> str:
    if has_tariff:
        return (
            "<p class='p2h-note'><b>How to read this.</b> The first view is what "
            f"{html.escape(label)}'s users pay for <i>electricity</i> transmission. "
            "The tariff is national: the existing regulated grid is charged to "
            f"{html.escape(label)} in proportion to its share of Belgian offtake, "
            "and that one line covers both the grid inside the region (not "
            "modelled) and the existing interconnectors. The new interconnections "
            "the model builds between Belgian nodes are charged the same way. "
            "Hatched = new capacity decided by the model; solid = existing. The "
            "other views split the model cost of every branch between the node "
            "and its neighbours, by neighbour. Gas transmission and the H\u2082 and "
            "CO\u2082 pipelines are in the table below the electricity one.</p>"
        )
    return (
        "<p class='p2h-note'><b>How to read this.</b> Model cost of the branches "
        "between this node and its neighbours, half of each branch to each end. "
        "Solid = capacity the first horizon already had (a model valuation of a "
        "sunk asset, not a regulated cost); hatched = capacity built by the model. "
        "The grid inside a node is not modelled, so it does not appear here.</p>"
    )


def _branch_note(label: str, has_tariff: bool) -> str:
    text = (
        "<p class='p2h-note'>Each branch connecting two nodes is charged half to "
        "each end, at the annuity of the horizon. <i>Existing</i> is the capacity "
        "the first horizon already had, <i>new</i> what the model added since."
    )
    if has_tariff:
        text += (
            " The existing electricity and gas branches belong to the regulated "
            "grids, so they are already inside the tariff view's existing-grid "
            "lines: they are shown here for transparency, not to be added. The new "
            f"branches are the model's decisions; their cost is charged to {html.escape(label)} "
            "in the tariff view through the Belgian offtake share."
        )
    return text + "</p>"


# ---------------------------------------------------------------------------
# Shared drawing
# ---------------------------------------------------------------------------

def _stacked(rows: Iterable[Row], *, unit: str, years: list[str],
             references: list | None = None, counted: pd.Series | None = None,
             height: int = CHART_HEIGHT) -> go.Figure:
    fig = go.Figure()
    seen_groups: set[str] = set()
    for row in rows:
        group_title = STATUS[row.status][0]
        hatched = row.status == "new" if row.pattern is None else row.pattern
        marker = {"color": row.color, "line": {"width": 0.5, "color": "rgba(0,0,0,0.25)"}}
        if hatched:
            marker["pattern"] = NEW_PATTERN
        fig.add_trace(go.Bar(
            x=years,
            y=[float(row.values.get(y, 0.0)) for y in years],
            name=row.label,
            marker=marker,
            legendgroup=row.status,
            legendgrouptitle_text=group_title if row.status not in seen_groups else None,
            hovertemplate=f"{html.escape(row.label)}: %{{y:.1f}}<extra></extra>",
        ))
        seen_groups.add(row.status)
    if counted is not None:
        fig.add_trace(go.Scatter(
            x=years, y=[float(counted.get(y, 0.0)) for y in years],
            mode="markers+text",
            marker={"symbol": "line-ew", "size": 60, "line": {"width": 3, "color": "#c00000"}},
            text=[f"{float(counted.get(y, 0.0)):.0f}" for y in years],
            textposition="middle left", textfont={"color": "#c00000"},
            name="Counted in the total system cost",
            legendgroup="counted", legendgrouptitle_text="System cost",
            hovertemplate="Counted in the total system cost: %{y:.1f}<extra></extra>",
        ))
    refs = [r for r in (references or []) if str(r.get("year")) in years]
    if refs:
        fig.add_trace(go.Scatter(
            x=[str(r["year"]) for r in refs], y=[float(r["value"]) for r in refs],
            mode="markers+text",
            marker={"symbol": "diamond", "size": 13, "color": "#111111",
                    "line": {"width": 1, "color": "#ffffff"}},
            text=[f"{float(r['value']):.0f}" for r in refs], textposition="middle right",
            customdata=[str(r.get("label", "")) for r in refs],
            name="Regulator's total (incl. losses and add-ons)",
            legendgroup="reference", legendgrouptitle_text="Reference",
            hovertemplate="%{customdata}: %{y:.1f}<extra></extra>",
        ))
    fig.update_layout(
        barmode="stack",
        height=height,
        hovermode="x unified",
        yaxis_title=strip_markup(unit),
        font={"size": FONT_SIZE},
        # Top of the legend = top of the stack.
        legend={"groupclick": "toggleitem", "tracegroupgap": 12,
                "traceorder": "reversed+grouped"},
        margin={"l": 60, "r": 30, "t": 60, "b": 50},
        xaxis={"type": "category", "title": ""},
    )
    return fig


def _fmt(value: float) -> str:
    if abs(value) < 0.05:
        return "0"
    return f"{value:,.1f}".replace(",", " ")


def _table(rows: list[Row], years: list[str], *, memos: list[Row] | None = None,
           total_label: str = "Total", references: list | None = None,
           show_where: bool = True, extra: str = "",
           counted: pd.Series | None = None) -> str:
    head = "<th>Item</th><th>Existing / new</th>"
    if show_where:
        head += "<th>Where</th>"
    head += "<th>Source</th>" + "".join(f"<th class='num'>{html.escape(y)}</th>" for y in years)
    body = []
    for row in rows:
        cells = (f"<td>{html.escape(row.label)}</td>"
                 f"<td>{html.escape(STATUS[row.status][1])}</td>")
        if show_where:
            cells += f"<td>{html.escape(row.where)}</td>"
        cells += f"<td>{html.escape(row.source)}</td>"
        cells += "".join(f"<td class='num'>{_fmt(float(row.values.get(y, 0.0)))}</td>" for y in years)
        body.append(f"<tr>{cells}</tr>")
    if extra:
        body.append(extra)
    total = sum((r.values.reindex(years).fillna(0.0) for r in rows), pd.Series(0.0, index=years))
    span = 4 if show_where else 3
    body.append(
        f"<tr class='p2h-total'><td colspan='{span}'>{html.escape(total_label)}</td>"
        + "".join(f"<td class='num'>{_fmt(float(total[y]))}</td>" for y in years) + "</tr>"
    )
    if counted is not None:
        body.append(
            f"<tr class='p2h-subtotal'><td colspan='{span}'>of which counted in the total "
            "system cost (existing grid, operation, new, smart meters)</td>"
            + "".join(f"<td class='num'>{_fmt(float(counted.get(y, 0.0)))}</td>" for y in years)
            + "</tr>"
        )
    for ref in references or []:
        cells = "".join(
            f"<td class='num'>{_fmt(float(ref['value']))}</td>" if str(ref.get("year")) == y
            else "<td></td>" for y in years
        )
        body.append(f"<tr class='p2h-muted-row'><td colspan='{span}'>Reference: "
                    f"{html.escape(str(ref.get('label', '')))}</td>{cells}</tr>")
    for row in memos or []:
        cells = "".join(f"<td class='num'>{_fmt(float(row.values.get(y, 0.0)))}</td>" for y in years)
        body.append(f"<tr class='p2h-muted-row'><td colspan='{span}'>Memo, not added: "
                    f"{html.escape(row.label)}</td>{cells}</tr>")
    return (
        "<table class='p2h-table'><thead><tr>" + head + "</tr></thead><tbody>"
        + "".join(body) + "</tbody></table>"
        + "<p class='p2h-unit'>M€₂₀₂₅ per year.</p>"
    )


def _segment_line(ctx, spec: NetworkCostsSpec, prefix: str, years: list[str]) -> str:
    """Tie the section to the cost-by-segment chart, when its file is there."""
    raw = ctx.read_csv(spec.segments_file, base="results", index_col=0) if spec.segments_file else None
    if raw is None:
        return ""
    raw.columns = [str(c) for c in raw.columns]
    hit = [i for i in raw.index.astype(str) if i.startswith(prefix)]
    if not hit:
        return ""
    values = raw.loc[hit[0]].reindex(years).astype(float)
    name = "Distribution" if prefix == "distr" else "Transport"
    composition = (
        "the electricity layers counted in the system cost plus the gas layers counted "
        "(red marks of the two views)"
        if prefix == "distr" else
        "the electricity transmission total plus the other networks (gas transmission, "
        "H\u2082 and CO\u2082 pipelines)"
    )
    return (
        f"<p class='p2h-note'>The <i>{name}</i> bar of the cost-by-segment chart is "
        f"{html.escape(composition)}: "
        + ", ".join(f"{y}: {_fmt(v)}" for y, v in values.items())
        + " M€/a.</p>"
    )


def _years(ctx, table: pd.DataFrame | None) -> list[str]:
    cols = [str(c) for c in ctx.year_columns]
    if table is None:
        return cols
    present = set(table["year"].astype(str))
    return [c for c in cols if c in present] or sorted(present)


def _node_label(ctx, code: str) -> str:
    try:
        return ctx.nodes[str(code)].label
    except (KeyError, AttributeError):
        labels = getattr(getattr(ctx.config, "nodes", None), "labels", {}) or {}
        return labels.get(str(code), str(code))
