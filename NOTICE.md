# Provenance and licensing

`pypsa2html` is released under the **MIT License** (see `LICENSE`).

The packaged taxonomy tables (`data/nodes.csv`, `data/processes_*.csv`,
`data/indicators.csv`, `data/regions.csv`, `data/domestic_*.csv`,
`data/carrier_flows_*.csv`) and parts of the Sankey / flow-algebra logic
originate from work developed for the **négaWatt Association** as part of the
CLEVER project, later extended for PyPSA-Eur studies.

Some map-plotting helpers in earlier code were verbatim copies of PyPSA-Eur's
own `scripts/plot_{power,hydrogen,gas}_network.py` (MIT-licensed, © PyPSA-Eur
authors). `pypsa2html` imports those from PyPSA-Eur where available rather
than vendoring them.
