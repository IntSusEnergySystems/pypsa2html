"""Configuration loading and validation.

A pypsa2html run is fully described by one YAML file.  Defaults live in
``pypsa2html/data/default.yaml`` and are deep-merged with the user file, so a
project config only states what differs.

The legacy tool spread its settings over four places -- a ``MAIN_PARAMS``
sheet in an xlsx, ``config/plots.yaml``, hardcoded literals in ``__main__``
blocks, and the Snakemake rule itself.  Everything that is genuinely a *choice*
now lives here; everything that is a *taxonomy* (nodes, processes, carrier
codes) lives in packaged CSVs -- see :mod:`pypsa2html.datafiles`.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULTS_PATH = Path(__file__).parent / "data" / "default.yaml"


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class ProjectConfig:
    name: str = "PyPSA results"
    subtitle: str = ""
    logo: str | None = None
    website: str | None = None
    decimals: int = 3


def format_number(value: float) -> str:
    """Compact display form of a swept parameter value (``4500.0`` -> ``4500``)."""
    return f"{float(value):g}"


@dataclass
class ScenarioSensitivity:
    """Marks a scenario as one *point* of a parameter sweep.

    A sweep point is not a scenario a reader browses: six runs that differ only
    in one number are one curve, not six dashboards.  Carrying the marker on
    the scenario entry (rather than listing names in the sweep) keeps the
    swept value next to the results directory it was produced from, so a point
    cannot be renamed into the wrong position on the x axis.
    """

    sweep: str
    value: float
    #: Axis/legend text for this point.  Defaults to the value itself.
    label: str = ""

    def __post_init__(self):
        self.sweep = str(self.sweep)
        try:
            self.value = float(self.value)
        except (TypeError, ValueError):
            raise ValueError(
                f"sensitivity value {self.value!r} for sweep {self.sweep!r} is not a number"
            ) from None
        self.label = str(self.label or format_number(self.value))


@dataclass
class ScenarioConfig:
    """One solved scenario: a directory of PyPSA-Eur results."""

    name: str
    label: str = ""
    results_dir: str = ""
    resources_dir: str = ""
    #: Optional free-form HTML shown at the top of this scenario's landing page.
    description: str | None = None
    #: ``{sweep: <id>, value: <x>}`` when this run is one point of a parameter
    #: sweep.  Such a scenario is excluded from the navigation, the scenario
    #: dropdown and the cross-scenario overview, and feeds the sensitivity page
    #: instead.  ``None`` for an ordinary scenario.
    sensitivity: ScenarioSensitivity | None = None

    def __post_init__(self):
        self.label = self.label or self.name
        if not self.results_dir:
            raise ValueError(f"scenario {self.name!r} has no results_dir")
        if isinstance(self.sensitivity, dict):
            self.sensitivity = _as_dataclass(ScenarioSensitivity, dict(self.sensitivity))
        if self.sensitivity is not None and not isinstance(
            self.sensitivity, ScenarioSensitivity
        ):
            raise ValueError(
                f"scenario {self.name!r}: 'sensitivity' must be a mapping with "
                "'sweep' and 'value' keys"
            )

    @property
    def is_sweep_point(self) -> bool:
        return self.sensitivity is not None


@dataclass
class DispatchWindowsConfig:
    """Calendar windows for dispatch charts (``[start, stop]`` ISO dates).

    ``None`` means derive a representative week from the network snapshots
    (February for winter, July for summer) instead of hardcoding a weather year.
    """

    winter: list[str] | None = None
    summer: list[str] | None = None


@dataclass
class ModelConfig:
    """How to find and read the model output."""

    clusters: str = "adm"
    opts: str = ""
    sector_opts: str = ""
    network_pattern: str = "networks/base_s_{clusters}_{opts}_{sector_opts}_{horizon}.nc"
    #: ``None`` means "discover from the files present in results_dir".
    planning_horizons: list[int] | None = None
    #: Optional historical/base-year column merged in from an exogenous source.
    base_year: int | None = None
    base_year_source: str | None = None
    #: Absolute threshold below which a flow is dropped, in TWh (or Mt for CO2).
    flow_threshold: float = 0.1
    #: Installed capacity (MW, or MWh for stores) below which no utilisation is
    #: reported.  A capacity factor is a ratio, so a degenerate p_nom_opt of a
    #: few kW — routinely left behind by a barrier solve with no crossover —
    #: divides a near-zero dispatch by a near-zero fleet and prints a confident
    #: percentage for a technology whose capacity bar is invisible.  The default
    #: is one megawatt: below that the capacity chart (GW, 3 significant digits)
    #: cannot draw the bar the factor would have to be read against.
    utilisation_capacity_floor: float = 1.0
    #: How many solved networks to keep in RAM.  ``None`` = one per horizon
    #: (avoids thrashing the LRU during dispatch/maps/overview).  Set a small
    #: positive int only when RAM is tight.
    network_cache_size: int | None = None
    dispatch_windows: DispatchWindowsConfig = field(default_factory=DispatchWindowsConfig)
    #: Keep every N-th snapshot when serialising dispatch charts (``1`` = hourly).
    #: Display-only: chart *shape* stays faithful, HTML payload shrinks ~N×.
    dispatch_step_hours: int = 2
    #: Decimal places for dispatch y-values before plotly JSON serialisation.
    dispatch_value_decimals: int = 3

    def network_path(self, results_dir: Path | str, horizon: int) -> Path:
        rel = self.network_pattern.format(
            clusters=self.clusters,
            opts=self.opts,
            sector_opts=self.sector_opts,
            horizon=horizon,
        )
        return Path(results_dir) / rel


@dataclass
class AggregateConfig:
    enabled: bool = True
    code: str = "ALL"
    label: str = ""


@dataclass
class NodeGroupConfig:
    """A synthetic node that sums an explicit list of real locations.

    Membership is always an explicit ``members`` list.  Prefix matching is
    rejected (``BE`` must not silently include ``BEWAL``).
    """

    code: str
    label: str = ""
    members: list[str] = field(default_factory=list)


@dataclass
class NodesConfig:
    detect: bool = True
    include: list[str] | None = None
    exclude: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    focus: str | None = None
    resolution: str = "location"
    aggregate: AggregateConfig = field(default_factory=AggregateConfig)
    #: Extra synthetic nodes, each summing ``members``.  Empty by default.
    groups: list[NodeGroupConfig] = field(default_factory=list)


@dataclass
class LandingConfig:
    """Which page the site opens on."""

    scenario: str | None = None
    node: str | None = None
    page: str = "overview"


@dataclass
class FeaturesConfig:
    """Optional behaviours that differ across models."""

    #: Attribute AC/DC transmission separately. ``None`` = auto (on when >1 node).
    transmission_costs: bool | None = None
    #: How capacity charts filter ``nodal_capacities.csv``.
    #: ``bus_carrier`` (default) keeps components attached to electricity /
    #: heat / H2 service buses; ``off`` plots every non-Store row.
    capacity_filter: str | None = "bus_carrier"
    #: Drop |bus price| above this cap (€/MWh) if prices are ever plotted.
    #: Empty-bus duals in PyPSA routinely hit 1e5–1e6 €/MWh.
    price_abs_cap: float = 1.0e4
    #: How nuclear counts in *primary-energy* independence (electricity
    #: independence always books reactor kWh as domestic). ``uranium`` (default)
    #: treats the fuel as an import; ``electricity`` books the kWh produced.
    nuclear_primary: str = "uranium"

    def __post_init__(self):
        self.price_abs_cap = float(self.price_abs_cap)
        mode = str(self.nuclear_primary or "uranium").strip().lower()
        if mode not in {"uranium", "electricity"}:
            raise ValueError(
                "features.nuclear_primary must be 'uranium' or 'electricity', "
                f"got {self.nuclear_primary!r}"
            )
        self.nuclear_primary = mode


#: Result tables a sensitivity metric can be read from.  Each entry gives the
#: extractor's ``kind`` vocabulary, the default kind, and the display defaults
#: (a MW capacity table is plotted in GW, a EUR cost table as it stands).
#: Adding a table here is the whole cost of supporting a new metric.
SENSITIVITY_TABLES: dict[str, dict[str, Any]] = {
    "capacity": {
        "kinds": ("power", "storage", "ccs"),
        "default_kind": "power",
        "scale": 1e-3,
        "units": {"power": "GW", "storage": "GWh", "ccs": "GW"},
    },
    "cost": {
        "kinds": ("total", "capital", "marginal", "clustered"),
        "default_kind": "total",
        "scale": 1.0,
        "units": {},
        "default_unit": "EUR/year",
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
    },
}


@dataclass
class SensitivityParameter:
    """The swept quantity — what the x axis of a sweep chart means."""

    label: str = "Parameter"
    unit: str = ""


@dataclass
class SensitivityMetric:
    """What a sweep chart reads off each solved point (the y axis).

    ``rows`` selects lines of the extracted table by their row label — the
    grouped technology name for ``capacity``/``cost``, the taxonomy code for
    ``indicator``.  Several rows are summed; an empty list sums the whole
    table.  A row missing from a point's table counts as zero, which is the
    truthful reading: the optimiser built none of it.
    """

    table: str = "capacity"
    kind: str = ""
    field: str = ""
    rows: list[str] = dc_field(default_factory=list)
    label: str = ""
    unit: str = ""
    scale: float | None = None

    def __post_init__(self):
        self.table = str(self.table).strip().lower()
        spec = SENSITIVITY_TABLES.get(self.table)
        if spec is None:
            raise ValueError(
                f"sensitivity metric table must be one of "
                f"{sorted(SENSITIVITY_TABLES)}, got {self.table!r}"
            )
        self.kind = str(self.kind or spec["default_kind"])
        if spec["kinds"] and self.kind not in spec["kinds"]:
            raise ValueError(
                f"sensitivity metric kind for table {self.table!r} must be one of "
                f"{list(spec['kinds'])}, got {self.kind!r}"
            )
        self.field = str(self.field or "")
        if spec.get("needs_field") and not self.field:
            raise ValueError(
                f"sensitivity metric table {self.table!r} needs a 'field' naming "
                "the indicator frame to read (e.g. ghg_sector, fec_carrier)"
            )
        self.rows = [str(r) for r in (self.rows or [])]
        if self.scale is None:
            self.scale = float(spec["scale"])
        else:
            self.scale = float(self.scale)
        if not self.unit:
            self.unit = str(spec["units"].get(self.kind, spec.get("default_unit", "")))
        if not self.label:
            subject = ", ".join(self.rows) if self.rows else "total"
            self.label = f"{subject} {self.field or self.kind} {self.table}".strip()


@dataclass
class SensitivityConfig:
    """One parameter sweep: a family of runs differing in a single number.

    The runs themselves are ordinary ``scenarios:`` entries carrying a
    ``sensitivity: {sweep: <id>, value: <x>}`` marker.  This block says what
    the sweep *means* — the parameter on the x axis, the metric on the y axis,
    and the region the metric is read for.
    """

    id: str
    label: str = ""
    parameter: SensitivityParameter = dc_field(default_factory=SensitivityParameter)
    #: Regions the metric is read for.  Empty = ``nodes.focus``.  A sweep page
    #: is node-independent by construction, so the region is pinned here rather
    #: than following the report's region selector.
    nodes: list[str] = dc_field(default_factory=list)
    metrics: list[SensitivityMetric] = dc_field(default_factory=list)
    #: HTML shown above the chart.  Folded into ``texts:`` under the section id.
    description: str = ""

    def __post_init__(self):
        self.id = str(self.id).strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.id):
            raise ValueError(
                f"sensitivity id {self.id!r} must be letters, digits or underscores "
                "(it becomes an HTML anchor and a 'plots:' toggle key)"
            )
        self.label = str(self.label or self.id.replace("_", " "))
        if isinstance(self.parameter, dict):
            self.parameter = _as_dataclass(SensitivityParameter, dict(self.parameter))
        if isinstance(self.nodes, str):
            self.nodes = [self.nodes]
        self.nodes = [str(n) for n in (self.nodes or [])]
        metrics = self.metrics or []
        if isinstance(metrics, dict):
            metrics = [metrics]
        self.metrics = [
            m if isinstance(m, SensitivityMetric) else _as_dataclass(SensitivityMetric, dict(m))
            for m in metrics
        ]
        if not self.metrics:
            raise ValueError(
                f"sensitivity {self.id!r} declares no 'metrics:' — a sweep with no "
                "quantity to plot has nothing to show"
            )
        self.description = str(self.description or "")

    @property
    def section_id(self) -> str:
        """Anchor / ``plots:`` toggle key of this sweep's report section."""
        return f"{SENSITIVITY_SECTION_PREFIX}{self.id}"


