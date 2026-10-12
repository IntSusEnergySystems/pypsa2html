"""The scenario summary: every number a comparison needs, in two small files.

A scenario build writes, next to its HTML::

    summary.csv    node, table, row, year, value, unit -- one line per number
    summary.json   provenance: versions, horizons, nodes, inputs, settings, KPIs

The comparison report reads these files instead of reopening the scenario's
solved networks.  That is what lets it run in seconds and in little memory
whatever the number of scenarios, and lets it combine trees solved on
different machines by copying a few hundred kilobytes -- see D24.

Because :class:`SummaryScenarioData` and
:class:`~pypsa2html.metrics.ContextScenarioData` answer the same
``table(node, key)`` calls, a figure read back from a summary is the figure
the scenario report showed.  :func:`check_summary` says whether a summary
still describes the tree it came from.
"""

from __future__ import annotations

import datetime
import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .metrics import (
    KPI,
    SYSTEM,
    ContextScenarioData,
    Metric,
    evaluate_kpis,
    kpi_records,
    metric_from,
    table_unit,
)

logger = logging.getLogger(__name__)

#: Bumped when the meaning of a column or table key changes.  A summary with
#: another schema is ``incompatible``: read as if it were missing.
SUMMARY_SCHEMA = 1
SUMMARY_CSV = "summary.csv"
SUMMARY_JSON = "summary.json"

#: Tables written for every node, besides those the KPIs and sweeps refer to.
NODE_TABLES: tuple[str, ...] = (
    "capacity.power",
    "capacity.storage",
    "capacity.ccs",
    "cost.total",
    "cost.capital",
    "cost.marginal",
    "indicator.ghg_sector",
    "indicator.ghg_source",
    "indicator.ghg_sector_cum",
    "indicator.fec_carrier",
    "indicator.fec_sector",
    "indicator.pec",
    "indicator.imports",
    "indicator.exports",
    "indicator.local_production",
    "self_sufficiency.ratio",
    "self_sufficiency.primary",
    "self_sufficiency.electricity",
    "balance.electricity",
    "price.electricity",
)
FLOW_TABLES: tuple[str, ...] = ("flow.energy", "flow.co2")
SYSTEM_TABLES: tuple[str, ...] = ("global.objective", "global.constraints")

#: Families whose absent rows read zero (quantities).  Others read NaN.
ZERO_FAMILIES = frozenset({"capacity", "cost", "indicator", "flow", "balance"})


# ---------------------------------------------------------------------------
# Which tables
# ---------------------------------------------------------------------------

def _metric_keys(metric: Metric, registry: dict[str, Metric], seen=None) -> set[str]:
    seen = set() if seen is None else seen
    if id(metric) in seen:
        return set()
    seen.add(id(metric))
    if metric.source == "ratio":
        out: set[str] = set()
        for part in (metric.numerator, metric.denominator):
            sub = registry.get(part) if isinstance(part, str) else metric_from(part)
            if sub is not None:
                out |= _metric_keys(sub, registry, seen)
        return out
    key = metric.table_key
    return {key} if key else set()


def summary_tables(config, kpis: list[KPI]) -> tuple[list[str], list[str]]:
    """``(node table keys, system table keys)`` a summary must hold."""
    node_keys = list(NODE_TABLES)
    summary_cfg = getattr(config, "summary", None)
    if getattr(summary_cfg, "flows", True):
        node_keys += list(FLOW_TABLES)
    for key in getattr(summary_cfg, "tables", None) or []:
        if key not in node_keys:
            node_keys.append(key)
    system_keys = list(SYSTEM_TABLES)
    registry = {k.id: k for k in kpis}
    metrics: list[Metric] = list(kpis)
    for sweep in getattr(config, "sensitivities", None) or []:
        metrics.extend(sweep.metrics)
    for metric in metrics:
        for key in sorted(_metric_keys(metric, registry)):
            target = system_keys if key.startswith("global.") else node_keys
            if key not in target:
                target.append(key)
    return node_keys, system_keys


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def _plain(value):
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def settings_from_config(config) -> dict:
    """The settings that change a number in the summary, as plain data.

    Recorded so a comparison can say *why* two scenarios' figures differ when
    the runs alone do not explain it (one built with nuclear as uranium, the
    other as electricity).
    """
    model = config.model
    features = config.features
    nodes = config.nodes
    return _plain({
        "model.clusters": model.clusters,
        "model.opts": model.opts,
        "model.sector_opts": model.sector_opts,
        "model.flow_threshold": model.flow_threshold,
        "model.base_year": model.base_year,
        "nodes.resolution": nodes.resolution,
        "nodes.aggregate": nodes.aggregate,
        "nodes.groups": nodes.groups,
        "features.nuclear_primary": features.nuclear_primary,
        "features.transmission_costs": features.transmission_costs,
        "features.capacity_filter": features.capacity_filter,
        "features.price_abs_cap": features.price_abs_cap,
        "features.capped_emissions": features.capped_emissions,
    })


