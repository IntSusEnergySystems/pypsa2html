"""Command-line interface.

    pypsa2html build     --config config/pypsa-wal.yaml [-s SCENARIO ...]
    pypsa2html compare   --config config/pypsa-wal.yaml [RESULTS_DIR ...]
    pypsa2html summarize --config config/pypsa-wal.yaml [-s SCENARIO ...]
    pypsa2html inspect   --config config/pypsa-wal.yaml
    pypsa2html kpis      --config config/pypsa-wal.yaml [--summary DIR]
    pypsa2html pages

``build`` writes one report per scenario (pages + summary files) and, when it
builds two or more, the comparison report.  ``compare`` builds the comparison
alone, from the summary files of any set of result folders.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", "-c", required=True, type=Path, help="project YAML config")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="override the directory relative paths resolve against",
    )
    parser.add_argument("--verbose", "-v", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pypsa2html",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="generate the HTML report")
    _add_common(p_build)
    p_build.add_argument(
        "--scenario",
        "-s",
        action="append",
        dest="scenarios",
        help="build only this scenario (repeatable); default: all",
    )
    p_build.add_argument(
        "--only",
        action="append",
        dest="only_pages",
        metavar="PAGE",
        help="write only this page id (repeatable); the left nav still lists "
        "every page in output.pages",
    )
    p_build.add_argument("--output", "-o", type=Path, default=None, help="override output.dir")
    p_build.add_argument(
        "--no-compare",
        dest="compare",
        action="store_const",
        const=False,
        default=None,
        help="do not build the comparison report, even for several scenarios",
    )

    p_compare = sub.add_parser(
        "compare",
        help="build the comparison report from the scenarios' summary files",
    )
    _add_common(p_compare)
    p_compare.add_argument(
        "sources",
        nargs="*",
        type=Path,
        metavar="DIR",
        help="results trees, HTML folders or summary.json files to compare; "
        "default: compare.scenarios, else every configured scenario",
    )
    p_compare.add_argument(
        "--scenario", "-s", action="append", dest="scenarios",
        help="compare this configured scenario (repeatable)",
    )
    p_compare.add_argument("--reference", "-r", default=None,
                           help="scenario the differences are taken against")
    p_compare.add_argument("--output", "-o", type=Path, default=None,
                           help="output directory (default: compare.dir)")
    p_compare.add_argument(
        "--recompute", action="store_true",
        help="rebuild a missing, stale or incompatible summary from its results tree",
    )
    p_compare.add_argument(
        "--write-summaries", action="store_true",
        help="with --recompute, also write the rebuilt summaries to disk",
    )
    p_compare.add_argument("--only", action="append", dest="only_pages", metavar="PAGE",
                           help="write only this comparison page (repeatable)")

    p_summarize = sub.add_parser(
        "summarize", help="write the summary files only (no pages)"
    )
    _add_common(p_summarize)
    p_summarize.add_argument(
        "--scenario", "-s", action="append", dest="scenarios",
        help="summarize only this scenario (repeatable); default: all, sweep points included",
    )

    p_kpis = sub.add_parser("kpis", help="list the key figures (KPIs) of a config")
    _add_common(p_kpis)
    p_kpis.add_argument(
        "--summary", type=Path, default=None,
        help="also list the tables and row labels this summary carries "
        "(a summary.json or the folder holding it)",
    )
    p_kpis.add_argument("--node", default=None, help="node whose rows to list (default: focus)")

    p_inspect = sub.add_parser(
        "inspect", help="show detected nodes, horizons and pages without building"
    )
    _add_common(p_inspect)

    sub.add_parser("pages", help="list the section ids available for the 'plots:' config")
    return parser


def _load(args):
    from .config import load_config

    overrides: dict = {}
    if getattr(args, "output", None) and args.command == "build":
        overrides["output"] = {"dir": str(args.output)}
    if args.root:
        overrides["root"] = str(args.root)
    return load_config(args.config, overrides or None)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    # pypsa/matplotlib are extremely chatty at INFO
    for noisy in ("pypsa", "matplotlib", "numexpr", "fiona", "rasterio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.command == "pages":
        from .pages import load_manifest

        for site, title in (("scenario", "scenario report (output.pages)"),
                            ("compare", "comparison report (compare.pages)")):
            print(f"# {title}")
            for page in load_manifest(site=site):
                print(f"{page.id}:")
                for section in page.sections:
                    print(f"    {section.id:26s} {section.title}")
        return 0

    try:
        config = _load(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "inspect":
        from .context import build_context
        from .pages import load_manifest

        manifest = load_manifest(
            enable=config.plots,
            include_pages=config.output.pages,
            sensitivities=config.sensitivities,
        )
        print(f"project   : {config.project.name}")
        print(f"root      : {config.root}")
        if config.output_is_per_scenario:
            print(f"output    : {config.output.dir}  (one folder per scenario)")
            print(f"entry     : {config.common_output_root() / 'index.html'}")
        else:
            print(f"output    : {config.output_dir()}")
        print(f"landing   : {config.landing.node or config.nodes.focus} / "
              f"{config.landing_scenario.name} / {config.landing.page}")
        from .summary import check_summary, read_meta

        print(f"compare   : {config.compare_dir()}  (pages: {', '.join(config.compare.pages)})")
        print(f"kpis      : {len(config.kpis)}")
        for scenario in config.report_scenarios:
            print(f"\nscenario '{scenario.name}' ({scenario.label})")
            summary = config.summary_dir(scenario.name) / "summary.json"
            status = check_summary(read_meta(summary), config.results_dir(scenario.name), config)
            print(f"    summary  : {status.status}"
                  f"{' (' + '; '.join(status.reasons[:2]) + ')' if status.reasons else ''}")
            try:
                ctx = build_context(config, scenario.name)
            except (OSError, ValueError) as exc:
                print(f"    UNAVAILABLE: {exc}")
                continue
            print(f"    results  : {ctx.results_dir}")
            print(f"    html     : {config.output_dir(scenario.name)}")
            print(f"    horizons : {ctx.horizons}")
            print(f"    nodes    : {', '.join(f'{n.code}({n.label})' for n in ctx.nodes)}")
            print(f"    focus    : {ctx.nodes.focus}")
        for sweep in config.sensitivities:
            points = config.sweep_points(sweep.id)
            values = ", ".join(p.sensitivity.label for p in points)
            print(f"\nsensitivity '{sweep.id}' ({sweep.label})")
            print(f"    parameter: {sweep.parameter.label} [{sweep.parameter.unit}]")
            print(f"    nodes    : {', '.join(sweep.nodes) or config.nodes.focus}")
            print(f"    metrics  : {', '.join(m.label for m in sweep.metrics)}")
            print(f"    points   : {len(points)} ({values})")
            missing = [
                p.name for p in points if not config.results_dir(p.name).exists()
            ]
            if missing:
                print(f"    UNSOLVED : {', '.join(missing)}")

        print(f"\npages: {len(manifest)}")
        for page in manifest:
            print(f"    {page.id:12s} {len(page.sections):2d} section(s)"
                  f"{'  [shared]' if page.shared else ''}")
        return 0

    if args.command == "build":
        from .build import build_site

        try:
            report = build_site(
                config, scenarios=args.scenarios, only_pages=args.only_pages,
                compare=args.compare,
            )
        except KeyError as exc:
            print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
            return 2
        return _print_report(report)

    if args.command == "compare":
        from .compare import compare_site

        try:
            report = compare_site(
                config,
                [str(p) for p in args.sources] or None,
                names=args.scenarios,
                reference=args.reference,
                out_dir=args.output,
                recompute=args.recompute,
                write_summaries=args.write_summaries,
                only_pages=args.only_pages,
            )
        except (OSError, ValueError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        code = _print_report(report)
        if report.index is not None:
            print(f"comparison: {report.index}")
        return code

    if args.command == "summarize":
        from .build import summarize_scenario
        from .networks import set_path_cache_max

        set_path_cache_max(getattr(config.model, "network_cache_size", None))
        names = args.scenarios or config.scenario_names
        written = 0
        for name in names:
            path = summarize_scenario(config, name)
            if path is not None:
                print(f"wrote {path}")
                written += 1
        set_path_cache_max(None)
        return 0 if written == len(names) else 1

    if args.command == "kpis":
        return _list_kpis(config, args)

    return 2


def _print_report(report) -> int:
    print(report.summary())
    if report.failed:
        print(f"\n{len(report.failed)} section(s) rendered as placeholders:", file=sys.stderr)
        for scenario, node, section in report.failed[:20]:
            print(f"    {scenario}/{node}/{section}", file=sys.stderr)
    return 0


def _list_kpis(config, args) -> int:
    from .charts.keyfigures import grouped

    for group, members in grouped(config.kpis):
        print(f"{group}:")
        for kpi in members:
            what = kpi.table_key or (f"csv {kpi.file}" if kpi.source == "csv" else kpi.source)
            rows = f" rows={kpi.rows}" if kpi.rows else ""
            print(f"    {kpi.id:24s} {kpi.label.strip()} [{kpi.unit}]  <- {what}{rows}")
    if args.summary is None:
        return 0
    from .summary import SummaryScenarioData

    data = SummaryScenarioData.load(args.summary)
    node = args.node or data.meta.get("focus")
    print(f"\nsummary of {data.name} -- tables of node {node}:")
    for key in sorted(data.meta.get("tables", {}).get(node, [])):
        table = data.table(node, key)
        rows = list(table.index) if table is not None else []
        print(f"  {key} ({len(rows)} rows)")
        for row in rows:
            print(f"      {row}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
