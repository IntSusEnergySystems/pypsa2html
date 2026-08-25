# Remaining work

Ordered by dependency. Each task states **where the source is**, **what to
build**, **how to verify**, and **what to fix on the way**.

**Read first, always:** [`INTERNALS.md`](INTERNALS.md) is the contract every
module must satisfy — it defines `BuildContext`, `NodeResolver`, the flow-table
shape and the six porting rules. [`DESIGN_DECISIONS.md`](DESIGN_DECISIONS.md)
explains why the framework is shaped the way it is.

**Ground rules for every task below** (from INTERNALS.md §6, repeated because
they are the whole point of the port):

1. No `snakemake` global, no module globals — everything from `ctx`.
2. No year literals (`ctx.horizons`, `ctx.horizon_weights()`), no node-code
   literals, no `'EU'` magic string (`ctx.is_aggregate(node)`).
3. No `.filter(like=node)` and no `.str[:2]` — use `ctx.resolver(horizon)`.
4. No `groupby(..., axis=1)` — removed in pandas 3. Use `df.T.groupby(...).sum().T`.
5. Never mutate an argument, `ctx.taxonomy`, or the colour palette — copy first.
6. Missing input → log a warning and return `None`. Never raise, never `NameError`.
7. Lookup tables belong in `src/pypsa2html/data/*.csv`, not in `if/elif` chains.

Verify with, from the repo root:

```bash
conda activate pypsa-eur
PYTHONPATH=src python -m pytest tests/ -q     # must stay under ~1 s
ruff check src tests
PYTHONPATH=src python -m pypsa2html.cli build -c config/negawatt.yaml -s ref -o /tmp/p2h
```

---

## Status

