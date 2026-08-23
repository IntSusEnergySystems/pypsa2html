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


def test_ghg_sector_cum_does_not_mutate_ghg_sector(
    indicator_ctx, energy_flows_be, carbon_flows_be
):
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
