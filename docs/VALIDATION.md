# Validation against legacy output

How `pypsa2html` compares to the output already in
`pypsa-eur_negawatt/results/ref/htmls/`, and every difference that is
intentional.

**Byte-identical output is not a goal** — see
[D12](DESIGN_DECISIONS.md#d12--bugs-are-fixed-so-output-is-not-byte-identical).
The brief was to fix obvious bugs, and several of them change numbers. The
target is "numerically equivalent except for the documented list below".

---

## Reference dataset

négaWatt run `ref`, clustering `base_s_adm__24h`, horizons 2030/2040/2050,
solved 2026-06-02. The tabular outputs are vendored under
`tests/data/negawatt-ref/`; the solved networks (640 MB) are not.

---

## A. Defects found in the *configuration data*

These were latent in the legacy configuration workbook and are independent of
any code change. Pinned by `tests/test_taxonomy_integrity.py` so they cannot
get worse.

### A1 — 28 energy-Sankey edges reference placeholder codes ❗

56 rows of the `PROCESSES` sheet name a `Value_Code` that never appears in real
model output; about 28 of those are keyboard-mash placeholders:

```
afzf  csqz  dfdfg  dfsfsfs  dz  eee  eff  efzef  efzz  fazfa  fftfy
fqzfzef  fz  fza  fzafz  fzeg  fzfaz  fzza  fzzaf  gegerg  iuygg
lll  llll  scvqs  sfqf  zfazfa  zfzf  zr
```

They are mostly transformation-loss arcs for CHP plants, waste-to-energy,
geothermal and solar thermal. **Those links have silently never rendered in any
legacy run**, so the energy Sankey has been missing loss flows for those
technologies throughout.

Not fixed here: supplying the right code for each arc is a modelling decision,
not a mechanical one. The remaining ~28 unresolved-but-structured codes
(`proght`, `proenm`, …) are legitimately absent because the négaWatt model does
not deploy those technologies.

### A2 — `lll` is used by two unrelated edges

`enc_pe → per` (solid biomass power transformation losses) and `wst_pe → per`
(waste-fired power transformation losses) share the placeholder `lll`, so had
it ever resolved, both arcs would have shown the same number.

### A3 — 17 carbon-Sankey edges reference codes no mapping can emit

`emmagrgasz`, `emmbmatmp`, `emmbnpower`, `emmcental`, `emmcoalchp`,
`emmcoalgen`, `emmefuelatm`, `emmexp`, `emmexpgas`, `emmliggen`, `emmmetexp`,
`emmmetimp`, `emmoilchp`, `emmoilgeb`, `emmoilind`, `emmprocesscc`, `emmrail`.

Unlike A1 these are plausibly named, so they look like planned-but-unimplemented
features rather than typos.

### A4 — duplicated carrier entry (**fixed**)

`urban decentral biomass boiler_2` appeared twice in `entries_to_select`, so the
selection loop concatenated it twice and `groupby('target').sum()` **doubled**
the value written to `lossbbb`. Deduplicated during conversion.

### A5 — `COUNTRIES.xlsx` was read from the wrong sheet (**fixed**)

`pd.read_excel(file, index_col=0)` with no `sheet_name` reads sheet 0, which is
a documentation table, not the country list. `ISO_Code` and `Label` were
therefore never available, and every `create_map()` call sat behind a condition
that was always false. Now `regions.csv`, with the right columns.

### A6 — malformed colour (**fixed**)

`agh_ghg` had a trailing space in its hex colour (`"#95d0fc "`), which plotly
rejects. Stripped; validation now catches it.

### A7 — packaged tables were négaWatt-only (**fixed**)

`processes_carbon` referenced `emindmetatm` and `emoilppatm`, which only the
pypsa-wal fork's mapping emits. The packaged tables are now the *union* of both
forks, including both spellings of the EV charger carrier (`EV charger` in
négaWatt, `BEV charger` in pypsa-wal → both map to `prebev`).

---

## B. Code defects that change numbers

Listed so a number that differs from the legacy report can be traced to a
cause. Status is tracked as the port lands.

| # | Where | Defect | Effect on output |
|---|---|---|---|
| B1 | indicator graph closure | Inside `for en_code in ['amm','met']` the write target is hardcoded `('met_fe','per','')` instead of `(en_code+'_fe','per','')` | The ammonia loss flow was never created; the methanol one was written twice. Both change. |
| B2 | `excel_generator.py:348` | `_2`/`_3` label suffixing is applied *after* threshold filtering, so `'CCGT_2'` means "the second surviving CCGT row" | If the primary row fell below 0.1 TWh, a losses value was written into a generation code. Affects small regions most — i.e. pypsa-wal. |
| B3 | `excel_generator.py:342` | `value >= 0.1` filter | Deleted every negative value, so net-negative CO2 rows vanished entirely. Now filtered on magnitude. |
| B4 | `excel_generator.py:208` | `total_e{i}` / `carrier_bus{i}` written onto the cached network and never removed | Contaminated the next call; the `EU` pass ran last and unfiltered, so EU losses were wrong for the affected links. |
| B5 | chart helpers | Unit passed as the 7th positional argument, landing in `interval_year` because the two `combine_charts` had different signatures | GHG chart-data sheets were labelled "(TWh/year)" instead of MtCO2eq. Cosmetic but wrong. |
| B6 | renewable-share ratios | `ren_bm == bm_columns` etc — numerator equals denominator | Four renewable-coverage ratios were always 100% or NaN. |
| B7 | biomass potential | `if planning_horizon != 2020` made the `== 2020` branch unreachable | The 2020 biomass row stayed NaN and propagated into the biomass export flow permanently. |
| B8 | `Pypsa_results.py:917` | Save loop nested inside the country loop *and* rebinding `country` | Every capacity CSV rewritten O(n²) times; last writer wins. |
| B9 | `Pypsa_results.py:1717` | `plot_map` never used its `country` argument | Six byte-identical 21.7 MB map files per scenario. Now one shared page (D8). |
| B10 | `Pypsa_results.py:2563` | `create_H2_map_plots` overwrote its own `planning_horizons` argument with `[2030,2040,2050]` | Wrong horizons on the H2 map for any other grid — e.g. pypsa-wal's 2025. |

---

## C. Structural differences (no numeric effect)

| Area | Legacy | pypsa2html |
|---|---|---|
| File names | `{node}_{page}_{scenario}.html` | unchanged, except shared pages drop the node prefix |
| Entry point | none — `localStorage` plus a filename convention in three places | `index.html` redirecting to the configured landing page |
| Navigation | hardcoded JS per node and per scenario | generated from the detected node set and scenario list |
| TOC | injected into a JS string literal | rendered into the DOM |
| Anchors | `id="ghg"` reused by four sections | unique per section |
| plotly | loaded up to 5× per page | once per page, pinned version |
| Dispatch pages | 7 nested `<html>` documents | single document |
| Missing input | `NameError`, whole run dies | labelled placeholder, run continues |

---

## D. How to re-run the comparison

```bash
conda activate pypsa-eur
cd /home/sylvain/svn/pypsa2html
pytest                                                   # fast invariants
pypsa2html build -c config/negawatt.yaml -s ref -o /tmp/p2h-ref
```

Then compare `/tmp/p2h-ref/BE_*.html` against
`pypsa-eur_negawatt/results/ref/htmls/BE_*.html`. Expect the differences in
sections B and C; anything else is a regression worth investigating.

> **Status.** Sections A and C are complete and enforced by the test suite.
> Section B is populated from the code analysis; the per-value numeric
> comparison lands with the extraction port.

---

## SEPIA chart traps (must not reproduce)

These are reporter bugs from the earlier SEPIA HTML tool, independent of the
B-list above. pypsa2html must not repeat them.

| # | Trap | Guard |
|---|---|---|
| C1 | Mixing MW and MWh in one bar | Power vs storage charts; Stores excluded from the power table. `capacity_filter` keeps service-bus topology. Pinned by `test_capacity_table_does_not_mix_store_mwh_and_link_mw`. |
| C2 | ×10 cumulative GHG ("years in decade") | No `* 10` on GHG. Annual series are plotted as annual; cumulative charts use `ctx.horizon_weights()`. Pinned by `test_cumulative_emissions_use_horizon_weights`. |
| C3 | `load` / inf load-shedding on the GW axis | `capacity_filter._is_junk_name` drops `load`, `*vent*`, `*to air*`, `*shedding*`. Pinned by `test_capacity_filter_drops_load_shedding_and_vents`. |
| C4 | Degenerate industry / vents dominating the axis | Same topology filter; extra junk is name patterns + service-bus logic, never a country denylist. |
| C6 | Year labels renamed positionally to 2020/2030/2040/2050 | `extract/tables.parse_nodal_csv` reads the `planning_horizon` header. Pinned by `test_parse_nodal_csv_reads_horizons_from_header`. |
| B3 | Garbage shadow prices (e.g. −592421 €/MWh on an empty methanol bus) | Prices are not plotted yet. `extract.tables.sanitize_prices` drops non-finite values and `|price| > features.price_abs_cap` (default 1e4 €/MWh). Call it before any price chart. |
