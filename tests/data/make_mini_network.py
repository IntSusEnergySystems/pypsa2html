#!/usr/bin/env python
"""Build ``tests/data/mini.nc`` — a tiny sector-coupled network for unit tests.

Two locations (AA, BB), ~24 hourly snapshots, AC / gas / H2 buses, a CCGT
link with a losses residual, a solar generator, a load, and an H2 pipe.

The two locations deliberately model electric vehicles the two *different*
ways the PyPSA-Eur family does, so one fixture covers both code paths of
``extract.ev``:

* **AA** — the pypsa-wal fork: demand split into a pinned natural-charging
  load on the AC bus and a flexible load behind a ``BEV charger`` link, plus a
  fleet battery and a ``V2G`` link.
* **BB** — upstream PyPSA-Eur and the négaWatt fork: the whole fleet behind an
  ``EV charger`` link (note the other carrier spelling), a fleet battery, no
  natural-charging load and no V2G.

Regenerate after PyPSA changes::

    conda activate pypsa-eur
    PYTHONPATH=src python tests/data/make_mini_network.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

OUT = Path(__file__).with_name("mini.nc")
N_SNAPSHOTS = 24


def build() -> pypsa.Network:
    n = pypsa.Network()
    snapshots = pd.date_range("2013-01-01", periods=N_SNAPSHOTS, freq="h")
    n.set_snapshots(snapshots)
    n.snapshot_weightings.objective = 1.0
    n.snapshot_weightings.generators = 1.0
    n.snapshot_weightings.stores = 1.0

    # Buses: electricity, gas and H2 at two locations (+ a shared EU gas hub).
    for loc, x, y in (("AA", 0.0, 50.0), ("BB", 2.0, 50.0)):
        n.add("Bus", f"{loc}", carrier="AC", location=loc, x=x, y=y)
        n.add("Bus", f"{loc} gas", carrier="gas", location=loc, x=x, y=y)
        n.add("Bus", f"{loc} H2", carrier="H2", location=loc, x=x, y=y)
    n.add("Bus", "EU gas", carrier="gas", location="EU", x=-5.0, y=50.0)

    # Solar generator at AA.
    n.add(
        "Generator",
        "AA solar",
        bus="AA",
        carrier="solar",
        p_nom=800.0,
        p_nom_opt=800.0,
    )
    profile = 0.5 + 0.5 * np.sin(np.linspace(0, 2 * np.pi, N_SNAPSHOTS))
    n.generators_t.p["AA solar"] = profile * 600.0
    n.generators_t.p_max_pu["AA solar"] = profile

    # Load at each AC bus (above the 100 MW dispatch display threshold).
    for loc, demand in (("AA", 500.0), ("BB", 300.0)):
        n.add("Load", f"{loc} load", bus=loc, carrier="AC", p_set=demand)
        n.loads_t.p[f"{loc} load"] = demand

    # CCGT link gas → AC at AA (efficiency 0.5 so port totals leave a residual).
    n.add(
        "Link",
        "AA CCGT",
        bus0="AA gas",
        bus1="AA",
        carrier="CCGT",
        p_nom=2000.0,
        p_nom_opt=2000.0,
        efficiency=0.5,
    )
    gas_in = np.full(N_SNAPSHOTS, 400.0)
    n.links_t.p0["AA CCGT"] = gas_in
    n.links_t.p1["AA CCGT"] = -gas_in * 0.5

    # DC link between AA and BB.
    n.add(
        "Link",
        "AA-BB DC",
        bus0="AA",
        bus1="BB",
        carrier="DC",
        p_nom=500.0,
        p_nom_opt=500.0,
        efficiency=0.95,
    )
    flow = np.full(N_SNAPSHOTS, 150.0)
    n.links_t.p0["AA-BB DC"] = flow
    n.links_t.p1["AA-BB DC"] = -flow * 0.95

    # H2 pipeline AA → BB (two parallel links to exercise group_pipes).
    for i, cap in enumerate((1000.0, 500.0), start=1):
        name = f"AA-BB H2 pipe {i}"
        n.add(
            "Link",
            name,
            bus0="AA H2",
            bus1="BB H2",
            carrier="H2 pipeline",
            p_nom=cap,
            p_nom_opt=cap,
            efficiency=1.0,
        )
        n.links_t.p0[name] = 0.1
        n.links_t.p1[name] = -0.1

    # Battery store at AA.
    n.add("Bus", "AA battery", carrier="battery", location="AA", x=0.0, y=50.0)
    n.add(
        "Store",
        "AA battery store",
        bus="AA battery",
        carrier="battery",
        e_nom=2.0,
        e_nom_opt=2.0,
    )
    n.stores_t.p["AA battery store"] = 0.0
    n.stores_t.e["AA battery store"] = 1.0

    # H2 store at BB (for the hydrogen map choropleth).
    n.add(
        "Store",
        "BB H2 store",
        bus="BB H2",
        carrier="H2",
        e_nom=1e6,
        e_nom_opt=1e6,
    )

    _add_electric_vehicles(n, profile)

    # Gas pipeline from EU hub (for the gas map).
    n.add(
        "Link",
        "EU-AA gas",
        bus0="EU gas",
        bus1="AA gas",
        carrier="gas pipeline",
        p_nom=2e3,
        p_nom_opt=2e3,
        efficiency=1.0,
    )
    n.links_t.p0["EU-AA gas"] = 0.5
    n.links_t.p1["EU-AA gas"] = -0.5

    return n


def _add_electric_vehicles(n: pypsa.Network, solar_profile: np.ndarray) -> None:
    """Two fleets: AA with a natural/smart split and V2G, BB fully optimised."""
    n.add("Carrier", "EV battery")
    n.add("Carrier", "V2G")
    hours = np.arange(N_SNAPSHOTS)

    # An evening-peaked uncontrolled charging shape, and a midday-heavy
    # optimised one that follows the solar profile — the shift the chart shows.
    evening = 60.0 + 240.0 * np.exp(-0.5 * ((hours - 19) / 2.5) ** 2)
    midday = 40.0 + 500.0 * solar_profile

    for loc, x in (("AA", 0.0), ("BB", 2.0)):
        bus = f"{loc} EV battery"
        n.add("Bus", bus, carrier="EV battery", location=loc, x=x, y=50.0)

        # The driving demand of the optimised fleet, on the fleet-battery bus.
        driving = 120.0 + 60.0 * np.sin(np.linspace(0, 2 * np.pi, N_SNAPSHOTS))
        n.add("Load", f"{loc} land transport EV", bus=bus, carrier="land transport EV")
        n.loads_t.p[f"{loc} land transport EV"] = driving
        n.loads_t.p_set[f"{loc} land transport EV"] = driving

        # Carrier spelling differs between the forks; detection is by topology.
        charger = f"{loc} BEV charger" if loc == "AA" else f"{loc} EV charger"
        n.add(
            "Link",
            charger,
            bus0=loc,
            bus1=bus,
            carrier="BEV charger" if loc == "AA" else "EV charger",
            p_nom=800.0,
            p_nom_opt=800.0,
            efficiency=0.9,
        )
        n.links_t.p0[charger] = midday
        n.links_t.p1[charger] = -midday * 0.9

        n.add(
            "Store",
            bus,
            bus=bus,
            carrier="EV battery",
            e_nom=4000.0,
            e_nom_opt=4000.0,
            e_cyclic=True,
        )
        soc = 2000.0 + 800.0 * np.cos(np.linspace(0, 2 * np.pi, N_SNAPSHOTS))
        n.stores_t.e[bus] = soc
        n.stores_t.p[bus] = np.gradient(-soc)

    # AA only: the share of demand pinned to the observed natural-charging
    # profile, on the AC bus, plus a V2G link back to it.
    n.add(
        "Load",
        "AA land transport EV inflexible",
        bus="AA",
        carrier="land transport EV inflexible",
    )
    n.loads_t.p["AA land transport EV inflexible"] = evening
    n.loads_t.p_set["AA land transport EV inflexible"] = evening

    n.add(
        "Link",
        "AA V2G",
        bus0="AA EV battery",
        bus1="AA",
        carrier="V2G",
        p_nom=400.0,
        p_nom_opt=400.0,
        efficiency=0.9,
    )
    v2g = 150.0 * np.exp(-0.5 * ((hours - 19) / 1.5) ** 2)
    n.links_t.p0["AA V2G"] = v2g
    n.links_t.p1["AA V2G"] = -v2g * 0.9


def main() -> None:
    n = build()
    if OUT.exists():
        OUT.unlink()
    n.export_to_netcdf(OUT)
    size_kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT} ({size_kb:.1f} kB, {len(n.snapshots)} snapshots)")


if __name__ == "__main__":
    main()
