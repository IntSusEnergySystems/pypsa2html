"""Metrics and KPIs: one way to read a number off a scenario.

Most tests use a fake ScenarioData -- a dict of tables -- because the point is
the reading rules (row selection, zero vs missing, file sources), not the
extraction, which has its own tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytest

from pypsa2html.metrics import (
    KPI,
    SYSTEM,
    Metric,
    evaluate_kpis,
    flows_to_table,
    load_kpis,
    metric_from,
    metric_series,
    parse_pypsa_summary,
    select_rows,
)


@dataclass
class FakeData:
    """A ScenarioData over in-memory tables and files."""

    tables: dict = field(default_factory=dict)       # (node, key) -> DataFrame
    files: dict = field(default_factory=dict)        # relpath -> DataFrame
    year_columns: list = field(default_factory=lambda: ["2030", "2050"])
    name: str = "demo"
    label: str = "Demo"
    meta: dict = field(default_factory=dict)
    locations: dict = field(default_factory=dict)    # node -> list | None
    stored: dict = field(default_factory=dict)       # (node, kpi id) -> Series

    def table(self, node, key):
        value = self.tables.get((node, key))
        return None if value is None else value.copy()

    def read_file(self, relpath, **kwargs):
        value = self.files.get(relpath)
        if value is None:
            return None
        if kwargs.get("header", "infer") is None:
            return pd.read_csv(pd.io.common.StringIO(value), header=None)
        return value.copy() if isinstance(value, pd.DataFrame) else pd.read_csv(
            pd.io.common.StringIO(value)
        )

    def locations_for(self, node):
        return self.locations.get(node, [node])

    def stored_kpi(self, node, kpi):
        return self.stored.get((node, kpi.id))


def _caps():
    return pd.DataFrame(
        {"2030": [1000.0, 500.0, 20.0], "2050": [3000.0, 1500.0, 0.0]},
        index=["nuclear", "solar PV (ground)", "solar thermal"],
    )


# -- the metric definition --------------------------------------------------

def test_table_is_an_alias_of_source():
    metric = Metric(table="capacity", kind="power", rows=["nuclear"])
    assert metric.source == "capacity"
    assert metric.table == "capacity"
    assert metric.table_key == "capacity.power"


def test_defaults_come_from_the_source():
    capacity = Metric(source="capacity")
    assert (capacity.unit, capacity.scale) == ("GW", 1e-3)
    balance = Metric(source="balance", rows=["supply:nuclear"])
    assert (balance.kind, balance.unit, balance.scale, balance.match) == (
        "electricity", "TWh/year", 1e-6, "glob"
    )
    price = Metric(source="price", rows=["mean"])
    assert price.missing == "none"


def test_unknown_source_and_kind_are_rejected():
    with pytest.raises(ValueError, match="metric source must be one of"):
        Metric(source="capacities")
    with pytest.raises(ValueError, match="kind for source 'capacity'"):
        Metric(source="capacity", kind="energy")
    with pytest.raises(ValueError, match="needs a 'field'"):
        Metric(source="indicator")
    with pytest.raises(ValueError, match="needs a 'file'"):
        Metric(source="csv")
    with pytest.raises(ValueError, match="numerator"):
        Metric(source="ratio", numerator="a")


def test_global_metrics_are_system_wide():
    assert Metric(source="global", kind="objective").is_system
    assert KPI(id="obj", source="global").scope == "system"
    assert KPI(id="cap", source="capacity").scope == "node"


def test_kpi_applies_to():
    node_kpi = KPI(id="x", source="capacity")
    assert node_kpi.applies_to("AA") and not node_kpi.applies_to(SYSTEM)
    pinned = KPI(id="y", source="capacity", nodes=["AA"])
    assert pinned.applies_to("AA") and not pinned.applies_to("BB")
    system = KPI(id="z", source="global")
    assert system.applies_to(SYSTEM) and not system.applies_to("AA")


def test_kpi_id_is_validated():
    with pytest.raises(ValueError, match="letters, digits or underscores"):
        KPI(id="bad id", source="capacity")


def test_digest_follows_the_definition_not_the_label():
    a = KPI(id="x", source="capacity", rows=["nuclear"], label="A")
    b = KPI(id="x", source="capacity", rows=["nuclear"], label="B")
    c = KPI(id="x", source="capacity", rows=["solar"], label="A")
    assert a.digest == b.digest != c.digest


# -- row selection ------------------------------------------------------------

def test_select_rows_exact_is_case_insensitive():
    out = select_rows(_caps(), ["NUCLEAR"])
    assert list(out.index) == ["nuclear"]


def test_select_rows_glob_and_exclude():
    out = select_rows(_caps(), ["solar*"], exclude=["solar thermal"], match="glob")
    assert list(out.index) == ["solar PV (ground)"]


def test_select_rows_empty_keeps_the_table():
    assert len(select_rows(_caps(), [])) == 3


# -- evaluation ----------------------------------------------------------------

def test_a_row_the_run_did_not_build_reads_zero():
    data = FakeData(tables={("AA", "capacity.power"): _caps()})
    series = metric_series(data, "AA", Metric(source="capacity", rows=["electrolysis"]))
    assert list(series) == [0.0, 0.0]


def test_a_missing_table_reads_missing():
    data = FakeData()
    assert metric_series(data, "AA", Metric(source="capacity", rows=["nuclear"])) is None


def test_a_missing_price_row_reads_missing_not_zero():
    prices = pd.DataFrame({"2030": [80.0], "2050": [90.0]}, index=["mean"])
    data = FakeData(tables={("AA", "price.electricity"): prices})
    assert metric_series(data, "AA", Metric(source="price", rows=["p95"])) is None
    series = metric_series(data, "AA", Metric(source="price", rows=["mean"]))
    assert list(series) == [80.0, 90.0]


def test_system_metrics_read_the_system_table():
    gc = pd.DataFrame({"2030": [-100.0, 5.0], "2050": [-600.0, 7.0]},
                      index=["CO2Limit", "other"])
    data = FakeData(tables={(SYSTEM, "global.constraints"): gc})
    metric = Metric(source="global", kind="constraints", rows=["CO2Limit"], scale=-1)
    series = metric_series(data, "AA", metric)   # node is ignored
    assert list(series) == [100.0, 600.0]


def test_a_year_absent_from_a_table_reads_zero_for_quantities():
    caps = _caps()[["2050"]]
    data = FakeData(tables={("AA", "capacity.power"): caps})
    series = metric_series(data, "AA", Metric(source="capacity", rows=["nuclear"]))
    assert series["2030"] == 0.0 and series["2050"] == pytest.approx(3.0)


def test_ratio_of_inline_metrics():
    balance = pd.DataFrame(
        {"2030": [1.0e6, 10.0e6], "2050": [2.0e6, 20.0e6]},
        index=["curtailment:solar", "potential:solar"],
    )
    data = FakeData(tables={("AA", "balance.electricity"): balance})
    metric = Metric(
        source="ratio",
        numerator={"source": "balance", "rows": ["curtailment:*"]},
        denominator={"source": "balance", "rows": ["potential:*"]},
    )
    assert list(metric_series(data, "AA", metric)) == pytest.approx([10.0, 10.0])


def test_ratio_of_kpi_ids_through_the_registry():
    caps = _caps()
    data = FakeData(tables={("AA", "capacity.power"): caps})
    kpis = load_kpis(
        [
            {"id": "nuc", "source": "capacity", "rows": ["nuclear"]},
            {"id": "all", "source": "capacity"},
            {"id": "share", "source": "ratio", "numerator": "nuc", "denominator": "all"},
        ],
        defaults=False,
    )
    values = evaluate_kpis(data, "AA", kpis)
    assert values.loc["share", "2050"] == pytest.approx(3000 / 4500 * 100)


def test_a_ratio_with_a_zero_denominator_is_missing():
    caps = pd.DataFrame({"2030": [0.0], "2050": [0.0]}, index=["nuclear"])
    data = FakeData(tables={("AA", "capacity.power"): caps})
    metric = Metric(source="ratio", numerator={"source": "capacity"},
                    denominator={"source": "capacity"})
    assert metric_series(data, "AA", metric).isna().all()


# -- project files ----------------------------------------------------------------

PTX = pd.DataFrame(
    {
        "run": ["r"] * 6,
        "year": [2030, 2050, 2030, 2050, 2050, 2050],
        "section": ["BEWAL CO2 (Mt/a)"] * 2 + ["PtX TWh in"] * 4,
        "metric": ["capture total", "capture total",
                   "Fischer-Tropsch @ BEWAL", "Fischer-Tropsch @ BEWAL",
                   "Fischer-Tropsch @ FR", "methanolisation @ BEWAL"],
        "value": [6.0, 8.4, 0.5, 2.0, 7.0, 1.5],
    }
)


def test_csv_long_with_node_substitution():
    data = FakeData(files={"csvs/ptx_report.csv": PTX})
    metric = Metric(source="csv", file="csvs/ptx_report.csv",
                    filters={"section": "{node} CO2 (Mt/a)", "metric": "capture total"})
    assert list(metric_series(data, "BEWAL", metric)) == [6.0, 8.4]


def test_csv_glob_filters_are_summed():
    data = FakeData(files={"csvs/ptx_report.csv": PTX})
    metric = Metric(source="csv", file="csvs/ptx_report.csv",
                    filters={"section": "PtX TWh in", "metric": "* @ {node}"})
    series = metric_series(data, "BEWAL", metric)
    assert series["2050"] == pytest.approx(3.5)


def test_csv_without_a_matching_row_is_missing_by_default():
    data = FakeData(files={"csvs/ptx_report.csv": PTX})
    metric = Metric(source="csv", file="csvs/ptx_report.csv",
                    filters={"section": "{node} CO2 (Mt/a)", "metric": "capture total"})
    assert metric_series(data, "DE", metric) is None
    zero = Metric(source="csv", file="csvs/ptx_report.csv", missing="zero",
                  filters={"section": "{node} CO2 (Mt/a)", "metric": "capture total"})
    assert list(metric_series(data, "DE", zero)) == [0.0, 0.0]


def test_csv_node_alone_expands_to_the_locations():
    table = pd.DataFrame({"location": ["AA", "BB", "CC"], "year": [2030] * 3,
                          "value": [1.0, 2.0, 4.0]})
    data = FakeData(files={"f.csv": table}, locations={"G": ["AA", "BB"], "ALL": None})
    metric = Metric(source="csv", file="f.csv", filters={"location": "{node}"})
    assert metric_series(data, "G", metric)["2030"] == 3.0
    assert metric_series(data, "ALL", metric)["2030"] == 7.0


def test_csv_absent_file_is_missing():
    metric = Metric(source="csv", file="csvs/none.csv")
    assert metric_series(FakeData(), "AA", metric) is None


METRICS_CSV = """cluster,adm,adm
opt,,
planning_horizon,2030,2050
co2_shadow,-97.9,-660.7
total costs,5.5e11,5.2e11
"""

COSTS_CSV = """cluster,,,adm,adm
opt,,,,
planning_horizon,,,2030,2050
cost,component,carrier,,
capital,Generator,solar,10.0,20.0
marginal,Generator,solar,1.0,2.0
"""


def test_parse_pypsa_summary_single_index():
    table = parse_pypsa_summary(pd.read_csv(pd.io.common.StringIO(METRICS_CSV), header=None))
    assert list(table.columns) == ["2030", "2050"]
    assert table.loc["co2_shadow", "2050"] == pytest.approx(-660.7)


def test_parse_pypsa_summary_multi_index_joins_labels():
    table = parse_pypsa_summary(pd.read_csv(pd.io.common.StringIO(COSTS_CSV), header=None))
    assert list(table.index) == ["capital>Generator>solar", "marginal>Generator>solar"]


def test_csv_pypsa_summary_source():
    data = FakeData(files={"csvs/metrics.csv": METRICS_CSV})
    metric = Metric(source="csv", file="csvs/metrics.csv", format="pypsa_summary",
                    rows=["co2_shadow"], scale=-1)
    assert list(metric_series(data, SYSTEM, metric)) == pytest.approx([97.9, 660.7])


# -- KPI lists ----------------------------------------------------------------

def test_packaged_kpis_load_and_ids_are_unique():
    kpis = load_kpis()
    ids = [k.id for k in kpis]
    assert len(ids) == len(set(ids)) > 20
    assert {"objective", "co2_price", "co2_captured", "elec_price_mean"} <= set(ids)


def test_project_kpis_merge_by_id():
    kpis = load_kpis(
        [
            {"id": "cap_nuclear", "label": "Nuclear fleet"},
            {"id": "elec_price_p95", "enabled": False},
            {"id": "wal_ft", "source": "csv", "file": "csvs/ptx_report.csv",
             "filters": {"metric": "Fischer-Tropsch @ {node}"}},
        ]
    )
    by_id = {k.id: k for k in kpis}
    assert by_id["cap_nuclear"].label == "Nuclear fleet"
    assert by_id["cap_nuclear"].rows == ["nuclear"]          # kept from the default
    assert "elec_price_p95" not in by_id
    assert list(by_id)[-1] == "wal_ft"


def test_a_new_source_replaces_the_default_definition():
    kpis = load_kpis([{"id": "cap_nuclear", "source": "balance",
                       "rows": ["supply:nuclear"]}])
    kpi = next(k for k in kpis if k.id == "cap_nuclear")
    assert kpi.source == "balance" and kpi.kind == "electricity"


def test_kpi_defaults_can_be_dropped():
    assert load_kpis([{"id": "x", "source": "capacity"}], defaults=False)[0].id == "x"


def test_unknown_kpi_key_is_rejected():
    with pytest.raises(ValueError, match="unknown key"):
        load_kpis([{"id": "x", "source": "capacity", "rowz": ["a"]}], defaults=False)


def test_evaluate_kpis_turns_errors_into_nan():
    data = FakeData(tables={("AA", "capacity.power"): _caps()})
    kpis = load_kpis(
        [
            {"id": "ok", "source": "capacity", "rows": ["nuclear"]},
            {"id": "bad", "source": "ratio", "numerator": "nope", "denominator": "ok"},
        ],
        defaults=False,
    )
    values = evaluate_kpis(data, "AA", kpis)
    assert values.loc["ok", "2050"] == pytest.approx(3.0)
    assert values.loc["bad"].isna().all()


def test_evaluate_kpis_prefers_a_stored_value():
    kpi = KPI(id="nuc", source="capacity", rows=["nuclear"])
    stored = pd.Series([9.0, 9.0], index=["2030", "2050"])
    data = FakeData(tables={("AA", "capacity.power"): _caps()},
                    stored={("AA", "nuc"): stored})
    assert evaluate_kpis(data, "AA", [kpi]).loc["nuc", "2050"] == 9.0


def test_metric_from_mapping_round_trip():
    metric = metric_from({"source": "flow", "kind": "co2", "rows": ["*>stm>*"]})
    assert metric.table_key == "flow.co2"


# -- flows ----------------------------------------------------------------------

def test_flow_rows_are_edges(built_indicators):
    data = built_indicators("BE")
    table = flows_to_table(data.flows_co2)
    assert all(label.count(">") == 2 for label in table.index)
    captured = select_rows(table, ["*>stm>*"], match="glob").sum()
    explicit = table.loc[[r for r in table.index if r.split(">")[1] == "stm"]].sum()
    pd.testing.assert_series_equal(captured, explicit)


def test_the_capture_kpi_sums_every_capture_edge(built_indicators):
    data = built_indicators("BE")
    table = flows_to_table(data.flows_co2)
    fake = FakeData(tables={("BE", "flow.co2"): table},
                    year_columns=list(table.columns))
    kpis = {k.id: k for k in load_kpis()}
    total = metric_series(fake, "BE", kpis["co2_captured"])
    parts = sum(
        metric_series(fake, "BE", kpis[k])
        for k in ("co2_captured_process", "co2_captured_fossil",
                  "co2_captured_biogenic", "co2_captured_dac")
    )
    # Every capture edge's source is one of the four groups.
    assert np.allclose(total.to_numpy(), parts.to_numpy())


# -- prices ------------------------------------------------------------------------

def test_price_statistics():
    from pypsa2html.extract.prices import series_stats

    index = pd.RangeIndex(10)
    price = pd.Series([0.5, 50, 50, 50, 50, 150, 150, 250, 250, np.nan], index=index)
    weights = pd.Series(2.0, index=index)       # 2-hour snapshots
    load = pd.Series([0, 1, 1, 1, 1, 1, 1, 1, 1, 1], index=index, dtype=float)
    stats = series_stats(price, weights, load)
    assert stats["mean"] == pytest.approx(np.mean([0.5, 50, 50, 50, 50, 150, 150, 250, 250]))
    assert stats["load_weighted_mean"] == pytest.approx((4 * 50 + 2 * 150 + 2 * 250) / 8)
    assert stats["hours_above_200"] == 4.0      # two snapshots of two hours
    assert stats["hours_above_100"] == 8.0
    assert stats["hours_below_1"] == 2.0
    assert stats["median"] == 50
    assert stats["p95"] == 250


def test_node_in_the_rows_of_a_system_table():
    """A per-region dual of a system table, read on each region's page."""
    gc = pd.DataFrame({"2030": [-95.0, -147.0], "2050": [0.0, 0.0]},
                      index=["co2_limit_per_countryBEWAL", "co2_limit_per_countryBEVLG"])
    data = FakeData(tables={(SYSTEM, "global.constraints"): gc})
    kpi = KPI(id="cap_dual", source="global", kind="constraints", scope="node",
              rows=["co2_limit_per_country{node}"], scale=-1)
    assert kpi.applies_to("BEWAL") and not kpi.applies_to(SYSTEM)
    assert list(metric_series(data, "BEWAL", kpi)) == [95.0, 0.0]
    assert metric_series(data, "BE", kpi) is None       # no such constraint


def test_after_places_a_project_kpi_under_its_anchor():
    kpis = load_kpis(
        [
            {"id": "x1", "source": "capacity", "after": "elec_gas"},
            {"id": "x2", "source": "capacity", "after": "elec_gas"},
        ]
    )
    ids = [k.id for k in kpis]
    i = ids.index("elec_gas")
    assert ids[i + 1: i + 3] == ["x1", "x2"]
    with pytest.raises(ValueError, match="names no KPI"):
        load_kpis([{"id": "y", "source": "capacity", "after": "nope"}])