| Layer | Module | State |
|---|---|---|
| Config, nodes, taxonomy, manifest, context, networks | — | **done**, tested |
| Nested aggregates (`nodes.groups`, `members_of`, `component_index`) | `nodes` `context` | **done**, tested |
| Build loop, CLI, rendering, navigation | — | **done**, tested |
| Energy / carbon extraction | `extract/flows.py`, `extract/emissions.py` | **done**, tested |
| Indicators, Sankeys, indicator charts | `indicators.py`, `charts/{base,sankey,indicators}.py` | **done**, tested |
| Costs, capacities, demands | `extract/tables.py`, `charts/results.py` | **T1 — done** |
| Multi-scenario overview | `charts/scenario.py` | **T2 — done** |
| Dispatch time series | `charts/dispatch.py` | **T3 — done** |
| Maps | `charts/maps.py` | **T4 — done** |
| EV charging (natural vs smart, V2G) | `extract/ev.py`, `charts/ev.py` | **new** — not a port, see [D16](DESIGN_DECISIONS.md#d16--ev-charging-is-detected-by-topology-and-the-counterfactual-is-energy-neutral) |
| Self-sufficiency (PE + electricity) | `indicators.py`, `charts/indicators.py` | **new** — see [D18](DESIGN_DECISIONS.md#d18--self-sufficiency-is-two-ratios-and-a-members-list) |

T1–T4 below are the original porting briefs (kept for the bug list). The
chart modules exist; remaining work is T5–T10 plus model-side decisions.

List unresolved builders with:

```bash
PYTHONPATH=src python -c "
from pypsa2html.pages import load_manifest
from pypsa2html.build import resolve_builder
for p in load_manifest():
    for s in p.sections:
        if resolve_builder(s.builder) is None: print(p.id, s.id, s.builder)"
```

---

## T1 — Costs, capacities and demands (`charts/results.py`)

**Highest value: it unblocks T2, which reuses the same tables.**

**Source:** `/home/sylvain/svn/pypsa-eur_negawatt/…/Pypsa_results.py`
- `costs` (line 336), `clustered_costs` (474), `Investment_costs` (505),
  `operational_costs` (628), `capacities` (798), `storage_capacities` (890),
  `plot_demands` (928)
- chart builders `create_bar_chart` (**2762**, not the dead one at 2732),
  `create_clustered_costs` (2821), `create_investment_costs` (2850),
  `create_operational_costs` (2906), `create_capacity_chart` (2965),
  `storage_capacity_chart` (3043)

Also read the fork `/home/sylvain/svn/pypsa-wal/…/Pypsa_results.py` — it
drops the transmission-cost layer and the historical base-year merge, both of
which must become **optional** rather than deleted.

**Build two things.**

`extract/tables.py` — the data layer:

```python
def cost_table(ctx, node, kind: str) -> pd.DataFrame | None   # kind: total|capital|marginal|clustered
def capacity_table(ctx, node, kind: str) -> pd.DataFrame | None  # kind: power|storage
def demand_table(ctx, node) -> pd.DataFrame | None
```

Each returns technologies as the index and `ctx.year_columns` as columns, or
`None` when the inputs are absent. Inputs are
`results/<scenario>/csvs/nodal_costs.csv` and `nodal_capacities.csv`, read via
`ctx.read_csv(..., base="results")` so they are cached and missing files come
back as `None`.

`charts/results.py` — the six builders, each `(ctx, node, section) -> Figure | None`:
`annual_costs`, `clustered_costs`, `investment_costs`, `operational_costs`,
`capacities`, `storage_capacities`, `sectoral_demands`.

**Fix while porting:**

- **Parse the horizon from the CSV header.** The original renamed columns
  positionally (`f'{cluster}.1'` → `'2040'`), which is why the pypsa-wal fork
  had to alias its 2025 horizon as `'2020'` and then re-label it at display
  time in ~12 places. Read the `planning_horizon` header row instead.
- **Collapse the four bar-chart builders into one.** They are 85–94% identical.
  One `stacked_bar(df, colors, unit, title, signed=True)` in `charts/base.py`
  (extend the existing module, don't fork it).
- **`Pypsa_results.py:917`** — the save loop is nested inside the country loop
  *and* rebinds `country`, so every capacity CSV is rewritten O(n²) times.
- **`Pypsa_results.py:474`** — `clustered_costs` round-trips through
  `{node}_costs.csv` on disk instead of using the frame already in memory.
- **Cross-border cost accounting** (`calculate_elec_import_export_costs` 179,
  `calculate_h2_import_export_costs` 256) is computed **twice** per node, once
  in `costs` and again in `operational_costs`. Compute once, cache on `ctx`.
  The two functions differ only by carrier — merge into
  `net_trade_cost(ctx, node, carrier)`.
- The inner `for t in flows.index: for line in flows.columns:` loop with a
  `.at[]` write per cell is O(8760 × n_lines) per node per horizon per carrier.
  Vectorise it.
- **Transmission-cost attribution** (`calculate_ac_transmission` 117,
  `calculate_dc_transmission` 127 — 86% identical) is meaningless for a 3-node
  intra-Belgian model and essential EU-wide. Make it a config flag under
  `features:`, defaulting to on when the model has more than one node.
- **Base-year merge with `country_csvs/costs_{node}.csv`** must be gated on
  `ctx.config.model.base_year` being set, not deleted as pypsa-wal did.
- The **GDP denominators** used by the cost map are a literal dict
  (`Pypsa_results.py:1830`, only BE/DE/FR/GB/NL, and pypsa-wal rewrote it).
  Add a `gdp_bneur` column to `src/pypsa2html/data/regions.csv` and read it
  there; a node with no entry must be skipped, not divided by zero.

**Verify:** `results/ref/country_csvs/BE_costs.csv` and `BE_capacities.csv` are
vendored under `tests/data/negawatt-ref/country_csvs/`. Compare against
`ChartData_BE.xlsx` — note the sheets have **two header rows**, so read with
`header=2, index_col=0`, and the columns are node *labels*, not codes.

---

## T2 — Multi-scenario overview (`charts/scenario.py`)

The landing page. Depends on T1's `extract/tables.py`.

**Source:** `/home/sylvain/svn/pypsa-eur_negawatt/…/scenario_results.py`
(1686 lines). The pypsa-wal fork (1008 lines) is the better starting point for
*structure* — it already refactored every plot from two hardcoded frames
(`*_ref`, `*_suff`) to a loop over N scenarios, which is exactly what is needed.
Take its loop shape and the négaWatt version's feature completeness.

**Build** eight builders with the usual signature: `cumulative_emissions`,
`energy_comparison`, `costs`, `investment_costs`, `operational_costs`,
`capacities`, `storage_capacities`, `cc_capacities`, `energy_independence`.

The key difference from every other builder: these read **all** scenarios, not
just `ctx.scenario`. Add a helper that builds a context per scenario:

```python
def scenario_contexts(ctx) -> dict[str, BuildContext]  # cache on ctx
```

and skip any scenario whose results are missing, with a warning — a comparison
page must still render when only two of three scenarios have been solved.

**Fix while porting:**

- **The merge loop bug** (fork `scenario_costs` ~338, and identically in
  `scenario_investment_costs`): `set_index('tech')` sits *inside* the merge
  loop, so the second iteration merges `on='tech'` against a frame whose `tech`
  is already the index. With three scenarios this raises `KeyError`.
- **`/3` and `/4` averaging divisors** must be `len(ctx.horizons)`.
- **`df.groupby(df.columns, axis=1)`** — removed in pandas 2.0+.
- **`tickvals` built from `positive.index` after the loop**, i.e. from whichever
  scenario happened to be last.
- **The choropleth** keys on `properties.NAME` in one fork and
  `properties.name` in the other, and only highlights a region if the shapefile
  literally contains a feature named `BEWAL`. Write one helper that is agnostic
  to the property casing and degrades to no highlight with a warning.
- `energy_independence` now uses the unclipped primary-energy and electricity
  ratios (D18) and applies to every node, including groups.
- Scenario labels come from `ctx.config.scenarios[i].label`, never from the
  directory name.

**Verify:** `pypsa2html build -c config/pypsa-wal.yaml` (three scenarios) and
`-c config/negawatt.yaml` (two). The overview page must render for every node.

---

## T3 — Dispatch time series (`charts/dispatch.py`)

**Source:** `Pypsa_results.py` `plot_series_power` (1059),
`plot_series_power_cluster` (1322), `plot_series_heat` (1524), plus
`Dispatch_plots_weekly.py` (454 lines).

**Do the shared extraction first.** These five functions share a ~110-line
identical prologue. Write it once:

```python
# extract/balance.py
def energy_balance(ctx, node, carrier, horizon, start=None, stop=None) -> pd.DataFrame
```

This is the single highest-value extraction in the whole codebase — it removes
~700 duplicated lines and is where the dispatch bugs live.

**Build:** `power_winter`, `power_summer`, `heat_winter`, `heat_summer`.

The week windows are currently hardcoded (`"2013-02-08"`…`"2013-07-07"`). Put
them in the config under `model:` as `dispatch_windows: {winter: [...], summer: [...]}`,
and **derive a sensible default from the snapshots** rather than assuming a 2013
weather year — pypsa-wal and négaWatt both use 2013 today, but nothing enforces it.

**Fix while porting:**

- **`Dispatch_plots_weekly.py:207/366`** — the slider sets
  `visible = [False] * len(weeks)` but the figure has `n_cols × n_weeks × 3`
  traces. Plotly needs one entry per trace, so **the slider does not work at all**.
- **`:206/365` vs `:258/417`** — `enumerate(weeks[1:])` skips week 0 while
  `'active': 0` and the title refer to `weeks[0]`. Off by one.
- **`:240`** — the dummy-legend loop is nested over weeks, producing ~52
  duplicate legend entries per carrier.
- **`:266`** — `tabs.save(...)` is inside the horizon loop but `tabs` is created
  per node, so `..._2030.html` has 1 tab, `..._2040.html` has 2, `..._2050.html` has 3.
- **`:194`** — `supplyn['electricity distribution grid'] += v2g['V2G']` with no
  existence guard → `KeyError`. The guard exists at `Pypsa_results.py:1218` and
  was never back-ported.
- **`:186`** — drops `H2 pipeline` but not `gas pipeline`; the other copy drops both.
- **`Pypsa_results.py:1162` vs `:1237`** — the literal `0.1` is applied before
  `/1e3` in one place and after in the other, so it means 0.1 MW here and
  100 MW there.
- **`:1148-1175`** — the mixed-sign split creates duplicate column names and
  then `groupby(columns).sum()` re-merges them: a net no-op, repeated four times.
- **`Dispatch_plots_weekly.py:23`** — a *fourth* `rename_techs_tyndp` taxonomy,
  different from the three in `Pypsa_results.py`, with unknown names falling
  through to `'black'`. Use the shared table (see T5).

**Panel/Bokeh:** the original saved a `panel.Tabs` object as a **complete HTML
document** and then pasted it inside a `<div>`, giving seven nested `<html>`
elements per dispatch page. Either build the tab strip with plotly
`updatemenus` (preferred — drops the `panel` dependency entirely) or return
`charts.base.Html` containing only a fragment.

---

## T4 — Maps (`charts/maps.py`)

Lowest priority: the pages are optional (`pip install -e ".[maps]"`) and the
build already degrades gracefully without them.

**Source:** `Pypsa_results.py` `plot_map` (1716), `plot_h2_map` (2100),
`plot_ch4_map` (2307), `group_pipes` (2077), and the three ~96%-identical page
assemblers `create_map_plots` (1973), `create_H2_map_plots` (2550),
`create_gas_map_plots` (2640).

**Do not port `group_pipes`** — it is byte-identical to PyPSA-Eur's
`scripts/plot_hydrogen_network.py:24-44`. `plot_h2_map` and `plot_ch4_map` are
~68% and ~49% copies of upstream. Import from PyPSA-Eur when it is on the path
and fall back to a vendored copy only if that proves impossible; record the
choice in `DESIGN_DECISIONS.md`.

**Build:** `costs`, `hydrogen`, `gas`, each returning `charts.base.Html`.

**Fix while porting:**

- **Render each figure once.** `create_map_plots` renders everything **twice** —
  the first loop calls `plot_map`, `savefig`s, computes `encoded_image` and then
  uses none of it. ~45 of 48 network deep-copies are wasted.
- **`create_H2_map_plots:2563`** overwrites its own `planning_horizons` argument
  with `[2030, 2040, 2050]`, so any other horizon grid silently shows the wrong
  years. This is why pypsa-wal's 2025 map was wrong.
- **Do not base64-embed the PNGs.** That is what makes the page 21.7 MB. Write
  `map_<section>_<horizon>.png` next to the HTML and reference it with `<img>`.
- **The geojson is re-read on every call** (42 reads). Cache it on `ctx`.
- **`plot_ch4_map:2319/2440`** filters `n.buses` to `carrier == "AC"` and then
  does `n.buses.loc["EU gas", "x"] = ...`, which *inserts a new row* with NaN
  everywhere rather than repositioning a bus. Presence-check instead.
- `output.shared_maps` is already honoured by `build.py`; the builder does not
  need to know about it. If you later add a per-node highlight, flip the
  manifest's `shared: true` on the `maps` page and update D8.

---

## T5 — Consolidate the technology taxonomy

`rename_techs_tyndp`, `rename_techs_tynd`, `rename_techs_tyndpp`,
`rename_techs_ty` (`Pypsa_results.py:36, 602, 772, 476`) plus a fourth copy in
`Dispatch_plots_weekly.py:23`. They overlap heavily and **disagree**, so the
same carrier gets different colours and legend groups on different pages.

This is data, not code. Create `src/pypsa2html/data/tech_groups.csv`:

| column | meaning |
|---|---|
| `pattern` | substring or regex matched against the carrier |
| `view` | which grouping this row belongs to (`costs`, `capacities`, `dispatch`, `map`) |
| `group` | the display group |
| `order` | sort position within the view |

with one `apply_tech_map(series, view)` helper in `charts/base.py`. Add
validation to `datafiles.py` and a test that every carrier appearing in the
vendored fixtures maps to some group under every view.

Also move the ~25 hardcoded `tech_colors[...] = "#..."` injections scattered
across `Pypsa_results.py` into `data/tech_colors.csv`, merged into a **copy** of
the palette at load — the originals mutate `snakemake.params.plotting` in place,
so charts contaminate each other depending on call order.

---

## T6 — Tests for the ported layers

Keep the suite under ~1 s. Do **not** add a test that renders a full report.

- `tests/test_indicators.py` — build the indicator frames from the vendored
  `tests/data/negawatt-ref/flows/*.csv` via `indicators.build(ctx, node, energy=..., carbon=...)`
  and assert against `ChartData_BE.xlsx` (`header=2, index_col=0`; columns are
  node **labels**, map with `taxonomy.nodes['Label']`). Currently verified by
  hand: `ghg_sector` and `ghg_source` match on 14 of 15 columns to <5e-3 for
  both BE and EU, with `Biomass` differing by design. Pin that.
- `tests/test_extract.py` — the extractors need a network, so mark
  `needs_model`. Add a *fast* test of the pure helpers (the carrier→code
  mapping, the deterministic port key, threshold filtering on magnitude).
- `tests/test_charts.py` — every manifest builder resolves and returns a
  `Figure`/`Html`/`None` when fed the fixtures. Parametrise over the manifest
  so a new section is covered automatically.
- Add a regression test for **each** bug listed in `VALIDATION.md` §B that the
  fixtures can reach.

**Build a tiny synthetic network** (`tests/data/mini.nc`, target <1 MB) so the
extraction layer can be tested without a 45 MB file — see D11. Two nodes, three
carriers, ~24 snapshots, a couple of links with `p2`/`p3` ports. Generate it
with a committed script (`tests/data/make_mini_network.py`) so it can be
regenerated when PyPSA changes.

---

## T7 — Finish the validation report

`docs/VALIDATION.md` §B lists the code defects that change numbers, populated
from the code analysis. Replace the qualitative "Effect on output" column with
**measured** before/after values, using the vendored fixtures and the shipped
`ChartData_*.xlsx` / `country_csvs/`.

Then run the full comparison and record the result:

```bash
pypsa2html build -c config/negawatt.yaml -s ref -o /tmp/p2h-ref
# compare /tmp/p2h-ref/ref/html/BE_*.html against
# /home/sylvain/svn/pypsa-eur_negawatt/results/ref/htmls/BE_*.html
```

---

## T8 — Retire the legacy report scripts in `pypsa-wal`

**Only once T1–T4 render real charts for `config/pypsa-wal.yaml`.**

1. Confirm the report is complete for `scen_demande_haute` — no "Not available"
   placeholders in the end-of-run summary.
2. Copy `examples/snakemake/pypsa2html.smk` into `pypsa-wal/rules/`, add
   `include: "rules/pypsa2html.smk"` to the `Snakefile`.
3. Delete the superseded postprocess rules that built the old HTML report
   (`prepare_results`, `prepare_dispatch_plots`, and any remaining report
   generation rules) from `pypsa-wal/rules/postprocess.smk`, and the
   `countries` / `STUDY` / `study_dir` helpers above them if nothing else uses
   them.
4. Remove the superseded legacy report directory from `pypsa-wal`.
5. Update `pypsa-wal/instructions.md` — the **HTML report (pypsa2html)** section
   already exists; remove the "still present but superseded" note and the
   legacy report directory from the repository-layout tree.

Note those rules are already unrunnable: they declare the legacy config workbook, countries table and HTML template as inputs, and all three
were caught by the `*.xlsx` / `*.html` patterns in `.gitignore` and never
committed.

---

## T9 — Open questions needing a modelling decision

Not bugs with an obvious fix — someone who knows the model must choose. Each is
marked with a comment at the relevant point in the code.

1. **Duplicate model codes in the extraction output.** `inputsBE.xlsx` maps two
   distinct model entries onto one code five times in `Inputs`
   (`prbelcchpgaz`, `prbvapchpgass`, `preehplyy`, `presenccfb`, `prespaccftaa`)
   and once in `Inputs_co2` (`emmresbmatm`). The legacy
   `data.loc[:, ~data.columns.duplicated()]` kept the first and **silently
   dropped the rest** — 12.73 TWh of gas-CHP power, 6.89 TWh of gas-CHP heat,
   6.36 TWh of residential electricity and 0.17 MtCO2 in the base year.
   `indicators.py` now sums them. Confirm summing is right, and if so fix the
   collision at source in `data/carrier_flows_*.csv`.
2. **The pypsa-wal distribution-loss correction is not ported.** The fork
   subtracts `elc_se→per (dis)` from `elc_fe→res`. With the shared extraction,
   `elc_fe` balances to 1e-12 *without* it and is short by exactly the loss
   *with* it — so it double-subtracts. If pypsa-wal genuinely needs it, the fix
   belongs in extraction, before the delivery flow is derived.
3. **Imported hydrogen, ammonia and methanol count as 100% renewable** (legacy
   convention, preserved). For methanol this contradicts the carbon algebra a
   few dozen lines away, which charges imported methanol `co2_intensity_met`.
   One-line change in `RENEWABLE_SHARES`; it moves a published number.
4. **The district-heat renewable denominator omits** `wst_pe`, `wst_ren_pe`,
   `wst_fos_pe`, `ght_pe`, `sth_pe`, `fat_pe` and `imp`, all of which do feed
   `vap_se` in the taxonomy.
5. **28 energy-Sankey edges reference placeholder codes** (`afzf`, `dfdfg`,
   `fzafz`, `zr`, `lll`, …) — mostly transformation-loss arcs for CHP,
   waste-to-energy, geothermal and solar thermal. Those links have never
   rendered in any legacy run. Supplying the right code per arc is a modelling
   decision. See `VALIDATION.md` §A1. Also 17 carbon-Sankey edges reference
   codes no mapping can emit (§A3).
6. **`prohydclamm` is produced by two entries** (`NH3` and `Haber-Bosch_4`),
   which are summed. Probably intentional — two routes to one indicator — but
   worth confirming.

---

## T10 — Deferred performance work

Listed in the README's *Known inefficiencies* section; not repeated here.
The two with the largest payoff:

- **Extract once per node, not once per (node × horizon).** Extraction re-runs
  the whole matmul/groupby pipeline per region — 18 times for a 6-region,
  3-horizon run — when one pass grouped by node would do.
- **Vectorise the per-link loss calculation** (`n.links.apply(..., axis=1)`).
