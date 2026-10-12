"""One way to read a number off a scenario.

Three consumers need "the value of X for region R in each planning horizon":
the parameter sweeps (:mod:`pypsa2html.charts.sensitivity`), the key-figures
table of a scenario report and the cross-scenario comparison.  They used to
read their numbers three different ways; now each of them describes the
number as a :class:`Metric` and asks a :class:`ScenarioData` for it.

A :class:`ScenarioData` answers ``table(node, key)`` -- a rows x year-string
table such as ``capacity.power`` or ``flow.co2``.  Two implementations exist:

* :class:`ContextScenarioData` computes the table from a live
  :class:`~pypsa2html.context.BuildContext` (networks and CSVs on disk);
* :class:`pypsa2html.summary.SummaryScenarioData` reads it back from the
  ``summary.csv`` a scenario build wrote, so a comparison never reopens a
  network.

Because both go through :func:`metric_series`, a figure in the comparison is
the same number the scenario report showed, by construction.

Sources
-------
=================  ==========================================================
``capacity``       ``kind`` power | storage | ccs -- grouped technology rows
``cost``           ``kind`` total | capital | marginal | clustered
``indicator``      ``field`` -- any DataFrame of :class:`~pypsa2html.indicators.Indicators`
``self_sufficiency`` ``kind`` ratio | primary | electricity
``flow``           ``kind`` energy | co2 -- graph edges ``Source>Target>Type``
``balance``        ``kind`` a bus carrier (or ``electricity``/``hydrogen``/``gas``/
                   ``heat``) -- annual ``supply:<carrier>``, ``withdrawal:<carrier>``,
                   ``curtailment:<carrier>``, ``potential:<carrier>`` in MWh
``price``          ``kind`` a bus carrier (or alias) -- ``mean``,
                   ``load_weighted_mean``, ``p05``, ``median``, ``p95``,
                   ``hours_above_100``, ``hours_above_200``, ``hours_below_1``
``global``         ``kind`` objective | constraints (duals, rows = names)
``csv``            a long table the project writes into its results tree
                   (``file``, ``filters``), or a PyPSA-Eur summary CSV
                   (``format: pypsa_summary``)
``ratio``          ``numerator`` / ``denominator`` -- KPI ids or inline metrics
=================  ==========================================================
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

KPIS_PATH = Path(__file__).parent / "data" / "kpis.yaml"

#: Node key under which node-independent tables are stored.
SYSTEM = "_system"

#: Bus-carrier aliases for the ``balance`` and ``price`` sources.  Anything else
#: is taken as a literal ``n.buses.carrier`` value.
BALANCE_CARRIERS: dict[str, tuple[str, ...]] = {
    # Rooftop PV and the distribution grid's far end sit on ``low voltage``.
    "electricity": ("AC", "low voltage"),
    "hydrogen": ("H2",),
    "gas": ("gas",),
}
PRICE_CARRIERS: dict[str, tuple[str, ...]] = {
    "electricity": ("AC",),
    "hydrogen": ("H2",),
    "gas": ("gas",),
}

#: What a metric can be read from.  Each entry gives the ``kind`` vocabulary
#: (``None`` = free text, e.g. a bus carrier), the default kind and the display
#: defaults.  ``missing`` is what a row the run does not carry reads: ``zero``
#: for quantities (the optimiser built none of it), ``none`` for prices,
#: ratios and project files (absence is not a value).  Adding a table here is
#: most of the cost of a new kind of metric.
SOURCES: dict[str, dict[str, Any]] = {
    "capacity": {
        "kinds": ("power", "storage", "ccs"),
        "default_kind": "power",
        "scale": 1e-3,
        "units": {"power": "GW", "storage": "GWh", "ccs": "GW"},
        "missing": "zero",
    },
    "cost": {
        "kinds": ("total", "capital", "marginal", "clustered"),
        "default_kind": "total",
        "scale": 1.0,
        "units": {},
        "default_unit": "EUR/year",
        "missing": "zero",
    },
    # Any flat DataFrame attribute of pypsa2html.indicators.Indicators
    # (``ghg_sector``, ``fec_carrier``, ...).  Rows are taxonomy *codes*.
    "indicator": {
        "kinds": (),
        "default_kind": "",
        "scale": 1.0,
        "units": {},
        "default_unit": "",
        "needs_field": True,
        "missing": "zero",
    },
    "self_sufficiency": {
        "kinds": ("ratio", "primary", "electricity"),
        "default_kind": "ratio",
        "scale": 1.0,
        "units": {"ratio": "%", "primary": "TWh/year", "electricity": "TWh/year"},
        "missing": "none",
    },
    "flow": {
        "kinds": ("energy", "co2"),
        "default_kind": "energy",
        "scale": 1.0,
        "units": {"energy": "TWh/year", "co2": "MtCO2/year"},
        "missing": "zero",
        "match": "glob",
    },
    "balance": {
        "kinds": None,
        "default_kind": "electricity",
        "scale": 1e-6,
        "units": {},
        "default_unit": "TWh/year",
        "missing": "zero",
        "match": "glob",
    },
    "price": {
        "kinds": None,
        "default_kind": "electricity",
        "scale": 1.0,
        "units": {},
        "default_unit": "EUR/MWh",
        "missing": "none",
    },
    "global": {
        "kinds": ("objective", "constraints"),
        "default_kind": "objective",
        "scale": 1.0,
        "units": {"objective": "EUR/year", "constraints": "EUR/unit"},
        "missing": "none",
        "system": True,
    },
    "csv": {
        "kinds": (),
        "default_kind": "",
        "scale": 1.0,
        "units": {},
        "default_unit": "",
        "missing": "none",
        "needs_file": True,
    },
    "ratio": {
        "kinds": (),
        "default_kind": "",
        "scale": 100.0,
        "units": {},
        "default_unit": "%",
        "missing": "none",
    },
}

#: Base unit of each table family, as written into ``summary.csv``.
TABLE_UNITS: dict[str, str] = {
    "capacity.power": "MW",
    "capacity.storage": "MWh",
    "capacity.ccs": "MW",
    "cost": "EUR/year",
    "indicator": "",
    "self_sufficiency.ratio": "%",
    "self_sufficiency": "TWh/year",
    "flow.energy": "TWh/year",
    "flow.co2": "MtCO2/year",
    "balance": "MWh/year",
    "price": "EUR/MWh | h",
    "global.objective": "EUR/year",
    "global.constraints": "EUR/unit",
}


def table_unit(key: str) -> str:
    """Base unit of the table ``key`` (``family.kind``)."""
    if key in TABLE_UNITS:
        return TABLE_UNITS[key]
    return TABLE_UNITS.get(key.partition(".")[0], "")


@dataclass
class Metric:
    """A number read off a scenario, per planning horizon.

    ``rows`` selects lines of the table by label -- the grouped technology name
    for ``capacity``/``cost``, the taxonomy code for ``indicator``, an edge
    ``Source>Target>Type`` for ``flow``, ``supply:<carrier>`` for ``balance``.
    Several rows are summed; an empty list sums the whole table.  ``match:
    glob`` allows ``*`` patterns.  ``exclude`` removes rows after selection.

    ``table`` is the name this field had when metrics only served sweeps and
    is still accepted as an alias of ``source``.
    """

    source: str = ""
    kind: str = ""
    field: str = ""
    rows: list[str] = dc_field(default_factory=list)
    exclude: list[str] = dc_field(default_factory=list)
    match: str = ""
    #: ``csv`` source: path relative to the scenario's results directory.
    #: ``{scenario}`` and ``{node}`` are substituted.
    file: str = ""
    #: ``long`` (a tidy table with a year and a value column) or
    #: ``pypsa_summary`` (PyPSA-Eur's ``csvs/metrics.csv`` layout).
    format: str = "long"
    #: ``csv`` long format: ``{column: value | [values]}``, glob patterns
    #: allowed.  ``"{node}"`` alone expands to the node's locations.
    filters: dict[str, Any] = dc_field(default_factory=dict)
    year_column: str = "year"
    value_column: str = "value"
    #: ``ratio`` source: KPI ids or inline metric mappings.
    numerator: Any = None
    denominator: Any = None
    #: What a row the run does not carry reads: ``zero`` or ``none``.
    missing: str = ""
    label: str = ""
    unit: str = ""
    scale: float | None = None
    #: Deprecated alias of ``source``.
    table: str = ""

    def __post_init__(self):
        source = str(self.source or self.table or "capacity").strip().lower()
        spec = SOURCES.get(source)
        if spec is None:
            raise ValueError(
                f"metric source must be one of {sorted(SOURCES)}, got {source!r} "
                "(a sweep metric's 'table:' key names the same thing)"
            )
        self.source = source
        self.table = source
        self.kind = str(self.kind or spec["default_kind"])
        kinds = spec["kinds"]
        if kinds and self.kind not in kinds:
            raise ValueError(
                f"metric kind for source {source!r} must be one of "
                f"{list(kinds)}, got {self.kind!r}"
            )
        self.field = str(self.field or "")
        if spec.get("needs_field") and not self.field:
            raise ValueError(
                f"metric source {source!r} needs a 'field' naming the indicator "
                "frame to read (e.g. ghg_sector, fec_carrier)"
            )
        self.file = str(self.file or "")
        if spec.get("needs_file") and not self.file:
            raise ValueError(f"metric source {source!r} needs a 'file'")
        if self.format not in ("long", "pypsa_summary"):
            raise ValueError(
                f"metric format must be 'long' or 'pypsa_summary', got {self.format!r}"
            )
        if source == "ratio" and (self.numerator is None or self.denominator is None):
            raise ValueError("a 'ratio' metric needs both 'numerator' and 'denominator'")
        self.rows = _as_list(self.rows)
        self.exclude = _as_list(self.exclude)
        self.match = str(self.match or spec.get("match", "exact")).lower()
        if self.match not in ("exact", "glob"):
            raise ValueError(f"metric match must be 'exact' or 'glob', got {self.match!r}")
        self.missing = str(self.missing or spec["missing"]).lower()
        if self.missing not in ("zero", "none"):
            raise ValueError(f"metric missing must be 'zero' or 'none', got {self.missing!r}")
        self.filters = dict(self.filters or {})
        self.scale = float(spec["scale"] if self.scale is None else self.scale)
        if not self.unit:
            self.unit = str(spec["units"].get(self.kind, spec.get("default_unit", "")))
        if not self.label:
            subject = ", ".join(self.rows) if self.rows else "total"
            self.label = f"{subject} {self.field or self.kind} {source}".strip()

    @property
    def table_key(self) -> str | None:
        """``family.kind`` of the table this metric reads; ``None`` for csv/ratio."""
        if self.source in ("csv", "ratio"):
            return None
        return f"{self.source}.{self.field or self.kind}"

    @property
    def is_system(self) -> bool:
        return bool(SOURCES[self.source].get("system"))

    def definition(self) -> dict:
        """The fields that decide the value (not its label), for digests."""
        keys = (
            "source", "kind", "field", "rows", "exclude", "match", "file", "format",
            "filters", "year_column", "value_column", "numerator", "denominator",
            "missing", "scale",
        )
        out = {k: getattr(self, k) for k in keys}
        for k in ("numerator", "denominator"):
            if isinstance(out[k], Metric):
                out[k] = out[k].definition()
        return out

    @property
    def digest(self) -> str:
        blob = json.dumps(self.definition(), sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]


@dataclass
class KPI(Metric):
    """A metric with an identity: a row of the key-figures tables.

    ``scope: system`` KPIs are node-independent (objective, CO2 price) and are
    shown once per page in their own block.  ``nodes`` restricts a KPI to some
    regions -- a project CSV written for one region reads nothing elsewhere.
    ``better`` colours a difference against the reference scenario.
    """

    id: str = ""
    group: str = "Other"
    scope: str = ""
    nodes: list[str] = dc_field(default_factory=list)
    decimals: int | None = None
    better: str | None = None
    enabled: bool = True
    description: str = ""
    #: Place this KPI right after another one (by id) instead of at the end --
    #: a project's "of which" line under the packaged total it details.
    after: str = ""

    def __post_init__(self):
        import re

        super().__post_init__()
        self.id = str(self.id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.id):
            raise ValueError(
                f"KPI id {self.id!r} must be letters, digits or underscores"
            )
        self.scope = str(self.scope or ("system" if self.is_system else "node")).lower()
        if self.scope not in ("node", "system"):
            raise ValueError(f"KPI {self.id!r}: scope must be 'node' or 'system'")
        self.nodes = [str(n) for n in _as_list(self.nodes)]
        if self.better is not None:
            self.better = str(self.better).lower()
            if self.better not in ("lower", "higher"):
                raise ValueError(f"KPI {self.id!r}: better must be 'lower' or 'higher'")
        self.group = str(self.group or "Other")

    def applies_to(self, node: str) -> bool:
        if self.scope == "system":
            return node == SYSTEM
        if node == SYSTEM:
            return False
        return not self.nodes or node in self.nodes


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (str, int, float)):
        return [str(value)]
    return [str(v) for v in value]


def _build(cls, data: dict):
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        what = "KPI" if cls is KPI else "metric"
        raise ValueError(
            f"unknown key(s) {sorted(unknown)} in a {what} definition. "
            f"Valid keys: {sorted(known)}"
        )
    return cls(**data)


def metric_from(data) -> Metric:
    """A :class:`Metric` from a mapping (or an existing metric)."""
    if isinstance(data, Metric):
        return data
    return _build(Metric, dict(data))


def load_kpis(project: list | None = None, *, defaults: bool = True) -> list[KPI]:
    """Packaged default KPIs merged with the project's ``kpis:`` list, by id.

    A project entry with an existing id overrides that KPI field by field;
    ``{id: x, enabled: false}`` removes it; a new id is appended.  ``defaults:
    false`` (``kpi_defaults: false`` in the config) starts from nothing.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    if defaults:
        with open(KPIS_PATH) as f:
            for entry in (yaml.safe_load(f) or {}).get("kpis", []):
                merged[str(entry["id"])] = dict(entry)
                order.append(str(entry["id"]))
    last_after: dict[str, str] = {}
    for entry in project or []:
        if not isinstance(entry, dict) or "id" not in entry:
            raise ValueError("every entry under 'kpis:' needs an 'id'")
        kid = str(entry["id"])
        if kid in merged:
            base = dict(merged[kid])
            # A new source replaces the definition rather than mixing fields of
            # two different sources.
            if "source" in entry and entry["source"] != base.get("source"):
                base = {k: v for k, v in base.items()
                        if k in ("id", "group", "label", "decimals", "better")}
            base.update(entry)
            merged[kid] = base
        else:
            merged[kid] = dict(entry)
            order.append(kid)
        anchor = str(entry.get("after") or "")
        if anchor:
            if anchor not in order:
                raise ValueError(f"KPI {kid!r}: 'after: {anchor}' names no KPI")
            order.remove(kid)
            # Several KPIs after the same anchor keep their declaration order.
            previous = last_after.get(anchor, anchor)
            order.insert(order.index(previous) + 1, kid)
            last_after[anchor] = kid
    out: list[KPI] = []
    for kid in order:
        entry = merged[kid]
        if entry.get("enabled", True) is False:
            continue
        out.append(_build(KPI, entry))
    return out


