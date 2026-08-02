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
`pandas` ≥2.1 `<3.0`, `numpy`, `plotly` ≥5.18, `jinja2`, `pyyaml`, `openpyxl`,
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

Two worked examples ship in [`config/`](config/): `negawatt.yaml` and
`pypsa-wal.yaml`.

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
Classification uses the solved network topology, so new technologies are
handled without a per-carrier denylist.  Set `features.capacity_filter: off`
only if you intentionally want the unfiltered CSV.  An optional `__omit__`
group in `tech_groups.csv` remains available as an extra name denylist.

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
│   ├── indicators.py              flow algebra (fec, pec, ghg, coverage ratios)
│   ├── build.py                   manifest × nodes × scenarios → files
│   ├── cli.py                     build / inspect / pages
│   ├── extract/                   solved network → tidy flow tables
│   │   ├── flows.py                 energy flows
│   │   └── emissions.py             carbon flows
│   ├── charts/                    tidy tables → plotly figures
│   │   ├── base.py                  shared chart helpers
│   │   ├── sankey.py  indicators.py      energy/carbon Sankeys, indicator charts
│   │   ├── results.py               costs, capacities, demands
│   │   ├── dispatch.py  maps.py     time series, geographic maps
│   │   └── scenario.py              multi-scenario overview
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
| Build loop, CLI, rendering | `build` `cli` `report` | done, tested |
| Energy / carbon extraction | `extract/flows` `extract/emissions` | in progress |
| Indicators, Sankeys, indicator charts | `indicators` `charts/base` `charts/sankey` `charts/indicators` | in progress |
| Costs, capacities, demands | `charts/results` | not started |
| Dispatch, maps, scenario overview | `charts/dispatch` `charts/maps` `charts/scenario` | not started |

---

## Known inefficiencies and future work

Deliberately **not** fixed during the port, to keep it reviewable. Recorded here
so they are not lost. Roughly in value order.

### Computational

1. **Extract once per node, not once per (node × horizon).** Extraction re-runs
   the full matmul/groupby pipeline per region — 18 times for a 6-region,
   3-horizon run — when a single pass grouped by node would do. The single
   biggest win available.
2. **Vectorise the per-link loss calculation.** `n.links.apply(calculate_losses, axis=1)`
   is a Python-level row apply over every link, repeated per call.
3. **Vectorise cross-border flow accounting.** `for t in flows.index: for line in flows.columns:`
   with a `.at[]` write per cell — O(8760 × n_lines) per country per horizon per carrier.
4. **Deduplicate the energy-balance prologue.** ~110 identical lines appear in
   five places (`plot_series_power`, `_cluster`, `_heat`, and twice in
   `Dispatch_plots_weekly`). One `energy_balance(n, carrier, node)` replaces all
   of them — and is where several dispatch-plot bugs live.
5. **Render maps once.** `create_map_plots` and its two siblings render every
   figure twice; the first loop computes a base64 image and discards it.
   ~45 of 48 network deep-copies are wasted.
6. **Cache the geojson and cost tables.** The regions geojson was re-read on
   every map call (42 reads); `nodal_costs.csv` three times; the costs CSV once
   per transmission call inside a double loop. `BuildContext.read_csv` caches
   now, but the call sites could avoid the reads entirely.
7. **Replace quadratic accumulation.** `pd.concat` inside 170- and 70-iteration
   loops, and `df.loc[len(df)] = ...` row appends.
8. **Stop embedding maps as base64.** 21.7 MB per maps page, and `raw_html/`
   reached 204 MB. Writing PNGs alongside the HTML would cut this by ~90%.

### Structural

9. **Collapse the four tech taxonomies.** `rename_techs_tyndp`, `_tynd`,
   `_tyndpp` and `rename_techs_ty` overlap heavily and disagree in places. They
   are data, not code — one CSV with named views.
10. **Merge the four near-identical bar-chart builders** (85–94% identical) into
    one parameterised `stacked_bar`.
11. **Import upstream map code instead of vendoring it.** `group_pipes` is
    byte-identical to PyPSA-Eur's `plot_hydrogen_network.py`; the three map
    functions are 50–70% copies of upstream.
12. **Give maps a per-node view.** Currently one shared page (D8); highlighting
    the selected region would make a per-node page meaningful again.
13. **A synthetic tiny network fixture**, so the extraction layer can be tested
    without a 45 MB `.nc` (D11).
14. **`pandas` 3.0 readiness.** `groupby(axis=1)` is removed there; ported code
    avoids it, but the pin is `<3.0` until the whole port is done.
15. **Per-node economic metadata** (GDP denominators for the cost map) is still
    a literal dict; it belongs in `regions.csv`.

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
