"""Key figures of one scenario: the indicator table a scenario report opens on.

Rows are the KPIs of ``data/kpis.yaml`` merged with the project's ``kpis:``,
grouped as declared; columns are the planning horizons.  The same KPIs, read
through the same :func:`~pypsa2html.metrics.metric_series`, fill the
comparison report's table -- so a number never differs between the two.
"""

from __future__ import annotations

import html
import math

import pandas as pd

from ..metrics import SYSTEM, ContextScenarioData, evaluate_kpis
from .base import Html

#: Placeholder for a value the run does not provide.
MISSING = "–"


def fmt(value, decimals: int | None = None, *, signed: bool = False) -> str:
    """Display form of one key figure.

    Without ``decimals`` the precision follows the magnitude, so a 290 000
    MEUR objective and a 0.43 Mt capture both read with three or more
    significant digits.  Thousands are separated by a narrow space.
    """
    if value is None:
        return MISSING
    try:
        number = float(value)
    except (TypeError, ValueError):
        return MISSING
    if math.isnan(number) or math.isinf(number):
        return MISSING
    if decimals is None:
        magnitude = abs(number)
        decimals = 0 if magnitude >= 100 else 1 if magnitude >= 10 else 2 if magnitude >= 0.1 else 3
    if round(number, decimals) == 0:
        return "0"
    text = f"{number:,.{decimals}f}".replace(",", " ")
    if signed and number > 0:
        text = "+" + text
    return text


def kpi_label(kpi) -> tuple[str, bool]:
    """``(label, indented)``: a label starting with spaces is a sub-item."""
    raw = str(kpi.label)
    return raw.strip(), raw != raw.lstrip()


def grouped(kpis) -> list[tuple[str, list]]:
    """KPIs in declaration order, grouped by ``group`` (first appearance)."""
    groups: dict[str, list] = {}
    for kpi in kpis:
        groups.setdefault(kpi.group, []).append(kpi)
    return list(groups.items())


def scenario_values(ctx, node: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(system KPIs, node KPIs)`` of a live scenario, KPI x year columns."""
    key = ("key_figures", str(node))
    cached = ctx._files.get(key)
    if cached is None:
        data = ContextScenarioData(ctx)
        kpis = list(getattr(ctx.config, "kpis", []) or [])
        system = ctx._files.get(("key_figures", SYSTEM))
        if system is None:
            system = evaluate_kpis(data, SYSTEM, kpis)
            ctx._files[("key_figures", SYSTEM)] = system
        cached = (system, evaluate_kpis(data, node, kpis))
        ctx._files[key] = cached
    return cached


def scenario_table(ctx, node: str, section) -> Html | None:
    """KPI x horizon table of one scenario and region."""
    kpis = list(getattr(ctx.config, "kpis", []) or [])
    if not kpis:
        return None
    system, values = scenario_values(ctx, node)
    years = [str(y) for y in ctx.year_columns]
    rows_html: list[str] = []
    for group, members in grouped(kpis):
        body = []
        for kpi in members:
            table = system if kpi.scope == "system" else values
            if kpi.id not in table.index:
                continue
            series = table.loc[kpi.id]
            if series.isna().all():
                continue
            label, indent = kpi_label(kpi)
            cells = "".join(
                f'<td class="num">{fmt(series.get(y), kpi.decimals)}</td>' for y in years
            )
            title = f' title="{html.escape(kpi.description)}"' if kpi.description else ""
            body.append(
                f'<tr><td class="{"p2h-indent" if indent else ""}"{title}>'
                f"{html.escape(label)}</td>"
                f'<td class="p2h-unit-col">{html.escape(kpi.unit)}</td>{cells}</tr>'
            )
        if body:
            rows_html.append(
                f'<tr class="p2h-group"><td colspan="{len(years) + 2}">'
                f"{html.escape(group)}</td></tr>"
            )
            rows_html.extend(body)
    if not rows_html:
        return None
    head = "".join(f'<th class="num">{html.escape(y)}</th>' for y in years)
    note = (
        '<p class="p2h-note">System figures are for the whole model; the others '
        "for the selected region. Every figure is also in <code>summary.csv</code> "
        "next to this page.</p>"
    )
    return Html(
        f'<table class="p2h-table"><thead><tr><th>Indicator</th><th>Unit</th>{head}'
        f"</tr></thead><tbody>{''.join(rows_html)}</tbody></table>{note}"
    )
