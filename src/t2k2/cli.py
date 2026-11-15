"""``t2k2-harvest`` entrypoint.

Exit codes follow the T2 droid contract: 0 success, 1 the run failed (a Trading212 API error, or
a sell order that couldn't be placed or cancelled), 2 configuration or strategy error, 3 skipped
(a lock is held). Every run writes a JSON run report.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import structlog
from dotenv import load_dotenv

from .client import Trading212Client, Trading212Error
from .config import ConfigError, load_settings
from .harvest import HarvestReport, run_harvest
from .market import YahooPriceHistory
from .strategy import StrategyError, load_strategy
from .t2contract import Exit, RunReport, Skipped, run_lock, t212_lock

DROID = "t2-k2"

EXIT_OK = Exit.OK
EXIT_FAILED = Exit.FAILED
EXIT_CONFIG = Exit.CONFIG
EXIT_SKIPPED = Exit.SKIPPED


def _configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    # httpx logs every request at INFO; our own structured logs already cover what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="t2k2-harvest",
        description="Find overbought Trading212 holdings and harvest part of their gains. "
        "Dry run unless --execute is given.",
    )
    parser.add_argument(
        "--execute", action="store_true", help="place the sell orders (default: dry run)"
    )
    parser.add_argument(
        "--strategy", type=Path, help="strategy YAML (default: T2K2_STRATEGY_FILE or "
        "config/strategy.yaml)"
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="write the JSON run report here (default: $T2_RUNS_DIR or var/runs)",
    )
    return parser.parse_args(argv)


def format_report(report: HarvestReport) -> str:
    """Render a report as a plain-text table for the terminal."""
    mode = "EXECUTE" if report.execute else "DRY RUN (pass --execute to place orders)"
    lines = [f"T2-K2 harvest — {mode} — {report.started_at:%Y-%m-%d %H:%M UTC}", ""]
    header = f"{'TICKER':<16}{'ACTION':<9}{'GAIN%':>8}  {'ORDER':<42}  REASON"
    for c in report.cancellations:
        placed = "?" if c.order.created_at is None else f"{c.order.created_at:%Y-%m-%d}"
        error = "" if c.error is None else f" | ERROR: {c.error}"
        lines.append(
            f"Expired sell order {c.order.id} for {c.order.ticker} (placed {placed}): "
            f"{c.status.value}{error}"
        )
    if report.cancellations:
        lines.append("")
    lines += [header, "-" * len(header)]
    for o in report.outcomes:
        d = o.decision
        order = ""
        if d.order is not None:
            price = "MKT" if d.order.limit_price is None else f"@{d.order.limit_price}"
            status = o.order_status.value if o.order_status else ""
            order = f"sell {d.order.quantity} {price} ~{d.order.estimated_value} {status}"
        reason = d.reason if o.error is None else f"{d.reason} | ERROR: {o.error}"
        lines.append(f"{d.ticker:<16}{d.action.value:<9}{d.gain_pct:>8}  {order:<42}  {reason}")
    return "\n".join(lines)


def _version() -> str:
    try:
        return version("t2-k2")
    except PackageNotFoundError:
        return "unknown"


def _summary(report: HarvestReport) -> str:
    orders = sum(o.decision.order is not None for o in report.outcomes)
    mode = "placed" if report.execute else "simulated"
    text = f"{len(report.outcomes)} holdings checked, {orders} sell orders {mode}"
    if report.cancellations:
        text += f", {len(report.cancellations)} expired orders cancelled"
    if report.failed_orders:
        text += f", {report.failed_orders} failed"
    return text


def run(args: argparse.Namespace, report: RunReport) -> Exit:
    """Run one harvest under the droid locks, recording the outcome in ``report``."""
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return report.finish(Exit.CONFIG, "configuration error", errors=[str(exc)])
    _configure_logging(settings.log_level)
    log = structlog.get_logger("t2k2")

    try:
        strategy = load_strategy(args.strategy or settings.strategy_file)
    except StrategyError as exc:
        print(f"Strategy error: {exc}", file=sys.stderr)
        return report.finish(Exit.CONFIG, "strategy error", errors=[str(exc)])

    if args.execute:
        log.warning("live_orders_enabled", base_url=settings.base_url, live=settings.is_live)

    try:
        with (
            run_lock(DROID),
            t212_lock(),
            Trading212Client(
                settings.api_key, settings.api_secret, base_url=settings.base_url
            ) as client,
        ):
            harvest = run_harvest(client, YahooPriceHistory(), strategy, execute=args.execute)
    except Skipped as exc:
        print(f"Skipped: {exc.reason}", file=sys.stderr)
        return report.finish(Exit.SKIPPED, exc.reason, {"skipped": "lock_busy"})
    except Trading212Error as exc:
        print(f"Trading212 error: {exc}", file=sys.stderr)
        return report.finish(Exit.FAILED, "Trading212 API error", errors=[str(exc)])

    print(format_report(harvest))
    failed = harvest.failed_orders
    return report.finish(
        Exit.FAILED if failed else Exit.OK,
        _summary(harvest),
        harvest.to_dict(),
        errors=[f"{failed} sell orders couldn't be placed or cancelled"] if failed else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run a harvest and write its run report. Returns the process exit code."""
    args = _parse_args(argv)
    load_dotenv()
    report = RunReport(
        droid=DROID,
        version=_version(),
        command="harvest",
        dry_run=not args.execute,
        argv=list(sys.argv[1:] if argv is None else argv),
    )
    code = run(args, report)
    try:
        path = report.write(args.report)
    except OSError as exc:
        print(f"Could not write the run report: {exc}", file=sys.stderr)
    else:
        print(f"Run report: {path}", file=sys.stderr)
    return int(code)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
