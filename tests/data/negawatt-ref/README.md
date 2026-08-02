# Vendored test dataset — négaWatt `ref` scenario

A small extract of a real PyPSA-Eur run, so the test suite exercises the
library against genuine model output without needing a solved network.

Source: `pypsa-eur_negawatt`, run `ref`, clustering `base_s_adm__24h`,
horizons 2030/2040/2050 (plus a 2020 historical column), solved 2026-06-02.

| Path | What it is | Produced by |
|---|---|---|
| `flows/{BE,EU}_energy.csv` | Energy flow table, TWh/year | `results/ref/sepia/inputs{NODE}.xlsx`, sheet `Inputs` |
| `flows/{BE,EU}_carbon.csv` | Carbon flow table, MtCO2/year | same file, sheet `Inputs_co2` |
| `country_csvs/*.csv` | Per-node cost, capacity and demand tables | `results/ref/country_csvs/` |

The flow CSVs are the legacy workbooks converted to the long format documented
in `docs/INTERNALS.md` §3 — `target` renamed to `code`, `source` (which held
the *unit*, confusingly) renamed to `unit`, and the year columns coerced to
float. They are therefore both a fixture for the chart layer and the reference
the extraction layer is validated against.

Why CSV rather than `.nc`: see `docs/DESIGN_DECISIONS.md` D11. In short, a
solved sector-coupled network is 10–190 MB and the full run is 640 MB; these
files are 116 kB and readable in a code review.

Tests that genuinely need a solved network are marked `needs_model` and skip
automatically when it is absent.
