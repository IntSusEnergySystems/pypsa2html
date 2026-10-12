"""Builders of the comparison report's key-figures and provenance pages.

``ctx`` is a :class:`~pypsa2html.compare.CompareContext`.  The key-figures
table holds every scenario's KPIs for every year at once; a year selector and
a "values / difference" selector swap what is shown, client side, so the page
stays one static file.
"""

from __future__ import annotations

import datetime
import html
import json
import math

import pandas as pd
import plotly.graph_objects as go

from ..metrics import SYSTEM
from .base import Html, combine_charts
from .keyfigures import MISSING, fmt, grouped, kpi_label
from .scenario import _grouped_bar

#: Modes of the key-figures table.
MODES = (("v", "Values"), ("d", "Difference to {ref}"), ("p", "Difference to {ref} (%)"))


def _years(ctx) -> list[str]:
    years = list(ctx.year_columns)
    wanted = ctx.config.compare.years
    if wanted:
        years = [y for y in years if y in wanted] or years
    return years


def _cell(kpi, value, ref_value, year: str, is_ref: bool) -> dict:
    """The three display forms of one cell, plus its colour class."""
    v = fmt(value, kpi.decimals)
    if is_ref or ref_value is None or _nan(ref_value) or _nan(value):
        d = p = v if is_ref else MISSING
        cls = ""
    else:
        delta = float(value) - float(ref_value)
        d = fmt(delta, kpi.decimals, signed=True)
        if kpi.unit.strip() == "%":
            p = d + " pp" if d != MISSING else MISSING
        elif float(ref_value) != 0:
            p = fmt(delta / abs(float(ref_value)) * 100.0, 1, signed=True) + " %"
        else:
            p = MISSING
        cls = ""
        if kpi.better and abs(delta) > 1e-12 and d.lstrip("+- 0.,") != "":
            good = (delta < 0) == (kpi.better == "lower")
            cls = "p2h-better" if good else "p2h-worse"
    return {"v": v, "d": d, "p": p, "c": cls}


def _nan(value) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def key_figures(ctx, node: str, section) -> Html | None:
    """KPIs x scenarios for one year, with the difference to the reference."""
    labels = list(ctx.scenarios)
    if not labels:
        return None
    years = _years(ctx)
    if not years:
        return None
    node_values = ctx.kpi_values(node)
    system_values = ctx.kpi_values(SYSTEM)
    reference = ctx.reference
    default_year = years[-1]

    rows_html: list[str] = []
    for group, members in grouped(ctx.kpis):
        body = []
        for kpi in members:
            source = system_values if kpi.scope == "system" else node_values
            if not kpi.applies_to(SYSTEM if kpi.scope == "system" else node):
                continue
            per_label = {
                label: (source[label].loc[kpi.id] if kpi.id in source[label].index else None)
                for label in labels
            }
            if all(s is None or s.isna().all() for s in per_label.values()):
                continue
            ref_series = per_label.get(reference)
            cells = []
            for label in labels:
                series = per_label[label]
                forms = {}
                for year in years:
                    value = None if series is None else series.get(year)
                    ref_value = None if ref_series is None else ref_series.get(year)
                    forms[year] = _cell(kpi, value, ref_value, year, label == reference)
                first = forms[default_year]
                ref_cls = " p2h-ref" if label == reference else ""
                cells.append(
                    f'<td class="num{ref_cls}" data-kf=\'{html.escape(json.dumps(forms), quote=True)}\'>'
                    f"{first['v']}</td>"
                )
            label_text, indent = kpi_label(kpi)
            title = f' title="{html.escape(kpi.description)}"' if kpi.description else ""
            body.append(
                f'<tr><td class="{"p2h-indent" if indent else ""}"{title}>'
                f"{html.escape(label_text)}</td>"
                f'<td class="p2h-unit-col">{html.escape(kpi.unit)}</td>{"".join(cells)}</tr>'
            )
        if body:
            rows_html.append(
                f'<tr class="p2h-group"><td colspan="{len(labels) + 2}">'
                f"{html.escape(group)}</td></tr>"
            )
            rows_html.extend(body)
    if not rows_html:
        return None

    head = []
    for label in labels:
        href = ctx.report_href(label, node)
        text = html.escape(label)
        if href:
            text = f'<a href="{html.escape(href, quote=True)}">{text}</a>'
        ref_cls = " p2h-ref" if label == reference else ""
        marker = "<br><small>reference</small>" if label == reference else ""
        head.append(f'<th class="num{ref_cls}">{text}{marker}</th>')

    root = f"p2h-kf-{html.escape(node, quote=True)}"
    year_options = "".join(
        f'<option value="{html.escape(y)}"{" selected" if y == default_year else ""}>'
        f"{html.escape(y)}</option>"
        for y in years
    )
    mode_options = "".join(
        f'<option value="{key}">{html.escape(text.format(ref=reference or "reference"))}</option>'
        for key, text in (MODES if reference and len(labels) > 1 else MODES[:1])
    )
    controls = (
        f'<div class="p2h-kf-controls" id="{root}-controls">'
        f'<span><label for="{root}-year">Year</label>'
        f'<select id="{root}-year">{year_options}</select></span>'
        f'<span><label for="{root}-mode">Show</label>'
        f'<select id="{root}-mode">{mode_options}</select></span></div>'
    )
    table = (
        f'<table class="p2h-table" id="{root}"><thead><tr><th>Indicator</th><th>Unit</th>'
        f"{''.join(head)}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
    )
    script = f"""<script>
(function() {{
  var table = document.getElementById({json.dumps(root)});
  var year = document.getElementById({json.dumps(root + "-year")});
  var mode = document.getElementById({json.dumps(root + "-mode")});
  function update() {{
    var cells = table.querySelectorAll('td[data-kf]');
    for (var i = 0; i < cells.length; i++) {{
      var forms = JSON.parse(cells[i].getAttribute('data-kf'))[year.value];
      if (!forms) {{ cells[i].textContent = '{MISSING}'; cells[i].className = 'num'; continue; }}
      cells[i].textContent = forms[mode.value];
      var cls = 'num' + (cells[i].classList.contains('p2h-ref') ? ' p2h-ref' : '');
      if (mode.value !== 'v' && forms.c) cls += ' ' + forms.c;
      cells[i].className = cls;
    }}
  }}
  year.addEventListener('change', update);
  mode.addEventListener('change', update);
}})();
</script>"""
    note = (
        '<p class="p2h-note">System figures are for the whole model; the others for '
        "the selected region. Differences are taken against the reference scenario; "
        "green and red mark the direction a figure is better in, where one is "
        "declared. Every number is in <code>comparison.csv</code> next to this page.</p>"
    )
    return Html(_banner(ctx, link=True) + controls + table + script + note)


