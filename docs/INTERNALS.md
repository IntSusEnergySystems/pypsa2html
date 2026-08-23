# Internals — the contract between layers

This is the reference for anyone adding or porting a chart. It describes the
objects every builder receives and the shape every builder must return.

```
config.yaml ─┐
             ├─► BuildContext ─► extract.* ─► DataFrames ─► charts.* ─► Figures ─► report.render ─► HTML
data/*.csv ──┘                      │                                                    ▲
                                    └────────────── pages.yaml (manifest) ───────────────┘
```

## 1. `BuildContext` (`pypsa2html/context.py`)

Constructed once per scenario by `build_context(config, scenario_name)`. It
replaces the `snakemake` global and the ~15 module globals the legacy scripts
set in their `__main__` blocks.

| Attribute | Type | Notes |
|---|---|---|
| `config` | `Config` | the validated YAML config |
| `scenario` | `ScenarioConfig` | `.name`, `.label`, `.results_dir` |
| `taxonomy` | `Taxonomy` | the validated CSV tables |
| `nodes` | `NodeSet` | iterable of `Node(code, label, aggregate, members)` |
| `networks` | `NetworkCache` | `ctx.networks[2030]` → `pypsa.Network`, lazy + LRU |
| `results_dir`, `resources_dir` | `Path` | absolute |
| `horizons` | `list[int]` | discovered or configured; **never hardcode years** |
| `year_columns` | `list[str]` | `horizons` as strings, plus `base_year` if set |

Methods:

- `ctx.horizon_weights() -> Series` — years represented by each horizon, for
  cumulative sums. Derived from the gaps between horizons. **Never write
  `*= 10`.** Annual GHG series are plotted as annual; cumulative charts
  multiply by these weights, not by a decade hack.
- `ctx.resolver(horizon) -> NodeResolver` — see §2.
- `ctx.is_aggregate(node) -> bool` — true for the study-wide sum *and* for
  every group aggregate. Replaces `if country != 'EU'`.
- `ctx.is_study_wide(node) -> bool` — true only when `node` is the unfiltered
  sum of every real location (`members is None`, or a group equal to all
  real codes). Use this for closed-system algebra (no trade), not
  `is_aggregate`.
- `ctx.members_of(node) -> list[str] | None` — `None` for a real node and for
  the study-wide aggregate; a **sorted** member list for a group.
- `ctx.locations_for(node) -> list[str] | None` — locations to keep.
  `None` means do not filter (study-wide). A list is the exact codes.
- `ctx.component_index(horizon, component, node) -> pd.Index` — the single
  membership helper. Charts and extractors must not fork this logic.
- `ctx.read_csv(relpath, base='results'|'resources', **kw)` — cached; returns
  `None` if the file is missing (do **not** let that raise).
- `ctx.read_excel(...)` — same.
- `ctx.cost(technology, parameter, default=None) -> float` — one scalar cost
  assumption. Handles both the wide and long cost-table layouts.

## 2. `NodeResolver` (`pypsa2html/nodes.py`)

Maps PyPSA components to nodes. **Never use `.filter(like=country)` or
`.str[:2]` in new code.** There is no prefix matching: `BE` is never treated
as matching location `BEWAL`. Group membership is always an explicit
`members` list in the config.

```python
r = ctx.resolver(horizon)
r.mask("links", "BEWAL")               # boolean Series over n.links.index
r.select("generators", "BEWAL")        # Index of matching generator names
r.select_locations("links", ["BEWAL", "BEVLG"])
r.bus_nodes(n.links.bus1)              # Series of bus names -> node codes
```

Use `ctx.component_index` instead of branching on `is_aggregate`:

```python
sel = ctx.component_index(h, "links", node)
# real node          → that location
# group (members)    → those locations
# study-wide         → every row (no filter)
```

If a group's `code` is already a detected location, the group is omitted with
a warning (the real node already has pages). Missing members are skipped the
same way. There is no ISO2 / prefix fallback.

## 3. Extraction (`pypsa2html/extract/`)

Extractors turn networks into tidy frames. They take `(ctx, node)` and return
a DataFrame — they never write files and never touch `snakemake`.

```python
def energy_flows(ctx: BuildContext, node: str) -> pd.DataFrame: ...
def carbon_flows(ctx: BuildContext, node: str) -> pd.DataFrame: ...
```

Both return the *long* flow table:

| column | meaning |
|---|---|
| `code` | internal indicator code (`proelcnuc`, `emmccgt`, …) — the join key |
| `label` | human-readable description |
| `unit` | `TWh` or `MtCO2` |
| one column per entry in `ctx.year_columns` | the value, `float` |

Rules:

- Column names come from `ctx.year_columns`. Never write `['2020','2030','2040','2050']`.
- A carrier absent from the model yields `0.0`, not a missing row.
- Values below `ctx.config.model.flow_threshold` are dropped **by magnitude**
  (`abs(value) < threshold`) so net-negative CO2 rows survive. The legacy
  `value >= 0.1` test deleted every negative value.

Two extractors return **time-indexed** frames instead of the long flow table,
because their charts are time series rather than year columns:

| Module | Returns | Unit |
|---|---|---|
| `extract/balance.py::energy_balance` | snapshots × technology group | MW |
| `extract/ev.py::charging_profiles` | `EVCharging` — snapshots × charging mode, plus weights, state of charge and the shape provenance | MW (%) |

They obey the same rules: node membership through `ctx.resolver` /
`ctx.locations_for`, missing input → warning and `None`, no year or node
literals. Both are windowed by the caller (`start` / `stop`), and both are
sign-consistent: positive means "drawn from the bus being balanced".

## 4. Charts (`pypsa2html/charts/`)

One module per family; the manifest's `builder` field is `<module>.<function>`
resolved relative to `pypsa2html.charts`.

```python
def my_chart(ctx: BuildContext, node: str, section: Section) -> ChartResult
```

`ChartResult` is either:

- a `plotly.graph_objects.Figure`, or
- `charts.base.Html(fragment)` for content that is already HTML (maps,
  dispatch tabs), or
- `None` to skip the section (the page shows a named placeholder).

Builders must:

- take colours from `ctx.taxonomy.colors` or the tech-colour table, and
  **never mutate** the shared palette dict — copy first;
- read the unit from `section.unit`, not a literal;
- raise nothing for missing inputs; return `None` and log a warning.

## 5. Page assembly (`pypsa2html/report/render.py`)

`build.py` walks the manifest, calls each builder, wraps the result in a
`RenderedSection`, and renders one file per `(node, page)`. The section `id`
is simultaneously the HTML anchor, the `plots:` toggle key and the `texts:`
narrative key — there is exactly one vocabulary.

Navigation (region selector, scenario selector, page list) is generated from
the node set, scenario list and manifest by the Jinja template. There is no
hardcoded JavaScript per node or per scenario.

## 6. Rules of thumb for porting

1. No `snakemake` anywhere below `cli.py` / the Snakemake shim.
2. No year literals; no node-code literals; no `'EU'` magic string.
3. No `groupby(..., axis=1)` — use `.T.groupby(...).sum().T` (pandas 3-safe).
   replacements valid in pandas ≥2.1 (`df.T.groupby(level).sum().T`).
4. No mutation of arguments, of `ctx.taxonomy`, or of the plotting palette.
5. Missing input → warning + `None`, never `NameError`.
6. If a value has to be looked up per technology, it belongs in a CSV under
   `data/`, not in an `if/elif` chain.
