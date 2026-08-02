"""Tests against the synthetic ``tests/data/mini.nc`` (no full model required)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from pypsa2html.charts.maps_pipes import _vendored_group_pipes
from pypsa2html.extract.balance import energy_balance
from pypsa2html.nodes import NodeResolver, detect_locations

MINI_NC = Path(__file__).parent / "data" / "mini.nc"


@pytest.fixture(scope="module")
def mini_network():
    pytest.importorskip("pypsa")
    if not MINI_NC.is_file():
        pytest.skip("tests/data/mini.nc missing — run make_mini_network.py")
    import pypsa

    return pypsa.Network(MINI_NC)


def test_mini_network_is_small():
    assert MINI_NC.is_file()
    assert MINI_NC.stat().st_size < 500_000


def test_detect_locations_on_mini(mini_network):
    locs = detect_locations(mini_network)
    assert "AA" in locs
    assert "BB" in locs
    assert "EU" not in locs  # pseudo / hub location filtered


def test_node_resolver_maps_buses(mini_network):
    resolver = NodeResolver(mini_network, "location")
    names = pd.Series(["AA", "BB H2"], index=["AA", "BB H2"])
    bus_nodes = resolver.bus_nodes(names)
    assert bus_nodes["AA"] == "AA"
    assert bus_nodes["BB H2"] == "BB"


def test_energy_balance_ac_on_mini(mini_network):
    resolver = NodeResolver(mini_network, "location")
    ctx = SimpleNamespace(
        networks={2030: mini_network},
        resolver=lambda horizon: resolver,
        is_aggregate=lambda node: False,
        config=SimpleNamespace(model=SimpleNamespace()),
        taxonomy=SimpleNamespace(),
        _files={},
    )
    balance = energy_balance(ctx, "AA", "AC", 2030)
    assert balance is not None
    assert not balance.empty
    assert len(balance) == len(mini_network.snapshots)


def test_group_pipes_on_mini_h2_links(mini_network):
    h2 = mini_network.links.carrier.str.contains("H2 pipeline", na=False)
    grouped = _vendored_group_pipes(mini_network.links.loc[h2])
    assert len(grouped) == 1
    assert grouped["p_nom_opt"].iloc[0] == pytest.approx(1500.0)
