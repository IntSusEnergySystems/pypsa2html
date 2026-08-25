"""Command-line interface.

    pypsa2html build   --config config/pypsa-wal.yaml
    pypsa2html inspect --config config/pypsa-wal.yaml
    pypsa2html pages
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
    parser = argparse.ArgumentParser(prog="pypsa2html", description=__doc__)
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

    p_inspect = sub.add_parser(
        "inspect", help="show detected nodes, horizons and pages without building"
    )
    _add_common(p_inspect)

    sub.add_parser("pages", help="list the section ids available for the 'plots:' config")
    return parser


def _load(args):
    from .config import load_config

    overrides: dict = {}
    if getattr(args, "output", None):
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

        for page in load_manifest():
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

        manifest = load_manifest(enable=config.plots, include_pages=config.output.pages)
        print(f"project   : {config.project.name}")
        print(f"root      : {config.root}")
        if config.output_is_per_scenario:
            print(f"output    : {config.output.dir}  (one folder per scenario)")
            print(f"entry     : {config.common_output_root() / 'index.html'}")
        else:
            print(f"output    : {config.output_dir()}")
        print(f"landing   : {config.landing.node or config.nodes.focus} / "
              f"{config.landing_scenario.name} / {config.landing.page}")
        for scenario in config.scenarios:
            print(f"\nscenario '{scenario.name}' ({scenario.label})")
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
        print(f"\npages: {len(manifest)}")
        for page in manifest:
            print(f"    {page.id:12s} {len(page.sections):2d} section(s)"
                  f"{'  [shared]' if page.shared else ''}")
        return 0

    if args.command == "build":
        from .build import build_site

        report = build_site(
            config, scenarios=args.scenarios, only_pages=args.only_pages
        )
        print(report.summary())
        if report.failed:
            print(f"\n{len(report.failed)} section(s) rendered as placeholders:", file=sys.stderr)
            for scenario, node, section in report.failed[:20]:
                print(f"    {scenario}/{node}/{section}", file=sys.stderr)
        return 0

    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
