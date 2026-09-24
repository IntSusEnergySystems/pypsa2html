"""Electric-vehicle charging: what the grid draws, and when.

A sector-coupled PyPSA-Eur network splits the electricity a car fleet consumes
into two very different things, and the interesting question is how much of
each there is:

*Natural (uncontrolled) charging*
    A load pinned to an observed charging profile.  It sits **directly on an
    electricity bus**, so the optimiser cannot move it: plugging in when you
    get home is not a decision the model makes.

*Local charging*
    Also pinned, and on the same bus, but following a *different* observed
    profile: charging steered behind the meter by a home tariff or by PV
    self-consumption, rather than by the market.  A fork that blends it into the
    natural-charging load can publish the blend weights (see
    :data:`MODE_SPLIT_FILE`) and get it reported separately; without that file
    the two are reported together as natural charging, which is what every
    upstream model does.

*Smart (optimised) charging*
    A charger Link into a fleet-battery bus, behind which a Store lets the
    optimiser move energy in time within an availability envelope.  Its
    grid-side profile is an *output* of the optimisation.

*V2G*
    A Link back out of the fleet-battery bus, returning energy to the grid.

Not every model has all three.  Upstream PyPSA-Eur and the négaWatt fork put
the whole fleet behind the charger (all of it optimised); the pypsa-wal fork
splits the demand, pinning a share to Elia's observed natural-charging profile
(``sector.bev_natural_charging_split``).  This module handles both by reading
the *topology* rather than carrier names:

======================================  ==========================================
any Link whose ``bus1`` is a fleet bus  charging (grid → fleet)
any Link whose ``bus0`` is a fleet bus  discharging (fleet → grid, i.e. V2G)
any Load **on** a fleet bus             driving demand of the optimised fleet
any EV Load **not** on a fleet bus      uncontrolled charging, pinned in time
any Store on a fleet bus                the fleet battery
======================================  ==========================================

That matters in practice: the same carrier is spelled ``EV charger`` upstream
and in négaWatt but ``BEV charger`` in pypsa-wal, and matching on the name
alone silently produced an empty chart on one of the two.

Topology cannot separate natural from local charging, because both are pinned
loads on the same bus and a fork is free to blend them into one component.  That
one split therefore comes from a published file rather than from the network —
:data:`MODE_SPLIT_FILE`, optional, absent everywhere but pypsa-wal.

Sign convention: every series returned here is **grid-side and positive when
the grid is being drawn from**, so ``NET = NATURAL + LOCAL + SMART + V2G`` with
``V2G`` carried as a negative number.  Values are MW; charts convert to GW.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from ..context import BuildContext

logger = logging.getLogger(__name__)

#: Substring identifying a fleet-battery bus carrier, matched case-insensitively.
#: PyPSA-Eur has spelled it ``EV battery`` since sector coupling was merged; a
#: fork that appends to the name (``EV battery DSM``) is still found.
EV_BUS_CARRIER = "ev battery"

#: A Load carrier that is EV electricity.  ``\bev\b`` deliberately requires
#: ``EV`` as a whole word, so ``land transport EV inflexible`` matches while
#: an unrelated carrier that merely contains the letters does not.
EV_LOAD_PATTERN = r"(?i)\bev\b"

#: Column labels of :attr:`EVCharging.frame`.  Also the palette keys in
#: ``data/tech_colors.csv`` and the legend entries of the chart.
NATURAL = "Natural charging"
LOCAL = "Local charging"
SMART = "Smart charging"
V2G = "V2G to grid"
NET = "Net EV grid draw"
COUNTERFACTUAL = "If charged naturally"
DELIVERED = "Delivered to vehicles"

#: Grid-draw columns whose signed sum is :data:`NET`.  :data:`LOCAL` is carved
#: out of :data:`NATURAL` when the model publishes a mode split, so the sum is
#: the same either way (see :func:`_split_uncontrolled`).
MODES = (NATURAL, LOCAL, SMART, V2G)

#: Resources file a fork may publish to separate the two *uncontrolled* modes
#: that share one load in the network.  ``{clusters}`` and ``{horizon}`` are
#: filled from the report config; the file carries a ``(mode, node)`` column
#: header with modes ``natural`` and ``local``.
MODE_SPLIT_FILE = "ev_charging_mode_split_s_{clusters}_{horizon}.csv"


@dataclass(frozen=True)
class EVComponents:
    """The electric-vehicle part of one network, restricted to one node."""

    buses: pd.Index
    charge_links: pd.Index
    v2g_links: pd.Index
    #: Loads on a fleet bus: the driving demand served by optimised charging.
    flexible_loads: pd.Index
    #: EV loads on an electricity bus: natural charging, fixed in time.
    natural_loads: pd.Index
    stores: pd.Index

    def __bool__(self) -> bool:
        """True when there is an EV grid draw to plot."""
        return bool(len(self.charge_links) or len(self.natural_loads))

    @property
    def has_split(self) -> bool:
        """True when the model separates natural from optimised charging."""
        return bool(len(self.natural_loads) and len(self.charge_links))

    @property
    def has_v2g(self) -> bool:
        return bool(len(self.v2g_links))


@dataclass(frozen=True)
class EVCharging:
    """Grid-side EV profiles for one node, horizon and time window.

    ``frame`` holds :data:`NATURAL`, :data:`LOCAL`, :data:`SMART`, :data:`V2G`,
    :data:`NET` and (when derivable) :data:`COUNTERFACTUAL`, in MW.
    :data:`LOCAL` is all-zero unless the model published a charging-mode split.
    """

    frame: pd.DataFrame
    #: Snapshot weightings over the same window, in hours.
    weights: pd.Series
    #: Energy reaching the vehicles (vehicle side, MW) -- *not* a grid draw.
    delivered: pd.Series
    #: Fleet-battery state of charge in % of installed capacity, when modelled.
    soc: pd.Series | None
    #: Where the natural-charging shape came from; shown in the chart legend.
    shape_source: str
    components: EVComponents

    def energy_mwh(self, column: str) -> float:
        """Snapshot-weighted energy of one column of :attr:`frame`."""
        if column not in self.frame.columns:
            return 0.0
        return float((self.frame[column] * self.weights).sum())

    @property
    def delivered_mwh(self) -> float:
        return float((self.delivered * self.weights).sum())


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def ev_components(
    network,
    resolver,
    locations: list[str] | None,
) -> EVComponents:
    """Find the EV components of ``network`` that belong to ``locations``.

    ``locations is None`` means "do not filter" (the study-wide aggregate),
    matching :meth:`BuildContext.locations_for`.
    """
    buses = network.buses
    carriers = buses.get("carrier")
    if carriers is None:
        return _empty_components(network)
    is_fleet = carriers.astype(str).str.lower().str.contains(EV_BUS_CARRIER, na=False)
    all_fleet_buses = buses.index[is_fleet]
    if not len(all_fleet_buses):
        return _empty_components(network)

    fleet_buses = all_fleet_buses
    if locations is not None:
        node_of = resolver.bus_nodes(pd.Series(fleet_buses, index=fleet_buses))
        fleet_buses = fleet_buses[node_of.isin(locations)]

    links = network.links
    charge = links.index[0:0]
    v2g = links.index[0:0]
    if len(fleet_buses) and len(links):
        no_match = pd.Series(False, index=links.index)
        into = links["bus1"].isin(fleet_buses) if "bus1" in links.columns else no_match
        out_of = links["bus0"].isin(fleet_buses) if "bus0" in links.columns else no_match
        # A link with a fleet bus on both ends would be counted twice; there is
        # no such component in PyPSA-Eur, but do not invent flows if one appears.
        both = into & out_of
        charge = links.index[into & ~both]
        v2g = links.index[out_of & ~both]

    loads = network.loads
    flexible = loads.index[0:0]
    natural = loads.index[0:0]
    if len(loads):
        on_fleet_bus = loads["bus"].isin(all_fleet_buses)
        flexible = loads.index[loads["bus"].isin(fleet_buses)]
        is_ev = loads["carrier"].astype(str).str.contains(EV_LOAD_PATTERN, na=False)
        natural = loads.index[is_ev & ~on_fleet_bus]
        natural = natural.intersection(resolver.select_locations("loads", locations))

    stores = network.stores
    fleet_stores = stores.index[0:0]
    if len(stores) and len(fleet_buses):
        fleet_stores = stores.index[stores["bus"].isin(fleet_buses)]

    return EVComponents(
        buses=fleet_buses,
        charge_links=charge,
        v2g_links=v2g,
        flexible_loads=flexible,
        natural_loads=natural,
        stores=fleet_stores,
    )


def _empty_components(network) -> EVComponents:
    return EVComponents(
        buses=network.buses.index[0:0],
        charge_links=network.links.index[0:0],
        v2g_links=network.links.index[0:0],
        flexible_loads=network.loads.index[0:0],
        natural_loads=network.loads.index[0:0],
        stores=network.stores.index[0:0],
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def charging_profiles(
    ctx: BuildContext,
    node: str,
    horizon: int,
    *,
    start: pd.Timestamp | str | None = None,
    stop: pd.Timestamp | str | None = None,
) -> EVCharging | None:
    """Grid-side EV profiles for one node and horizon, optionally windowed.

    Returns ``None`` -- with a warning, never an exception -- when the horizon
    is missing or the model has no electric vehicles.
    """
    networks = getattr(ctx, "networks", None)
    if networks is None:
        logger.warning("context carries no solved networks; EV sections skipped")
        return None
    if horizon not in networks:
        logger.warning("no network for horizon %s", horizon)
        return None
    try:
        n = networks[horizon]
    except FileNotFoundError:
        logger.warning("solved network missing for horizon %s", horizon)
        return None
    if not len(n.snapshots):
        logger.warning("network for horizon %s has no snapshots", horizon)
        return None

    locations = ctx.locations_for(node)
    components = ev_components(n, ctx.resolver(horizon), locations)
    if not components:
        logger.info(
            "no electric vehicles for node %s horizon %s; EV sections skipped",
            node,
            horizon,
        )
        return None

    uncontrolled = _load_power(n, components.natural_loads)
    natural, local = _split_uncontrolled(
        ctx, n, horizon, components, ctx.resolver(horizon), uncontrolled
    )
    smart = _link_power(n, components.charge_links, "p0")
    # ``p1`` is negative when a link delivers to bus1, so a V2G link returning
    # energy to the grid already carries the grid-side sign we want.
    v2g = _link_power(n, components.v2g_links, "p1")
    delivered = _load_power(n, components.flexible_loads) + uncontrolled
    soc = _state_of_charge(n, components.stores)
    weights = n.snapshot_weightings.generators.astype(float)

    if start is not None or stop is not None:
        uncontrolled = uncontrolled.loc[start:stop]
        natural = natural.loc[start:stop]
        local = local.loc[start:stop]
        smart = smart.loc[start:stop]
        v2g = v2g.loc[start:stop]
        delivered = delivered.loc[start:stop]
        weights = weights.loc[start:stop]
        soc = None if soc is None else soc.loc[start:stop]

    if not len(uncontrolled):
        logger.warning(
            "EV window is empty for node %s horizon %s (start=%s stop=%s)",
            node, horizon, start, stop,
        )
        return None

    frame = pd.DataFrame({NATURAL: natural, LOCAL: local, SMART: smart, V2G: v2g})
    frame[NET] = frame[list(MODES)].sum(axis=1)

    # The counterfactual stays on the *whole* uncontrolled profile: it answers
    # "what if none of this were steered by the market", which is the question
    # the dashed line has always been drawn for, and splitting the baseline
    # would make it a different comparison.
    shape, shape_source = _natural_shape(
        uncontrolled, delivered - uncontrolled, components
    )
    counterfactual = _counterfactual(shape, frame[NET], weights)
    if counterfactual is not None:
        frame[COUNTERFACTUAL] = counterfactual

    return EVCharging(
        frame=frame,
        weights=weights,
        delivered=delivered,
        soc=soc,
        shape_source=shape_source,
        components=components,
    )


def _split_uncontrolled(
    ctx: BuildContext,
    n,
    horizon: int,
    components: EVComponents,
    resolver,
    uncontrolled: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    """Separate the uncontrolled EV load into natural and local charging.

    Both modes are pinned loads on the same electricity bus, and a fork is free
    to merge them into one component -- pypsa-wal does, blending Elia's ``V0``
    curve with its ``V1H``/``V2H`` ones.  The blend is linear with published
    weights, so :data:`MODE_SPLIT_FILE` carries the two shape components and the
    load is split in their ratio, snapshot by snapshot.

    Returns ``(natural, local)`` in MW.  Without the file -- upstream PyPSA-Eur,
    négaWatt, or a pypsa-wal tree built before the file existed -- ``local`` is
    zero and ``natural`` carries everything, exactly as before.
    """
    zeros = pd.Series(0.0, index=uncontrolled.index)
    loads = components.natural_loads
    if not len(loads):
        return uncontrolled, zeros

    share = _mode_split_shares(ctx, horizon, n.snapshots)
    if share is None:
        return uncontrolled, zeros

    nodes = resolver.bus_nodes(n.loads.loc[loads, "bus"])
    natural = zeros.copy()
    unsplit = []
    for name, location in nodes.items():
        series = _load_power(n, pd.Index([name]))
        if location not in share.columns:
            unsplit.append(name)
            natural = natural + series
            continue
        natural = natural + series * share[location]
    if unsplit:
        # Reported as natural, which is what the whole load was before: a mode
        # this model does not describe must not silently disappear from a bar.
        logger.warning(
            "no charging-mode split published for %s; reported as %s",
            ", ".join(sorted(unsplit)),
            NATURAL,
        )
    return natural, uncontrolled - natural


def _mode_split_shares(
    ctx: BuildContext,
    horizon: int,
    snapshots: pd.Index,
) -> pd.DataFrame | None:
    """Natural share of the uncontrolled load per node, on ``snapshots``."""
    clusters = getattr(getattr(ctx.config, "model", None), "clusters", None)
    read_csv = getattr(ctx, "read_csv", None)
    if clusters is None or read_csv is None:
        return None
    raw = read_csv(
        MODE_SPLIT_FILE.format(clusters=clusters, horizon=horizon),
        base="resources",
        index_col=0,
        header=[0, 1],
        parse_dates=True,
    )
    if raw is None:
        return None
    modes = set(raw.columns.get_level_values(0))
    if not {"natural", "local"} <= modes:
        logger.warning(
            "charging-mode split for %s has modes %s, expected natural and local",
            horizon,
            sorted(modes),
        )
        return None
    natural, local = raw["natural"], raw["local"]
    total = natural + local
    share = (natural / total).where(total > 0)
    share = _align_to_snapshots(share, snapshots, weights=total)
    if share is None:
        return None
    if share.isna().any().any():
        logger.warning(
            "charging-mode split for %s does not cover every snapshot; ignored",
            horizon,
        )
        return None
    return share.clip(0.0, 1.0)


def _align_to_snapshots(
    share: pd.DataFrame,
    snapshots: pd.Index,
    *,
    weights: pd.DataFrame,
) -> pd.DataFrame | None:
    """Put an hourly share on ``snapshots``, averaging blocks by ``weights``.

    A run solved at 6 h keeps one snapshot per block, so the share of that block
    is the *energy-weighted* mean of its hours -- weighting by the load shape
    itself, since that is what the share will be multiplied by.
    """
    index = share.index
    if not isinstance(index, pd.DatetimeIndex):
        return None
    if getattr(index, "tz", None) is not None and getattr(snapshots, "tz", None) is None:
        index = index.tz_convert("UTC").tz_localize(None)
        share, weights = share.set_axis(index), weights.set_axis(index)
    if index.equals(snapshots):
        return share
    if len(index) < len(snapshots):
        logger.warning(
            "charging-mode split has %d rows against %d snapshots; ignored",
            len(index),
            len(snapshots),
        )
        return None
    block = snapshots.searchsorted(index, side="right") - 1
    inside = block >= 0
    if not inside.any():
        return None
    keys = pd.Index(snapshots[block[inside]])
    inner = weights[inside]
    numerator = (share[inside] * inner).groupby(keys).sum()
    denominator = inner.groupby(keys).sum()
    return (numerator / denominator).reindex(snapshots)


def _natural_shape(
    natural: pd.Series,
    driving: pd.Series,
    components: EVComponents,
) -> tuple[pd.Series | None, str]:
    """The unnormalised shape uncontrolled charging would follow, and its source.

    Two shapes are available, in order of preference:

    1. the model's own natural-charging load -- an observed profile, so this is
       the honest answer wherever the fork provides it;
    2. the driving profile of the optimised fleet.  Without a fleet battery,
       PyPSA-Eur's energy balance forces the charger to follow driving demand
       exactly, so this *is* what the same model does with flexibility off.
    """
    if len(components.natural_loads) and float(natural.abs().sum()) > 0:
        return natural, "observed profile"
    if float(driving.abs().sum()) > 0:
        return driving, "no-flexibility limit"
    return None, "unavailable"


def _counterfactual(
    shape: pd.Series | None,
    net: pd.Series,
    weights: pd.Series,
) -> pd.Series | None:
    """Scale ``shape`` to carry the whole EV grid draw of the window.

    The comparison is deliberately **energy-neutral**: the counterfactual has
    the same weighted energy over the window as the actual net draw, so the
    difference between the two curves is purely one of *timing* and the reader
    cannot mistake a conversion loss for a shifted peak.  The volume side of
    flexibility -- charger and V2G round-trip losses -- is reported by the
    annual chart instead (see :func:`annual_energy`).
    """
    if shape is None:
        return None
    shape_energy = float((shape * weights).sum())
    if shape_energy <= 0:
        logger.warning("natural-charging shape carries no energy; no counterfactual")
        return None
    target = float((net * weights).sum())
    if target <= 0:
        logger.warning(
            "net EV grid draw over the window is %.3g MWh; no counterfactual drawn",
            target,
        )
        return None
    return shape * (target / shape_energy)


def _load_power(n, names: pd.Index) -> pd.Series:
    """Consumption of ``names`` in MW, positive, summed (zeros when empty)."""
    zeros = pd.Series(0.0, index=n.snapshots)
    if not len(names):
        return zeros
    solved = n.loads_t.p
    known = names.intersection(solved.columns)
    total = solved[known].sum(axis=1) if len(known) else zeros
    missing = names.difference(solved.columns)
    if len(missing):
        # An unsolved network still carries ``p_set``; use it rather than
        # silently dropping a whole branch of the chart.
        total = total + _p_set(n, missing)
    return total.astype(float)


def _p_set(n, names: pd.Index) -> pd.Series:
    varying = n.loads_t.p_set
    known = names.intersection(varying.columns)
    total = (
        varying[known].sum(axis=1)
        if len(known)
        else pd.Series(0.0, index=n.snapshots)
    )
    static = names.difference(varying.columns)
    if len(static):
        total = total + float(n.loads.loc[static, "p_set"].sum())
    return total


def _link_power(n, names: pd.Index, port: str) -> pd.Series:
    """Flow at ``port`` of ``names`` in MW, summed (zeros when empty)."""
    zeros = pd.Series(0.0, index=n.snapshots)
    if not len(names):
        return zeros
    solved = getattr(n.links_t, port, None)
    if solved is None or not len(solved.columns):
        return zeros
    known = names.intersection(solved.columns)
    if not len(known):
        return zeros
    return solved[known].sum(axis=1).astype(float)


def _state_of_charge(n, stores: pd.Index) -> pd.Series | None:
    """Fleet-battery state of charge in % of installed capacity."""
    if not len(stores):
        return None
    energy = n.stores_t.e
    known = stores.intersection(energy.columns)
    if not len(known):
        return None
    static = n.stores.loc[known]
    capacity = float(static.get("e_nom_opt", static["e_nom"]).sum())
    if capacity <= 0:
        capacity = float(static["e_nom"].sum())
    if capacity <= 0:
        return None
    return energy[known].sum(axis=1).astype(float) / capacity * 100.0


# ---------------------------------------------------------------------------
# Annual view
# ---------------------------------------------------------------------------

def annual_energy(ctx: BuildContext, node: str) -> pd.DataFrame | None:
    """EV energy by charging mode and horizon, in MWh/year.

    Index: :data:`MODES` (rows that are zero everywhere are dropped) plus
    :data:`DELIVERED`, which is *vehicle side* and therefore not part of the
    stack -- the gap between the stacked total and :data:`DELIVERED` is the
    charging and V2G round-trip loss.  Columns: ``str(horizon)``.
    """
    columns: dict[str, pd.Series] = {}
    for horizon in ctx.horizons:
        data = charging_profiles(ctx, node, horizon)
        if data is None:
            continue
        values = {mode: data.energy_mwh(mode) for mode in MODES}
        values[DELIVERED] = data.delivered_mwh
        columns[str(horizon)] = pd.Series(values)
    if not columns:
        logger.warning("no EV energy for node %s in any horizon", node)
        return None
    table = pd.DataFrame(columns)
    table = table.loc[(table != 0).any(axis=1)]
    return table if not table.empty else None
