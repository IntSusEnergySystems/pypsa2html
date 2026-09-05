# Design decisions

Every non-obvious choice made while turning the legacy reporting scripts into
`pypsa2html`, with the alternatives and why they lost. If one of these turns
out to be wrong, this file is where to start unwinding it.

Legend: **D**ecision · *Alternative* · Reversibility (Easy / Moderate / Hard).

---

## D1 — Configuration format: CSV for tables, YAML for settings

**Decided.** The taxonomy tables (`nodes`, `processes_*`, `carrier_flows_*`,
`regions`, `domestic_*_production`) ship as **CSV** under
`src/pypsa2html/data/`. Settings and structure (`default.yaml`, `pages.yaml`,
project configs) are **YAML**. The two Excel workbooks are gone.

| | Pro | Con |
|---|---|---|
| **CSV (chosen, tables)** | `git diff` and merge work; 30–330 rectangular rows; still opens in Excel/LibreOffice for the modellers who maintain them; trivially loadable | No colour swatches or cell formatting; no cross-sheet formulas |
| **YAML (chosen, settings)** | Comments — essential when a key's meaning is not obvious; nested structure; already the PyPSA-Eur idiom | Poor for 330-row tables (unscannable) |
| *Excel (rejected)* | Colour swatches visible inline; sorting/filtering; familiar to modellers | Binary: no diff, no merge, no review. Two people editing `NODES` in one week means one loses their work. It also hid three bugs — a duplicated row, a trailing space in a colour, and a sheet being read by position so the wrong one loaded |
| *JSON (rejected)* | Ubiquitous | No comments; verbose; worse than both for hand editing |
| *SQLite / Parquet (rejected)* | Typed, fast | Binary again, and the data is ~800 rows total — speed is irrelevant |

Load-time validation replaces what Excel's rigid grid used to provide: unknown
`Type` values, dangling `Source`/`Target` references, out-of-range Sankey
coordinates and malformed colours are now errors, not silently missing links.

**Reversibility: Easy.** All reads go through `datafiles.load_taxonomy()`.

---

## D2 — Node identity comes from `n.buses.location`

**Decided.** `NodeResolver` maps components to nodes via the bus `location`
column that PyPSA-Eur populates during clustering.

The legacy code used three mutually inconsistent methods, sometimes in one
function: `.str[:2]` slicing, `== country` equality, and
`.filter(like=country)` substring matching on the *component name*.

| | Pro | Con |
|---|---|---|
| **`location` (chosen)** | Correct and unambiguous; works for `BE`, `BEWAL` and `BE1 0` alike; present in both reference models | Requires a sector-coupled PyPSA-Eur network |
| *Substring (rejected as default)* | Works on any network | `filter(like="BE")` also matches `BEWAL`. Only safe while no node code is a prefix of another — nothing enforced that |
| *`n.buses.country` (rejected)* | Standard attribute | In pypsa-wal it holds a mix of `BE` and `BEWAL`; too coarse for sub-national models |

Substring matching is retained behind `nodes.resolution: substring` purely for
comparison against legacy output.

**Reversibility: Easy** — one config key.

---

## D3 — The aggregate node is named `ALL`, not `EU`

**Decided.** Default `nodes.aggregate.code: ALL`.

`EU` is a *real* PyPSA-Eur bus location — the global oil, gas, coal and biomass
buses live there. The legacy tool also used `EU` as the name of the sum-of-all-regions
aggregate, so the string meant two different things and the code special-cased
`country == 'EU'` to mean "do not filter" in ~120 places.

Node detection excludes the pseudo-locations `EU` and `""`, and
`build_node_set` **raises** if the configured aggregate code collides with a
detected node.

The negawatt config keeps `code: EU` so its output file names still match the
existing `results/ref/htmls/EU_*.html`. New projects should not.

**Reversibility: Easy**, but renaming changes output file names.

---

## D15 — Nested aggregates use explicit member lists, never ISO2 slicing

**Decided.** `nodes.aggregate` remains the study-wide sum of every real
location. Extra synthetic nodes are declared under `nodes.groups` with an
explicit `members` list:

```yaml
nodes:
  aggregate: {enabled: true, code: ALL, label: All regions}
  groups:
    - {code: BE, label: Belgium, members: [BEVLG, BEWAL, BEBRU]}
```

