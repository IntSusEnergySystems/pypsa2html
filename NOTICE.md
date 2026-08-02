# Provenance and licensing

`pypsa2html` is a restructured successor to the **SEPIA** scripts
(*Simplified Energy Prospective and Interterritorial Analysis tool*, v1.8)
developed by Adrien Jacob for the **négaWatt Association** as part of the
CLEVER project, and subsequently extended for PyPSA-Eur by Umair Tareen and
Sylvain Quoilin.

Those scripts carried `__license__ = "GPL"`, so this repository is released
under the **GNU General Public License v3.0 or later** (see `LICENSE`).

## What was inherited

| Component | Origin |
|---|---|
| `data/nodes.csv`, `data/processes_*.csv`, `data/indicators.csv` | `SEPIA_config.xlsx` sheets `NODES`, `PROCESSES{,_2,_3}`, `INDICATORS` |
| `data/regions.csv` | `COUNTRIES.xlsx`, sheet `COUNTRIES` |
| `data/domestic_{gas,oil}_production.csv` | `SEPIA_config.xlsx` sheets `GAS_PRO`, `OIL_PRO` |
| `data/carrier_flows_*.csv` | the `entries_to_select` / `entry_label_mapping` literals in `excel_generator.py` |
| Sankey, flow-algebra and indicator logic | `SEPIA.py`, `SEPIA_functions.py`, `SEPIA_additional_functions.py` |
| Cost/capacity/dispatch/map chart logic | `Pypsa_results.py`, `Dispatch_plots_weekly.py`, `scenario_results.py` |

Some map-plotting helpers in the original were verbatim copies of PyPSA-Eur's
own `scripts/plot_{power,hydrogen,gas}_network.py` (MIT-licensed, © PyPSA-Eur
authors). `pypsa2html` imports those from PyPSA-Eur where available rather
than vendoring them.

> **Action required before publishing.** Confirm the GPL-3.0 choice with
> négaWatt, since the inherited configuration tables and Sankey logic are
> substantially their work.
