"""Node (region) discovery and resolution.

A *node* is a spatial unit of the model that gets its own set of HTML pages.
In PyPSA-Eur this is whatever the clustering produced: a country code (``BE``),
an administrative region (``BEWAL``), or a numbered cluster (``BE1 0``).

The legacy code decided node membership with
``component_names.filter(like=country)`` -- a case-sensitive substring match on
the *component name*.  That is fragile: ``filter(like="BE")`` also matches
``BEWAL``, and it silently depends on node codes never being prefixes of one
another.  This module instead uses ``n.buses.location``, which PyPSA-Eur
populates during clustering and which both reference models already carry.

See ``docs/DESIGN_DECISIONS.md`` (D2, D3) for the trade-offs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)

#: Locations PyPSA-Eur uses for buses that belong to no single region.
#: ``EU`` carries the global oil/gas/coal/biomass buses; ``""`` is the fallback
#: for buses ``assign_locations`` could not attribute.
PSEUDO_LOCATIONS = frozenset({"", "EU"})


@dataclass(frozen=True)
class Node:
    """One spatial unit of the report."""

    code: str
    label: str
    #: True for the synthetic "sum of all nodes" entry, which has no buses of
    #: its own and is resolved by *not* filtering.
    aggregate: bool = False

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


@dataclass
class NodeSet:
    """The ordered set of nodes a report is generated for."""

    nodes: list[Node]
    focus: str
    aggregate_code: str | None = None

    def __iter__(self):
        return iter(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    @property
    def codes(self) -> list[str]:
        return [n.code for n in self.nodes]

    @property
    def real_codes(self) -> list[str]:
        """Node codes excluding the synthetic aggregate."""
        return [n.code for n in self.nodes if not n.aggregate]

    def __getitem__(self, code: str) -> Node:
        for n in self.nodes:
            if n.code == code:
                return n
        raise KeyError(f"unknown node {code!r}; known nodes: {self.codes}")

    def is_aggregate(self, code: str) -> bool:
        return self.aggregate_code is not None and code == self.aggregate_code


def detect_locations(network, exclude: frozenset[str] = PSEUDO_LOCATIONS) -> list[str]:
    """Return the sorted real locations present in ``network``.

    Raises ``ValueError`` if the network has no ``location`` column, which
    means it was not produced by a PyPSA-Eur sector-coupled workflow.
    """
    buses = network.buses
    if "location" not in buses.columns:
        raise ValueError(
            "network.buses has no 'location' column -- cannot auto-detect nodes. "
            "Set nodes.include explicitly in the configuration."
        )
    locations = {str(loc) for loc in buses["location"].dropna().unique()}
    return sorted(locations - set(exclude))


def build_node_set(
    detected: list[str],
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    labels: dict[str, str] | None = None,
    focus: str | None = None,
    aggregate_code: str | None = None,
    aggregate_label: str | None = None,
) -> NodeSet:
    """Assemble the final :class:`NodeSet` from detection plus config overrides.

    ``include`` fully overrides detection when given; otherwise detection runs
    and ``exclude`` is subtracted.  The aggregate node, when enabled, is always
    appended last so that positional output ordering stays stable.
    """
    labels = labels or {}
    codes = list(include) if include else [c for c in detected if c not in set(exclude or ())]

    if not codes:
        raise ValueError(
            "no nodes left after filtering. "
            f"detected={detected} include={include} exclude={exclude}"
        )

    if include:
        unknown = [c for c in include if c not in detected]
        if unknown and detected:
            logger.warning(
                "configured nodes %s were not found in the network (detected: %s)",
                unknown,
                detected,
            )

    nodes = [Node(code=c, label=labels.get(c, c)) for c in codes]

    if aggregate_code:
        if aggregate_code in codes:
            raise ValueError(
                f"aggregate node code {aggregate_code!r} collides with a real model "
                f"node. Choose a different nodes.aggregate.code (detected: {codes})."
            )
        nodes.append(
            Node(
                code=aggregate_code,
                label=aggregate_label or f"All {len(codes)} regions",
                aggregate=True,
            )
        )

    focus = focus or codes[0]
    known = [n.code for n in nodes]
    if focus not in known:
        raise ValueError(f"nodes.focus={focus!r} is not one of {known}")

    return NodeSet(nodes=nodes, focus=focus, aggregate_code=aggregate_code)


class NodeResolver:
    """Maps PyPSA components to nodes for one network.

    Two strategies are available:

    ``location`` (default)
        Map each component to the location of its bus (``bus`` for one-port
        components, ``bus0`` for links and lines).  Correct and unambiguous.

    ``substring``
        Reproduce the legacy behaviour -- case-sensitive substring match
        on the component *name*.  Only for byte-comparison against old output.
    """

    def __init__(self, network, strategy: str = "location"):
        if strategy not in ("location", "substring"):
            raise ValueError(f"unknown node resolution strategy {strategy!r}")
        self.n = network
        self.strategy = strategy
        self._bus_location = (
            network.buses["location"] if "location" in network.buses.columns else None
        )
        if strategy == "location" and self._bus_location is None:
            raise ValueError(
                "strategy='location' requires a 'location' column on network.buses"
            )

    def bus_nodes(self, buses: pd.Series) -> pd.Series:
        """Map a Series of bus names to their node codes."""
        return buses.map(self._bus_location)

    def mask(self, component: str, node: str, *, bus_attr: str | None = None) -> pd.Series:
        """Boolean mask selecting the rows of ``component`` belonging to ``node``.

        ``bus_attr`` defaults to ``bus`` for one-port components and ``bus0``
        for branches.
        """
        static = self.n.static(component) if hasattr(self.n, "static") else getattr(self.n, component)
        if self.strategy == "substring":
            return pd.Series(static.index.str.contains(node, regex=False), index=static.index)
        if bus_attr is None:
            bus_attr = "bus0" if "bus0" in static.columns else "bus"
        return self.bus_nodes(static[bus_attr]) == node

    def select(self, component: str, node: str, *, bus_attr: str | None = None) -> pd.Index:
        """Index of the rows of ``component`` belonging to ``node``."""
        static = self.n.static(component) if hasattr(self.n, "static") else getattr(self.n, component)
        return static.index[self.mask(component, node, bus_attr=bus_attr)]