The legacy SEPIA tool used `.str[:2]` / `filter(like="BE")`, which treats
`BEWAL` as Belgium *and* as a match for `BE`. Prefix matching is rejected
as a config key and is not implemented.

| | Pro | Con |
|---|---|---|
| **Explicit members (chosen)** | `BE` and `BEWAL` can coexist; works for NUTS-1, ISO2, and numbered clusters; missing members warn and skip (INTERNALS §6) | The modeller must list the codes |
| *Prefix / ISO2 slice (rejected)* | Shorter YAML | Collides the moment one code is a prefix of another — the original `BE`/`BEWAL` bug |

`Node.members` is `None` on the study-wide aggregate (meaning every real
code) and a sorted tuple on a group. `ctx.component_index` is the only
membership helper extractors and charts should call. If a group's `code`
is already a detected location, the group is omitted with a warning so the
same config works before and after a model splits `BE` into NUTS-1 nodes.

**Reversibility: Easy** — one config block.

---

## D4 — Planning horizons are discovered, and interval weights derived

**Decided.** `model.planning_horizons: null` scans the results directory. Only
`data/pages.yaml` and the config mention years at all.

The legacy code hardcoded `[2030, 2040, 2050]` in five scripts (ignoring
`snakemake.params.planning_horizons`), wrote `['2020','2030','2040','2050']` as
literal column names in ~20 places, and weighted cumulative sums with `*= 10`.
pypsa-wal then bolted a 5-year first period on with `*= 5`, a positional
`'2020'`-means-2025 alias, and ~12 `display_label` relabellings.

`ctx.horizon_weights()` derives each horizon's weight from the gap to the next
one, so 2025/2030/2040/2050 yields 5/10/10/10 with no special-casing, and a
2035 or 2045 horizon just works.

| | Pro | Con |
|---|---|---|
| **Derived (chosen)** | Any horizon grid works; no relabelling shims | A deliberately irregular grid gets a plausible-but-unstated weighting |
| *Explicit list (rejected)* | Total control | Was the status quo, and it drifted out of sync with the configs in both repos |

**Reversibility: Easy** — set `model.planning_horizons` explicitly.

---

## D5 — One page manifest replaces four parallel vocabularies

**Decided.** `data/pages.yaml` gives each section a single `id` that is
simultaneously the HTML anchor, the `plots:` toggle key and the `texts:`
narrative key.

The legacy tool maintained four namespaces by hand: prose keys in `plots.yaml`
(`"Cummulative Emissions"` — the misspelling was load-bearing), HTML anchors
(`#ghg`, reused for four different sections), `html_texts` slugs, and literal
titles in a hardcoded `sections` list. Two pairs had already drifted apart in
the shipped output, producing dead TOC links.

Disabling a section now removes it from the list. Previously, page assembly
dereferenced variables defined inside the disabled `if` branch, so switching
any single plot off raised `NameError`; and the `else` branch still built the
full figure and discarded it, so disabling a plot saved nothing.

An unknown key under `plots:` is an error rather than a silent no-op.

**Reversibility: Moderate** — the manifest is the backbone of `build.py`.

---

## D6 — Server-side navigation

**Decided.** The Jinja template generates the region selector, scenario
selector and page list from the node set, scenario list and manifest.

The legacy `pypsa.html` hardcoded a six-`<option>` country list, a six-branch
`updateCountry()` chain, six byte-identical 17-entry `fileOptions` arrays and a
90-line eight-branch scenario switcher. Adding a region meant three edits;
adding a scenario meant five.

The table of contents is also rendered into the DOM instead of being injected
into a JavaScript string literal (`secondaryTOC.innerHTML = "{{TOC}}"`), which
removes the escaping landmine that forced every TOC entry to use single-quoted
`href` attributes.

**Reversibility: Easy** — one template.

---

## D7 — The intermediate Excel handoff is gone

**Decided.** Extraction returns a DataFrame straight to the chart layer.

The legacy tool wrote `results/<study>/report/inputs<NODE>.xlsx` and read it back in the
next rule, with the reader hardcoding `usecols="C:G"` — exactly four year
columns. Adding a horizon silently truncated the data.