def settings_diff(a: dict, b: dict) -> list[str]:
    """Keys whose values differ between two settings records."""
    keys = sorted(set(a) | set(b))
    return [k for k in keys if json.dumps(a.get(k), sort_keys=True, default=str)
            != json.dumps(b.get(k), sort_keys=True, default=str)]


def _git_describe() -> str:
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "-C", str(here), "describe", "--always", "--dirty", "--tags"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _stat(path: Path) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return {"mtime_ns": int(st.st_mtime_ns), "size": int(st.st_size)}


def _relative(path: Path, roots: dict[str, Path]) -> tuple[str, str]:
    """``(root name, path relative to it)`` -- relocatable input records."""
    resolved = path.resolve()
    for name, root in roots.items():
        try:
            return name, resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    return "absolute", str(resolved)


def _inputs(ctx) -> dict[str, dict[str, dict]]:
    """Every file the summary was computed from, by root, with mtime and size."""
    roots = {"results": Path(ctx.results_dir), "resources": Path(ctx.resources_dir)}
    paths: set[Path] = set()
    networks = getattr(ctx, "networks", None)
    for path in getattr(networks, "paths", {}).values():
        paths.add(Path(path))
    for key in list(getattr(ctx, "_files", {})):
        if isinstance(key, tuple) and len(key) >= 2 and key[0] in ("csv", "xls"):
            paths.add(Path(key[1]))
    out: dict[str, dict[str, dict]] = {}
    for path in sorted(paths):
        stat = _stat(path)
        if stat is None:
            continue
        root, rel = _relative(path, roots)
        out.setdefault(root, {})[rel] = stat
    return out


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _solved_at(ctx) -> str | None:
    networks = getattr(ctx, "networks", None)
    stamps = []
    for path in getattr(networks, "paths", {}).values():
        try:
            stamps.append(Path(path).stat().st_mtime)
        except OSError:
            continue
    return _iso(max(stamps)) if stamps else None


def _node_records(nodes) -> list[dict]:
    return [
        {
            "code": n.code,
            "label": n.label,
            "aggregate": bool(n.aggregate),
            "members": list(n.members) if n.members is not None else None,
        }
        for n in nodes
    ]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _long_rows(node: str, key: str, table: pd.DataFrame) -> pd.DataFrame:
    family = key.partition(".")[0]
    frame = table.copy()
    frame.index = [str(i) for i in frame.index]
    frame.columns = [str(c) for c in frame.columns]
    frame = frame.apply(pd.to_numeric, errors="coerce")
    if family in ZERO_FAMILIES:
        # Absent rows read zero, so all-zero rows carry no information.
        frame = frame.loc[(frame.fillna(0.0).abs() > 0).any(axis=1)]
    if frame.empty:
        return pd.DataFrame(columns=["node", "table", "row", "year", "value"])
    long = frame.rename_axis("row").reset_index().melt(
        id_vars="row", var_name="year", value_name="value"
    )
    if family in ZERO_FAMILIES:
        long = long.loc[long["value"].fillna(0.0) != 0]
    else:
        long = long.dropna(subset=["value"])
    long.insert(0, "table", key)
    long.insert(0, "node", node)
    return long


