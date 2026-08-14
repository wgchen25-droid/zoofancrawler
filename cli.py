"""Command-line entry point for ZooFanCrawler."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import importlib
import inspect
import json
import logging
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Optional, Sequence, cast


DEFAULT_CONFIG = Path(__file__).resolve().parent / "config" / "zoos.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zoofan", description="Crawl configured zoo news sources")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="YAML source registry")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    commands = parser.add_subparsers(dest="command", required=True)

    crawl = commands.add_parser("crawl", help="run one batch crawl")
    # ``selection`` is kept for backwards compatibility with the original
    # ``crawl <selection>`` contract.  The explicit selectors below are useful
    # to scripts and are resolved against the loaded registry, rather than
    # duplicating zoo or group names in this module.
    crawl.add_argument("selection", nargs="?", default=None, help="legacy zoo selection (defaults to all)")
    selection = crawl.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="crawl every enabled configured zoo")
    selection.add_argument("--group", metavar="GROUP", help="crawl enabled zoos in a configured group")
    selection.add_argument("--zoo", metavar="ID", help="crawl one configured zoo by id or alias")
    crawl.add_argument("--db", default=":memory:", help="SQLite database path")
    crawl.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="retain candidates newer than N days; undated candidates are retained",
    )
    crawl.add_argument(
        "--live",
        action="store_true",
        help="run structured live-smoke mode (site failures are reported in JSON; runner errors are nonzero)",
    )
    crawl.add_argument(
        "--max-candidates-per-source",
        type=int,
        default=None,
        metavar="N",
        help="live-smoke bound: fetch at most N article candidates per source",
    )
    crawl.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help="live-smoke bound: follow at most N source/archive pages",
    )

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
    commands.add_parser("acceptance", help="alias for the live endgoal acceptance workflow")

    validate = commands.add_parser("validate-config", help="strictly validate the configured zoo registry")
    validate.add_argument(
        "--json",
        "--machine-readable",
        dest="machine_readable",
        action="store_true",
        help="emit validation results as JSON",
    )
    validate.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="validation output format (json is equivalent to --json)",
    )

    report = commands.add_parser(
        "build-acceptance-report",
        help="build read-only acceptance report artifacts from a crawl database",
    )
    report.add_argument("--db", default="data/zoofan.db", help="SQLite database path")
    report.add_argument("--output-dir", default="reports", help="report output directory")
    return parser


def _attribute(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a domain record or a test/integration mapping."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _zoo_key(zoo: Any) -> str:
    """Return the stable selection key used by :class:`Crawler`."""

    for name in ("id", "slug", "name"):
        value = _attribute(zoo, name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _zoo_groups(zoo: Any) -> tuple[str, ...]:
    groups = _attribute(zoo, "groups", ())
    if isinstance(groups, str):
        groups = (groups,)
    if not isinstance(groups, Iterable):
        return ()
    return tuple(str(group).strip() for group in groups if str(group).strip())


def _enabled(zoo: Any) -> bool:
    value = _attribute(zoo, "enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "disabled"}
    return bool(value)


def _resolve_selection(args: argparse.Namespace, config: Any) -> tuple[Optional[str | list[str]], Optional[str]]:
    """Resolve explicit CLI selectors without inventing registry entries.

    The crawler remains responsible for historical aliases and final selection
    semantics.  This helper only maps ``--group`` to configured zoo IDs and
    ensures that an empty group cannot silently turn into a zero-zoo success.
    """

    explicit = args.all or args.group is not None or args.zoo is not None
    if explicit and args.selection is not None:
        return None, "a positional selection cannot be combined with --all, --group, or --zoo"

    if args.all:
        return "all", None
    if args.zoo is not None:
        zoo_id = str(args.zoo).strip()
        if not zoo_id:
            return None, "--zoo requires a non-empty zoo ID"
        return zoo_id, None
    if args.group is not None:
        group = str(args.group).strip()
        if not group:
            return None, "--group requires a non-empty configured group"
        zoos = _attribute(config, "zoos", ()) or ()
        # Group labels are configuration data.  Matching case-insensitively is
        # convenient for shell callers while preserving the configured zoo
        # order and the exact ID passed to Crawler.
        wanted = group.casefold()
        selected: list[str] = []
        seen: set[str] = set()
        for zoo in zoos:
            if not _enabled(zoo):
                continue
            if not any(item.casefold() == wanted for item in _zoo_groups(zoo)):
                continue
            key = _zoo_key(zoo)
            dedup_key = key.casefold()
            if key and dedup_key not in seen:
                selected.append(key)
                seen.add(dedup_key)
        if not selected:
            return None, f"configured group {group!r} is unknown or has no enabled zoos"
        return selected, None

    return (args.selection or "all"), None


def _supports_keyword(callable_value: Any, keyword: str) -> bool:
    """Check public call signatures before forwarding optional CLI values."""

    try:
        signature = inspect.signature(callable_value)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == keyword:
            return parameter.kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
    return False


def _result_payload(result: Any) -> dict[str, Any]:
    """Serialize a crawl result while preserving stable coverage fields."""

    if hasattr(result, "as_dict") and callable(result.as_dict):
        try:
            value = result.as_dict()
        except Exception:
            value = None
    elif isinstance(result, Mapping):
        value = dict(result)
    elif is_dataclass(result):
        # ``is_dataclass`` also accepts dataclass classes at runtime, while
        # ``asdict`` only accepts instances.  CLI results are instances, and
        # the explicit cast keeps that runtime contract visible to mypy.
        value = asdict(cast(Any, result))
    else:
        try:
            value = dict(vars(result))
        except (TypeError, ValueError):
            value = {"result": result}
    if not isinstance(value, dict):
        value = {"result": value}

    value.setdefault("zoo_results", [])
    coverage_value = value.get("coverage")
    metadata = value.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    coverage = dict(coverage_value) if isinstance(coverage_value, Mapping) else {}
    for key in ("processed", "enabled", "processed_zoos", "enabled_zoos", "selected_zoos"):
        coverage.setdefault(key, value.get(key, metadata.get(key, [] if key.endswith("zoos") else 0)))
    processed_zoos = coverage.get("processed_zoos")
    if not isinstance(processed_zoos, list):
        processed_zoos = list(processed_zoos) if isinstance(processed_zoos, (tuple, set)) else []
    result_counts: dict[str, int] = {}
    zoo_results = value.get("zoo_results")
    if isinstance(zoo_results, list):
        for item in zoo_results:
            zoo_id = item.get("zoo_id") if isinstance(item, Mapping) else getattr(item, "zoo_id", None)
            if zoo_id is not None:
                key = str(zoo_id)
                result_counts[key] = result_counts.get(key, 0) + 1
    coverage.setdefault("zoo_result_counts", result_counts)
    coverage.setdefault(
        "missing_processed_zoos",
        [zoo_id for zoo_id in processed_zoos if result_counts.get(str(zoo_id), 0) == 0],
    )
    coverage.setdefault(
        "duplicate_processed_zoos",
        [zoo_id for zoo_id in processed_zoos if result_counts.get(str(zoo_id), 0) > 1],
    )
    coverage.setdefault(
        "exactly_one_result_per_processed_zoo",
        bool(processed_zoos)
        and not coverage["missing_processed_zoos"]
        and not coverage["duplicate_processed_zoos"]
        and len(result_counts) == len(processed_zoos),
    )
    value["coverage"] = coverage
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str))


def _live_error_payload(message: str, *, status: str, code: int, error_category: str = "runner_error") -> int:
    """Emit a structured live-runner error and return its process code."""

    _print_json(
        {
            "live": True,
            "mode": "live_smoke",
            "machine_readable": True,
            "status": status,
            "runner_status": "failed",
            "exit_code": code,
            "error": message,
            "error_category": error_category,
            "site_failures": [],
            "coverage": {
                "processed": 0,
                "enabled": 0,
                "processed_zoos": [],
                "enabled_zoos": [],
                "selected_zoos": [],
                "zoo_result_counts": {},
                "missing_processed_zoos": [],
                "duplicate_processed_zoos": [],
                "exactly_one_result_per_processed_zoo": False,
            },
        }
    )
    return code


def _live_payload(result: Any, *, max_candidates_per_source: Optional[int], max_pages: Optional[int]) -> dict[str, Any]:
    """Add stable machine-readable live-smoke semantics to a crawl result."""

    payload = _result_payload(result)
    zoo_results = payload.get("zoo_results", [])
    if not isinstance(zoo_results, list):
        zoo_results = []
    site_failures: list[dict[str, Any]] = []
    for item in zoo_results:
        if isinstance(item, Mapping):
            status = str(item.get("status", ""))
            if status in {"failed", "partial", "error", "failure", "warning", "completed_with_errors"}:
                site_failures.append(
                    {
                        "zoo_id": item.get("zoo_id"),
                        "status": status,
                        "source_status": item.get("source_status"),
                        "error_category": item.get("error_category", item.get("source_status")),
                        "error_summary": item.get("error_summary"),
                    }
                )
    coverage = payload.get("coverage")
    coverage_ok = isinstance(coverage, Mapping) and bool(
        coverage.get("exactly_one_result_per_processed_zoo", False)
    )
    runner_ok = coverage_ok
    payload.update(
        {
            "live": True,
            "mode": "live_smoke",
            "machine_readable": True,
            # ``status`` remains the crawler's truthful site-level aggregate;
            # the runner completed all selected zoos even when one site failed.
            "runner_status": "completed" if runner_ok else "failed",
            "exit_code": 0 if runner_ok else 1,
            "site_failures": site_failures,
            "site_failure_count": len(site_failures),
            "limits": {
                "max_candidates_per_source": max_candidates_per_source,
                "max_pages": max_pages,
            },
        }
    )
    return payload


def _result_status(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("status", "completed"))
    return str(getattr(result, "status", "completed"))


def _report_payload(report: Any) -> dict[str, Any]:
    """Expose generated report paths as JSON without depending on one result class."""

    if isinstance(report, Mapping):
        paths = report.get("paths", report.get("files", report))
        output_dir = report.get("output_dir")
    else:
        paths = getattr(report, "paths", getattr(report, "files", {}))
        output_dir = getattr(report, "output_dir", getattr(report, "output_directory", None))
    if not isinstance(paths, Mapping):
        paths = {}
    path_payload = {str(key): str(value) for key, value in paths.items()}
    if output_dir is None and path_payload:
        output_dir = str(Path(next(iter(path_payload.values()))).parent)
    return {"output_dir": str(output_dir) if output_dir is not None else None, "paths": path_payload}


def _call_build_reports(builder: Any, config_path: Path, db_path: str, output_dir: str) -> Any:
    """Call current and small test-double variants of ``build_reports``."""

    try:
        parameters: Mapping[str, inspect.Parameter] = inspect.signature(builder).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if "config" in parameters:
        kwargs["config"] = config_path
    elif "config_path" in parameters or accepts_kwargs:
        kwargs["config_path"] = config_path
    if "db" in parameters:
        kwargs["db"] = db_path
    elif "db_path" in parameters or accepts_kwargs:
        kwargs["db_path"] = db_path
    if "output_dir" in parameters or accepts_kwargs:
        kwargs["output_dir"] = output_dir
    elif "reports_dir" in parameters:
        kwargs["reports_dir"] = output_dir
    else:
        # The real API has output_dir; retain a useful fallback for opaque
        # callables that do not expose a signature.
        kwargs["output_dir"] = output_dir
    return builder(**kwargs)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")
    if args.command == "validate-config":
        from zoofan.config import validate_config
        machine_readable = bool(args.machine_readable or args.format == "json")

        try:
            validation = validate_config(args.config, strict=True)
        except Exception as exc:
            error_payload: dict[str, Any] = {
                "valid": False,
                "errors": [
                    {
                        "path": "config",
                        "message": f"{type(exc).__name__}: {exc}",
                        "code": "load",
                    }
                ],
            }
            if machine_readable:
                _print_json(error_payload)
            else:
                print(f"configuration error: {error_payload['errors'][0]['message']}", file=sys.stderr)
            return 1
        payload: dict[str, Any] = validation.as_dict() if hasattr(validation, "as_dict") else {
            "valid": bool(getattr(validation, "valid", validation)),
            "errors": [str(error) for error in getattr(validation, "errors", ())],
        }
        if machine_readable:
            _print_json(payload)
        elif payload.get("valid"):
            print("configuration is valid")
        else:
            for error in payload.get("errors", []):
                if isinstance(error, Mapping):
                    path = error.get("path", "config")
                    message = error.get("message", error)
                    print(f"{path}: {message}", file=sys.stderr)
                else:
                    print(str(error), file=sys.stderr)
        return 0 if payload.get("valid") else 1

    if args.command == "build-acceptance-report":
        try:
            reporting = importlib.import_module("zoofan.reporting")

            report = _call_build_reports(
                reporting.build_reports,
                args.config,
                args.db,
                args.output_dir,
            )
            _print_json(_report_payload(report))
            return 0
        except Exception as exc:
            logging.getLogger(__name__).error("cannot build acceptance report: %s", exc)
            return 1

    if args.command in {"crawl", "scheduler"}:
        from zoofan.config import load_config
        from zoofan.crawler import Crawler
        from zoofan.storage import SQLiteStorage

        try:
            config = load_config(args.config)
        except Exception as exc:
            logging.getLogger(__name__).error("configuration error: %s", exc)
            if args.command == "crawl" and args.live:
                return _live_error_payload(
                    f"{type(exc).__name__}: {exc}",
                    status="configuration_error",
                    code=2,
                    error_category="configuration_error",
                )
            return 2
        selection: Optional[str | list[str]] = None
        if args.command == "crawl":
            selection, selection_error = _resolve_selection(args, config)
            if selection_error or selection is None:
                message = selection_error or "configuration error: no zoo selection was resolved"
                logging.getLogger(__name__).error("configuration error: %s", message)
                if args.live:
                    return _live_error_payload(
                        message,
                        status="configuration_error",
                        code=2,
                        error_category="configuration_error",
                    )
                return 2
            invalid_limit = next(
                (
                    (name, value)
                    for name, value in (
                        ("--since-days", args.since_days),
                        ("--max-candidates-per-source", args.max_candidates_per_source),
                        ("--max-pages", args.max_pages),
                    )
                    if value is not None and (isinstance(value, bool) or value < 0 or (name != "--since-days" and value == 0))
                ),
                None,
            )
            if invalid_limit is not None:
                name, value = invalid_limit
                requirement = "non-negative" if name == "--since-days" else "positive"
                message = f"configuration error: {name} must be {requirement}"
                logging.getLogger(__name__).error(message)
                if args.live:
                    return _live_error_payload(
                        message,
                        status="configuration_error",
                        code=2,
                        error_category="configuration_error",
                    )
                return 2
        try:
            storage_context = SQLiteStorage(args.db)
        except Exception as exc:
            logging.getLogger(__name__).error("runner error: cannot open storage: %s", exc)
            if args.command == "crawl" and args.live:
                return _live_error_payload(
                    f"cannot open storage: {type(exc).__name__}: {exc}",
                    status="runner_error",
                    code=1,
                )
            return 1
        with storage_context as storage:
            crawler_kwargs: dict[str, Any] = {}
            option_values: dict[str, Any] = {}
            if args.command == "crawl":
                option_values = {
                    "since_days": args.since_days,
                    "max_candidates_per_source": args.max_candidates_per_source,
                    "max_pages": args.max_pages,
                }
            crawl_method_options: dict[str, Any] = {}
            for option_name, option_value in option_values.items():
                if option_value is None:
                    continue
                constructor_supports = _supports_keyword(Crawler, option_name)
                crawl_declares = _supports_keyword(getattr(Crawler, "crawl", None), option_name)
                if not constructor_supports and not crawl_declares:
                    message = f"configuration error: --{option_name.replace('_', '-')} is not supported by the current Crawler"
                    logging.getLogger(__name__).error(message)
                    if args.live:
                        return _live_error_payload(
                            message,
                            status="configuration_error",
                            code=2,
                            error_category="configuration_error",
                        )
                    return 2
                if constructor_supports and not crawl_declares:
                    crawler_kwargs[option_name] = option_value
                if crawl_declares:
                    crawl_method_options[option_name] = option_value
            try:
                crawler = Crawler(config, storage=storage, **crawler_kwargs)
            except Exception as exc:
                logging.getLogger(__name__).error("runner error: cannot initialize crawler: %s", exc)
                if args.command == "crawl" and args.live:
                    return _live_error_payload(
                        f"cannot initialize crawler: {type(exc).__name__}: {exc}",
                        status="runner_error",
                        code=1,
                    )
                return 1
            if args.command == "crawl":
                crawl_method: Any = crawler.crawl
                try:
                    crawl_result = crawl_method(selection, **crawl_method_options)
                except ValueError as exc:
                    # Unknown zoo selections are configuration errors, not
                    # uncaught crawler tracebacks.  The crawler still owns
                    # source/article failure isolation; this boundary only
                    # handles invalid command arguments.
                    logging.getLogger(__name__).error("configuration error: %s", exc)
                    if args.live:
                        return _live_error_payload(
                            str(exc),
                            status="configuration_error",
                            code=2,
                            error_category="configuration_error",
                        )
                    return 2
                except Exception as exc:
                    logging.getLogger(__name__).error("runner error: crawl did not complete: %s", exc)
                    if args.live:
                        return _live_error_payload(
                            f"crawl did not complete: {type(exc).__name__}: {exc}",
                            status="runner_error",
                            code=1,
                        )
                    return 1
            else:
                from zoofan.scheduler import CrawlScheduler

                scheduler = CrawlScheduler(crawler, hours=args.hours)
                crawl_result = scheduler.run_once() if args.once else scheduler.start(blocking=True)
                if not args.once:
                    return 0
            if args.command == "crawl":
                if args.live:
                    payload = _live_payload(
                        crawl_result,
                        max_candidates_per_source=args.max_candidates_per_source,
                        max_pages=args.max_pages,
                    )
                else:
                    payload = _result_payload(crawl_result)
                _print_json(payload)
            elif hasattr(crawl_result, "as_dict"):
                _print_json(_result_payload(crawl_result))
            if args.command == "crawl" and args.live:
                return int(payload.get("exit_code", 0) or 0)
            return 0 if _result_status(crawl_result) == "completed" else 1

    module_names = {
        "dashboard": "zoofan.dashboard",
        "control": "zoofan.control",
        "endgoal": "zoofan.endgoal",
        "acceptance": "zoofan.endgoal",
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
        elif args.command in {"endgoal", "acceptance"}:
            # ``endgoal.main`` accepts the selected registry, while retaining
            # a no-argument fallback for older integrations that expose the
            # original default-only hook.
            if _supports_keyword(handler, "config_path"):
                value = handler(config_path=args.config)
            elif _supports_keyword(handler, "config"):
                value = handler(config=args.config)
            else:
                value = handler()
        else:
            value = handler()
        return int(value or 0)
    logger.error("optional module %s has no main()", module_name)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