Each workbook was also written twice (created with `mode='w'`, reopened with
`mode='a'`), and the files were matched to nodes by *list position*
(`snakemake.output.excelfile[countries.index(country)]`).

`ChartData_<node>.xlsx` — the per-chart data download for end users — stays,
because that is a deliverable rather than an internal handoff.

| | Pro | Con |
|---|---|---|
| **In-memory (chosen)** | No positional coupling, no column-letter limit, no double write, no schema drift | The intermediate is no longer inspectable on disk |
| *Keep the xlsx (rejected)* | Debuggable; Snakemake can checkpoint it | It is precisely where the four-horizon limit and the ordering bug lived |

Mitigation for the con: `pypsa2html extract --dump` (planned, see future work)
would write the frame as CSV on request.

**Reversibility: Moderate.**

---

## D8 — Maps are shared by default; per-node highlight is opt-in

**Decided.** `output.shared_maps: true` (default). The maps page is written as
`maps_<scenario>.html` with no node prefix. Set `shared_maps: false` to emit
one page per node and outline the selected region on each PNG.

`plot_map` took a `country` argument and never used it, so the legacy run wrote
six byte-identical 21.7 MB files per scenario — 130 MB of duplication. Worse,
`create_map_plots` rendered every figure *twice* (the first loop computed a
base64 image and discarded it), so 36 renders produced 3 distinct figures.
PNGs are now small and content-hashed across scenarios, so a per-node view with
a region outline is affordable when wanted.

| | Pro | Con |
|---|---|---|
| **Shared default (chosen)** | Compact multi-scenario sites; one maps nav entry | Nav is not node-specific |
| **Per-node (`shared_maps: false`)** | Selected region highlighted; consistent with other pages | More HTML files |

**Reversibility: Easy** — one config key.

---

## D9 — Missing input degrades to a placeholder

**Decided.** A builder returns `None` (skip) or the framework catches the
exception and renders a named "Not available" box. `ctx.read_csv` returns
`None` for an absent file rather than raising.

The legacy scripts crashed the entire report if one CSV was missing — which is
why the pypsa-wal fork *deleted* whole functions rather than guarding them, and
how the two forks diverged so far.

| | Pro | Con |
|---|---|---|
| **Placeholder (chosen)** | One codebase serves models with different available data; partial results still useful | A silently incomplete report if nobody reads the log |
| *Fail fast (rejected)* | Impossible to miss | Guarantees the fork-and-delete cycle repeats |

Mitigated by `BuildReport.summary()` and a non-empty `failed` list printed to
stderr at the end of every run.

**Reversibility: Easy** — a `--strict` flag would flip it.

---

## D10 — Package layout: `src/` with packaged data

**Decided.** `src/pypsa2html/`, with taxonomy CSVs and templates inside the
package via `package-data`, and project configs in a top-level `config/`.

| | Pro | Con |
|---|---|---|
| **`src/` (chosen)** | Cannot accidentally import from the working directory; standard for installable libraries | One extra directory level |
| *Flat (rejected)* | Slightly shorter paths | `import pypsa2html` may pick up the source tree instead of the installed package |

Data lives *inside* the package so `pip install pypsa2html` is self-sufficient,
and `load_taxonomy(data_dir=...)` lets a project override the vocabulary
without forking.

**Reversibility: Hard** once people have installed it.

---

## D11 — Test data: vendored CSV plus a tiny synthetic network

**Decided.** The repository vendors the small tabular outputs of a negawatt
run, plus a synthetic `tests/data/mini.nc` (~81 kB) for the network-reading
layer.

A solved sector-coupled network is 10–190 MB; three horizons of the negawatt
`24h` run is ~30 MB, and the full-resolution run is 640 MB. Git is the wrong
place for either. The mini network (two nodes, AC/gas/H2, 24 snapshots) is
generated by `tests/data/make_mini_network.py` and covers node detection,
`energy_balance` and `group_pipes` without a full model checkout.

| | Pro | Con |
|---|---|---|
| **CSV/xlsx fixtures (chosen)** | ~500 kB total; tests run in seconds; readable in review | Cannot alone test network-reading |
| *Vendored full `.nc` (rejected)* | Tests the whole pipeline | 30 MB+ in git forever |
| **Synthetic mini.nc (chosen)** | Small, regenerable, exercises extract/balance | Does not replace a real-model smoke test |