def collect(ctx, kpis: list[KPI], *, data=None) -> tuple[pd.DataFrame, dict]:
    """Compute the summary of ``ctx``: the long table and the table inventory.

    Never raises for one table: a table that fails is logged into the
    returned ``errors`` and left out, so a summary is written whatever the
    state of one extractor.
    """
    data = data or ContextScenarioData(ctx)
    node_keys, system_keys = summary_tables(ctx.config, kpis)
    parts: list[pd.DataFrame] = []
    inventory: dict[str, list[str]] = {}
    errors: list[str] = []

    def take(node: str, key: str) -> None:
        try:
            table = data.table(node, key)
        except Exception as exc:  # noqa: BLE001 - one table must not sink the summary
            logger.warning("summary: %s/%s failed: %s", node, key, exc)
            errors.append(f"{node}/{key}: {type(exc).__name__}: {exc}")
            return
        if table is None:
            return
        inventory.setdefault(node, []).append(key)
        parts.append(_long_rows(node, key, table))

    # Network-free and already-cached tables first, then the families that
    # read networks -- all of them in one pass over the horizons, so a bounded
    # network cache loads each network once for the whole summary.
    late = ("balance.", "price.")
    if isinstance(data, ContextScenarioData):
        from .metrics import horizon_pass

        horizon_pass(ctx, [k for k in node_keys + system_keys
                           if k.startswith(late + ("global.",))])
    for node in ctx.nodes:
        for key in node_keys:
            if not key.startswith(late):
                take(node.code, key)
    for key in system_keys:
        take(SYSTEM, key)
    for node in ctx.nodes:
        for key in node_keys:
            if key.startswith(late):
                take(node.code, key)

    kpi_parts = []
    for node in [SYSTEM, *[n.code for n in ctx.nodes]]:
        values = evaluate_kpis(data, node, kpis)
        if values.empty:
            continue
        long = values.rename_axis("row").reset_index().melt(
            id_vars="row", var_name="year", value_name="value"
        ).dropna(subset=["value"])
        long.insert(0, "table", "kpi")
        long.insert(0, "node", node)
        kpi_parts.append(long)
    parts.extend(kpi_parts)

    frame = (
        pd.concat([p for p in parts if not p.empty], ignore_index=True)
        if any(not p.empty for p in parts)
        else pd.DataFrame(columns=["node", "table", "row", "year", "value"])
    )
    units = {k.id: k.unit for k in kpis}
    frame["unit"] = [
        units.get(row, "") if table == "kpi" else table_unit(table)
        for table, row in zip(frame["table"], frame["row"], strict=True)
    ]
    return frame, {"tables": inventory, "errors": errors}


def build_summary(ctx, kpis: list[KPI] | None = None, *, data=None) -> tuple[dict, pd.DataFrame]:
    """``(meta, frame)`` of the summary of ``ctx``, without writing anything."""
    from . import __version__

    kpis = list(kpis if kpis is not None else getattr(ctx.config, "kpis", []) or [])
    frame, extra = collect(ctx, kpis, data=data)
    scenario = ctx.scenario
    sensitivity = getattr(scenario, "sensitivity", None)
    weights = ctx.horizon_weights()
    meta = {
        "generator": "pypsa2html",
        "schema_version": SUMMARY_SCHEMA,
        "pypsa2html_version": __version__,
        "git": _git_describe(),
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "solved_at": _solved_at(ctx),
        "project": getattr(ctx.config.project, "name", ""),
        "scenario": {
            "name": scenario.name,
            "label": scenario.label,
            "description": getattr(scenario, "description", None),
            "results_dir": str(ctx.results_dir),
            "resources_dir": str(ctx.resources_dir),
            "sensitivity": _plain(sensitivity) if sensitivity is not None else None,
        },
        "horizons": [int(h) for h in ctx.horizons],
        "year_columns": [str(y) for y in ctx.year_columns],
        "horizon_weights": {str(k): float(v) for k, v in weights.items()},
        "nodes": _node_records(ctx.nodes),
        "focus": ctx.nodes.focus,
        "inputs": _inputs(ctx),
        "settings": settings_from_config(ctx.config),
        "kpis": kpi_records(kpis),
        "tables": extra["tables"],
        "errors": extra["errors"],
        "rows": int(len(frame)),
    }
    return meta, frame


