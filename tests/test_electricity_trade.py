"""The energy Sankey draws measured electricity trade, not a node residual.

`_close_energy_graph` used to derive ``imp -> elc_se`` and ``elc_se -> exp``
from the annual net balance of the electricity node. That was wrong twice:

* a region both imports and exports within a year, and an annual net can only
  ever show one of the two — the other arrow was always zero;
* the residual is whatever *closes* the node, so it silently absorbed every
  mis-attribution elsewhere on it. On BEWAL 2050 of the 2026-09-05 run it drew
  **17.2 TWh** of imports and no exports, against a physical 10.0 in / 2.0 out
  — and the model was running a 10 TWh import cap at the time, so the report
  contradicted a constraint the solver had satisfied exactly.

Trade is now read from the network's own cross-border branches. See D20.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from pypsa2html import indicators
from pypsa2html.nodes import NodeResolver

pypsa = pytest.importorskip("pypsa")


def _two_node_network(flows_mw: list[float]) -> "pypsa.Network":
    """One AC line between WAL and VLG; ``flows_mw`` is p0, + = WAL -> VLG."""
    n = pypsa.Network()
    n.set_snapshots(range(len(flows_mw)))
    n.snapshot_weightings["generators"] = 1000.0  # 1 GWh per MW per snapshot
    n.add("Carrier", "AC")
    n.add("Bus", "WAL", carrier="AC", location="WAL")
    n.add("Bus", "VLG", carrier="AC", location="VLG")
    n.add("Line", "x", bus0="WAL", bus1="VLG", x=0.1, r=0.01, s_nom=1e4)
    n.lines_t.p0 = pd.DataFrame({"x": flows_mw}, index=n.snapshots)
    n.lines_t.p1 = -n.lines_t.p0
    return n


def _ctx(network) -> SimpleNamespace:
    resolver = NodeResolver(network, "location")
    return SimpleNamespace(
        networks={2050: network},
        horizons=[2050],
        year_columns=["2050"],
        resolver=lambda horizon: resolver,
        locations_for=lambda node: [node],
        is_aggregate=lambda node: False,
        _files={},
        config=SimpleNamespace(raw={}, model=SimpleNamespace(base_year=None)),
    )


def test_import_and_export_are_both_reported():
    """A node that exports 3 GWh and imports 4 GWh must show both."""
    # + = WAL -> VLG (export), - = VLG -> WAL (import)
    n = _two_node_network([2.0, 1.0, -4.0])
    years = pd.Index([2050], name="Year")
    measured = indicators._measured_electricity_trade(_ctx(n), "WAL", years)
    assert measured is not None
    imports, exports = measured
    # 4 MW x 1000 h = 4 GWh = 0.004 TWh in; (2+1) x 1000 = 0.003 TWh out
    assert imports.loc[2050] == pytest.approx(0.004)
    assert exports.loc[2050] == pytest.approx(0.003)
    # The annual net is an export of 0.001 TWh: a residual would have shown
    # zero imports and hidden the 0.004 entirely.
    assert imports.loc[2050] > 0 and exports.loc[2050] > 0


def test_sign_convention_follows_the_side_that_touches_the_node():
    """Same line read from the other end gives the mirror image."""
    n = _two_node_network([2.0, 1.0, -4.0])
    years = pd.Index([2050], name="Year")
    wal_in, wal_out = indicators._measured_electricity_trade(_ctx(n), "WAL", years)
    vlg_in, vlg_out = indicators._measured_electricity_trade(_ctx(n), "VLG", years)
    assert wal_in.loc[2050] == pytest.approx(vlg_out.loc[2050])
    assert wal_out.loc[2050] == pytest.approx(vlg_in.loc[2050])


def test_no_networks_keeps_the_residual_path(indicator_ctx):
    """The négaWatt reference tables have no networks; nothing may change."""
    years = pd.Index([2020, 2030, 2040, 2050], name="Year")
    assert not hasattr(indicator_ctx, "networks")
    assert indicators._measured_electricity_trade(indicator_ctx, "BE", years) is None


def test_a_broken_network_does_not_break_the_report():
    """Any failure falls back to the residual rather than killing the build."""
    years = pd.Index([2050], name="Year")
    ctx = _ctx(_two_node_network([1.0]))
    ctx.resolver = lambda horizon: (_ for _ in ()).throw(RuntimeError("boom"))
    assert indicators._measured_electricity_trade(ctx, "WAL", years) is None
