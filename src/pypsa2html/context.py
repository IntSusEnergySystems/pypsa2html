"""The build context -- the object that replaces the magic ``snakemake`` global.

The legacy tool read ``snakemake`` from three different scopes and carried a
further dozen values (``study``, ``countries``, ``loaded_files``, ``fn``,
``logo``, ``file_path``, ``planning_horizons``, ...) as module globals set in a
``__main__`` block.  Four of those globals were *rebound from a function to a
DataFrame* (``costs = costs(...)``), which made the function uncallable
afterwards.  Every ported routine now takes a :class:`BuildContext` instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import pandas as pd

from .config import Config, ScenarioConfig
from .datafiles import Taxonomy, load_taxonomy
from .networks import NetworkCache, build_cache
from .nodes import NodeResolver, NodeSet, build_node_set, detect_locations

logger = logging.getLogger(__name__)


@dataclass
class BuildContext:
    """Everything one scenario's page builders need, passed explicitly."""

    config: Config
    scenario: ScenarioConfig
    taxonomy: Taxonomy
    nodes: NodeSet
    networks: NetworkCache
    results_dir: Path
    resources_dir: Path
    #: Cache for CSV/xlsx reads, so a file is parsed once per build rather
    #: than once per (node x horizon) as in the original.
    _files: dict = field(default_factory=dict, repr=False)

    # -- derived ----------------------------------------------------------
    @property
    def horizons(self) -> list[int]:
        return self.networks.horizons

    @property
    def year_columns(self) -> list[str]:
        """Output column labels, derived from the horizons.

        Replaces the ``['2020','2030','2040','2050']`` literals that appeared
        in ~20 places and made 2035/2045 structurally impossible.
        """
        base = self.config.model.base_year
        cols = [str(h) for h in self.horizons]
        if base is not None and str(base) not in cols:
            cols = [str(base)] + cols
        return cols

    def horizon_weights(self) -> pd.Series:
        """Years represented by each horizon, for cumulative sums.

        The legacy tool hardcoded ``*= 10`` for a decadal grid and left the first
        horizon unweighted; the pypsa-wal fork bolted on ``*= 5``. Here the
        weight of each horizon is the gap to the next one, with the last
        horizon inheriting the previous gap.
        """
        h = self.horizons
        if len(h) == 1:
            return pd.Series([1.0], index=h, dtype=float)
        gaps = [h[i + 1] - h[i] for i in range(len(h) - 1)]
        return pd.Series(gaps + [gaps[-1]], index=h, dtype=float)

    def resolver(self, horizon: int) -> NodeResolver:
        key = ("node_resolver", int(horizon), self.config.nodes.resolution)
        cached = self._files.get(key)
        if cached is None:
            cached = NodeResolver(self.networks[horizon], self.config.nodes.resolution)
            self._files[key] = cached
        return cached

    def is_aggregate(self, node: str) -> bool:
        """True for the study-wide aggregate and for every group aggregate."""
        return self.nodes.is_aggregate(node)

    def is_study_wide(self, node: str) -> bool:
        """True when ``node`` is the unfiltered sum of every real location."""
        return self.nodes.is_study_wide(node)

    def members_of(self, node: str) -> list[str] | None:
        """See :meth:`NodeSet.members_of`."""
        return self.nodes.members_of(node)

    def locations_for(self, node: str) -> list[str] | None:
        """See :meth:`NodeSet.locations_for`.

        ``None`` = do not filter.  A list = keep exactly those location codes.
        """
        return self.nodes.locations_for(node)

    def component_index(
        self,
        horizon: int,
        component: str,
        node: str,
        *,
        bus_attr: str | None = None,
    ) -> pd.Index:
        """Index of ``component`` rows belonging to ``node``.

        * Real node: that location only.
        * Group aggregate: rows whose resolved location is in ``members_of``.
        * Study-wide aggregate (``members`` is None, or equals every real
          code): no filter.

        Links with no ``bus_attr`` are attributed to the first bus that sits
        in a real location, so commodity hubs at the pseudo-location ``EU``
        still count toward the region they serve.  Charts and extractors must
        use this helper rather than forking ``is_aggregate`` / ``.str[:2]``.
        """
        resolver = self.resolver(horizon)
        locations = self.locations_for(node)
        network = self.networks[horizon]
        static = getattr(network, component)
        if locations is None:
            return static.index
        if (
            component == "links"
            and bus_attr is None
            and resolver.strategy == "location"
        ):
            assigned = resolver.first_real_link_nodes()
            return static.index[assigned.isin(locations)]
        return resolver.select_locations(component, locations, bus_attr=bus_attr)

    # -- cached file access ------------------------------------------------
    def read_csv(self, relpath: str | Path, *, base: str = "results", **kwargs):
        """Read a CSV relative to the scenario's results or resources dir.

        Returns ``None`` when the file is absent, so a page builder can degrade
        to a placeholder instead of crashing the whole run.
        """
        root = self.results_dir if base == "results" else self.resources_dir
        path = Path(root) / relpath
        key = ("csv", str(path), tuple(sorted(kwargs.items())))
        if key not in self._files:
            if not path.exists():
                logger.warning("missing input, section will be skipped: %s", path)
                self._files[key] = None
            else:
                self._files[key] = pd.read_csv(path, **kwargs)
        value = self._files[key]
        return None if value is None else value.copy()

    def read_excel(self, relpath: str | Path, *, base: str = "results", **kwargs):
        root = self.results_dir if base == "results" else self.resources_dir
        path = Path(root) / relpath
        key = ("xls", str(path), tuple(sorted(kwargs.items())))
        if key not in self._files:
            if not path.exists():
                logger.warning("missing input, section will be skipped: %s", path)
                self._files[key] = None
            else:
                self._files[key] = pd.read_excel(path, **kwargs)
        value = self._files[key]
        return None if value is None else (
            {k: v.copy() for k, v in value.items()} if isinstance(value, dict) else value.copy()
        )

    @cached_property
    def costs(self) -> pd.DataFrame | None:
        """Technology cost assumptions, indexed by ``(technology, parameter)``.

        The legacy code did ``pd.read_csv(fn, index_col=[0, 1])`` on what is
        actually a *wide* table, so ``.loc[("gas", "CO2 intensity")]`` returned
        a length-1 Series and ``float(...)`` on it raises from pandas 2.2 on.
        Here the shape is detected and normalised.
        """
        for candidate in (
            "costs_2050_processed.csv",
            f"costs_{self.horizons[-1]}_processed.csv",
            "costs.csv",
        ):
            raw = self.read_csv(candidate, base="resources")
            if raw is not None:
                return _normalise_costs(raw)
        logger.warning("no technology cost table found under %s", self.resources_dir)
        return None

    def cost(self, technology: str, parameter: str, default: float | None = None) -> float:
        """One scalar cost assumption, or ``default`` when unavailable."""
        table = self.costs
        if table is None:
            if default is None:
                raise KeyError(f"no cost table available for ({technology}, {parameter})")
            return default
        try:
            return float(table.at[(technology, parameter), "value"])
        except (KeyError, ValueError, TypeError):
            if default is None:
                raise KeyError(
                    f"cost assumption ({technology!r}, {parameter!r}) not found"
                ) from None
            logger.debug("cost (%s, %s) missing, using %s", technology, parameter, default)
            return default