def write_summary(ctx, out_dir: str | Path, kpis: list[KPI] | None = None, *, data=None) -> Path:
    """Write ``summary.csv`` and ``summary.json`` for ``ctx`` into ``out_dir``.

    Returns the path of ``summary.json``.
    """
    meta, frame = build_summary(ctx, kpis, data=data)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / SUMMARY_CSV
    frame.to_csv(csv_path, index=False, float_format="%.10g")
    json_path = out_dir / SUMMARY_JSON
    json_path.write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    errors = meta.get("errors") or []
    logger.info(
        "wrote %s (%d rows, %.0f kB)%s",
        csv_path, len(frame), csv_path.stat().st_size / 1024,
        f", {len(errors)} table(s) failed" if errors else "",
    )
    return json_path


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def summary_path(path: str | Path) -> Path | None:
    """``summary.json`` named by ``path`` (the file, or a directory holding it)."""
    path = Path(path)
    if path.is_file() and path.name == SUMMARY_JSON:
        return path
    if path.is_file() and path.name == SUMMARY_CSV:
        return path.with_name(SUMMARY_JSON)
    candidate = path / SUMMARY_JSON
    return candidate if candidate.exists() else None


def _read_frame(source) -> pd.DataFrame:
    frame = pd.read_csv(
        source,
        dtype={"node": str, "table": str, "row": str, "year": str, "unit": str},
        keep_default_na=False,
        na_values=[""],
    )
    frame["row"] = frame["row"].fillna("")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    return frame


class SummaryScenarioData:
    """A scenario read back from its summary files -- see the module docstring."""

    def __init__(self, meta: dict, frame: pd.DataFrame, path: Path | None = None,
                 results_dir: Path | None = None):
        self.meta = meta
        self.path = path
        scenario = meta.get("scenario", {}) or {}
        self.name = str(scenario.get("name", ""))
        self.label = str(scenario.get("label") or self.name)
        self.year_columns = [str(y) for y in meta.get("year_columns", [])]
        self.horizons = [int(h) for h in meta.get("horizons", [])]
        self.results_dir = Path(results_dir) if results_dir else None
        self._frame = frame
        self._tables: dict[tuple[str, str], pd.DataFrame | None] = {}
        self._files: dict = {}
        inventory = meta.get("tables", {}) or {}
        self._inventory = {str(n): set(keys) for n, keys in inventory.items()}
        self._kpi_digest = {k["id"]: k.get("digest") for k in meta.get("kpis", [])}
        self._groups = {node: sub for node, sub in frame.groupby("node", sort=False)} \
            if len(frame) else {}

    @classmethod
    def from_frame(cls, meta: dict, frame: pd.DataFrame,
                   results_dir: Path | None = None) -> SummaryScenarioData:
        """A summary held in memory (recomputed, not read from disk).

        Goes through a CSV round trip so the values are exactly those a
        written-then-read summary would give.
        """
        import io

        buffer = io.StringIO()
        frame.to_csv(buffer, index=False, float_format="%.10g")
        buffer.seek(0)
        return cls(meta, _read_frame(buffer), None, results_dir)

    @classmethod
    def load(cls, path: str | Path, *, results_dir: Path | None = None) -> SummaryScenarioData:
        json_path = summary_path(path)
        if json_path is None:
            raise FileNotFoundError(f"no {SUMMARY_JSON} at {path}")
        meta = json.loads(json_path.read_text(encoding="utf-8"))
        frame = _read_frame(json_path.with_name(SUMMARY_CSV))
        if results_dir is None:
            recorded = (meta.get("scenario") or {}).get("results_dir")
            if recorded and Path(recorded).exists():
                results_dir = Path(recorded)
        return cls(meta, frame, json_path, results_dir)

    # -- protocol --------------------------------------------------------
    @property
    def nodes(self) -> list[dict]:
        return list(self.meta.get("nodes", []))

    def table(self, node: str, key: str) -> pd.DataFrame | None:
        cache = (str(node), key)
        if cache not in self._tables:
            self._tables[cache] = self._pivot(str(node), key)
        value = self._tables[cache]
        return None if value is None else value.copy()

    def _pivot(self, node: str, key: str) -> pd.DataFrame | None:
        if key not in self._inventory.get(node, set()):
            return None
        sub = self._groups.get(node)
        family = key.partition(".")[0]
        if sub is not None:
            sub = sub.loc[sub["table"] == key]
        if sub is None or sub.empty:
            return pd.DataFrame(columns=self.year_columns, dtype=float)
        wide = sub.pivot_table(index="row", columns="year", values="value",
                               aggfunc="sum", sort=False)
        years = [y for y in self.year_columns if y in wide.columns]
        years += [c for c in wide.columns if c not in years]
        wide = wide.reindex(columns=years)
        # Restore the row order the summary was written in.
        order = list(dict.fromkeys(sub["row"]))
        wide = wide.reindex(order)
        wide.index = [str(i) for i in wide.index]
        wide.columns = [str(c) for c in wide.columns]
        if family in ZERO_FAMILIES:
            wide = wide.reindex(columns=self.year_columns).fillna(0.0)
        return wide.astype(float)

    def read_file(self, relpath: str, **kwargs) -> pd.DataFrame | None:
        if self.results_dir is None:
            return None
        path = self.results_dir / relpath
        key = (str(path), repr(sorted(kwargs.items())))
        if key not in self._files:
            self._files[key] = pd.read_csv(path, **kwargs) if path.exists() else None
        value = self._files[key]
        return None if value is None else value.copy()

    def locations_for(self, node: str) -> list[str] | None:
        if node == SYSTEM:
            return None
        records = {n["code"]: n for n in self.nodes}
        real = [n["code"] for n in self.nodes if not n.get("aggregate")]
        record = records.get(node)
        if record is None or not record.get("aggregate"):
            return [node]
        members = record.get("members")
        if members is None or set(members) == set(real):
            return None
        return list(members)

    def stored_kpi(self, node: str, kpi) -> pd.Series | None:
        digest = self._kpi_digest.get(kpi.id)
        if digest is None or digest != kpi.digest:
            return None
        sub = self._groups.get(str(node))
        if sub is None:
            return None
        sub = sub.loc[(sub["table"] == "kpi") & (sub["row"] == kpi.id)]
        if sub.empty:
            return pd.Series(np.nan, index=self.year_columns, dtype=float)
        series = sub.set_index("year")["value"].astype(float)
        return series.reindex(self.year_columns)


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