#: Section ids of sweep sections are this prefix plus the sweep id.
SENSITIVITY_SECTION_PREFIX = "sensitivity_"

#: Page in data/pages.yaml whose sections are generated from the sweeps.
SENSITIVITY_PAGE = "sensitivity"


@dataclass
class OutputConfig:
    dir: str = "html"
    #: Page ids to build, in navigation order.  See ``data/pages.yaml``.
    pages: list[str] = field(default_factory=list)
    #: Emit one shared maps page instead of one byte-identical copy per node.
    shared_maps: bool = True
    #: ``cdn`` keeps pages small but requires network access to render;
    #: ``inline`` embeds plotly.js once per page (~3 MB) and works offline.
    plotly: str = "cdn"
    #: Write the per-chart data workbook next to the HTML.
    chart_data: bool = True
    write_index: bool = True
    #: Raise instead of warning when a Sankey transformation node does not
    #: conserve energy.  A published Sankey with an unbalanced electricity node
    #: is a wrong page, not a cosmetic one, but making it fatal by default would
    #: turn every taxonomy gap into a failed pipeline — so the build writes
    #: ``graph_imbalances.csv`` and logs a consolidated summary, and a caller
    #: that wants a gate turns this on.
    fail_on_graph_imbalance: bool = False


@dataclass
class Config:
    project: ProjectConfig
    model: ModelConfig
    nodes: NodesConfig
    output: OutputConfig
    landing: LandingConfig
    scenarios: list[ScenarioConfig]
    plots: dict[str, bool]
    texts: dict[str, Any]
    features: FeaturesConfig = field(default_factory=FeaturesConfig)
    #: Declared parameter sweeps.  Empty for a report with no sensitivities.
    sensitivities: list[SensitivityConfig] = field(default_factory=list)
    #: Directory the relative paths in this config resolve against.
    root: Path = field(default_factory=Path.cwd)
    raw: dict = field(default_factory=dict)

    # -- convenience ------------------------------------------------------
    @property
    def scenario_names(self) -> list[str]:
        return [s.name for s in self.scenarios]

    @property
    def report_scenarios(self) -> list[ScenarioConfig]:
        """Scenarios a reader browses — every one that is not a sweep point.

        Sweep points stay in ``scenarios`` (the sensitivity page reads them)
        but are absent from the navigation, the scenario dropdown, the
        cross-scenario overview and the set of scenarios pages are built for.
        """
        return [s for s in self.scenarios if not s.is_sweep_point]

    @property
    def report_scenario_names(self) -> list[str]:
        return [s.name for s in self.report_scenarios]

    def sensitivity(self, sweep_id: str) -> SensitivityConfig:
        for sweep in self.sensitivities:
            if sweep.id == sweep_id:
                return sweep
        raise KeyError(
            f"unknown sensitivity {sweep_id!r}; known: "
            f"{[s.id for s in self.sensitivities]}"
        )

    def sweep_points(self, sweep_id: str) -> list[ScenarioConfig]:
        """The scenarios making up ``sweep_id``, ordered by swept value."""
        points = [
            s
            for s in self.scenarios
            if s.sensitivity is not None and s.sensitivity.sweep == sweep_id
        ]
        return sorted(points, key=lambda s: s.sensitivity.value)

    def scenario(self, name: str) -> ScenarioConfig:
        for s in self.scenarios:
            if s.name == name:
                return s
        raise KeyError(f"unknown scenario {name!r}; known: {self.scenario_names}")

    @property
    def landing_scenario(self) -> ScenarioConfig:
        return self.scenario(self.landing.scenario or self.scenarios[0].name)

    def results_dir(self, scenario: str) -> Path:
        return (self.root / self.scenario(scenario).results_dir).resolve()

    def resources_dir(self, scenario: str) -> Path:
        sc = self.scenario(scenario)
        return (self.root / (sc.resources_dir or sc.results_dir)).resolve()

    @property
    def output_is_per_scenario(self) -> bool:
        """True when ``output.dir`` contains a ``{scenario}`` placeholder."""
        return "{scenario}" in self.output.dir

    def output_dir(self, scenario: str | None = None) -> Path:
        """Directory a scenario's pages are written to.

        ``output.dir`` may contain ``{scenario}``, which puts each scenario's
        HTML next to that scenario's other results (``csvs/``, ``graphs/``,
        ``networks/``) so the whole tree uploads as one unit. Without the
        placeholder every scenario shares one directory.
        """
        pattern = self.output.dir
        if self.output_is_per_scenario:
            if scenario is None:
                raise ValueError(
                    "output.dir contains '{scenario}' so a scenario name is required; "
                    "call output_dir(scenario)"
                )
            pattern = pattern.format(scenario=scenario)
        out = Path(pattern)
        if not out.is_absolute():
            out = self.root / out
        return out.resolve()

    def common_output_root(self) -> Path:
        """Deepest directory containing every scenario's output.

        Where the top-level ``index.html`` goes when output is per-scenario.
        Sweep points have no pages, so their directories must not drag the
        common root up a level.
        """
        if not self.output_is_per_scenario:
            return self.output_dir()
        import os

        dirs = [str(self.output_dir(s.name)) for s in self.report_scenarios]
        if not dirs:
            raise ValueError(
                "every configured scenario is a sensitivity sweep point, so the "
                "report has no pages; at least one ordinary scenario is required"
            )
        return Path(os.path.commonpath(dirs)) if len(dirs) > 1 else Path(dirs[0]).parent


