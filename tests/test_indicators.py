"""Indicator algebra from vendored négaWatt flow tables."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest

from pypsa2html import indicators
from tests.conftest import CHARTDATA_GHG_SECTOR, CHARTDATA_GHG_SOURCE, CHARTDATA_ROOT

MATCH_TOLERANCE = 5e-3
BIOMASS_LABEL = "Biomass"


def _build(indicator_ctx, node, energy, carbon):
    return indicators.build(indicator_ctx, node, energy=energy, carbon=carbon)


def _reference_ghg(node: str, sheet: str) -> pd.DataFrame | None:
    path = CHARTDATA_ROOT / f"ChartData_{node}.xlsx"
    if not path.exists():
        return None
    frame = pd.read_excel(path, sheet_name=sheet, header=2, index_col=0)
    frame.index = pd.Index([int(y) for y in frame.index], name="Year")
    return frame


def _labelled(frame: pd.DataFrame, taxonomy) -> pd.DataFrame:
    code_to_label = taxonomy.nodes["Label"].to_dict()
    return frame.rename(columns=code_to_label)


@pytest.mark.parametrize(
    ("node", "energy_fixture", "carbon_fixture"),
    [
        ("BE", "energy_flows_be", "carbon_flows_be"),
        ("EU", "energy_flows_eu", "carbon_flows_eu"),
    ],
)
def test_build_returns_non_empty_frames_with_year_index(
    indicator_ctx, node, energy_fixture, carbon_fixture, request
):
    energy = request.getfixturevalue(energy_fixture)
    carbon = request.getfixturevalue(carbon_fixture)
    result = _build(indicator_ctx, node, energy, carbon)

    assert list(result.flows.index) == [2020, 2030, 2040, 2050]
    assert not result.ghg_sector.empty
    assert not result.ghg_source.empty
    assert result.ghg_sector.index.equals(result.ghg_source.index)


@pytest.mark.parametrize(
    ("node", "energy_fixture", "carbon_fixture"),
    [
        ("BE", "energy_flows_be", "carbon_flows_be"),
        ("EU", "energy_flows_eu", "carbon_flows_eu"),
    ],
)
def test_ghg_frames_match_chartdata_except_biomass(
    indicator_ctx,
    node,
    energy_fixture,
    carbon_fixture,
    chartdata_available,
    request,
):
    if not chartdata_available:
        pytest.skip("ChartData reference workbooks not on disk")

    energy = request.getfixturevalue(energy_fixture)
    carbon = request.getfixturevalue(carbon_fixture)
    result = _build(indicator_ctx, node, energy, carbon)

    for sheet, got in (
        (CHARTDATA_GHG_SECTOR, _labelled(result.ghg_sector, indicator_ctx.taxonomy)),
        (CHARTDATA_GHG_SOURCE, _labelled(result.ghg_source, indicator_ctx.taxonomy)),
    ):
        reference = _reference_ghg(node, sheet)
        assert reference is not None
        assert len(reference.columns) == len(got.columns)
        for column in reference.columns:
            assert column in got.columns, f"missing column {column!r} in {sheet}"
            if column == BIOMASS_LABEL:
                assert (reference[column] - got[column]).abs().max() > MATCH_TOLERANCE
                continue
            pd.testing.assert_series_equal(
                got[column],
                reference[column].astype(float),
                check_names=False,
                check_dtype=False,
                atol=MATCH_TOLERANCE,
                rtol=0,
            )


def test_cumulative_emissions_use_horizon_weights(indicator_ctx, energy_flows_be, carbon_flows_be):
    """SEPIA C2: annual series stay annual; no 'years in decade × 10' hack."""
    result = _build(indicator_ctx, "BE", energy_flows_be, carbon_flows_be)
    weights = indicator_ctx.horizon_weights().reindex(result.ghg_sector.index).to_numpy()
    expected = result.ghg_sector.mul(weights, axis=0).cumsum()
    pd.testing.assert_frame_equal(result.ghg_sector_cum, expected)


def test_ghg_sector_cum_does_not_mutate_ghg_sector(indicator_ctx, energy_flows_be, carbon_flows_be):
    result = _build(indicator_ctx, "BE", energy_flows_be, carbon_flows_be)
    before = result.ghg_sector.copy(deep=True)
    _ = result.ghg_sector_cum
    pd.testing.assert_frame_equal(result.ghg_sector, before)


def test_flows_from_long_sums_duplicate_codes(year_columns):
    table = pd.DataFrame(
        {
            "code": ["proelcnuc", "proelcnuc", "proelccms"],
            "label": ["A", "B", "C"],
            "unit": ["TWh"] * 3,
            "2020": [1.0, 2.0, 3.0],
            "2030": [4.0, 5.0, 6.0],
        }
    )
    wide = indicators.flows_from_long(table, year_columns)
    assert wide.loc[2020, "proelcnuc"] == pytest.approx(3.0)
    assert wide.loc[2030, "proelcnuc"] == pytest.approx(9.0)


def test_year_weights_cover_base_year_outside_horizons(indicator_ctx):
    weights = indicators.year_weights(indicator_ctx, [2019, 2020, 2030, 2040, 2050])
    assert list(weights.index) == [2019, 2020, 2030, 2040, 2050]
    assert weights.loc[2019] == pytest.approx(1.0)
    assert weights.loc[2050] == pytest.approx(10.0)


def test_build_does_not_mutate_input_frames(indicator_ctx, energy_flows_be, carbon_flows_be):
    energy = energy_flows_be.copy(deep=True)
    carbon = carbon_flows_be.copy(deep=True)
    before_energy = deepcopy(energy)
    before_carbon = deepcopy(carbon)
    _build(indicator_ctx, "BE", energy, carbon)
    pd.testing.assert_frame_equal(energy, before_energy)
    pd.testing.assert_frame_equal(carbon, before_carbon)


def test_renewable_share_denominator_is_not_the_numerator_list(
    indicator_ctx, energy_flows_be, carbon_flows_be
):
    """FIX 2: enc_fe must not be 100% when fossil sources feed electricity."""
    result = _build(indicator_ctx, "BE", energy_flows_be, carbon_flows_be)
    share = result.ren_cov_ratio.loc[2030, "elc_fe"]
    assert 50.0 < share < 100.0


def test_ammonia_synthesis_losses_use_amm_fe_target(
    indicator_ctx, energy_flows_eu, carbon_flows_eu
):
    """FIX 1: aggregate closure writes each carrier's electricity to its own per row."""
    from pypsa2html.indicators import column

    result = _build(indicator_ctx, "EU", energy_flows_eu, carbon_flows_eu)
    for carrier in ("amm", "met"):
        code = f"{carrier}_fe"
        electricity = column(result.flows, ("elc_se", code, ""))
        losses = column(result.flows, (code, "per", ""))
        pd.testing.assert_series_equal(losses, electricity, check_names=False)


