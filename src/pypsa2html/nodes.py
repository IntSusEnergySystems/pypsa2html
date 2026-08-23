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
from collections.abc import Mapping, Sequence
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
    #: True for any synthetic node (study-wide sum, or a configured group).
    #: Synthetic nodes have no buses of their own; membership is ``members``.
    aggregate: bool = False
    #: ``None`` on a real node and on the study-wide aggregate (every real
    #: location).  A tuple on a group aggregate: the explicit member codes.
    members: tuple[str, ...] | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


@dataclass
class NodeSet:
    """The ordered set of nodes a report is generated for."""

    nodes: list[Node]
    focus: str
    #: Study-wide "sum of all real nodes" code, when enabled.  Group
    #: aggregates are *also* synthetic and are not stored here.
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
        """Node codes excluding every synthetic aggregate (study-wide and groups)."""
        return [n.code for n in self.nodes if not n.aggregate]

    def __getitem__(self, code: str) -> Node:
        for n in self.nodes:
            if n.code == code:
                return n
        raise KeyError(f"unknown node {code!r}; known nodes: {self.codes}")

    def is_aggregate(self, code: str) -> bool:
        """True for the study-wide aggregate *and* for every group aggregate."""
        for n in self.nodes:
            if n.code == code:
                return n.aggregate
        return False

    def is_study_wide(self, code: str) -> bool:
        """True when ``code`` is the unfiltered sum of every real location.

        That is the ``nodes.aggregate`` entry (``members is None``), or a
        group whose remaining members happen to be exactly ``real_codes``.
        """
        if not self.is_aggregate(code):
            return False
        members = self[code].members
        if members is None:
            return True
        return set(members) == set(self.real_codes)

    def members_of(self, code: str) -> list[str] | None:
        """Location codes that make up ``code``.

        ``None`` for a real node and for the study-wide aggregate (meaning
        "every real location").  For a group aggregate, the sorted unique
        member codes that were present at detection — never a prefix match.
        """
        node = self[code]
        if not node.aggregate or node.members is None:
            return None
        return list(node.members)

    def locations_for(self, code: str) -> list[str] | None:
        """Locations to keep when selecting components for ``code``.

        ``None`` means do not filter (study-wide, or a group equal to all
        real nodes).  Otherwise the exact location codes to keep — a single
        real node, or a group's members.  Never derived from ``.str[:2]``.
        """
        node = self[code]
        if not node.aggregate:
            return [code]
        if node.members is None or set(node.members) == set(self.real_codes):
            return None
        return list(node.members)


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


def _group_fields(spec: Mapping | object) -> tuple[str, str, list[str]]:
    """``(code, label, members)`` from a mapping or a config dataclass."""
    if isinstance(spec, Mapping):
        code = str(spec["code"])
        label = str(spec.get("label") or "")
        members = [str(m) for m in (spec.get("members") or [])]
        extra = set(spec) - {"code", "label", "members"}
        if extra:
            raise ValueError(
                f"unknown key(s) {sorted(extra)} on nodes.groups entry {code!r}. "
                "Only explicit 'members' lists are supported — no prefix matching."
            )
        return code, label, members
    return str(spec.code), str(getattr(spec, "label", "") or ""), [
        str(m) for m in (getattr(spec, "members", None) or [])
    ]


