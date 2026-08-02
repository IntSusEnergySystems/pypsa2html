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
| `nodes` | `NodeSet` | iterable of `Node(code, label, aggregate)` |
| `networks` | `NetworkCache` | `ctx.networks[2030]` → `pypsa.Network`, lazy + LRU |
| `results_dir`, `resources_dir` | `Path` | absolute |
| `horizons` | `list[int]` | discovered or configured; **never hardcode years** |
| `year_columns` | `list[str]` | `horizons` as strings, plus `base_year` if set |

Methods:

- `ctx.horizon_weights() -> Series` — years represented by each horizon, for
  cumulative sums. Derived from the gaps between horizons. **Never write
  `*= 10`.**
- `ctx.resolver(horizon) -> NodeResolver` — see §2.
- `ctx.is_aggregate(node) -> bool` — replaces every `if country != 'EU'`.
- `ctx.read_csv(relpath, base='results'|'resources', **kw)` — cached; returns
  `None` if the file is missing (do **not** let that raise).
- `ctx.read_excel(...)` — same.
- `ctx.cost(technology, parameter, default=None) -> float` — one scalar cost
  assumption. Handles both the wide and long cost-table layouts.

## 2. `NodeResolver` (`pypsa2html/nodes.py`)

Maps PyPSA components to nodes. **Never use `.filter(like=country)` or
`.str[:2]` in new code.**

```python
r = ctx.resolver(horizon)
r.mask("links", "BEWAL")               # boolean Series over n.links.index
r.select("generators", "BEWAL")        # Index of matching generator names
r.bus_nodes(n.links.bus1)              # Series of bus names -> node codes
```

For the aggregate node there is no mask: skip the filter entirely.

```python
if ctx.is_aggregate(node):
    sel = static.index                      # everything
else:
    sel = ctx.resolver(h).select("links", node)
```

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