def test_base_year_ghg_sector_is_populated(indicator_ctx, energy_flows_be, carbon_flows_be):
    """FIX 5: vectorised carbon closure gives the calibration year a value."""
    result = _build(indicator_ctx, "BE", energy_flows_be, carbon_flows_be)
    assert result.ghg_sector.loc[2020].notna().all()
    assert result.ghg_sector.loc[2020].abs().sum() > 0


def test_region_value_does_not_treat_bewal_as_be():
    """No prefix matching: BEWAL is not a clustered form of BE."""
    series = pd.Series({"BEWAL": 1.0, "BE1 0": 2.0, "DE": 3.0})
    assert indicators._region_value(series, "BE") == pytest.approx(2.0)
    assert indicators._region_value(series, "BEWAL") == pytest.approx(1.0)
    assert pd.isna(indicators._region_value(series, "FR"))


def test_sufficiency_ratio_exceeds_100_for_net_exporter():
    """The legacy coverage ratios clipped at 100; exporters must stay visible."""
    domestic = pd.Series([80.0, 120.0], index=[2030, 2050])
    net_import = pd.Series([20.0, -30.0], index=[2030, 2050])
    ratio = indicators._sufficiency_ratio(domestic, net_import)
    assert ratio.loc[2030] == pytest.approx(80.0)
    assert ratio.loc[2050] == pytest.approx(120.0 / 90.0 * 100.0)
    empty = indicators._sufficiency_ratio(
        pd.Series([0.0], index=[2030]), pd.Series([0.0], index=[2030])
    )
    assert pd.isna(empty.loc[2030])


def test_sum_long_tables_adds_year_columns(energy_flows_be):
    doubled = indicators._sum_long_tables([energy_flows_be, energy_flows_be])
    years = [c for c in energy_flows_be.columns if c not in ("code", "label", "unit")]
    sample = energy_flows_be["code"].iloc[0]
    original = energy_flows_be.set_index("code").loc[sample, years].astype(float)
    summed = doubled.set_index("code").loc[sample, years].astype(float)
    pd.testing.assert_series_equal(summed, original * 2, check_names=False)