def build_node_set(
    detected: list[str],
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    labels: dict[str, str] | None = None,
    focus: str | None = None,
    aggregate_code: str | None = None,
    aggregate_label: str | None = None,
    groups: Sequence | None = None,
) -> NodeSet:
    """Assemble the final :class:`NodeSet` from detection plus config overrides.

    ``include`` fully overrides detection when given; otherwise detection runs
    and ``exclude`` is subtracted.  Configured group aggregates are appended
    after the real nodes; the study-wide aggregate, when enabled, is always
    last so that positional output ordering stays stable.

    Group membership is an explicit ``members`` list.  There is no prefix
    matching (``BE`` never silently includes ``BEWAL``).  A group whose
    ``code`` is already a detected location is omitted with a warning.
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

    for spec in groups or ():
        g_code, g_label, g_members = _group_fields(spec)
        if g_code in codes:
            logger.warning(
                "group %s collides with a real model node %s; group omitted "
                "(the real node already has pages)",
                g_code,
                g_code,
            )
            continue
        if aggregate_code and g_code == aggregate_code:
            raise ValueError(
                f"group node code {g_code!r} collides with nodes.aggregate.code. "
                f"Choose a different nodes.groups[].code."
            )
        if g_code in {n.code for n in nodes if n.aggregate}:
            raise ValueError(
                f"group node code {g_code!r} is duplicated in nodes.groups."
            )
        present: list[str] = []
        missing: list[str] = []
        for member in g_members:
            if member in codes:
                present.append(member)
            else:
                missing.append(member)
        if missing:
            logger.warning(
                "group %s: member(s) %s were not found in the detected nodes "
                "(%s); skipped",
                g_code,
                missing,
                codes,
            )
        if not present:
            logger.warning(
                "group %s has no remaining members after detection; group omitted",
                g_code,
            )
            continue
        members = tuple(sorted(set(present)))
        nodes.append(
            Node(
                code=g_code,
                label=labels.get(g_code, g_label or g_code),
                aggregate=True,
                members=members,
            )
        )

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
                members=None,
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
        self._first_real_link_nodes: pd.Series | None = None

    def _static(self, component: str) -> pd.DataFrame:
        return getattr(self.n, component)

    def bus_nodes(self, buses: pd.Series) -> pd.Series:
        """Map a Series of bus names to their node codes."""
        return buses.map(self._bus_location)

    def first_real_link_nodes(self) -> pd.Series:
        """Map every link to the first of its buses that sits in a real location.

        ``bus0`` alone is not enough: PyPSA-Eur keeps oil/coal/methanol
        commodity buses at the pseudo-location ``EU``, so a regional naphtha
        link (``bus0 = "EU oil"``, ``bus1 = "<node> naphtha for industry"``)
        would otherwise belong to no node.  Cached on the resolver.
        """
        if self._first_real_link_nodes is not None:
            return self._first_real_link_nodes
        links = self.n.links
        nodes = pd.Series(pd.NA, index=links.index, dtype=object)
        bus_columns = [c for c in links.columns if c.startswith("bus")]
        for column in sorted(bus_columns, key=lambda c: int(c[3:] or 0)):
            candidate = self.bus_nodes(links[column])
            candidate = candidate.where(~candidate.isin(PSEUDO_LOCATIONS))
            nodes = nodes.mask(nodes.isna(), candidate)
        if "bus0" in links.columns:
            nodes = nodes.mask(nodes.isna(), self.bus_nodes(links["bus0"]))
        self._first_real_link_nodes = nodes
        return nodes

    def mask(self, component: str, node: str, *, bus_attr: str | None = None) -> pd.Series:
        """Boolean mask selecting the rows of ``component`` belonging to ``node``.

        ``bus_attr`` defaults to ``bus`` for one-port components and ``bus0``
        for branches.  For a set of locations use :meth:`select_locations`.
        """
        static = self._static(component)
        if self.strategy == "substring":
            return pd.Series(static.index.str.contains(node, regex=False), index=static.index)
        if bus_attr is None:
            bus_attr = "bus0" if "bus0" in static.columns else "bus"
        return self.bus_nodes(static[bus_attr]) == node

    def select(self, component: str, node: str, *, bus_attr: str | None = None) -> pd.Index:
        """Index of the rows of ``component`` belonging to ``node``."""
        return self.select_locations(component, [node], bus_attr=bus_attr)

    def select_locations(
        self,
        component: str,
        locations: list[str] | None,
        *,
        bus_attr: str | None = None,
    ) -> pd.Index:
        """Index of rows whose resolved location is in ``locations``.

        ``locations is None`` means no filter (study-wide aggregate).  An empty
        list selects nothing.  There is no prefix matching: ``"BE"`` does not
        match location ``"BEWAL"``.

        ``bus_attr`` defaults to ``bus0`` / ``bus``, same as :meth:`mask`.
        For energy-flow link attribution (first real bus) use
        :meth:`first_real_link_nodes` via ``BuildContext.component_index``.
        """
        static = self._static(component)
        if locations is None:
            return static.index
        if not locations:
            return static.index[0:0]
        if self.strategy == "substring":
            mask = pd.Series(False, index=static.index)
            for loc in locations:
                mask |= pd.Series(
                    static.index.str.contains(loc, regex=False), index=static.index
                )
            return static.index[mask]
        if bus_attr is None:
            bus_attr = "bus0" if "bus0" in static.columns else "bus"
        return static.index[self.bus_nodes(static[bus_attr]).isin(locations)]