#: Statuses, best first.
OK = "ok"
SETTINGS_DIFFER = "settings_differ"
UNVERIFIED = "unverified"
STALE = "stale"
INCOMPATIBLE = "incompatible"
MISSING = "missing"


@dataclass
class SummaryStatus:
    status: str
    reasons: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status in (OK, SETTINGS_DIFFER, UNVERIFIED, STALE)


def check_summary(meta: dict | None, results_dir: Path | None, config=None) -> SummaryStatus:
    """Does a summary still describe the tree it was written from?

    ``stale``: a network or an input file changed after the summary was
    written.  ``unverified``: the tree is not reachable from here (a summary
    copied from another machine) -- usable, but say so.  ``settings_differ``:
    written under other number-changing settings than ``config``.
    """
    if meta is None:
        return SummaryStatus(MISSING, ["no summary file"])
    if meta.get("schema_version") != SUMMARY_SCHEMA:
        return SummaryStatus(
            INCOMPATIBLE,
            [f"schema {meta.get('schema_version')!r}, this version reads {SUMMARY_SCHEMA}"],
        )
    reasons: list[str] = []
    if results_dir is None or not Path(results_dir).exists():
        status = UNVERIFIED
        reasons.append("results tree not reachable from this machine")
    else:
        status = OK
        results_dir = Path(results_dir)
        recorded = (meta.get("inputs") or {}).get("results", {}) or {}
        for rel, stat in recorded.items():
            now = _stat(results_dir / rel)
            if now is None:
                reasons.append(f"input gone: {rel}")
            elif now["mtime_ns"] != stat.get("mtime_ns") or now["size"] != stat.get("size"):
                reasons.append(f"input changed: {rel}")
        built = meta.get("built_at")
        if built:
            try:
                built_ts = datetime.datetime.fromisoformat(built).timestamp()
            except ValueError:
                built_ts = None
            networks = results_dir / "networks"
            if built_ts is not None and networks.is_dir():
                for nc in sorted(networks.glob("*.nc")):
                    rel = nc.relative_to(results_dir).as_posix()
                    if rel not in recorded and nc.stat().st_mtime > built_ts:
                        reasons.append(f"new network: {rel}")
        if reasons:
            status = STALE
    if config is not None and status in (OK, UNVERIFIED):
        diff = settings_diff(meta.get("settings") or {}, settings_from_config(config))
        if diff:
            reasons.append("built with other settings: " + ", ".join(diff))
            if status == OK:
                status = SETTINGS_DIFFER
    return SummaryStatus(status, reasons)


def read_meta(path: str | Path | None) -> dict | None:
    json_path = summary_path(path) if path is not None else None
    if json_path is None or not json_path.exists():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("unreadable %s: %s", json_path, exc)
        return None
