"""Configuration loading and validation.

A pypsa2html run is fully described by one YAML file.  Defaults live in
``pypsa2html/data/default.yaml`` and are deep-merged with the user file, so a
project config only states what differs.

The legacy SEPIA tool spread its settings over four places -- a ``MAIN_PARAMS``
sheet in an xlsx, ``config/plots.yaml``, hardcoded literals in ``__main__``
blocks, and the Snakemake rule itself.  Everything that is genuinely a *choice*
now lives here; everything that is a *taxonomy* (nodes, processes, carrier
codes) lives in packaged CSVs -- see :mod:`pypsa2html.datafiles`.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
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


@dataclass
class ScenarioConfig:
    """One solved scenario: a directory of PyPSA-Eur results."""

    name: str
    label: str = ""
    results_dir: str = ""
    resources_dir: str = ""
    #: Optional free-form HTML shown at the top of this scenario's landing page.
    description: str | None = None

    def __post_init__(self):
        self.label = self.label or self.name
        if not self.results_dir:
            raise ValueError(f"scenario {self.name!r} has no results_dir")


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
class NodesConfig:
    detect: bool = True
    include: list[str] | None = None
    exclude: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    focus: str | None = None
    resolution: str = "location"
    aggregate: AggregateConfig = field(default_factory=AggregateConfig)


@dataclass
class LandingConfig:
    """Which page the site opens on."""

    scenario: str | None = None
    node: str | None = None
    page: str = "overview"


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
    #: Directory the relative paths in this config resolve against.
    root: Path = field(default_factory=Path.cwd)
    raw: dict = field(default_factory=dict)

    # -- convenience ------------------------------------------------------
    @property
    def scenario_names(self) -> list[str]:
        return [s.name for s in self.scenarios]

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

    def output_dir(self, scenario: str | None = None) -> Path:
        out = Path(self.output.dir)
        if not out.is_absolute():
            out = self.root / out
        return out.resolve()


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

    nodes_raw = dict(merged.get("nodes", {}))
    aggregate = _as_dataclass(AggregateConfig, dict(nodes_raw.pop("aggregate", {})))
    nodes = _as_dataclass(NodesConfig, nodes_raw)
    nodes.aggregate = aggregate
    if nodes.resolution not in ("location", "substring"):
        raise ValueError(
            f"nodes.resolution must be 'location' or 'substring', got {nodes.resolution!r}"
        )

    landing = _as_dataclass(LandingConfig, dict(merged.get("landing", {})))
    if landing.scenario is None:
        landing.scenario = scenarios[0].name
    elif landing.scenario not in [s.name for s in scenarios]:
        raise ValueError(
            f"landing.scenario={landing.scenario!r} is not a declared scenario "
            f"({[s.name for s in scenarios]})"
        )

    cfg = Config(
        project=_as_dataclass(ProjectConfig, dict(merged.get("project", {}))),
        model=_as_dataclass(ModelConfig, dict(merged.get("model", {}))),
        nodes=nodes,
        output=_as_dataclass(OutputConfig, dict(merged.get("output", {}))),
        landing=landing,
        scenarios=scenarios,
        plots=dict(merged.get("plots", {})),
        texts=dict(merged.get("texts", {})),
        root=root,
        raw=merged,
    )

    if cfg.output.plotly not in ("cdn", "inline"):
        raise ValueError(
            f"output.plotly must be 'cdn' or 'inline', got {cfg.output.plotly!r}"
        )
    return cfg
