# Optional pypsa2html integration for a PyPSA-Eur workflow.
#
# Drop this file next to your other rule files and add to the Snakefile:
#
#     include: "rules/pypsa2html.smk"
#
# then add `rules.generate_html_report.output` to your `all` rule's input (or
# just run `snakemake generate_html_report`).
#
# If pypsa2html is not installed the rule is not defined at all, so the rest of
# the workflow is unaffected -- this is the "if the library is available it is
# loaded" behaviour.

try:
    import pypsa2html as _pypsa2html

    HAVE_PYPSA2HTML = True
except ImportError:
    HAVE_PYPSA2HTML = False
    print(
        "pypsa2html not installed -- HTML report rule disabled. "
        "Install with: pip install -e /path/to/pypsa2html --no-deps"
    )


if HAVE_PYPSA2HTML:

    # Path to the project config. Keep it in the model repo, not in the
    # library, so the two can be versioned independently.
    PYPSA2HTML_CONFIG = config.get("pypsa2html", {}).get("config", "config/pypsa2html.yaml")

    rule generate_html_report:
        """Build the interactive HTML report from the solved networks.

        Unlike the legacy rules this replaces, every file read is declared
        here. The originals declared nine inputs of which four were never
        read, and read a further nine by hardcoded path that Snakemake could
        therefore neither track nor clean.
        """
        params:
            config_file=PYPSA2HTML_CONFIG,
            scenario=lambda w: config["run"]["name"],
        input:
            networks=expand(
                RESULTS
                + "networks/base_s_{clusters}_{opts}_{sector_opts}_{planning_horizons}.nc",
                **config["scenario"],
                allow_missing=True,
            ),
            config_file=PYPSA2HTML_CONFIG,
        output:
            index=RESULTS + "html/index.html",
        log:
            RESULTS + "logs/pypsa2html.log",
        benchmark:
            RESULTS + "benchmarks/pypsa2html"
        threads: 1
        resources:
            mem_mb=8000,
        conda:
            "../envs/environment.yaml"
        run:
            import logging
            from pathlib import Path

            from pypsa2html import build_site, load_config

            logging.basicConfig(
                filename=log[0],
                level=logging.INFO,
                format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            )

            cfg = load_config(
                params.config_file,
                # Write next to the rest of this run's results.
                overrides={"output": {"dir": str(Path(output.index).parent)}},
            )
            report = build_site(cfg, scenarios=[params.scenario])
            logging.info(report.summary())


    rule generate_html_report_all_scenarios:
        """Cross-scenario report, including the comparison overview page.

        Run this after every scenario has been solved. It needs each
        scenario's results directory to exist, which Snakemake cannot express
        here because the scenarios live in sibling run directories -- so this
        rule is deliberately not wired into `all`.
        """
        params:
            config_file=PYPSA2HTML_CONFIG,
        output:
            index="results/html/index.html",
        log:
            "logs/pypsa2html_all.log",
        threads: 1
        run:
            import logging
            from pathlib import Path

            from pypsa2html import build_site, load_config

            logging.basicConfig(filename=log[0], level=logging.INFO)
            cfg = load_config(
                params.config_file,
                overrides={"output": {"dir": str(Path(output.index).parent)}},
            )
            logging.info(build_site(cfg).summary())