def _as_dataclass(cls, data: dict):
    """Instantiate ``cls`` from ``data``, erroring on unknown keys."""
    known = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(data) - known
    if unknown:
        raise ValueError(
            f"unknown key(s) {sorted(unknown)} in the '{cls.__name__}' config section. "
            f"Valid keys: {sorted(known)}"
        )
    return cls(**data)


def load_config(path: str | Path, overrides: dict | None = None) -> Config:
    """Load ``path``, merge it over the packaged defaults, and validate."""
    path = Path(path).resolve()
    with open(DEFAULTS_PATH) as f:
        defaults = yaml.safe_load(f) or {}
    with open(path) as f:
        user = yaml.safe_load(f) or {}
    merged = deep_merge(defaults, user)
    if overrides:
        merged = deep_merge(merged, overrides)

    root = Path(merged.pop("root", path.parent))
    if not root.is_absolute():
        root = (path.parent / root).resolve()

    scenarios = [ScenarioConfig(**s) for s in merged.get("scenarios", [])]
    if not scenarios:
        raise ValueError(
            "at least one entry is required under 'scenarios:' "
            "(each needs a name and a results_dir)"
        )

    model_raw = dict(merged.get("model", {}))
    dispatch_windows = _as_dataclass(
        DispatchWindowsConfig, dict(model_raw.pop("dispatch_windows", {}))
    )
    model = _as_dataclass(ModelConfig, model_raw)
    model.dispatch_windows = dispatch_windows

    nodes_raw = dict(merged.get("nodes", {}))
    aggregate = _as_dataclass(AggregateConfig, dict(nodes_raw.pop("aggregate", {})))
    groups_raw = nodes_raw.pop("groups", []) or []
    if not isinstance(groups_raw, list):
        raise ValueError(
            "nodes.groups must be a list of {code, label, members} mappings"
        )
    groups = [_as_dataclass(NodeGroupConfig, dict(g)) for g in groups_raw]
    nodes = _as_dataclass(NodesConfig, nodes_raw)
    nodes.aggregate = aggregate
    nodes.groups = groups
    if nodes.resolution not in ("location", "substring"):
        raise ValueError(
            f"nodes.resolution must be 'location' or 'substring', got {nodes.resolution!r}"
        )

    sensitivities_raw = merged.get("sensitivities", []) or []
    if not isinstance(sensitivities_raw, list):
        raise ValueError(
            "sensitivities must be a list of {id, parameter, metrics} mappings"
        )
    sensitivities = [
        _as_dataclass(SensitivityConfig, dict(sw)) for sw in sensitivities_raw
    ]
    _validate_sensitivities(sensitivities, scenarios)

    browsable = [s for s in scenarios if not s.is_sweep_point]
    if not browsable:
        raise ValueError(
            "every configured scenario carries a 'sensitivity:' marker, so the "
            "report would have no pages; at least one ordinary scenario is required"
        )

    landing = _as_dataclass(LandingConfig, dict(merged.get("landing", {})))
    if landing.scenario is None:
        landing.scenario = browsable[0].name
    elif landing.scenario not in [s.name for s in browsable]:
        detail = (
            " (it is a sensitivity sweep point, which has no pages)"
            if landing.scenario in [s.name for s in scenarios]
            else ""
        )
        raise ValueError(
            f"landing.scenario={landing.scenario!r} is not a browsable scenario"
            f"{detail} ({[s.name for s in browsable]})"
        )

    cfg = Config(
        project=_as_dataclass(ProjectConfig, dict(merged.get("project", {}))),
        model=model,
        nodes=nodes,
        output=_as_dataclass(OutputConfig, dict(merged.get("output", {}))),
        landing=landing,
        scenarios=scenarios,
        plots=dict(merged.get("plots", {})),
        texts=dict(merged.get("texts", {})),
        features=_as_dataclass(FeaturesConfig, dict(merged.get("features", {}))),
        sensitivities=sensitivities,
        root=root,
        raw=merged,
    )

    if cfg.output.plotly not in ("cdn", "inline"):
        raise ValueError(
            f"output.plotly must be 'cdn' or 'inline', got {cfg.output.plotly!r}"
        )
    _wire_sensitivities(cfg)
    return cfg