def kpi_charts(ctx, node: str, section) -> go.Figure | None:
    """One grouped-bar chart per KPI, switched by a dropdown."""
    labels = list(ctx.scenarios)
    if not labels:
        return None
    node_values = ctx.kpi_values(node)
    system_values = ctx.kpi_values(SYSTEM)
    figures: list[tuple[str, go.Figure | None]] = []
    for kpi in ctx.kpis:
        scope_node = SYSTEM if kpi.scope == "system" else node
        if not kpi.applies_to(scope_node):
            continue
        source = system_values if kpi.scope == "system" else node_values
        frame = pd.DataFrame({
            label: source[label].loc[kpi.id]
            for label in labels if kpi.id in source[label].index
        })
        if frame.empty or frame.isna().all().all():
            continue
        label_text, _ = kpi_label(kpi)
        figures.append((f"{label_text} ({kpi.unit})", _grouped_bar(frame, unit=kpi.unit)))
    if not figures:
        return None
    return combine_charts(figures, menu_title="Indicator")


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

_STATUS_CLASS = {
    "ok": "p2h-status-ok",
    "settings_differ": "p2h-status-warn",
    "unverified": "p2h-status-warn",
    "stale": "p2h-status-bad",
    "incompatible": "p2h-status-bad",
    "missing": "p2h-status-bad",
}


def _date(text: str | None) -> str:
    if not text:
        return MISSING
    try:
        return datetime.datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return html.escape(text)


