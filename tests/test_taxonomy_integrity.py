"""Cross-table integrity: do the Sankey edges resolve against real output?

These use the vendored négaWatt run as ground truth for "codes a real model
actually produces". They pin known-bad counts rather than asserting zero,
because the underlying defects are inherited from the legacy config workbook and
fixing them is a domain decision -- see docs/VALIDATION.md.
"""

from __future__ import annotations

import pathlib
import re

import pandas as pd
import pytest

from pypsa2html.datafiles import load_taxonomy

DATA = pathlib.Path(__file__).parent / "data" / "negawatt-ref"

#: Codes that are clearly placeholder text rather than a real indicator.
PLACEHOLDER = re.compile(r"^(?:[a-z]{1,3}|[fzeadqsc]{2,8}|lll+|dfdfg|dfsfsfs|gegerg|iuygg)$")


@pytest.fixture(scope="module")
def tax():
    return load_taxonomy()


@pytest.fixture(scope="module")
def produced_codes(tax) -> set[str]:
    """Every code the vendored négaWatt run actually emitted."""
    codes: set[str] = set()
    for node in ("BE", "EU"):
        for kind in ("energy", "carbon"):
            codes |= set(pd.read_csv(DATA / "flows" / f"{node}_{kind}.csv")["code"])
    codes |= set(tax.indicators["Value_Code"].dropna())
    codes |= set(tax.indicators.index)
    return codes


@pytest.fixture(scope="module")
def producible_codes(tax) -> set[str]:
    """Codes the extraction layer *can* emit, for any model.

    Distinct from `produced_codes`: a technology absent from one particular
    run legitimately produces no row, but a code no mapping can ever emit is
    a dead Sankey edge.
    """
    return (
        set(tax.carrier_flows_energy["code"])
        | set(tax.carrier_flows_carbon["code"])
        | set(tax.indicators["Value_Code"].dropna())
        | set(tax.indicators.index)
    )


def test_carbon_sankey_dead_edges_do_not_increase(tax, producible_codes):
    """17 carbon-Sankey edges reference a code no mapping can produce.

    Inherited from the legacy config workbook. Unlike the energy-Sankey placeholders
    these are plausibly-named (`emmcoalchp`, `emmrail`, ...), so they look like
    features that were specified but never implemented rather than typos.
    """
    referenced = set(tax.processes_carbon["Value_Code"].dropna())
    dead = sorted(referenced - producible_codes)
    assert len(dead) <= 17, f"new dead carbon Sankey edge(s): {dead}"


def test_codes_added_by_the_pypsa_wal_fork_are_producible(tax, producible_codes):
    """The packaged tables must be the union of both forks, not just négaWatt.

    processes_carbon references `emindmetatm` (industry methanol) and
    `emoilppatm` (oil plants), which only the pypsa-wal fork's mapping emits.
    """
    for code in ("emindmetatm", "emoilppatm"):
        assert code in producible_codes


def test_both_carrier_spellings_of_the_ev_charger_are_mapped(tax):
    """négaWatt calls it `EV charger`, pypsa-wal `BEV charger`."""
    entries = dict(
        zip(
            tax.carrier_flows_energy["entry"],
            tax.carrier_flows_energy["code"],
            strict=True,
        )
    )
    assert entries["EV charger"] == entries["BEV charger"] == "prebev"
    assert entries["EV charger_2"] == entries["BEV charger_2"] == "prebevloss"


def test_energy_sankey_placeholder_codes_do_not_increase(tax, produced_codes):
    """~28 energy-Sankey edges reference keyboard-mash placeholder codes.

    Inherited from the legacy config workbook: those links have never rendered in any
    run. Not removed here because deleting a Sankey edge is a modelling
    decision. This test stops the count growing.
    """
    referenced = set(tax.processes_energy["Value_Code"].dropna())
    placeholders = {c for c in referenced - produced_codes if PLACEHOLDER.match(c)}
    assert len(placeholders) <= 28, (
        f"new placeholder Value_Code(s) introduced: {sorted(placeholders)}"
    )


def test_energy_sankey_unresolved_edges_do_not_increase(tax, produced_codes):
    """Total unresolved edges, including legitimately model-specific ones.

    A code such as `proght` (geothermal) is absent simply because the négaWatt
    model has no geothermal, which is fine. The placeholders above are not.
    """
    referenced = set(tax.processes_energy["Value_Code"].dropna())
    assert len(referenced - produced_codes) <= 199


def test_value_codes_reused_across_edges_are_known(tax):
    """A reused Value_Code makes two Sankey links read the same number."""
    vc = tax.processes_energy["Value_Code"].dropna()
    reused = sorted(set(vc[vc.duplicated(keep=False)]))
    # prebatelcdloss / prebatloss are intentional (battery losses on two arcs);
    # `lll` is a placeholder pasted into two unrelated loss edges.
    assert reused == ["lll", "prebatelcdloss", "prebatloss"]


def test_every_produced_carbon_code_is_consumed(tax):
    """Extraction must not compute a carbon flow nothing displays."""
    produced = set(tax.carrier_flows_carbon["code"])
    consumed = set(tax.processes_carbon["Value_Code"].dropna()) | set(
        tax.processes_ghg["Value_Code"].dropna()
    )
    assert produced - consumed == set()


def test_fixture_flow_tables_match_the_documented_contract(year_columns):
    """tests/data must keep the shape INTERNALS.md 3 promises."""
    for node in ("BE", "EU"):
        for kind, unit in (("energy", "TWh"), ("carbon", "MtCO2")):
            df = pd.read_csv(DATA / "flows" / f"{node}_{kind}.csv")
            assert list(df.columns) == ["code", "label", "unit"] + year_columns
            assert set(df["unit"]) == {unit}
            assert df[year_columns].dtypes.map(lambda d: d.kind == "f").all()
            assert df["code"].notna().all()