Tests that genuinely need a full solved model remain marked `needs_model`.

**Reversibility: Easy.**

---

## D12 — Bugs are fixed, so output is not byte-identical

**Decided.** Correct the confirmed bugs, and treat validation as "numerically
equivalent except for a documented list" rather than `diff`.

The analysis found 28 confirmed bugs in the legacy code alone. Several change
numbers — for example the ammonia loss flow was written into the methanol
slot, and `excel_generator.py`'s `_2`/`_3` label suffixing meant
`'CCGT_2'` denoted "the second row labelled CCGT that survived threshold
filtering", so in a small region a losses value could land in a generation
code.

`docs/VALIDATION.md` lists every intentional numeric difference.

| | Pro | Con |
|---|---|---|
| **Fix (chosen)** | The tool is correct; explicitly requested | Byte-comparison against existing published output is impossible |
| *Bug-for-bug port (rejected)* | Trivially verifiable | Ships known-wrong numbers |

**Reversibility: Hard** — and deliberately so.

---

## D13 — Plotly from CDN by default

**Decided.** `output.plotly: cdn`, emitting the bundle tag once per page, with
`inline` available for offline use.

Legacy pages passed `include_plotlyjs='cdn'` per figure, so a costs page loaded
the bundle five times, and dispatch pages nested seven complete `<html>`
documents inside one another.

| | Pro | Con |
|---|---|---|
| **CDN (chosen)** | Pages stay ~100 kB; the browser caches one copy across the whole site | Needs network access; charts do not render offline |
| *Inline (available)* | Fully self-contained | ~3 MB per page; ~200 MB for a full site |

The CDN version is pinned (`plotly-2.35.2`) so a page rendered today keeps
rendering the same way.

**Reversibility: Easy** — one config key.

---

## D14 — Scope: the HTML pipeline only

**Decided.** Port extraction, the indicator/Sankey pages, the
cost/capacity/demand/dispatch/map pages and the multi-scenario overview.

Left behind: `sensitivity_results.py`, `sensitivity_scenario.py`,
`construct_JRC.py`, `atlite_max_vre_potentials.py`, `capacities_costs.py`,
`DBA.py`. These are data preparation and sensitivity analysis, not HTML
generation, and they are the most négaWatt-specific code in the tree.

`build_folder.sh` (13 kB of hand-written `cp` commands plus an FTP upload) is
replaced by writing the whole site into one directory. Deployment is out of
scope for a library.

**Reversibility: Easy** — they can be added as extra page kinds later.

---

## D16 — EV charging is detected by topology, and the counterfactual is energy-neutral

**Decided.** The three EV sections on the dispatch page find their components
by reading the network's *topology*, and compare the optimised charging profile
against an uncontrolled one that carries the **same energy** over the plotted
window.

### Detection

| | Pro | Con |
|---|---|---|
| **Topology (chosen)** | Any Link into a fleet-battery bus charges, any Link out of one is V2G; an EV Load on an electricity bus is pinned in time, one on the fleet bus is driving demand | One indirection to read |
| *Carrier names (rejected)* | Obvious at the call site | The carrier is `EV charger` upstream and in négaWatt but `BEV charger` in pypsa-wal, so a name match renders an empty chart on one of the two — exactly the class of bug D2/D15 exist to prevent |

Only the bus carrier is matched by name (`EV battery`, case-insensitive
substring), because that is the one label the whole PyPSA-Eur family shares.
A model with no fleet-battery bus returns a falsy `EVComponents`, the builders
return `None`, and the sections are simply absent (D9).

Consequence worth stating: whether the *split* exists is a property of the
model, not of the report. `sector.bev_natural_charging_split` in pypsa-wal
pins a share of demand to Elia's observed profile; upstream PyPSA-Eur and
négaWatt put the whole fleet behind the charger. The same chart renders both,
with the natural-charging series simply absent in the second case — not
present-and-zero, which would put a dead entry in the legend.

### The energy Sankey

