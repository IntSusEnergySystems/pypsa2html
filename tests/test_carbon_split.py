"""Carbon Sankey: one label per edge, and the regional-cap split of the net."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pypsa
import pytest

from pypsa2html.extract.emissions import capped_emissions, capped_emissions_enabled

DATA = Path(__file__).resolve().parents[1] / "src" / "pypsa2html" / "data"


def test_every_carbon_edge_carries_one_technology():
    """The Sankey labels a merged edge with its first row's label.

    Before 2026-10-01 ``gas_ghg -> stm`` summed industrial gas CC and gas CHP CC
    under "CCGT with carbon capture", so a node with no CCGT CC at all showed
    1.15 Mt of it.  Merging is only allowed between rows with the same label.
    """
    table = pd.read_csv(DATA / "processes_carbon.csv", keep_default_na=False)
    labels = table.groupby(["Source", "Target", "Type"])["Label"].agg(
        lambda s: sorted({x.strip() for x in s})
    )
    merged = labels[labels.map(len) > 1]
    assert merged.empty, merged.to_dict()


def test_split_edges_exist_and_their_codes_are_produced():
    processes = pd.read_csv(DATA / "processes_carbon.csv", keep_default_na=False)
    codes = set(pd.read_csv(DATA / "carrier_flows_carbon.csv")["code"])
    nodes = set(pd.read_csv(DATA / "nodes.csv")["Code"])
    for target, code in (("netcap_ghg", "emmnetcap"), ("netoth_ghg", "emmnetoth")):
        assert ((processes.Source == "atm") & (processes.Target == target)
                & (processes.Value_Code == code)).sum() == 1
        assert code in codes and target in nodes


def _network():
    n = pypsa.Network()
    n.set_snapshots([0])
    n.snapshot_weightings.loc[:, :] = 1e6  # one snapshot = 1 Mt per t/h
    n.add("Carrier", ["co2", "gas", "oil", "AC"])
    n.add("Bus", "co2 atmosphere", carrier="co2", location="EU")
    n.add("Bus", "co2 stored A", carrier="co2 stored", location="A")
    for node in ("A", "B"):
        n.add("Bus", f"{node} gas", carrier="gas", location=node)
        n.add("Bus", f"{node}", carrier="AC", location=node)
    n.add("Bus", "EU oil", carrier="oil", location="EU")
    # A plant at A emitting 1, a CC plant at A emitting 0.1 and storing 0.9,
    # aviation at A (booked to bus1 = A) emitting 2, a plant at B emitting 5.
    n.add("Link", "A plant", bus0="A gas", bus1="A", bus2="co2 atmosphere", carrier="CCGT")
    n.add("Link", "A plant CC", bus0="A gas", bus1="A", bus2="co2 atmosphere",
          bus3="co2 stored A", carrier="CCGT CC")
    n.add("Link", "A kerosene", bus0="EU oil", bus1="A", bus2="co2 atmosphere",
          carrier="kerosene for aviation")
    n.add("Link", "B plant", bus0="B gas", bus1="B", bus2="co2 atmosphere", carrier="CCGT")
    # p_k is a withdrawal from bus k: delivering x into the atmosphere is -x.
    n.links_t.p2 = pd.DataFrame(
        {"A plant": [-1.0], "A plant CC": [-0.1], "A kerosene": [-2.0], "B plant": [-5.0]},
        index=n.snapshots,
    )
    n.links_t.p3 = pd.DataFrame({"A plant CC": [-0.9]}, index=n.snapshots)
    n.links_t.p0 = pd.DataFrame(0.0, index=n.snapshots, columns=n.links.index)
    n.links_t.p1 = pd.DataFrame(0.0, index=n.snapshots, columns=n.links.index)
    return n


def _ctx(features, locations):
    return SimpleNamespace(
        networks={2050: _network()},
        _files={},
        config=SimpleNamespace(raw={"features": features}),
        locations_for=lambda node: locations,
    )


def test_split_is_off_by_default():
    ctx = _ctx({}, ["A"])
    assert not capped_emissions_enabled(ctx)
    assert capped_emissions(ctx, "A", 2050) is None


def test_capped_part_excludes_aviation_and_other_nodes():
    features = {"capped_emissions": {"enable": True,
                                     "exclude_patterns": ["kerosene for aviation"]}}
    assert capped_emissions(_ctx(features, ["A"]), "A", 2050) == pytest.approx(1.1)
    # Without the exclusion aviation is part of A's capped total.
    features["capped_emissions"]["exclude_patterns"] = []
    assert capped_emissions(_ctx(features, ["A"]), "A", 2050) == pytest.approx(3.1)
    # An aggregate over both nodes.
    assert capped_emissions(_ctx(features, ["A", "B"]), "AB", 2050) == pytest.approx(8.1)


def test_uncovered_atmosphere_ports_still_reach_the_net():
    """A carrier with no row of its own is counted, not dropped (oil plants were)."""
    from pypsa2html.extract.emissions import (
        CARBON_FLOWS,
        _discover_atmosphere_flows,
        _discover_capture_flows,
    )

    n = _network()
    n.add("Link", "A mystery", bus0="A gas", bus1="A", bus2="co2 atmosphere", carrier="mystery")
    # Same order as _balance_for_horizon: capture discovery first.
    found = _discover_atmosphere_flows(n, CARBON_FLOWS + _discover_capture_flows(n))
    assert [(f.label, f.port, f.target) for f in found] == [("other: mystery", 2, "co2 atmosphere")]
    # Covered carriers are not discovered twice.
    assert not [f for f in found if f.carriers == ("CCGT",)]