def test_self_sufficiency_from_negawatt_flows(
    indicator_ctx, energy_flows_be, carbon_flows_be, energy_flows_eu, carbon_flows_eu
):
    be = _build(indicator_ctx, "BE", energy_flows_be, carbon_flows_be)
    eu = _build(indicator_ctx, "EU", energy_flows_eu, carbon_flows_eu)

    be_ss = be.self_sufficiency.ratio
    assert list(be_ss.columns) == ["primary", "electricity"]
    assert be_ss.notna().any().all()
    # Belgium is a net energy importer in the reference run.
    assert (be_ss["primary"] < 100).all()
    assert (be_ss["electricity"] > 0).all()
    # Not the legacy "mean of clipped per-carrier coverage".
    assert not be_ss["primary"].equals(be.cov_ratio.mean(axis=1))

    eu_ss = eu.self_sufficiency.ratio
    # Study-wide: no electricity trade with the world outside the model.
    assert (eu_ss["electricity"] - 100).abs().max() < 1e-6
    # Fossil and uranium imports still make primary energy < 100 %.
    assert (eu_ss["primary"] < 100).all()


def test_nuclear_primary_electricity_books_kwh_as_domestic(
    indicator_ctx, energy_flows_be, carbon_flows_be
):
    """Item 6b: uranium-as-import vs electricity-produced, no offshore share."""
    fuel = _build(indicator_ctx, "BE", energy_flows_be, carbon_flows_be)
    ctx = deepcopy(indicator_ctx)
    ctx.config = type(
        "Config",
        (),
        {
            "project": indicator_ctx.config.project,
            "model": indicator_ctx.config.model,
            "output": indicator_ctx.config.output,
            "raw": {},
            "features": type("Features", (), {"nuclear_primary": "electricity"})(),
        },
    )()
    elec = _build(ctx, "BE", energy_flows_be, carbon_flows_be)
    pd.testing.assert_series_equal(
        fuel.self_sufficiency.ratio["electricity"],
        elec.self_sufficiency.ratio["electricity"],
    )
    delta = elec.self_sufficiency.ratio["primary"] - fuel.self_sufficiency.ratio["primary"]
    assert (delta.fillna(0) >= -1e-9).all()
    assert (delta.fillna(0) > 0).any()


def test_nuclear_primary_rejects_unknown_mode(indicator_ctx):
    ctx = deepcopy(indicator_ctx)
    ctx.config = type(
        "Config",
        (),
        {"features": type("Features", (), {"nuclear_primary": "offshore"})(), "raw": {}},
    )()
    with pytest.raises(ValueError, match="nuclear_primary"):
        indicators.nuclear_primary_mode(ctx)


def test_self_sufficiency_members_matches_node(indicator_ctx, energy_flows_be, carbon_flows_be):
    result = _build(indicator_ctx, "BE", energy_flows_be, carbon_flows_be)
    indicator_ctx._files[("indicators", "BE")] = result
    by_node = indicators.self_sufficiency(indicator_ctx, node="BE")
    by_members = indicators.self_sufficiency(indicator_ctx, members=["BE"])
    pd.testing.assert_frame_equal(by_node, by_members)


def test_self_sufficiency_rejects_neither_or_both(indicator_ctx):
    with pytest.raises(ValueError, match="exactly one"):
        indicators.self_sufficiency(indicator_ctx)
    with pytest.raises(ValueError, match="exactly one"):
        indicators.self_sufficiency(indicator_ctx, node="BE", members=["BE"])


def test_self_sufficiency_members_sums_cached_flows(
    indicator_ctx, energy_flows_be, carbon_flows_be
):
    """An ad-hoc group uses cached member tables and does not mutate NodeSet."""
    from copy import copy

    from pypsa2html.nodes import build_node_set

    ctx = copy(indicator_ctx)
    ctx._files = {}
    ctx.nodes = build_node_set(
        [],
        include=["BEVLG", "BEWAL", "BEBRU"],
        labels={"BEVLG": "Flanders", "BEWAL": "Wallonia", "BEBRU": "Brussels"},
        focus="BEWAL",
        aggregate_code="ALL",
        aggregate_label="All",
    )
    for loc in ("BEVLG", "BEWAL", "BEBRU"):
        ctx._files[("energy_flows", loc)] = energy_flows_be
        ctx._files[("carbon_flows", loc)] = carbon_flows_be

    by_node = indicators.self_sufficiency(ctx, node="BEWAL")
    by_one_member = indicators.self_sufficiency(ctx, members=["BEWAL"])
    pd.testing.assert_frame_equal(by_node, by_one_member)

    two = indicators.self_sufficiency(ctx, members=["BEWAL", "BEVLG"])
    assert two is not None
    assert list(two.columns) == ["primary", "electricity"]
    assert two.notna().any().all()
    assert "BEVLG+BEWAL" not in ctx.nodes.codes