The charger Link is the only model output that feeds the BEV node (`bev_se`).
The graph-closure step then sets `bev_se → bev_fe` to **all** EV final demand,
and both `land transport EV` and `land transport EV inflexible` map to that
demand. On a split model the inflexible load never goes through the charger, so
the BEV node had more leaving than arriving — on the 2026-08-18 Walloon run the
hole was exactly the natural-charging volume (0.47 TWh in 2025, 8.4 TWh in 2050).

Natural charging is therefore copied as its own electricity-grid → BEV flow
(`prenatbev`, label **Natural charging**). The charger keeps label **Smart
charging**. A model without the split has no such row, so the extra ribbon is
absent. `graph_imbalances` is the regression test: after closure, every
primary / secondary / final node must conserve energy.

### The dashed counterfactual

The shape uncontrolled charging would follow is, in order of preference:

1. the model's own natural-charging load — an observed profile, so it is the
   honest answer wherever the fork provides one;
2. the driving profile of the optimised fleet. Without a fleet battery,
   PyPSA-Eur's energy balance on the fleet bus forces the charger to follow
   driving demand exactly, so this *is* what the same model does with
   flexibility switched off — not an invented shape.

That shape is then scaled to the actual net grid draw of the window:

| | Pro | Con |
|---|---|---|
| **Energy-neutral (chosen)** | The two curves enclose the same area, so the difference is unambiguously a shift in *time*; a reader cannot mistake a conversion loss for a shifted peak | Does not show, on that chart, that flexibility costs energy |
| *Delivered-energy basis (rejected)* | Shows the loss | The dashed curve then encloses less area than the stack for two unrelated reasons at once (timing *and* losses), and the natural reading — "energy disappeared" — is wrong |

The volume side is not dropped, it is moved: `ev_energy_by_mode` reports it
annually as the gap between the stacked bar (grid draw) and the
"delivered to vehicles" marker (what reaches the cars). Charger and V2G
round-trip losses are visible there, per horizon, without overloading the
weekly chart.

The chart caption states the peak comparison rather than implying it, because
the honest answer is not always "smart charging shaves the peak": with no
distribution-grid constraint the optimiser concentrates charging into the
cheapest hours, and on the pypsa-wal 2025 horizon the optimised peak is *above*
the uncontrolled one. A chart that only ever showed peak shaving would be
telling a story the model does not support.

**Reversibility: Easy** — `plots: {ev_charging_winter: false, …}` switches the
sections off; the counterfactual rule is one function
(`extract.ev._counterfactual`).

---

## D17 — Capture variants are a naming convention, not a technology list

**Decided.** PyPSA-Eur names a capture-equipped sibling by appending ` CC`
to the unabated carrier (`CCGT CC`, `SMR CC`, `coal CC`, …). The report
treats that suffix as a *convention*:

| Layer | What happens |
|---|---|
| Energy Sankey | Unmapped `{tech} CC` / `{tech} CC_2` fold onto `{tech}` / `{tech}_2` when those entries exist (`carriers.fold_ccs_variants`). Explicit CSV rows (SMR CC, CHP CC, …) still win. |
| Carbon Sankey | `CCGT` / `OCGT` match **exactly**, so a CCS sibling is not booked as unabated. Unlisted four-bus capture Links (atmosphere + stored ports) are discovered by topology. Residual CO₂ folds onto the parent; captured CO₂ needs a `carrier_flows_carbon` row to appear as a Sankey edge (`CCGT CC_2` → `emmccgtcc`). Sequestration still sees the stored port either way. |
| Capacities | Topology already keeps any Link attached to AC. The faceted CCGT panel also draws `{tech} CC` siblings present in the table. Costs/map group any name containing `CCGT` with fossil power. |
| Colour | An explicit `CCGT CC` swatch if present; otherwise `{tech} CC` uses the CCS tint. |

A model-specific plant such as pypsa-wal's natural-gas CCGT with capture
therefore shows up without a Python `if carrier == "CCGT CC"`. Adding another
`{fuel} CC` power plant needs at most a captured-CO₂ CSV row for the Sankey.

| | Pro | Con |
|---|---|---|
| **Suffix + topology (chosen)** | New CCS plants work; library stays model-agnostic | Captured-CO₂ Sankey edge is still a taxonomy row (the residual is free) |
| *Hardcode `CCGT CC` (rejected)* | Obvious at the call site | The next Allam / coal-CC / methanol-CCGT-CC fork repeats the patch |