# ---------------------------------------------------------------------------
# Scenario data
# ---------------------------------------------------------------------------

class ScenarioData(Protocol):
    """What a metric needs from a scenario -- see the module docstring."""

    name: str
    label: str
    year_columns: list[str]
    meta: dict

    def table(self, node: str, key: str) -> pd.DataFrame | None: ...

    def read_file(self, relpath: str, **kwargs) -> pd.DataFrame | None: ...

    def locations_for(self, node: str) -> list[str] | None: ...

    def stored_kpi(self, node: str, kpi) -> pd.Series | None: ...


def _years_str(columns) -> list[str]:
    return [str(c) for c in columns]


def frame_to_table(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """years x codes (an indicator frame) -> codes x year-string columns."""
    if frame is None:
        return None
    out = frame.T.copy()
    out.columns = _years_str(out.columns)
    out.index = [str(i) for i in out.index]
    return out.astype(float)


def as_year_frame(table: pd.DataFrame | None) -> pd.DataFrame | None:
    """rows x year-string columns -> years (int index) x rows, for area charts."""
    if table is None:
        return None
    out = table.T.copy()
    out.index = [int(y) if str(y).isdigit() else y for y in out.index]
    return out


def flows_to_table(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    """A flow table (years x (Source, Target, Type)) as edge rows x years."""
    if frame is None:
        return None
    labels = []
    for col in frame.columns:
        if isinstance(col, tuple):
            parts = ["" if (p is None or (isinstance(p, float) and np.isnan(p))) else str(p)
                     for p in col]
            parts = (parts + ["", "", ""])[:3]
            labels.append(">".join(parts))
        else:
            labels.append(str(col))
    out = pd.DataFrame(frame.to_numpy(dtype=float).T, index=labels,
                       columns=_years_str(frame.index))
    if out.index.duplicated().any():
        out = out.groupby(level=0, sort=False).sum()
    return out


class ContextScenarioData:
    """Tables computed from a live :class:`~pypsa2html.context.BuildContext`.

    Every table is cached on the context, so the key-figures page, the summary
    file and a sweep reading the same table pay for it once.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.name = ctx.scenario.name
        self.label = ctx.scenario.label
        self.year_columns = [str(y) for y in ctx.year_columns]
        self.meta = {}

    # -- protocol --------------------------------------------------------
    def table(self, node: str, key: str) -> pd.DataFrame | None:
        cache = ("metric_table", str(node), key)
        files = self.ctx._files
        if cache not in files:
            files[cache] = self._compute(node, key)
        value = files[cache]
        return None if value is None else value.copy()

    def read_file(self, relpath: str, **kwargs) -> pd.DataFrame | None:
        return self.ctx.read_csv(relpath, base="results", **kwargs)

    def locations_for(self, node: str) -> list[str] | None:
        if node == SYSTEM:
            return None
        return self.ctx.locations_for(node)

    def stored_kpi(self, node: str, kpi) -> pd.Series | None:
        return None

    # -- dispatch --------------------------------------------------------
    def _compute(self, node: str, key: str) -> pd.DataFrame | None:
        family, _, kind = key.partition(".")
        ctx = self.ctx
        if family == "capacity":
            from .extract.tables import capacity_table

            return capacity_table(ctx, node, kind)
        if family == "cost":
            from .extract.tables import cost_table

            return cost_table(ctx, node, kind)
        if family in ("indicator", "self_sufficiency", "flow"):
            from . import indicators

            data = indicators.for_node(ctx, node)
            if data is None:
                return None
            if family == "indicator":
                return frame_to_table(_indicator_frame(data, kind))
            if family == "self_sufficiency":
                return frame_to_table(getattr(data.self_sufficiency, kind))
            frame = data.flows if kind == "energy" else data.flows_co2
            return flows_to_table(frame)
        if family == "balance":
            from .extract.balance import annual_balance

            return annual_balance(ctx, kind).get(node)
        if family == "price":
            from .extract.prices import price_stats

            return price_stats(ctx, kind).get(node)
        if family == "global":
            return global_table(ctx, kind)
        raise ValueError(f"unknown table {key!r}")


def _indicator_frame(data, field_name: str) -> pd.DataFrame:
    if not hasattr(type(data), field_name) and not hasattr(data, field_name):
        raise ValueError(
            f"indicator frame {field_name!r} does not exist; available: "
            f"{indicator_fields(data)}"
        )
    frame = getattr(data, field_name)
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(
            f"indicator {field_name!r} is a {type(frame).__name__}, not a table of "
            f"rows by year; a metric needs one of {indicator_fields(data)}"
        )
    return frame


def indicator_fields(data) -> list[str]:
    """Public attributes of an ``Indicators`` object that are plain tables."""
    out = []
    for name in sorted(dir(data)):
        if name.startswith("_"):
            continue
        try:
            if isinstance(getattr(data, name), pd.DataFrame):
                out.append(name)
        except Exception:  # noqa: BLE001 - a broken frame must not mask the error
            continue
    return out


def global_table(ctx, kind: str) -> pd.DataFrame | None:
    """``objective`` or the global-constraint duals, rows x year columns.

    Read by :func:`horizon_pass` with the other network tables and cached on
    the context, so every node page shares one pass.
    """
    key = ("global_table", kind)
    if key not in ctx._files:
        horizon_pass(ctx, [f"global.{kind}"])
    cached = ctx._files[key]
    return None if cached is None else cached.copy()


def global_horizon(n, kind: str) -> pd.Series | None:
    """One horizon of :func:`global_table`."""
    if kind == "objective":
        value = getattr(n, "objective", None)
        return None if value is None else pd.Series({"objective": float(value)})
    gc = getattr(n, "global_constraints", None)
    if gc is None or gc.empty or "mu" not in gc.columns:
        return None
    return pd.to_numeric(gc["mu"], errors="coerce")


#: Table families read from the networks, and the cache key each fills.
_NETWORK_FAMILIES = {
    "balance": "annual_balance",
    "price": "price_stats",
    "global": "global_table",
}


def horizon_pass(ctx, keys) -> None:
    """Compute every network-read table of ``keys`` in ONE pass over the horizons.

    ``balance``, ``price`` and ``global`` tables each need every horizon's
    network.  Computing them family by family loaded each network once per
    family -- several times over under a bounded ``model.network_cache_size``.
    Here each network is loaded once and every pending table takes what it
    needs from it; the results are cached on the context under the keys
    :func:`~pypsa2html.extract.balance.annual_balance`,
    :func:`~pypsa2html.extract.prices.price_stats` and :func:`global_table`
    read.
    """
    from .extract.balance import balance_horizon, finalize_balance
    from .extract.prices import finalize_prices, price_horizon

    jobs: list[tuple[str, str, tuple, dict]] = []
    seen: set[tuple] = set()
    for key in keys:
        family, _, kind = str(key).partition(".")
        cache_name = _NETWORK_FAMILIES.get(family)
        if cache_name is None:
            continue
        cache = (cache_name, kind)
        if cache in ctx._files or cache in seen:
            continue
        seen.add(cache)
        jobs.append((family, kind, cache, {}))
    if not jobs:
        return
    for horizon in ctx.horizons:
        try:
            n = ctx.networks[horizon]
        except Exception as exc:  # noqa: BLE001 - a missing horizon is not fatal
            logger.warning("network tables: no network for %s (%s)", horizon, exc)
            continue
        for family, kind, _cache, acc in jobs:
            try:
                if family == "balance":
                    part = balance_horizon(ctx, n, horizon, kind)
                elif family == "price":
                    part = price_horizon(ctx, n, horizon, kind)
                else:
                    part = global_horizon(n, kind)
            except Exception as exc:  # noqa: BLE001 - one table must not sink the rest
                logger.warning("%s.%s for %s failed: %s", family, kind, horizon, exc)
                continue
            if part is not None and len(part):
                acc[str(horizon)] = part
    for family, _kind, cache, acc in jobs:
        if family == "balance":
            ctx._files[cache] = finalize_balance(acc)
        elif family == "price":
            ctx._files[cache] = finalize_prices(acc)
        else:
            table = pd.DataFrame(acc) if acc else None
            if table is not None:
                table.index = [str(i) for i in table.index]
            ctx._files[cache] = table


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _matches(label: str, pattern: str, mode: str) -> bool:
    label_l, pattern_l = str(label).strip().lower(), str(pattern).strip().lower()
    if mode == "glob":
        return fnmatch.fnmatchcase(label_l, pattern_l)
    return label_l == pattern_l


def select_rows(
    table: pd.DataFrame,
    rows: list[str],
    *,
    exclude: list[str] | None = None,
    match: str = "exact",
) -> pd.DataFrame:
    """Rows of ``table`` named by ``rows``, matched case-insensitively.

    ``match: glob`` accepts ``*``/``?`` patterns.  An empty ``rows`` keeps the
    whole table.  ``exclude`` drops rows after selection.
    """
    labels = list(table.index)
    if rows:
        keep = [lab for lab in labels if any(_matches(lab, r, match) for r in rows)]
    else:
        keep = labels
    if exclude:
        keep = [lab for lab in keep if not any(_matches(lab, e, match) for e in exclude)]
    if not keep:
        return table.iloc[0:0]
    # Keep order and duplicates of the original index.
    mask = pd.Index(labels).isin(keep)
    return table.loc[mask]


def metric_series(
    data,
    node: str,
    metric: Metric,
    *,
    registry: dict[str, Metric] | None = None,
) -> pd.Series | None:
    """One metric for ``node``, indexed by year string, or ``None`` if unreadable.

    A row the config asks for but the run does not carry reads **zero** for
    quantities -- the optimiser built none of it -- and ``None`` for prices,
    ratios and project files.  A missing table always reads ``None``, so a
    line breaks rather than dropping to zero and inventing a result.
    """
    years = [str(y) for y in data.year_columns]
    # ``{node}`` in a row label names a per-region row of a system table
    # (``co2_limit_per_country{node}``), read with the region's code.
    rows = [r.replace("{node}", node) for r in metric.rows]
    exclude = [r.replace("{node}", node) for r in metric.exclude]
    if metric.is_system:
        node = SYSTEM
    if metric.source == "ratio":
        return _ratio_series(data, node, metric, registry or {}, years)
    if metric.source == "csv":
        table = _csv_table(data, node, metric)
    else:
        table = data.table(node, metric.table_key)
    if table is None:
        return None
    selected = select_rows(table, rows, exclude=exclude, match=metric.match)
    if selected.empty and (rows or exclude):
        if metric.missing == "none":
            return None
        logger.debug(
            "metric %s: no row of %s/%s matched %s in %s; reading zero",
            metric.label, node, metric.table_key, rows,
            getattr(data, "name", "?"),
        )
    present = [y for y in years if y in selected.columns]
    if metric.missing == "none" and not present:
        return None
    fill = 0.0 if metric.missing == "zero" else np.nan
    selected = selected.reindex(columns=years, fill_value=fill)
    values = selected.astype(float)
    if metric.missing == "none":
        summed = values.sum(axis=0, min_count=1)
    else:
        summed = values.fillna(0.0).sum(axis=0)
    return summed.astype(float) * float(metric.scale)


def _ratio_series(data, node, metric, registry, years) -> pd.Series | None:
    def resolve(part):
        if isinstance(part, str):
            if part not in registry:
                raise ValueError(
                    f"ratio metric {metric.label!r} refers to unknown KPI {part!r}"
                )
            return registry[part]
        return metric_from(part)

    num = metric_series(data, node, resolve(metric.numerator), registry=registry)
    den = metric_series(data, node, resolve(metric.denominator), registry=registry)
    if num is None or den is None:
        return None
    den = den.reindex(years).replace(0.0, np.nan)
    return num.reindex(years) / den * float(metric.scale)


def _expand(value, node: str, locations: list[str] | None, scenario: str):
    """Filter value(s) with ``{node}`` substituted.

    ``"{node}"`` on its own expands to the node's locations (``None`` = no
    filter on that column, for the study-wide aggregate); inside a longer
    string it is replaced by the node code.
    """
    values = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for v in values:
        text = str(v)
        if text == "{node}":
            if locations is None:
                return None
            out.extend(locations)
        else:
            out.append(text.replace("{node}", node).replace("{scenario}", scenario))
    return out


def _csv_table(data, node: str, metric: Metric) -> pd.DataFrame | None:
    relpath = metric.file.replace("{scenario}", str(getattr(data, "name", "")))
    relpath = relpath.replace("{node}", node)
    if metric.format == "pypsa_summary":
        raw = data.read_file(relpath, header=None)
        if raw is None:
            return None
        return parse_pypsa_summary(raw)
    frame = data.read_file(relpath)
    if frame is None:
        return None
    locations = data.locations_for(node) if node != SYSTEM else None
    mask = pd.Series(True, index=frame.index)
    for column, wanted in metric.filters.items():
        if column not in frame.columns:
            logger.warning(
                "metric %s: column %r not in %s (columns: %s)",
                metric.label, column, relpath, list(frame.columns),
            )
            return None
        values = _expand(wanted, node, locations, str(getattr(data, "name", "")))
        if values is None:
            continue
        col = frame[column].astype(str)
        hit = pd.Series(False, index=frame.index)
        for v in values:
            if any(ch in v for ch in "*?["):
                hit |= col.map(lambda s, p=v: fnmatch.fnmatchcase(s.lower(), p.lower()))
            else:
                hit |= col.str.lower() == v.lower()
        mask &= hit
    picked = frame.loc[mask]
    if metric.year_column not in frame.columns or metric.value_column not in frame.columns:
        logger.warning(
            "metric %s: %s has no %r/%r columns", metric.label, relpath,
            metric.year_column, metric.value_column,
        )
        return None
    if picked.empty:
        if metric.missing == "none":
            return None
        return pd.DataFrame(0.0, index=["value"], columns=[str(y) for y in data.year_columns])
    years = picked[metric.year_column].map(
        lambda y: str(int(float(y))) if _is_number(y) else str(y)
    )
    values = pd.to_numeric(picked[metric.value_column], errors="coerce")
    summed = values.groupby(years).sum(min_count=1)
    return pd.DataFrame([summed.to_numpy()], index=["value"], columns=list(summed.index))


def _is_number(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return not np.isnan(number)


def parse_pypsa_summary(raw: pd.DataFrame) -> pd.DataFrame | None:
    """A PyPSA-Eur summary CSV (``metrics.csv``, ``costs.csv``, ...) as rows x years.

    Those files carry three header rows (``cluster``, ``opt``,
    ``planning_horizon``), sometimes a row naming the index columns, then data
    rows whose leading cells are the index.  Row labels are the non-empty
    index cells joined by ``>`` (``capital>Generator>solar``).
    """
    first = raw.iloc[:, 0].astype(str).str.strip()
    hits = list(first.index[first == "planning_horizon"])
    if not hits:
        return None
    p = raw.index.get_loc(hits[0])
    header = raw.iloc[p]
    year_cols = [j for j in range(1, raw.shape[1]) if _is_number(header.iloc[j])]
    if not year_cols:
        return None
    n_index = year_cols[0]
    years = [str(int(float(header.iloc[j]))) for j in year_cols]
    body = raw.iloc[p + 1:]
    if len(body):
        values_first = body.iloc[0, year_cols]
        if values_first.isna().all() or (values_first.astype(str).str.strip() == "").all():
            body = body.iloc[1:]  # the index-names row
    labels = []
    for _, row in body.iterrows():
        cells = [str(row.iloc[j]).strip() for j in range(n_index)
                 if not pd.isna(row.iloc[j]) and str(row.iloc[j]).strip() != ""]
        labels.append(">".join(cells))
    data = body.iloc[:, year_cols].apply(pd.to_numeric, errors="coerce")
    out = pd.DataFrame(data.to_numpy(), index=labels, columns=years)
    if out.index.duplicated().any():
        out = out.groupby(level=0, sort=False).sum(min_count=1)
    return out


def evaluate_kpis(data, node: str, kpis: list[KPI]) -> pd.DataFrame:
    """KPI x year-string table for ``node`` (``SYSTEM`` for system KPIs).

    A KPI the summary already holds under the same definition is read back
    rather than recomputed; a KPI that cannot be read is a row of NaN, never
    an exception.
    """
    registry = {k.id: k for k in kpis}
    years = [str(y) for y in data.year_columns]
    rows: dict[str, pd.Series] = {}
    for kpi in kpis:
        if not kpi.applies_to(node):
            continue
        series = data.stored_kpi(node, kpi)
        if series is None:
            try:
                series = metric_series(data, node, kpi, registry=registry)
            except Exception as exc:  # noqa: BLE001 - one bad KPI must not sink the table
                logger.warning("KPI %s for %s: %s", kpi.id, node, exc)
                series = None
        rows[kpi.id] = (
            pd.Series(np.nan, index=years, dtype=float)
            if series is None else series.reindex(years).astype(float)
        )
    if not rows:
        return pd.DataFrame(columns=years, dtype=float)
    return pd.DataFrame(rows).T.reindex(columns=years)


def kpi_records(kpis: list[KPI]) -> list[dict]:
    """JSON-friendly description of ``kpis`` (for ``summary.json``)."""
    out = []
    for k in kpis:
        out.append({
            "id": k.id, "label": k.label, "unit": k.unit, "group": k.group,
            "scope": k.scope, "digest": k.digest,
        })
    return out