def warnings(ctx) -> list[str]:
    """Reasons the compared runs may not belong together (HTML items)."""
    from ..compare import vintage_spread
    from ..summary import settings_diff

    out: list[str] = []
    spread = vintage_spread(ctx)
    limit = ctx.config.compare.vintage_warn_days
    if spread is not None:
        first, last = spread
        days = (last - first).total_seconds() / 86400.0
        if days > limit:
            out.append(
                f"The runs were solved between {first:%Y-%m-%d} and {last:%Y-%m-%d} "
                f"({days:.0f} days apart, more than {limit:g}): check that they share "
                "the same inputs before reading their differences."
            )
    usable = [s for s in ctx.sources if s.usable]
    versions = {
        (s.data.meta.get("pypsa2html_version"), s.data.meta.get("git") or "") for s in usable
    }
    if len(versions) > 1:
        listed = ", ".join(sorted(f"{v} {g}".strip() for v, g in versions))
        out.append(f"The summaries were written by different pypsa2html versions ({listed}).")
    if len(usable) > 1:
        base = usable[0].data.meta.get("settings") or {}
        differing: set[str] = set()
        for s in usable[1:]:
            differing |= set(settings_diff(base, s.data.meta.get("settings") or {}))
        if differing:
            out.append(
                "The summaries were built with different settings: "
                + ", ".join(f"<code>{html.escape(k)}</code>" for k in sorted(differing))
                + "."
            )
    for s in ctx.sources:
        status = s.status.status if s.status else "missing"
        if not s.usable:
            out.append(f"<b>{html.escape(s.label)}</b> is not compared: {html.escape(s.note or status)}.")
        elif status == "stale":
            out.append(
                f"<b>{html.escape(s.label)}</b>: the summary is older than its results tree "
                f"({html.escape('; '.join(s.status.reasons[:2]))})."
            )
    return out


def _banner(ctx, *, link: bool = False) -> str:
    items = warnings(ctx)
    if not items:
        return ""
    more = ""
    if link:
        tag = ctx.config.compare.name
        more = (
            f' See <a href="scenarios_{html.escape(tag, quote=True)}.html">Scenarios</a> '
            "for where each number comes from."
        )
    return (
        f'<div class="p2h-banner"><b>Read the differences with care.</b>{more}<ul>'
        + "".join(f"<li>{item}</li>" for item in items)
        + "</ul></div>"
    )


def provenance(ctx, node: str, section) -> Html:
    """Where every number comes from, and whether the runs belong together."""
    rows = []
    for source in ctx.sources:
        meta = (source.data.meta if source.usable else source.meta) or {}
        status = source.status.status if source.status else "missing"
        if source.note == "recomputed":
            status = "recomputed"
        cls = _STATUS_CLASS.get(status, "p2h-status-ok")
        reasons = "; ".join((source.status.reasons if source.status else [])[:3])
        if source.note and source.note != "recomputed":
            reasons = "; ".join(r for r in (source.note, reasons) if r)
        href = ctx.report_href(source.label) if source.usable else None
        link = f'<a href="{html.escape(href, quote=True)}">report</a>' if href else MISSING
        horizons = ", ".join(str(h) for h in meta.get("horizons", [])) or MISSING
        version = " ".join(
            str(x) for x in (meta.get("pypsa2html_version"), meta.get("git")) if x
        ) or MISSING
        ref = " (reference)" if source.label == ctx.reference else ""
        results = (meta.get("scenario") or {}).get("results_dir") or (
            str(source.results_dir) if source.results_dir else MISSING
        )
        rows.append(
            "<tr>"
            f"<td><b>{html.escape(source.label)}</b>{ref}<br><small>{html.escape(source.name)}</small></td>"
            f"<td><code>{html.escape(str(results))}</code></td>"
            f"<td>{_date(meta.get('solved_at'))}</td>"
            f"<td>{_date(meta.get('built_at'))}</td>"
            f"<td>{html.escape(horizons)}</td>"
            f"<td>{html.escape(version)}</td>"
            f'<td class="{cls}">{html.escape(status)}'
            f"{'<br><small>' + html.escape(reasons) + '</small>' if reasons else ''}</td>"
            f"<td>{link}</td>"
            "</tr>"
        )
    for sweep_id, points in (getattr(ctx, "sweeps", None) or {}).items():
        if points:
            values = ", ".join(p.sensitivity.label for p, _ in points)
            rows.append(
                f'<tr class="p2h-muted-row"><td colspan="8">Sweep <b>{html.escape(sweep_id)}</b>: '
                f"{len(points)} point(s) read ({html.escape(values)})</td></tr>"
            )
    banner = _banner(ctx)
    table = (
        '<table class="p2h-table"><thead><tr><th>Scenario</th><th>Results tree</th>'
        "<th>Solved</th><th>Summary written</th><th>Horizons</th><th>pypsa2html</th>"
        "<th>Summary</th><th>Own report</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    note = (
        '<p class="p2h-note">Every figure of this comparison is read from the '
        "<code>summary.csv</code> each scenario report wrote; no network was "
        "reopened. <i>stale</i>: the results tree changed after the summary was "
        "written; <i>unverified</i>: the tree is not reachable from where this "
        "report was built.</p>"
    )
    return Html(banner + table + note)
