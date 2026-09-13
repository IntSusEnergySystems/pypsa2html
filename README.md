# pypsa2html

Turn solved [PyPSA](https://pypsa.org)(-Eur) networks into a navigable,
self-contained HTML report: Sankey diagrams, emissions, energy balances, costs,
capacities, dispatch and maps — one set of pages per region, with a scenario
switcher.

It is a model-agnostic successor to earlier reporting scripts used in the négaWatt
PyPSA-Eur studies. Regions, planning horizons and navigation are *detected from
the model* rather than hardcoded, so the same library serves a 5-country
European study and a 3-region Belgian one without edits. See
[`NOTICE.md`](NOTICE.md) for provenance.

> **Status: work in progress.** The framework — config, node detection,
> taxonomy, page manifest, build loop, navigation — is complete and tested. The
> chart builders are being ported; sections whose builder is not yet
> implemented render as a labelled placeholder, so the site is navigable today.
> See [Porting status](#porting-status).

---

## Why it exists

The original tool worked, but it was eleven thousand lines across eight scripts
that could only be run by Snakemake, and it had been forked-and-edited for a
second model until the two copies diverged badly. Concretely:

- the region list, planning horizons and technology lists were **hardcoded in
  five places each**, so adding a region meant three edits to a JavaScript blob
  and adding a horizon was structurally impossible (`usecols="C:G"` — exactly
  four year columns);
- a region was identified by **substring-matching the component name**, which
  silently breaks the moment one region code is a prefix of another
  (`BE` / `BEWAL`);
- `EU` meant both "the global oil/gas bus location" and "the sum of all
  regions", disambiguated by ~120 hand-written `if country != 'EU'` branches;
- the same logical chart had **four different names** — a prose key in
  `plots.yaml`, an HTML anchor, a narrative slug and a literal in a hardcoded
  list — and two pairs had already drifted apart, producing dead links;
- switching any single plot off raised `NameError`, because page assembly
  dereferenced variables defined inside the disabled branch;
- configuration lived in two binary `.xlsx` workbooks, so it could not be
  reviewed, diffed or merged.

`pypsa2html` keeps the domain logic and replaces the plumbing.

---

## Install

The library needs far less than PyPSA-Eur. If you already have a `pypsa-eur`
environment it is a strict superset — just install into it:

```bash
conda activate pypsa-eur
pip install -e /path/to/pypsa2html --no-deps
```

Standalone:

```bash
conda env create -f environment.yaml
conda activate pypsa2html
pip install -e . --no-deps
```

**Minimum requirements** (see `pyproject.toml`): Python ≥3.10, `pypsa` ≥0.31,
`pandas` ≥2.1, `numpy`, `plotly` ≥5.18, `jinja2`, `pyyaml`, `openpyxl`,
`netCDF4`. The map pages additionally need `matplotlib`, `geopandas` and
`cartopy` (`pip install -e ".[maps]"`); the weekly-dispatch widgets need
`panel` (`".[dispatch]"`). Both are optional — without them those pages are
skipped with a warning rather than failing the run.

---

## Use

Point a YAML config at your results and build:

```bash
pypsa2html inspect --config config/pypsa-wal.yaml   # what would be built
pypsa2html build   --config config/pypsa-wal.yaml   # build it
pypsa2html pages                                    # list toggleable sections
```

Or from Python:

```python
from pypsa2html import load_config, build_site

report = build_site(load_config("config/pypsa-wal.yaml"))
print(report.summary())
```

`inspect` is the fast way to check a config — it reads one network per scenario
and prints what it found:

```
project   : PyPSA-Wal
landing   : BEWAL / scen_demande_haute / overview

scenario 'scen_demande_haute' (High demand)
    horizons : [2025, 2030, 2040, 2050]
    nodes    : BEBRU(Brussels), BEVLG(Flanders), BEWAL(Wallonia), DE(Germany),
               FR(France), GB(Great Britain), LU(Luxembourg), NL(Netherlands),
               ALL(All regions)
    focus    : BEWAL
```

### Configuration

A project config states only what differs from
[`src/pypsa2html/data/default.yaml`](src/pypsa2html/data/default.yaml), which
documents every key. The essentials:

```yaml
root: /home/sylvain/svn/pypsa-wal      # relative paths resolve against this

project:
  name: PyPSA-Wal

model:
  clusters: adm
  planning_horizons: null              # null = discover from the .nc files

nodes:
  detect: true                         # read n.buses.location
  focus: BEWAL                         # the main region of interest
  labels: {BEWAL: Wallonia, BEVLG: Flanders}
  aggregate: {enabled: true, code: ALL, label: All regions}
  # Extra synthetic nodes. Explicit `members` only — never `.str[:2]`.
  # groups:
  #   - {code: BE, label: Belgium, members: [BEVLG, BEWAL, BEBRU]}

scenarios:
  - {name: scen_demande_haute, label: High demand, results_dir: results/.../scen_demande_haute}
  - {name: scen_base,          label: Base,        results_dir: results/.../scen_base}

landing:                               # what index.html opens on
  scenario: scen_demande_haute
  node: BEWAL
  page: overview

output:
  dir: results/times-pypsa/html
  pages: [overview, emissions, sankeys, fec, demands, costs, capacities, dispatch, maps]

plots:                                 # per-section switches, keyed by section id
  ghg_cum_by_source: false

texts:                                 # optional narrative HTML above a section
  sankey_energy:
    default: "<p>Energy flows across the system.</p>"
    BEWAL:   "<p>Wallonia-specific commentary.</p>"
```

Two worked examples ship in [`config/`](config/): `negawatt.yaml` (NUTS-1 Belgian
regions plus a `BE` group and an `EU` study-wide sum; landing `focus: BE`) and
`pypsa-wal.yaml` (the same three Belgian regions plus a `BE` group so
self-sufficiency can be read for the country; landing `focus: BEWAL`).

### Point of attention — capacity charts

PyPSA-Eur's `nodal_capacities.csv` lists *every* controllable component, including
fuel-potential Generators (biogas on a biogas bus), unconstrained industry
feedstock Links, heat vents and CO₂ / material Stores.  Their `p_nom_opt` is a
modelling bound, not installed conversion capacity — plotting them raw produces
axes in the 10⁵–10⁸ GW range.

By default (`features.capacity_filter: bus_carrier`) capacity charts keep only
`(component, carrier)` pairs that attach to an electricity, heat or H₂
*service* bus (power) or Store carriers on battery / H₂ / gas / water
buses (storage).  Using the component type matters: the same carrier name can
be a fuel-potential Generator and a power-plant Link (e.g. `lignite`).
**Power (MW) and storage (MWh) are never summed into one bar** (SEPIA C1) —
Stores are excluded from the power chart.  Classification uses the solved
network topology, so new technologies are handled without a per-carrier
denylist.  Set `features.capacity_filter: off` only if you intentionally want
the unfiltered CSV.  An optional `__omit__` group in `tech_groups.csv` remains
available as an extra name denylist.

### Point of attention — EV charging

The dispatch page carries three electric-vehicle sections (`ev_charging_winter`,
`ev_charging_summer`, `ev_energy_by_mode`) that separate the part of the fleet's
electricity the optimiser **cannot** move from the part it can:

- **natural (uncontrolled) charging** — a Load pinned to an observed charging
  profile, sitting *directly on an electricity bus*;
- **smart charging** — the grid-side draw of the charger Link into the
  fleet-battery bus, an output of the optimisation;
- **V2G** — energy returned to the grid, drawn below the axis.

Only the pypsa-wal fork splits the two (`sector.bev_natural_charging_split`);
upstream PyPSA-Eur and the négaWatt fork put the whole fleet behind the charger.
Both work, because detection reads the **topology** rather than carrier names —
any Link into a fleet-battery bus charges, any Link out of one discharges. That
matters concretely: the same carrier is spelled `EV charger` upstream and
`BEV charger` in pypsa-wal, so a name match would have produced an empty chart
on one of the two. A model with no electric vehicles simply skips the sections.

The dashed line is the same energy charged **uncontrolled** instead of
optimised. Its shape is the model's own natural-charging profile where the fork
provides one, and otherwise the driving profile — which is exactly what
PyPSA-Eur charges when flexibility is off, since without a fleet battery the
energy balance forces the charger to follow driving demand. It is scaled to
carry the same weighted energy as the actual net draw over the plotted window,
so the difference between the two curves is purely one of *timing*; the volume
cost of flexibility (charger and V2G round-trip losses) is reported by
`ev_energy_by_mode` instead, as the gap between the stacked bar and the
"delivered to vehicles" marker. See
[D16](docs/DESIGN_DECISIONS.md#d16--ev-charging-is-detected-by-topology-and-the-counterfactual-is-energy-neutral).

### Point of attention — self-sufficiency

Each region's Energy consumption page has two self-sufficiency sections
(primary energy and electricity, as percent and as a TWh balance). The
overview page compares the same ratios across scenarios. Both are unclipped:
a net exporter is above 100 %. Nuclear generation counts as domestic
electricity and as imported primary energy.

The Python interface is the same for a single node and for a group — no extra
library:

```python
from pypsa2html import load_config, self_sufficiency
from pypsa2html.context import build_context

ctx = build_context(load_config("config/pypsa-wal.yaml"), "scen_demande_haute")
self_sufficiency(ctx, node="BEWAL")
self_sufficiency(ctx, node="BE")                          # nodes.groups
self_sufficiency(ctx, members=["BEWAL", "BEVLG", "BEBRU"])  # ad-hoc
```

`config/pypsa-wal.yaml` declares a `BE` group with those three members so the
HTML report has a Belgium page. See
[D18](docs/DESIGN_DECISIONS.md#d18--self-sufficiency-is-two-ratios-and-a-members-list).

### Sensitivity analyses (parameter sweeps)

A family of runs differing in a single number is **one curve, not N scenarios**.
Mark each run on its own scenario entry and declare what the family means:

```yaml
scenarios:
  - {name: central, results_dir: results/central}
  - {name: nuc_4500, results_dir: results/nuc_4500, sensitivity: {sweep: nuclear_capex, value: 4500}}
  - {name: nuc_6000, results_dir: results/nuc_6000, sensitivity: {sweep: nuclear_capex, value: 6000}}
  - {name: nuc_9500, results_dir: results/nuc_9500, sensitivity: {sweep: nuclear_capex, value: 9500}}

sensitivities:
  - id: nuclear_capex
    label: Nuclear investment cost
    parameter: {label: Overnight cost of new nuclear, unit: EUR/kW}
    nodes: [BEWAL]                 # default: nodes.focus
    metrics:
      - {table: capacity, kind: power, rows: [nuclear], label: Installed nuclear capacity}
    description: "<p>Optional HTML above the chart.</p>"
```

A scenario carrying `sensitivity:` is a **sweep point**: no pages of its own, absent
from the scenario dropdown and from the cross-scenario overview. Each sweep becomes
one section (`sensitivity_<id>`) of the shared **Sensitivity analyses** page, with two
views in a dropdown — the swept value on the x axis with one line per planning
horizon, and the same numbers over time with one line per swept value. With no
sweep declared the page does not exist.

A metric names a table any PyPSA-Eur results tree produces:

| `table` | `kind` / `field` | rows are | default unit |
|---|---|---|---|
| `capacity` | `power` · `storage` · `ccs` | grouped technology names | GW / GWh |
| `cost` | `total` · `capital` · `marginal` · `clustered` | grouped technology names | EUR/year |
| `indicator` | `field:` an `Indicators` frame (`ghg_sector`, `fec_carrier`, …) | taxonomy codes | — |

`rows:` are summed; omit it to sum the whole table. A row the run did not build
reads **zero** — a point on the curve. A point whose table is missing entirely
reads *missing*, so the line breaks instead of dropping to the origin.

`pypsa2html inspect` prints each sweep, its points and any that are unsolved. See
[D22](docs/DESIGN_DECISIONS.md#d22--a-parameter-sweep-is-a-section-not-a-set-of-scenarios).

### Output

One directory containing `index.html` (redirecting to the landing page) plus
`<node>_<page>_<scenario>.html` for every combination, and
`<page>_<scenario>.html` for pages that do not depend on the region. Region and
scenario dropdowns are generated from the detected node set and the configured
scenario list.

---

## Code structure

```
pypsa2html/
├── config/                        worked example project configs
├── docs/
│   ├── DESIGN_DECISIONS.md        every trade-off, with alternatives  ← start here
│   ├── INTERNALS.md               the contract between layers
│   └── VALIDATION.md              differences vs the legacy output
├── src/pypsa2html/
│   ├── config.py                  YAML loading, deep-merge over defaults, validation
│   ├── nodes.py                   node detection + component→node resolution
│   ├── networks.py                lazy LRU network cache, horizon discovery
│   ├── datafiles.py               loads + validates the taxonomy CSVs
│   ├── pages.py                   the page manifest
│   ├── context.py                 BuildContext — replaces the `snakemake` global
│   ├── indicators.py              flow algebra (fec, pec, ghg, coverage, self-sufficiency)
│   ├── build.py                   manifest × nodes × scenarios → files
│   ├── cli.py                     build / inspect / pages
│   ├── extract/                   solved network → tidy flow tables
│   │   ├── flows.py                 energy flows
│   │   ├── emissions.py             carbon flows
│   │   ├── balance.py               nodal supply/demand time series
│   │   └── ev.py                    EV charging: natural vs smart, V2G
│   ├── charts/                    tidy tables → plotly figures
│   │   ├── base.py                  shared chart helpers
│   │   ├── sankey.py  indicators.py      energy/carbon Sankeys, indicator charts
│   │   ├── results.py               costs, capacities, demands
│   │   ├── dispatch.py  maps.py     time series, geographic maps
│   │   ├── ev.py                    EV charging weeks + annual split
│   │   ├── scenario.py              multi-scenario overview
│   │   └── sensitivity.py           parameter sweeps (config-declared)
│   ├── report/                    HTML assembly (jinja2 template + renderer)
│   └── data/                      the packaged taxonomy — CSV + YAML
└── tests/
    └── data/negawatt-ref/         116 kB of real négaWatt output as fixtures
```

**Data flow.** `config.yaml` + the solved networks → `BuildContext` →
`extract.*` produces tidy flow tables → `indicators` derives the energy-system
indicators → `charts.*` builds plotly figures → `report.render` assembles pages
from the manifest.

**Configuration formats.** Taxonomy tables are **CSV** (diffable, mergeable,
still Excel-openable); settings and structure are **YAML** (comments matter).
The two `.xlsx` workbooks the original used are gone — reasoning in
[D1](docs/DESIGN_DECISIONS.md). Everything is validated at load: unknown node
types, dangling process references, out-of-range Sankey coordinates and
malformed colours are errors, not silently missing links.

---

## Tests

```bash
pytest                       # ~0.3 s, no model needed
pytest -m "not slow"         # the default anyway
```

The suite deliberately does **not** run a full HTML generation. It covers
taxonomy validation, node detection and resolution, config loading, horizon
discovery, cost-table normalisation, the page manifest, and page rendering with
stubbed charts. Tests needing a solved network are marked `needs_model` and skip
when it is absent.

Many tests pin behaviour that was buggy in the original — the `BE`/`BEWAL`
prefix collision, the `EU` aggregate/location clash, the duplicated carrier
entry, the wrong worksheet being read, the misspelled-plot-key no-op — so they
double as regression guards.

---

## Snakemake integration

`pypsa2html` is an ordinary library, so a workflow can use it when installed and
skip the rule otherwise:

```python
try:
    import pypsa2html
    HAVE_PYPSA2HTML = True
except ImportError:
    HAVE_PYPSA2HTML = False

if HAVE_PYPSA2HTML:
    rule generate_html_report:
        input:
            networks=expand(RESULTS + "networks/base_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}.nc",
                            **config["scenario"], allow_missing=True),
            config="config/pypsa2html.yaml",
        output:
            index=RESULTS + "html/index.html",
        log:
            RESULTS + "logs/pypsa2html.log",
        run:
            from pypsa2html import build_site, load_config
            build_site(load_config(input.config))
```

Unlike the rules it replaces, this declares the inputs it actually reads. The
originals declared nine inputs of which four were never read, and read a further
nine files by hardcoded path that Snakemake therefore could not track.

---

## Porting status

| Layer | Module | State |
|---|---|---|
| Config, nodes, taxonomy, manifest | `config` `nodes` `datafiles` `pages` `networks` `context` | done, tested |
| Nested aggregates (`nodes.groups`) | `nodes` `context` | done, tested |
| Build loop, CLI, rendering | `build` `cli` `report` | done, tested |
| Energy / carbon extraction | `extract/flows` `extract/emissions` | done, tested |
| Indicators, Sankeys, indicator charts | `indicators` `charts/base` `charts/sankey` `charts/indicators` | done, tested |
| Costs, capacities, demands | `extract/tables` `charts/results` | done, tested |
| Dispatch, maps, scenario overview | `charts/dispatch` `charts/maps` `charts/scenario` | done, tested |
| EV charging (natural vs smart, V2G) | `extract/ev` `charts/ev` | new, tested |
| Self-sufficiency (primary energy, electricity) | `indicators` `charts/indicators` `charts/scenario` | new, tested |
| Sensitivity analyses (parameter sweeps) | `config` `pages` `charts/sensitivity` | new, tested |

---

## Known inefficiencies and future work

Deliberately **not** fixed during the port, to keep it reviewable. Recorded here
so they are not lost. Roughly in value order.

### Computational

Progress tracker (✅ done / 🔧 residual polish / ☐ open):

| # | Item | Status |
|---|---|---|
| 1 | Extract once per horizon (shared matmuls across nodes) | ✅ |
| 2 | Vectorise the per-link loss calculation | ✅ |
| 3 | Vectorise cross-border flow accounting | ✅ |
| 4 | Deduplicate the energy-balance prologue | ✅ |
| 5 | Render maps once (no wasted deep-copies) | ✅ |
| 6 | Cache the geojson and cost tables | ✅ |
| 7 | Replace quadratic accumulation | ✅ |
| 8 | Stop embedding maps as base64 | ✅ |
| 9 | Downsample / slim plotly dispatch serialisation | ✅ |
| 10 | Cache map PNGs across scenarios when identical | ✅ |

1. **Extract once per horizon, share across nodes.** ✅ Snapshot-weighted
   `weights @ links_t.pN` (and generator / load totals) run once per horizon via
   `_link_port_totals` / `_one_port_totals` / `_load_totals` on `ctx._files`.
   Assembled `energy_flows` / `carbon_flows` tables are cached per node so
   `demand_table` and `indicators.for_node` do not re-extract. Ranking and
   assembly remain per-node (entry suffixes are node-local).
2. **Vectorise the per-link loss calculation.** ✅ Losses are the residual of
   non-CO₂ port totals in `_link_flows` — no `n.links.apply(calculate_losses,
   axis=1)`.
3. **Vectorise cross-border flow accounting.** ✅
   `extract/balance.py::_import_export` is a vectorised line/link sum; the
   legacy O(8760 × n_lines) `.at[]` loop is gone. Cost-side import/export
   loops were never ported (costs come from `nodal_costs.csv`).
4. **Deduplicate the energy-balance prologue.** ✅ One
   `extract/balance.py::energy_balance` replaces the five ~110-line copies;
   all dispatch charts call it.
5. **Render maps once.** ✅ `_build_maps_page` loads regions once, renders each
   horizon once, and uses `NetworkCache` (no `deepcopy`). Shared maps page
   via `output.shared_maps` (D8).
6. **Cache the geojson and cost tables.** ✅ Regions geojson under
   `ctx._files[("geojson", …)]`; nodal costs via `_load_nodal` + `read_csv`;
   technology costs via `ctx.costs`; CLEVER industry / AFOLU CSVs via absolute
   path cache keys.
7. **Replace quadratic accumulation.** ✅ Flow / emission / balance helpers
   accumulate in lists and `pd.concat` once. No `df.loc[len(df)] = …` row
   appends in the hot paths.
8. **Stop embedding maps as base64.** ✅ PNGs are written next to the HTML
   (`map_{section}_{horizon}.png`) and referenced with `<img src=…>`.
9. **Downsample / slim plotly dispatch serialisation.** ✅ Weekly dispatch
   frames keep every `model.dispatch_step_hours` snapshot (default 2) and
   round y-values to `model.dispatch_value_decimals` (default 3) before
   plotly JSON embedding. Chart values at retained timestamps stay faithful;
   set `dispatch_step_hours: 1` for full hourly density.
10. **Cache map PNGs across scenarios when identical.** ✅ `_build_maps_page`
    fingerprints the *plotted* inputs (cost/GDP ratio, pipe capacities above
    the drawing threshold, H₂ storage) and hardlinks or copies a previously
    written `map_{section}_{horizon}.png` when the fingerprint matches.
    Cleared at the start of each `build_site` run. On pypsa-wal this reuses
    all cost-map PNGs across the three scenarios (and some gas maps).

**Validation (2026-08-02, items 9–17):** rebuilt all three pypsa-wal scenarios
(221 pages, **131 s**). Against the previous HTML tree (`/tmp/pypsa2html-wal-after`):

- **193 / 193** non-dispatch HTML bodies identical after stripping generation
  timestamps, colour strings, and plotly uids;
- **27 dispatch pages** intentionally smaller (−34% HTML bytes) from
  `dispatch_step_hours: 2` (item 9);
- **15 / 36 map PNGs** byte-identical (all gas maps); **12 cost PNGs** differ
  because `gdp_bneur` now includes BEWAL/BEVLG/BEBRU/LU (item 17);
  **9 hydrogen PNGs** differ slightly from `group_pipes` aggregation (item 13);
- map PNG fingerprint cache reused across scenarios (items 10).

**Validation (2026-08-02, items 1–8):** rebuilt all three pypsa-wal scenarios (221 pages,
832 s before the network-cache fix below). Against the previous HTML tree: all
36 map PNGs byte-identical; all 220 HTML bodies identical after stripping
generation timestamps, colour strings, and nav chrome. The only surface diffs
were footer clocks and, on 18 `scen_demande_haute` cost/capacity pages, a
fuller page-nav list in the new build (previous nav only listed
Capacities/Costs).

**Performance (2026-08-02):** the dominant cost was reloading solved `.nc`
files. With `NetworkCache` LRU size 2 and 4 horizons, a single-scenario build
performed ~400 disk loads (~0.8 s each). Fixes:

- default cache size = number of horizons (`model.network_cache_size: null`);
- process-wide path cache so overview / multi-scenario builds share loads;
- shared `BuildContext` pool across scenarios so overview does not re-extract
  every node three times.

pypsa-wal full site: **~832 s → ~131 s** (12 network loads). Items 9–10:
dispatch HTML is thinner (`dispatch_step_hours: 2`) and identical map PNGs are
reused across scenarios via content fingerprints.

### Structural

11. **Collapse the four tech taxonomies.** ✅ `tech_groups.csv` +
    `apply_tech_map(series, view)` replace `rename_techs_tyndp` / `_tynd` /
    `_tyndpp` / `rename_techs_ty`. Views: `costs`, `capacities`, `dispatch`,
    `map`, `clustered`.
12. **Merge the four near-identical bar-chart builders** ✅ into
    `charts.base.stacked_bar` (used by costs, capacities and scenario overview).
13. **Import upstream map code instead of vendoring it.** ✅
    `charts/maps_pipes.group_pipes` imports PyPSA-Eur's
    `scripts.plot_hydrogen_network.group_pipes` when on `sys.path`, else uses a
    vendored copy. Hydrogen maps draw grouped parallel pipes.
14. **Give maps a per-node view.** ✅ Set `output.shared_maps: false` to emit
    one maps page per node with the selected region outlined. Default remains
    shared (D8) for compact multi-scenario sites.
15. **A synthetic tiny network fixture.** ✅ `tests/data/mini.nc` (~81 kB)
    built by `tests/data/make_mini_network.py` — two nodes, AC/gas/H2, 24
    snapshots. Covered by `tests/test_mini_network.py` (no full model needed).
16. **`pandas` 3.0 readiness.** ✅ No `groupby(axis=1)` remains; helpers use
    `.T.groupby(...).sum().T`. The dependency pin is `pandas>=2.1` (upper
    bound lifted).
17. **Per-node economic metadata** ✅ `gdp_bneur` column in `regions.csv`,
    read by the cost map (nodes without an entry are skipped, not divided by
    zero). Includes Belgian sub-regions and LU for pypsa-wal.

---

## Documentation

- [`docs/TODO.md`](docs/TODO.md) — the remaining work, with sources, what to
  build, what to fix on the way and how to verify. Start here to contribute.
- [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md) — every trade-off, with
  the alternatives considered and how hard each is to reverse.
- [`docs/INTERNALS.md`](docs/INTERNALS.md) — the contract between layers; read
  this before adding a chart.
- [`docs/VALIDATION.md`](docs/VALIDATION.md) — how output compares to the legacy
  tool, and every intentional numeric difference.
- [`NOTICE.md`](NOTICE.md) — provenance and licensing.

## License

MIT. See [`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md).
