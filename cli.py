"""Command-line entry point for ZooFanCrawler."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional, Sequence


DEFAULT_CONFIG = Path(__file__).resolve().parent / "config" / "zoos.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zoofan", description="Crawl configured zoo news sources")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="YAML source registry")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    commands = parser.add_subparsers(dest="command", required=True)

    crawl = commands.add_parser("crawl", help="run one batch crawl")
    crawl.add_argument("selection", nargs="?", default="all", help="all, zoo-berlin, koeln-zoo, or gaiazoo")
    crawl.add_argument("--db", default=":memory:", help="SQLite database path")

    scheduler = commands.add_parser("scheduler", help="run a six-hour configured crawl scheduler")
    scheduler.add_argument("--db", default=":memory:", help="SQLite database path")
    scheduler.add_argument("--hours", type=float, default=6.0)
    scheduler.add_argument("--once", action="store_true", help="run one crawl and exit")

    # These are lazy hooks for modules delivered by a later milestone.
    dashboard = commands.add_parser("dashboard", help="open the optional dashboard module")
    dashboard.add_argument("--db", default="data/zoofan.db", help="SQLite database path")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8000)
    dashboard.add_argument("--control-url", default=None, help="optional crawler control UI URL")

    control = commands.add_parser("control", help="open the optional crawler control module")
    control.add_argument("--db", default="data/zoofan.db", help="SQLite database path")
    control.add_argument("--host", default="127.0.0.1")
    control.add_argument("--port", type=int, default=8001)
    control.add_argument("--dashboard-url", default="http://127.0.0.1:8000")
    commands.add_parser("endgoal", help="open the optional endgoal module")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")
    if args.command in {"crawl", "scheduler"}:
        from zoofan.config import load_config
        from zoofan.crawler import Crawler
        from zoofan.storage import SQLiteStorage

        config = load_config(args.config)
        with SQLiteStorage(args.db) as storage:
            crawler = Crawler(config, storage=storage)
            if args.command == "crawl":
                result = crawler.crawl(args.selection)
            else:
                from zoofan.scheduler import CrawlScheduler

                scheduler = CrawlScheduler(crawler, hours=args.hours)
                result = scheduler.run_once() if args.once else scheduler.start(blocking=True)
                if not args.once:
                    return 0
            if hasattr(result, "as_dict"):
                print(json.dumps(result.as_dict(), ensure_ascii=False, default=str))
            return 0 if getattr(result, "status", "completed") == "completed" else 1

    module_names = {
        "dashboard": "zoofan.dashboard",
        "control": "zoofan.control",
        "endgoal": "zoofan.endgoal",
    }
    module_name = module_names[args.command]
    logger = logging.getLogger(__name__)
    try:
        module = __import__(module_name, fromlist=["main"])
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            logger.error("optional module %s is not installed", module_name)
        else:
            logger.error("cannot load %s: missing dependency %s", module_name, exc.name or exc)
        return 1
    except Exception as exc:
        logger.error("cannot load %s: %s", module_name, exc)
        return 1
    handler = getattr(module, "main", None)
    if callable(handler):
        if args.command == "dashboard":
            value = handler(args.db, args.host, args.port, args.control_url)
        elif args.command == "control":
            value = handler(args.db, args.config, args.host, args.port, args.dashboard_url)
        else:
            value = handler()
        return int(value or 0)
    logger.error("optional module %s has no main()", module_name)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