# ---------------------------------------------------------------------------
# Energy-Sankey node balance (BEV / natural charging)
# ---------------------------------------------------------------------------


def _energy_table(year_columns, values: dict[str, float]) -> pd.DataFrame:
    """One-horizon-repeated long table so ``build`` has a year index."""
    return pd.DataFrame(
        [
            {"code": code, "label": code, "unit": "TWh", **dict.fromkeys(year_columns, v)}
            for code, v in values.items()
        ]
    )


def _carbon_zeros(year_columns) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "emmresbmatm",
                "label": "unused",
                "unit": "MtCO2",
                **dict.fromkeys(year_columns, 0.0),
            }
        ]
    )


def test_bev_sankey_node_unbalanced_without_natural_charging(
    indicator_ctx, year_columns
):
    """The pypsa-wal split books both EV loads as ``bev_fe`` demand.

    Only the charger Link feeds ``bev_se``.  Without copying the inflexible
    load in as Natural charging, the BEV node has more leaving than arriving
    — the hole that showed up on the 2026-08-18 Walloon Sankey.
    """
    energy = _energy_table(
        year_columns,
        {"prebev": 1.0, "preselccftra": 5.0},
    )
    result = _build(indicator_ctx, "BE", energy, _carbon_zeros(year_columns))
    imb = indicators.graph_imbalances(result.flows, indicator_ctx.taxonomy, atol=0.01, rtol=0.0)
    bev = imb[imb["node"] == "bev_se"]
    assert not bev.empty
    assert (bev["gap"] > 3.0).all()  # ~4 TWh of missing natural charging


def test_bev_sankey_node_balances_with_natural_charging(indicator_ctx, year_columns):
    energy = _energy_table(
        year_columns,
        {"prebev": 1.0, "preselccftra": 5.0, "prenatbev": 4.0},
    )
    result = _build(indicator_ctx, "BE", energy, _carbon_zeros(year_columns))
    imb = indicators.graph_imbalances(result.flows, indicator_ctx.taxonomy, atol=0.01, rtol=0.0)
    assert imb[imb["node"] == "bev_se"].empty
    # Two distinct charging edges, so the Sankey can label them separately.
    cols = result.flows.columns
    assert ("elc_se", "bev_se", "") in cols
    assert ("elc_se", "bev_se", "nat") in cols
    smart = result.flows[("elc_se", "bev_se", "")].iloc[0]
    natural = result.flows[("elc_se", "bev_se", "nat")].iloc[0]
    assert smart == pytest.approx(1.0)
    assert natural == pytest.approx(4.0)


def test_ambient_heat_node_balances_when_agriculture_is_split_out(
    indicator_ctx, year_columns
):
    """``pac_fe`` conserves energy even though agriculture is booked separately.

    Agriculture heat is re-bused onto the rural heat load, so the rural
    heat-pump edge and ``pac_fe -> agr`` describe the same ambient twice and the
    agricultural part comes off the residential edge.  That correction used to
    run *after* the ``pac_pe -> pac_fe`` closure had already totalled the
    outflow, which left the node short by exactly the agricultural heat —
    0.147 TWh on BEWAL, in every horizon of the 2026-09-06 run.
    """
    energy = _energy_table(
        year_columns,
        {"prespaccfta": 4.0, "prespaccfftt": 2.0, "presvapcfagr": 0.5},
    )
    result = _build(indicator_ctx, "BE", energy, _carbon_zeros(year_columns))
    imb = indicators.graph_imbalances(
        result.flows, indicator_ctx.taxonomy, atol=0.001, rtol=0.0
    )
    assert imb[imb["node"] == "pac_fe"].empty
    # The correction still happened: 4.0 of rural ambient minus 0.5 agricultural.
    assert result.flows[("pac_fe", "res", "gr")].iloc[0] == pytest.approx(3.5)
    assert result.flows[("pac_pe", "pac_fe", "")].iloc[0] == pytest.approx(6.0)