def _normalise_costs(raw: pd.DataFrame) -> pd.DataFrame:
    """Coerce either cost-table layout into ``(technology, parameter) -> value``."""
    cols = {c.lower(): c for c in raw.columns}
    if "technology" in cols and "parameter" in cols and "value" in cols:
        long = raw.rename(
            columns={cols["technology"]: "technology", cols["parameter"]: "parameter",
                     cols["value"]: "value"}
        )
        return long.set_index(["technology", "parameter"])[["value"]]

    # Wide layout: first column is the technology, the rest are parameters.
    tech_col = raw.columns[0]
    long = raw.melt(id_vars=[tech_col], var_name="parameter", value_name="value")
    long = long.rename(columns={tech_col: "technology"})
    long = long.dropna(subset=["value"])
    long = long.drop_duplicates(subset=["technology", "parameter"], keep="first")
    return long.set_index(["technology", "parameter"])[["value"]]


def build_context(config: Config, scenario_name: str | None = None) -> BuildContext:
    """Assemble the context for one scenario, auto-detecting nodes."""
    scenario = config.scenario(scenario_name) if scenario_name else config.scenarios[0]
    results_dir = config.results_dir(scenario.name)
    resources_dir = config.resources_dir(scenario.name)
    if not results_dir.exists():
        raise FileNotFoundError(
            f"scenario {scenario.name!r}: results_dir does not exist: {results_dir}"
        )

    networks = build_cache(results_dir, config.model)

    detected: list[str] = []
    if config.nodes.detect:
        detected = detect_locations(networks.first())
        logger.info("detected nodes in %s: %s", scenario.name, detected)

    agg = config.nodes.aggregate
    nodes = build_node_set(
        detected,
        include=config.nodes.include,
        exclude=config.nodes.exclude,
        labels=config.nodes.labels,
        focus=config.nodes.focus,
        aggregate_code=agg.code if agg.enabled else None,
        aggregate_label=agg.label or None,
        groups=config.nodes.groups,
    )

    return BuildContext(
        config=config,
        scenario=scenario,
        taxonomy=load_taxonomy(),
        nodes=nodes,
        networks=networks,
        results_dir=results_dir,
        resources_dir=resources_dir,
    )
