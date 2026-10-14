# pypsa2html

Turn solved [PyPSA](https://pypsa.org)(-Eur) networks into navigable,
self-contained HTML reports: Sankey diagrams, emissions, energy balances, costs,
capacities, dispatch and maps — one set of pages per region for every scenario,
plus a comparison report that sets the scenarios side by side.

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

pypsa2html writes two kinds of report:

- a **scenario report** per solved run — every page of one run, opening on its
  *Key figures*, with no reference to any other run. Next to its pages it writes
  `summary.csv` / `summary.json`: every number a comparison needs, with where it
  came from;
- a **comparison report** — the key figures of several runs side by side with
  their difference to a reference run, the comparison charts, the parameter
  sweeps and a provenance page. It is built **from the summary files only**, so it
  never reopens a network and can combine runs solved on different machines.

```bash
pypsa2html inspect   --config config/pypsa-wal.yaml               # what would be built
pypsa2html build     --config config/pypsa-wal.yaml -s scen_central   # one scenario report
pypsa2html build     --config config/pypsa-wal.yaml               # every scenario + the comparison
pypsa2html compare   --config config/pypsa-wal.yaml \
    results/walloon/scen_central results/walloon/scen_nuc4gw --reference scen_central
pypsa2html summarize --config config/pypsa-wal.yaml -s scen_nuc4gw   # summary only (e.g. on a cluster)
pypsa2html kpis      --config config/pypsa-wal.yaml               # the key figures defined
pypsa2html pages                                                  # list toggleable sections
```

`compare` takes results trees, HTML folders or `summary.json` files; with none it
compares `compare.scenarios`, else every configured scenario. A scenario whose
summary is missing is listed as *not compared*; one older than its results tree
is used with a warning (`compare.on_stale`), or rebuilt with `--recompute`.

Or from Python:

```python
from pypsa2html import load_config, build_site, compare_site

cfg = load_config("config/pypsa-wal.yaml")
build_site(cfg, scenarios=["scen_central"])
report = compare_site(cfg, ["results/walloon/scen_central", "results/walloon/scen_nuc4gw"])
print(report.summary())
```

`inspect` is the fast way to check a config — it reads one network per scenario
and prints what it found, including whether its summary is up to date:

```
project   : PyPSA-Wal
landing   : BEWAL / scen_central / key_figures
compare   : results/walloon/compare  (pages: key_figures, overview, sensitivity, scenarios)

scenario 'scen_central' (Central)
    summary  : ok
    horizons : [2025, 2030, 2040, 2050]
    nodes    : BEBRU(Brussels), BEVLG(Flanders), BEWAL(Wallonia), DE(Germany),
               FR(France), GB(Great Britain), LU(Luxembourg), NL(Netherlands),
               BE(Belgium), ALL(All regions)
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
  page: key_figures

output:
  dir: results/times-pypsa/{scenario}/html
  pages: [key_figures, emissions, sankeys, fec, demands, heat, costs, capacities, dispatch, maps]
  compare_url: ../../compare/index.html  # optional link from every scenario page

compare:                               # the comparison report
  reference: scen_base                 # differences are taken against this run
  dir: results/times-pypsa/compare
  pages: [key_figures, overview, sensitivity, scenarios]

kpis:                                  # key figures: merged by id over data/kpis.yaml
  - {id: elec_price_p95, enabled: false}
  - id: wal_ft
    group: Power-to-X
    source: csv                        # a long table the project writes
    file: csvs/ptx_report.csv
    filters: {section: "PtX TWh in", metric: "Fischer-Tropsch @ {node}"}
    nodes: [BEWAL]
    unit: TWh/year

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
comparison report compares the same ratios across scenarios. Both are unclipped:
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

### Point of attention — network costs

The *Annual costs* charts value the grids as the optimiser sees them: the
annuity of the capacity in the model.  That is not what the grids cost their
users, and a reader comparing the *distribution network* row with a
regulator's allowed revenue will find it several times too low (it was, in a
stakeholder review of the pypsa-wal cabinet deck, Sept 2026).  Two sections at
the bottom of the Costs page answer the question as it is asked:

- **Distribution network costs** — electricity and gas *separately*, every
  layer of the regulated revenue stacked: existing grid capital charges and
  operating costs (exogenous, from the regulator's accounts), the
  reinforcement and PV-hosting costs the model adds (hatched), the regulated
  add-ons (public-service obligations, road-use fee, smart meters) and the
  losses, with the regulator's own total marked as a reference.
- **Transmission network costs** — a *tariff view* (what the region's users
  pay: the national regulated revenue and the new inter-node lines times the
  region's share of offtake, plus H₂ and CO₂ pipelines at 50/50) and one
  *branch view* per network (each link to a neighbouring node, existing and
  new, at model cost, 50/50).  The table under the chart says for every line
  whether it is existing or new and whether it is inside the region or an
  interconnection, and states that a one-bus region has no modelled internal
  grid.

Both read a calibrated long table the *project* writes next to its results
(pypsa-wal: `csvs/network_costs_calibrated.csv`, columns
`network, view, layer, item, year, value`, layers `L1` existing, `L2` new,
`L3` OPEX, `L4` losses and add-ons, `memo` never added).  It describes one
region, named in the config:

```yaml
features:
  network_costs:
    node: BEWAL                                  # the region the table describes
    file: csvs/network_costs_calibrated.csv      # default
    segments_file: csvs/network_cost_segments.csv  # optional tie to the segment chart
    references:                                  # optional regulator totals
      electricity_distribution: [{year: 2025, value: 896.3, label: CWaPE allowed revenue 2025}]
```

Without it the distribution section is skipped and transmission shows the
branch view only — which is computed from the networks and works for any real
or group node (a branch with both ends inside a group counts in full, as
"inside"). See
[D23](docs/DESIGN_DECISIONS.md#d23--network-costs-are-shown-as-the-users-pay-them-with-the-model-view-beside).

### Point of attention — heat supply and district heating

The **Heat supply** page answers "how is the residential and tertiary heat
demand met": a pie by delivering technology, whose district-heating slice is
expanded into a second pie, what feeds the network (one view per horizon, with
the numbers in a table).  A heat bus shared with other users (agriculture on a
decentral bus, industrial low-temperature heat or DAC on the district-heating
bus) has its production shared out *pro rata* to each user's take, and heat
storage is left out (it only moves heat in time), so the left pie sums exactly
to the building heat load.  The pies sit in a fixed-size figure: a pie's radius
depends on the pixel size of its domain, and a responsive figure would move
the slice away from the connector lines.

The Dispatch page carries the **district-heating bus alone**, winter and
summer week, with CHP with and without capture, electric boilers and large heat
pumps kept apart, the charging and discharging of pit / tank storage, and the
storage level on a second axis.  Its display floor is 5 MW,
not the 0.1 GW of the regional charts, which would erase most of a
regional heat network.

A week cannot show what a thermal store does over a year, when electric heat
runs, or why a heat pump was not built, so the Heat supply page also tells the
network **as a year** (see
[D25](docs/DESIGN_DECISIONS.md#d25--district-heating-is-shown-as-a-year-and-why-not-built-is-an-earnings-figure)),
in four sections that open on the last horizon:

* **District heating over the year** — daily supply by source against the demand
  line, the store's filling and release, the energy in the store, and the
  electricity and heat prices, on one time axis;
* **Thermal store through the year** — the store's level as a share of its
  capacity, one panel per horizon, with its capacity, cycles per year and a table
  (a store that fills and empties several times a year is a buffer for spells of
  weeks, not a seasonal store);
* **Heat supplied at each electricity price** — the year sorted into ten equal
  groups of hours by electricity price, and what each source delivered in each
  (electric heat in the cheap groups, CHP in the dear ones);
* **Is electric heat worth building?** — what one more MW of each electric
  heating technology would earn at the solved prices against its annualised
  capital cost.  A technology that earns less than it costs is not built; one
  built at the margin earns exactly its cost, which is the built-in check.

The prices are the marginal prices (duals) of the solved network — the cost of
one more MWh in that hour, not a tariff — so the two sections that read them
(`dh_by_price`, `dh_electric_heat`) are shown for single regions only.

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

A scenario carrying `sensitivity:` is a **sweep point**: no pages of its own, and it
is never set beside the scenarios in the comparison. `build` writes only its summary
file. Each sweep becomes one section (`sensitivity_<id>`) of the **Sensitivity
analyses** page of the *comparison report*, read from the points' summaries, with two
views in a dropdown — the swept value on the x axis with one line per planning
horizon, and the same numbers over time with one line per swept value. With no
sweep declared the page does not exist.

A metric is the same object as a key figure (next section): any `source` of
[`metrics.py`](src/pypsa2html/metrics.py) works, `table:` being the older name of
`source:`. `rows:` are summed; omit it to sum the whole table. A row the run did not
build reads **zero** — a point on the curve. A point whose table is missing entirely
reads *missing*, so the line breaks instead of dropping to the origin.

`pypsa2html inspect` prints each sweep, its points and any that are unsolved. See
[D22](docs/DESIGN_DECISIONS.md#d22--a-parameter-sweep-is-a-section-not-a-set-of-scenarios).

### Key figures (KPIs)

A scenario report opens on its **Key figures**: a table of indicators across the
planning horizons. The comparison report shows the same indicators for every
scenario, one year at a time, with a *values / difference / difference (%)*
selector against the reference scenario, and writes them all to
`comparison.csv` (scenario × node × KPI × year, with the difference).

The indicators are declared, not coded. The packaged set,
[`data/kpis.yaml`](src/pypsa2html/data/kpis.yaml), covers the system cost and CO₂
price, emissions, CO₂ captured by origin and stored, final and primary energy,
self-sufficiency, electricity generation by technology, curtailment, nodal price
statistics, regional costs and capacities. A project's `kpis:` list is merged over
it **by id** — override a field, drop one with `enabled: false`, add its own, place
it with `after: <id>`. A KPI reads one of these sources:

| `source` | `kind` / `field` | rows are | default unit |
|---|---|---|---|
| `capacity` | `power` · `storage` · `ccs` | grouped technology names | GW / GWh |
| `cost` | `total` · `capital` · `marginal` · `clustered` | grouped technology names | EUR/year |
| `indicator` | `field:` an `Indicators` frame (`ghg_sector`, `fec_carrier`, …) | taxonomy codes | — |
| `self_sufficiency` | `ratio` · `primary` · `electricity` | `primary`, `electricity` / `domestic`, `net_import` | % / TWh |
| `flow` | `energy` · `co2` | graph edges `Source>Target>Type`, globs allowed | TWh / MtCO₂ |
| `balance` | a bus carrier, or `electricity` (AC + low voltage), `hydrogen`, `gas` | `supply:<carrier>`, `withdrawal:<carrier>`, `curtailment:<carrier>`, `potential:<carrier>` (raw PyPSA carriers) | TWh |
| `price` | same | `mean`, `load_weighted_mean`, `p05`, `median`, `p95`, `hours_above_100`, `hours_above_200`, `hours_below_1` | EUR/MWh, h |
| `global` | `objective` · `constraints` | constraint names; `{node}` allowed (`co2_limit_per_country{node}`) | EUR |
| `csv` | `file:` a long table in the results tree, `filters:` on its columns (`{node}` substituted); or `format: pypsa_summary` for `csvs/metrics.csv` and its siblings | — | — |
| `ratio` | `numerator` / `denominator`: KPI ids or inline metrics | — | % |

`balance` exists because the energy graph groups technologies for the Sankey
(CCGT with and without capture, ground and rooftop PV); a key figure needs the raw
carrier. `pypsa2html kpis --summary <dir>` lists the row labels a summary carries,
to write `rows:` against. See
[D24](docs/DESIGN_DECISIONS.md#d24--a-scenario-report-is-self-contained-the-comparison-reads-summaries).



### Output

**Scenario report** — one directory per scenario (`output.dir`, which may contain
`{scenario}`) holding `index.html` (redirecting to the scenario's landing page,
`<focus>_key_figures_<scenario>.html`), `<node>_<page>_<scenario>.html` for every
node and page, `<page>_<scenario>.html` for pages that do not depend on the region,
`graph_imbalances.csv`, and the two summary files:

| file | content |
|---|---|
| `summary.csv` | `node, table, row, year, value, unit` — every table the comparison reads (capacities, costs, indicator frames, self-sufficiency, energy and CO₂ graph edges, electricity balance and prices per node; objective and constraint duals under `_system`) and every KPI (`table = kpi`) |
| `summary.json` | schema and pypsa2html versions, build and solve dates, the scenario, horizons, nodes, every input file with its size and mtime, the number-changing settings, the KPI definitions' digests, the tables present and any table that failed |

When several scenarios share one output directory, their summaries go to
`summaries/<scenario>/`.

**Comparison report** — `compare.dir` (default `compare/` under the scenarios'
common output root) holding `<node>_key_figures_compare.html`,
`<node>_overview_compare.html`, `sensitivity_compare.html`,
`scenarios_compare.html`, `comparison.csv` and `index.html`. A `build` of several
scenarios also writes an `index.html` at their common root, pointing at it.

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
│   ├── metrics.py                 metric / KPI definitions, ScenarioData, evaluation
│   ├── summary.py                 summary.csv/json: writer, reader, freshness check
│   ├── build.py                   manifest × nodes → one scenario report (+ summary)
│   ├── compare.py                 summaries → the comparison report
│   ├── cli.py                     build / compare / summarize / kpis / inspect / pages
│   ├── extract/                   solved network → tidy flow tables
│   │   ├── flows.py                 energy flows
│   │   ├── emissions.py             carbon flows
│   │   ├── balance.py               nodal supply/demand time series, annual balance
│   │   ├── prices.py                nodal price statistics
│   │   └── ev.py                    EV charging: natural vs smart, V2G
│   ├── charts/                    tidy tables → plotly figures
│   │   ├── base.py                  shared chart helpers
│   │   ├── sankey.py  indicators.py      energy/carbon Sankeys, indicator charts
│   │   ├── results.py               costs, capacities, demands
│   │   ├── dispatch.py  maps.py     time series, geographic maps
│   │   ├── ev.py                    EV charging weeks + annual split
│   │   ├── keyfigures.py            key-figures table of one scenario
│   │   ├── comparison.py            comparison key figures, KPI charts, provenance
│   │   ├── scenario.py              comparison charts (with difference views)
│   │   └── sensitivity.py           parameter sweeps (config-declared)
│   ├── report/                    HTML assembly (jinja2 template + renderer)
│   └── data/                      the packaged taxonomy — CSV + YAML
└── tests/
    └── data/negawatt-ref/         116 kB of real négaWatt output as fixtures
```

**Data flow.** *Scenario report:* `config.yaml` + the solved networks →
`BuildContext` → `extract.*` produces tidy flow tables → `indicators` derives the
energy-system indicators → `charts.*` builds plotly figures → `report.render`
assembles pages from the manifest → `summary` writes the scenario's numbers.
*Comparison report:* the summary files of several scenarios →
`SummaryScenarioData` → `CompareContext` → `charts.comparison` /
`charts.scenario` / `charts.sensitivity` → the same `report.render`.

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
skip the rules otherwise. One rule per run writes that run's report and summary;
one rule compares the runs, taking their *summaries* as inputs — small files, so
the comparison is a real Snakemake dependency instead of a rule that reads sibling
trees behind the workflow's back:

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
            summary=RESULTS + "html/summary.json",
        log:
            RESULTS + "logs/pypsa2html.log",
        run:
            from pathlib import Path
            from pypsa2html import build_site, load_config
            name = config["run"]["name"]
            cfg = load_config(input.config, overrides={"output": {"dir": str(Path(output.index).parent)}})
            build_site(cfg.with_scenario(name, Path(output.index).parent.parent), scenarios=[name])

    rule generate_html_comparison:
        input:
            summaries=expand("results/{run}/html/summary.json", run=COMPARED_RUNS),
            config="config/pypsa2html.yaml",
        output:
            index="results/compare/index.html",
        run:
            from pathlib import Path
            from pypsa2html import compare_site, load_config
            compare_site(load_config(input.config),
                         [Path(p).parent for p in input.summaries],
                         out_dir=Path(output.index).parent)
```

Unlike the rules it replaces, this declares the inputs it actually reads. The
originals declared nine inputs of which four were never read, and read a further
nine files by hardcoded path that Snakemake therefore could not track. A complete
pair of rules is in [`examples/snakemake/`](examples/snakemake/).

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
| District heating as a year (store, electricity-price groups, earnings test) | `extract/district_heating` `charts/district_heating` | new, tested |
| EV charging (natural vs smart, V2G) | `extract/ev` `charts/ev` | new, tested |
| Self-sufficiency (primary energy, electricity) | `indicators` `charts/indicators` `charts/scenario` | new, tested |
| Sensitivity analyses (parameter sweeps) | `config` `pages` `charts/sensitivity` | new, tested |
| Scenario summaries, key figures, comparison report | `metrics` `summary` `compare` `charts/keyfigures` `charts/comparison` `extract/prices` | new, tested |

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
  every node three times (superseded 2026-10-12: the comparison reads summary
  files, and a scenario build releases its networks before the next one — D24).

**Memory bound (2026-10-06).** `model.network_cache_size` is now a real,
process-wide bound on the solved networks kept in RAM. The path cache is the only
owner of loaded networks; each scenario's `NetworkCache` is an index into it.
Before, the path cache kept up to 24 networks whatever the setting, and every
`NetworkCache` held its own references on top.
* `null` keeps the default of 24 (no reloading).
* `2` caps RAM at about two networks.
* Measured on pypsa-wal, one scenario with four 1 h networks of about 2.5 GB each:
  7.8 GB peak and 36 min with the bound at 2, against more than 10 GB (OOM in a 10 GB cgroup)
  without it.
* ~~A one-scenario build still reads every configured scenario whose results
  tree exists on disk, for the scenario-comparison charts.~~ Since 2026-10-12 a
  scenario build reads its own networks only; the comparison reads the
  scenarios' summary files and flags mixed vintages (D24).
* **2026-10-12: the bound now holds.** Two things kept evicted networks in RAM:
  a `pypsa.Network` is a reference cycle, freed only when the cyclic collector
  happens to run (now forced on eviction), and the per-horizon `NodeResolver`
  cached for the whole build held its network (now a weak reference, reloaded
  through the cache when needed). pypsa-wal `scen_central`, bound 2: Sankey pages
  **14.7 GB → 2.7 GB** peak (116 loads, same time); a full scenario build stays at
  2.7 GB over 330 loads. Unbounded builds are unchanged (3.4 GB).

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
