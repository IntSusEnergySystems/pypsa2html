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
        is_study_wide=lambda node: False,
        locations_for=lambda node: [node],
        members_of=lambda node: None,
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


# -- key-figure extractors on a real network -----------------------------------

def _kf_ctx(network):
    from pypsa2html.nodes import build_node_set

    resolver = NodeResolver(network, "location")
    nodes = build_node_set(["AA", "BB"], focus="AA", aggregate_code="ALL")
    return SimpleNamespace(
        horizons=[2030],
        networks={2030: network},
        resolver=lambda horizon: resolver,
        nodes=nodes,
        locations_for=nodes.locations_for,
        config=SimpleNamespace(features=SimpleNamespace(price_abs_cap=1e4)),
        _files={},
    )


def test_annual_balance_by_raw_carrier(mini_network):
    import numpy as np

    from pypsa2html.extract.balance import annual_balance

    tables = annual_balance(_kf_ctx(mini_network), "electricity")
    aa = tables["AA"]["2030"]
    profile = mini_network.generators_t.p_max_pu["AA solar"]
    assert aa["supply:CCGT"] == pytest.approx(200.0 * 24)
    assert aa["supply:solar"] == pytest.approx(float((profile * 600).sum()))
    assert aa["withdrawal:DC"] == pytest.approx(150.0 * 24)
    assert aa["curtailment:solar"] == pytest.approx(float((profile * 200).sum()))
    assert aa["potential:solar"] == pytest.approx(float((profile * 800).sum()))
    assert tables["BB"]["2030"]["supply:DC"] == pytest.approx(142.5 * 24)
    # Study-wide: both ends of the DC link fall inside, only its losses remain.
    assert tables["ALL"]["2030"]["withdrawal:DC"] == pytest.approx(7.5 * 24)
    assert np.isfinite(tables["ALL"]["2030"]).all()


def test_price_stats_per_node_and_load_weighted_for_groups(mini_network):
    import numpy as np

    from pypsa2html.extract.prices import price_stats

    n = mini_network.copy()
    prices = pd.DataFrame(index=n.snapshots, columns=n.buses.index, dtype=float)
    prices["AA"] = 50.0
    prices["BB"] = 100.0
    prices.loc[n.snapshots[:3], "AA"] = 250.0
    prices["AA gas"] = 1e6        # an empty-bus dual, dropped by the cap
    n.buses_t["marginal_price"] = prices.fillna(0.0)
    stats = price_stats(_kf_ctx(n), "electricity")
    aa = stats["AA"]["2030"]
    assert aa["mean"] == pytest.approx((21 * 50 + 3 * 250) / 24)
    assert aa["hours_above_200"] == 3.0
    assert stats["BB"]["2030"]["mean"] == pytest.approx(100.0)
    both = stats["ALL"]["2030"]["mean"]
    assert stats["AA"]["2030"]["mean"] < both < 100.0
    assert np.isfinite(both)


def test_global_table_reads_objective_and_duals(mini_network):
    from pypsa2html.metrics import global_table

    gc = pd.DataFrame({"mu": [-120.0, 3.0]}, index=["CO2Limit", "biomass limit"])
    proxy = SimpleNamespace(objective=1.5e9, global_constraints=gc)
    ctx = _kf_ctx(mini_network)
    ctx.networks = {2030: proxy}
    assert global_table(ctx, "objective").loc["objective", "2030"] == 1.5e9
    duals = global_table(ctx, "constraints")
    assert duals.loc["CO2Limit", "2030"] == -120.0
