"""``t2k2-harvest`` entrypoint.

Exit codes: 0 success, 2 configuration or strategy error, 3 Trading212 API error,
5 one or more sell orders failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import structlog
from dotenv import load_dotenv

from .client import Trading212Client, Trading212Error
from .config import ConfigError, load_settings
from .harvest import HarvestReport, OrderStatus, run_harvest
from .market import YahooPriceHistory
from .strategy import StrategyError, load_strategy

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_API = 3
EXIT_ORDERS_FAILED = 5


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
    parser.add_argument("--report", type=Path, help="also write the report as JSON here")
    return parser.parse_args(argv)


def format_report(report: HarvestReport) -> str:
    """Render a report as a plain-text table for the terminal."""
    mode = "EXECUTE" if report.execute else "DRY RUN (pass --execute to place orders)"
    lines = [f"T2-K2 harvest — {mode} — {report.started_at:%Y-%m-%d %H:%M UTC}", ""]
    header = f"{'TICKER':<16}{'ACTION':<9}{'GAIN%':>8}  {'ORDER':<42}  REASON"
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run a harvest. Returns the process exit code."""
    args = _parse_args(argv)
    load_dotenv()
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    _configure_logging(settings.log_level)
    log = structlog.get_logger("t2k2")

    try:
        strategy = load_strategy(args.strategy or settings.strategy_file)
    except StrategyError as exc:
        print(f"Strategy error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.execute:
        log.warning("live_orders_enabled", base_url=settings.base_url, live=settings.is_live)

    try:
        with Trading212Client(
            settings.api_key, settings.api_secret, base_url=settings.base_url
        ) as client:
            report = run_harvest(client, YahooPriceHistory(), strategy, execute=args.execute)
    except Trading212Error as exc:
        print(f"Trading212 error: {exc}", file=sys.stderr)
        return EXIT_API

    print(format_report(report))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")

    failed = any(o.order_status is OrderStatus.FAILED for o in report.outcomes)
    return EXIT_ORDERS_FAILED if failed else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