**Reversibility: Easy** — the helper is `pypsa2html.carriers`; discovery is
one function in `extract/emissions.py`.

---

## D18 — Self-sufficiency is two ratios and a members list

**Decided.** The legacy SEPIA "local production coverage" chart mixed three
ideas: per-carrier geographic coverage, the renewable share of gas and oil
(substituted into the same frame), and a Belgium-only pie of 2050 imports.
Ratios were clipped at 100 %, so a net exporter looked the same as autarky,
and the overview "energy independence" series was the unweighted mean of
those clipped columns.

`pypsa2html` reports two ratios, both unclipped:

| Metric | Domestic | Denominator |
|---|---|---|
| **Primary energy** | every `prod → *_pe` edge | domestic + imports − exports |
| **Electricity** | inflows to `elc_se` except `imp` | domestic generation + net electricity trade |

Nuclear kWh generated inside the node are domestic electricity and an
imported primary fuel (uranium). That is the point of having two numbers.
A ratio above 100 % is a net exporter.

The spatial interface is the same one the rest of the report already uses:

```python
self_sufficiency(ctx, node="BEWAL")                 # real location
self_sufficiency(ctx, node="BE")                    # nodes.groups entry
self_sufficiency(ctx, members=["BEWAL", "BEVLG"])   # ad-hoc, no extra library
```

`members` is expanded through `ctx.locations_for` (group codes allowed) and
matched against the configured NodeSet before anything is extracted, so
`members=['BEVLG','BEWAL','BEBRU']` on a config that already has a `BE`
group is the same cache key as `node='BE'`. An unmatched list is attached as
a transient group on a **copy** of `ctx`; page generation never sees it.
Internal trade cancels because electricity (and hydrogen) trade is the
residual of the group's closed graph, not a sum of regional import columns.

| | Pro | Con |
|---|---|---|
| **Two ratios + members (chosen)** | Matches the question energy-system studies actually ask; works for Wallonia, Belgium, and "these three nodes" without a new dependency | Primary energy still counts imported electricity as imported energy (energy-content, not primary-equivalent) |
| *Mean of per-carrier coverage (rejected)* | One number | Mixes geography with renewable share; clip hides exporters |
| *Pie of 2050 imports (rejected)* | Shows import mix | Hardcoded to Belgium, two scenarios, one year |

**Reversibility: Easy** — `plots: {self_sufficiency: false, self_sufficiency_balance: false, energy_independence: false}`.

## D19 — PV is three capacities and one capacity factor

**Decided.** `tech_groups.csv` collapsed every carrier whose name contains
`solar` into a single `solar` capacity group. That hid two different things.

**The three PV carriers are not interchangeable.** `solar` (fixed-tilt ground),
`solar-hsat` (single-axis tracking) and `solar rooftop` compete for different
resources — ground and tracking PV need land, rooftop does not — carry
different capital costs, and only the two ground carriers pay the
`electricity grid connection` adder, because rooftop PV is added behind the
distribution grid in `prepare_sector_network`. In the 2026-09-05 Walloon run
that adder is 18 589 EUR/MW/a, i.e. **31 % of ground PV's annualised cost and
0 % of rooftop's**. The split is where the result lives: BEWAL goes from
2 318 MW ground + 1 770 MW rooftop in 2025 to **0 ground + 5 250 rooftop +
1 305 tracking** in 2050, while the total moves only 4 088 → 6 554 MW. One
`solar` bar showed a smooth rise and hid a fleet turning over completely.

**`solar thermal` is not PV at all.** The substring rule also caught
`rural solar thermal`, `urban central solar thermal` and
`urban decentral solar thermal` — MW_th collectors on a heat bus — and stacked
them onto an axis labelled GW_e. At BEWAL that added 446 MW_th to 4 088 MW of
2025 PV, an **11 % overstatement**. Collectors now have their own group.