def _validate_sensitivities(
    sensitivities: list[SensitivityConfig], scenarios: list[ScenarioConfig]
) -> None:
    """Check that sweeps and their points refer to each other.

    A typo in either direction is silent otherwise: a mislabelled point simply
    vanishes from the curve, and a sweep nobody references renders an empty
    section.  Both are errors here.
    """
    declared = [sw.id for sw in sensitivities]
    duplicates = sorted({i for i in declared if declared.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate sensitivity id(s) {duplicates} under 'sensitivities:'")

    referenced: dict[str, list[str]] = {}
    for scenario in scenarios:
        if scenario.sensitivity is None:
            continue
        referenced.setdefault(scenario.sensitivity.sweep, []).append(scenario.name)

    unknown = sorted(set(referenced) - set(declared))
    if unknown:
        raise ValueError(
            f"scenario(s) reference undeclared sensitivity sweep(s) {unknown}: "
            + "; ".join(f"{k}: {referenced[k]}" for k in unknown)
            + f". Declared: {declared}"
        )

    empty = [i for i in declared if not referenced.get(i)]
    if empty:
        raise ValueError(
            f"sensitivity sweep(s) {empty} have no points: no scenario carries "
            "'sensitivity: {sweep: <id>, value: <x>}' for them"
        )

    for sweep_id, names in referenced.items():
        values = [
            s.sensitivity.value for s in scenarios if s.name in names and s.sensitivity
        ]
        if len(set(values)) != len(values):
            raise ValueError(
                f"sensitivity sweep {sweep_id!r} has two points at the same value; "
                f"points: {dict(zip(names, values, strict=True))}"
            )


def _wire_sensitivities(cfg: Config) -> None:
    """Make a declared sweep visible: narrative text and navigation entry.

    Two conveniences, both of which are silent failures otherwise.  A sweep's
    ``description`` is folded into ``texts:`` so it renders through the one
    narrative path every other section uses, and the sensitivity page is added
    to ``output.pages`` when the project pinned that list before sweeps
    existed — a configured sweep that renders nowhere is never what was meant.
    """
    if not cfg.sensitivities:
        return
    for sweep in cfg.sensitivities:
        if sweep.description and sweep.section_id not in cfg.texts:
            cfg.texts[sweep.section_id] = {"default": sweep.description}
    if cfg.output.pages and SENSITIVITY_PAGE not in cfg.output.pages:
        cfg.output.pages = [*cfg.output.pages, SENSITIVITY_PAGE]
        logger.info(
            "sensitivities are configured; appending %r to output.pages",
            SENSITIVITY_PAGE,
        )