**Capacity factors stay folded.** The same irradiation drives all three PV
carriers and the only spread is the tracking gain (11.1 / 11.1 / 12.9 % at
BEWAL in 2050), so three near-identical bars would be noise where one number
is the answer. `UTILISATION_FOLD` in `extract/tables.py` folds them back, and
`_fold_groups` applies the same fold to the faceted panel membership. The fold
is applied to `energy` and `capacity` **before** the division, so the reported
factor is capacity-weighted (11.43 % for BEWAL 2050) and not a mean of ratios
(which would read 12.0 %).

| | Pro | Con |
|---|---|---|
| **Split capacity, fold utilisation (chosen)** | The chart shows what moves; the factor chart stays readable; units no longer mixed | Two group vocabularies to keep in step — `_fold_groups` is the only bridge |
| *Split both (rejected)* | One vocabulary | Three bars that differ by 1.8 points, on a chart read for outliers |
| *Collapse both (previous)* | Simplest | Hides the ground→rooftop→tracking turnover and mixes MW_th into GW_e |

**Reversibility: Easy** — put `solar,capacities,solar,10` back in
`tech_groups.csv` and restore `["solar"]` in `_POWER_GROUPS`.

## D20 — Electricity trade is measured, not left as the node residual

**Decided.** `_close_energy_graph` derived the energy Sankey's `imp → elc_se`
and `elc_se → exp` arrows from the annual **net** balance of the electricity
node: `consumed − produced`, clipped positive for imports and negative for
exports. Two things are wrong with that.

**A region imports and exports in the same year.** An annual net can only show
one of them, so one arrow was always exactly zero. Wallonia in 2050 of the
2026-09-05 run moves 10.0 TWh in and 2.0 TWh out; the Sankey drew 0 exports.

**Worse, the residual is a plug.** Because it was whatever *closed* `elc_se`,
it silently absorbed every mis-attribution elsewhere on that node. The same
2050 case drew **17.2 TWh** of imports — the 10.0 TWh that physically crossed
the border plus 7.2 TWh of graph error — and the node balanced perfectly, so
nothing warned. The model was running a **10 TWh import cap** at the time,
satisfied exactly (dual −5.39 EUR/MWh), so the report contradicted a constraint
the solver had met. A number that is defined as "whatever makes the picture add
up" cannot be read as a result.

Electricity is now taken from the model's own cross-border branches, via
`extract.balance.import_export_series` — the same quantity the energy balance
already used, and the one an import cap constrains: the hourly positive and
negative parts of the node's net exchange over AC lines and DC links,
snapshot-weighted. Verified against both available runs:

| BEWAL | 2025 | 2030 | 2040 | 2050 |
|---|---:|---:|---:|---:|
| 2026-09-05 6h, import / export | 1.98 / 2.20 | 2.90 / 3.75 | **6.47** / 3.53 | **10.00** / 1.99 |
| 20260905 1h, import / export | 0.84 / 4.67 | 2.79 / 4.32 | 14.10 / 1.64 | 21.18 / 0.77 |

The 6h 2040 and 2050 imports land on the cap to the third decimal, which is the
check that the arrow now means what it says.

**The hole this was hiding is now visible, and that is the point.** With the
plug gone, `graph_imbalances` reports `elc_se` short by 5.2–5.3 TWh in 2040 and
9.2–9.5 TWh in 2050 on both runs. It traces to the residential/tertiary branch:
`elc_fe → res` carries both the specific-electricity codes (`preselccfres`) and
a second family of heat-pump and electric-heater codes, and their sum exceeds
what the network withdraws. That is a taxonomy-mapping question for whoever owns
the code set, not something to paper over again.

Falls back to the old residual whenever `ctx` has no `networks` — which is how
the négaWatt reference fixtures run, so their ChartData comparisons are
untouched — and on any extraction error, so a malformed network degrades the
arrow rather than the build.

| | Pro | Con |
|---|---|---|
| **Measured trade (chosen)** | The arrow is the physical flow and matches any import constraint; imports and exports both appear; mapping errors surface instead of hiding | The Sankey no longer closes on `elc_se` until the demand-side mapping is fixed |
| *Residual plug (previous)* | Node always balances; no network needed | The headline trade number is a plug; exports invisible; silently absorbs bugs |
| *Measured, then re-plug the remainder (rejected)* | Pretty and correct-looking | Same lie in a new place, and no warning |

**Reversibility: Easy** — drop the `measured is not None` branch in
`_close_energy_graph` step 8.
