"""Local-only control surface for starting configured crawler runs.

This module intentionally has no dashboard dependency: it starts a crawl in a
single background thread and exposes only a small, safe status summary.  The
inspection dashboard remains the place for reading crawl data.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .config import load_config
from .crawler import Crawler
from .storage import SQLiteStorage


_PUBLIC_STATUSES = {
    "idle",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
    # A source-level run can be rejected by a durable crawler/storage lease.
    # Keep this as a first-class, safe status instead of leaking the exception
    # through the background thread and presenting a generic 500-like failure.
    "busy",
    "rejected",
}

_BUSY_EXCEPTION_NAMES = frozenset(
    {
        "leasebusy",
        "leasebusyerror",
        "leasebusyexception",
        "runbusy",
        "runbusyerror",
        "crawlerbusy",
        "crawlerbusyerror",
    }
)


def _now_text(clock: Callable[[], Any]) -> str:
    value = clock()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _number(value: Any) -> int:
    """Return a bounded, presentable count without serialising crawler internals."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _summary(result: Any, *, next_batch: bool = False) -> dict[str, int]:
    """Return a deliberately small, integer-only public result summary.

    The original zoo/all crawl API exposed the four historical counters below;
    keep that shape for compatibility.  ``next_batch`` has a separate compact
    summary whose names mirror the source queue semantics and the crawler's
    durable run metadata.
    """

    if next_batch:
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, Mapping):
            metadata = {}
        serialized: Mapping[str, Any] = {}
        as_dict = getattr(result, "as_dict", None)
        if callable(as_dict):
            try:
                candidate = as_dict()
                if isinstance(candidate, Mapping):
                    serialized = candidate
            except Exception:
                serialized = {}

        def value(*names: str, default: Any = 0) -> Any:
            for name in names:
                if isinstance(result, Mapping) and name in result:
                    return result[name]
                if name in serialized:
                    return serialized[name]
                candidate = getattr(result, name, None)
                if candidate is not None:
                    return candidate
                if name in metadata:
                    return metadata[name]
            return default

        return {
            "processed": _number(
                value(
                    "processed_count",
                    "candidates_processed_count",
                    "candidates_processed",
                    "processed",
                    "discovered_count",
                    default=0,
                )
            ),
            "stored": _number(value("stored_count", "stored", default=0)),
            "known": _number(
                value(
                    "already_known_count",
                    "already_known",
                    "known_count",
                    "known",
                    default=0,
                )
            ),
            "duplicate": _number(
                value(
                    "duplicate_candidate_count",
                    "duplicate_filtered_count",
                    "duplicate_filtered",
                    "duplicate_count",
                    "duplicate",
                    default=0,
                )
            ),
            "errors": _number(value("error_count", "errors", "failed_count", default=0)),
            "remaining_estimate": _number(
                value(
                    "remaining_estimate",
                    "remaining_estimate_count",
                    "remaining",
                    default=0,
                )
            ),
        }
    return {
        "discovered": _number(getattr(result, "discovered_count", 0)),
        "fetched": _number(getattr(result, "fetched_count", 0)),
        "stored": _number(getattr(result, "stored_count", 0)),
        "errors": _number(getattr(result, "error_count", 0)),
    }


def _is_busy_error(error: BaseException) -> bool:
    """Recognise only an explicit crawler/storage lease-busy signal.

    This intentionally does not classify arbitrary ``RuntimeError`` messages
    such as ``"database is busy"``.  A future crawler may expose a dedicated
    ``LeaseBusyError`` (or a compatible named exception/flag); all other
    failures remain ordinary safe failures.
    """

    error_type = type(error)
    names = {
        str(getattr(error_type, "__name__", "")).strip().lower().replace("_", ""),
        str(getattr(error_type, "__qualname__", "")).rsplit(".", 1)[-1]
        .strip()
        .lower()
        .replace("_", ""),
    }
    if names & _BUSY_EXCEPTION_NAMES or any(
        "lease" in name and "busy" in name for name in names if name
    ):
        return True
    # Keep compatibility narrow: only an explicit lease/busy marker from the
    # future boundary is trusted, never arbitrary exception text.
    for attribute in ("lease_busy", "busy", "rejected"):
        if getattr(error, attribute, False) is True:
            return True
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.strip().lower().replace("-", "_") in {
        "lease_busy",
        "already_running",
        "run_busy",
    }:
        return True
    return False


def _source_choices(config: Any) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    """Build the only source selection set exposed by Control.

    A source is selectable only when its configured zoo and the source itself
    are both enabled.  Duplicate/empty source identifiers are excluded rather
    than guessed, which keeps the POST boundary fail-closed.
    """

    zoos = [
        zoo
        for zoo in getattr(config, "zoos", ())
        if bool(getattr(zoo, "enabled", True))
    ]
    zoo_by_ref: dict[str, dict[str, str]] = {}
    for zoo in zoos:
        slug = str(getattr(zoo, "slug", "") or "").strip()
        zoo_id = str(getattr(zoo, "id", "") or "").strip()
        if not slug:
            continue
        entry = {"slug": slug, "name": str(getattr(zoo, "name", "") or slug)}
        zoo_by_ref[slug] = entry
        if zoo_id:
            zoo_by_ref[zoo_id] = entry

    choices: list[dict[str, str]] = []
    by_id: dict[str, dict[str, str]] = {}
    duplicate_ids: set[str] = set()
    for source in getattr(config, "sources", ()):
        if not bool(getattr(source, "enabled", True)):
            continue
        source_id = str(getattr(source, "id", "") or "").strip()
        zoo_ref = str(getattr(source, "zoo_id", "") or "").strip()
        zoo = zoo_by_ref.get(zoo_ref)
        if not source_id or zoo is None:
            continue
        choice = {
            "id": source_id,
            "name": str(getattr(source, "name", "") or source_id),
            "kind": str(getattr(source, "kind", "") or ""),
            "zoo_slug": zoo["slug"],
            "zoo_name": zoo["name"],
        }
        if source_id in by_id:
            duplicate_ids.add(source_id)
            continue
        by_id[source_id] = choice
        choices.append(choice)
    if duplicate_ids:
        choices = [choice for choice in choices if choice["id"] not in duplicate_ids]
        for source_id in duplicate_ids:
            by_id.pop(source_id, None)
    return choices, by_id


def _run_id(result: Any) -> str | None:
    """Extract an opaque run identifier without exposing arbitrary payloads."""
    value = getattr(getattr(result, "run", None), "id", None)
    if value is None:
        as_dict = getattr(result, "as_dict", None)
        if callable(as_dict):
            try:
                value = as_dict().get("run_id")
            except Exception:
                value = None
    if not isinstance(value, (str, int)):
        return None
    result_id = str(value)
    if not result_id or len(result_id) > 128 or not all(char.isalnum() or char in "_-" for char in result_id):
        return None
    return result_id


def _safe_dashboard_url(value: Any) -> str | None:
    """Only render a conventional external link, never a script/data URL."""
    candidate = str(value or "")
    try:
        parsed = urlparse(candidate)
        has_netloc = bool(parsed.netloc)
    except (TypeError, ValueError, AttributeError):
        return None
    return candidate if parsed.scheme in {"http", "https"} and has_netloc else None


def _public_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only fields that are safe for the browser or status API."""
    status = state.get("status", "idle")
    public = {
        "status": status if status in _PUBLIC_STATUSES else "failed",
        "selection": state.get("selection"),
        "run_id": state.get("run_id"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "summary": dict(state.get("summary") or {}),
        "message": state.get("message", "Ready to start a crawl."),
    }
    # Preserve the old status payload for all/zoo crawls while exposing the
    # source identity for a source-level run.  This is a configured identifier,
    # never a URL or selector supplied by the browser.
    source_id = state.get("source_id")
    if source_id is not None:
        public["source_id"] = source_id
    return public


def create_app(
    db_path: str | os.PathLike[str],
    config_path: str | os.PathLike[str],
    *,
    dashboard_url: str = "http://127.0.0.1:8000/",
    crawler_factory: Callable[..., Any] = Crawler,
    storage_factory: Callable[..., Any] = SQLiteStorage,
    thread_factory: Callable[..., Any] = threading.Thread,
    clock: Callable[[], Any] = lambda: datetime.now(timezone.utc),
) -> Flask:
    """Create the local crawl-control application.

    ``crawler_factory``, ``storage_factory``, ``thread_factory`` and ``clock``
    are injectable so the control boundary can be tested without network
    activity or real background threads.
    """
    config = load_config(config_path)
    enabled_zoos = [zoo for zoo in getattr(config, "zoos", ()) if bool(getattr(zoo, "enabled", True))]
    zoo_choices = [
        {"slug": str(zoo.slug), "name": str(zoo.name or zoo.slug)}
        for zoo in enabled_zoos
        if getattr(zoo, "slug", None)
    ]
    allowed_selections = {choice["slug"] for choice in zoo_choices}

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).with_name("templates")),
        static_folder=str(Path(__file__).with_name("static")),
    )
    app.config.update(
        CONTROL_DB_PATH=os.fspath(db_path),
        CONTROL_DASHBOARD_URL=_safe_dashboard_url(dashboard_url),
        CONTROL_CSRF_TOKEN=secrets.token_urlsafe(32),
    )
    app.extensions["control_lock"] = threading.RLock()
    app.extensions["control_state"] = {
        "status": "idle",
        "selection": None,
        "source_id": None,
        "run_id": None,
        "started_at": None,
        "finished_at": None,
        "summary": {"discovered": 0, "fetched": 0, "stored": 0, "errors": 0},
        "message": "Ready to start a crawl.",
    }

    def state_snapshot() -> dict[str, Any]:
        with app.extensions["control_lock"]:
            return _public_state(app.extensions["control_state"])

    def render_control(
        *,
        status_code: int = 200,
        selected_source_id: str | None = None,
        form_error: str | None = None,
    ) -> tuple[str, int] | str:
        state = state_snapshot()
        source_list, _ = _source_choices(config)
        if selected_source_id is None:
            selected_source_id = state.get("source_id")
        return render_template(
            "control.html",
            state=state,
            zoos=zoo_choices,
            sources=source_list,
            selected_source_id=selected_source_id,
            form_error=form_error,
            csrf_token=app.config["CONTROL_CSRF_TOKEN"],
            dashboard_url=app.config["CONTROL_DASHBOARD_URL"],
        ), status_code if status_code != 200 else 200

    def run_operation(
        operation: str,
        selection: str,
        *,
        source_id: str | None = None,
    ) -> None:
        storage: Any = None
        try:
            storage = storage_factory(app.config["CONTROL_DB_PATH"])
            crawler = crawler_factory(config, storage)
            if operation == "next_batch":
                # The server-side source validation happens before this
                # worker is created.  Pass only the configured identifier and
                # the fixed safety limit; no URL/selector can enter Crawler.
                result = crawler.next_batch(source_id, limit=10)
            else:
                result = crawler.crawl(selection)
            raw_result_status = (
                result.get("status", "completed")
                if isinstance(result, Mapping)
                else getattr(result, "status", "completed")
            )
            result_status = str(raw_result_status or "completed").strip().lower()
            if result_status in {"busy", "rejected", "lease_busy", "lease-busy"}:
                status = "busy" if result_status in {"busy", "lease_busy", "lease-busy"} else "rejected"
                message = (
                    "Next batch was rejected because another crawl is already running."
                    if operation == "next_batch"
                    else "Crawl was rejected because another crawl is already running."
                )
            elif result_status in {"completed", "completed_with_errors"}:
                status = result_status
                message = (
                    "Next batch completed."
                    if operation == "next_batch" and status == "completed"
                    else "Next batch completed with errors."
                    if operation == "next_batch"
                    else "Crawl completed."
                    if status == "completed"
                    else "Crawl completed with errors."
                )
            else:
                status = "failed"
                message = "Next batch failed." if operation == "next_batch" else "Crawl failed."
            with app.extensions["control_lock"]:
                app.extensions["control_state"].update(
                    status=status,
                    run_id=_run_id(result),
                    finished_at=_now_text(clock),
                    summary=_summary(result, next_batch=operation == "next_batch"),
                    message=message,
                )
        except Exception as error:
            if _is_busy_error(error):
                # Lease contention is an expected operational outcome, not a
                # server error.  Do not expose exception text or a traceback to
                # the browser; the local log remains available to operators.
                app.logger.info("Control crawl rejected by an active lease")
                busy_message = (
                    "Next batch was rejected because another crawl is already running."
                    if operation == "next_batch"
                    else "Crawl was rejected because another crawl is already running."
                )
                with app.extensions["control_lock"]:
                    app.extensions["control_state"].update(
                        status="busy",
                        finished_at=_now_text(clock),
                        message=busy_message,
                    )
                return
            # Deliberately do not return exception text; it may include local
            # paths, URLs, or data returned by a remote source.
            app.logger.exception("Control crawl failed")
            with app.extensions["control_lock"]:
                app.extensions["control_state"].update(
                    status="failed",
                    finished_at=_now_text(clock),
                    message="Next batch failed." if operation == "next_batch" else "Crawl failed.",
                )
        finally:
            close = getattr(storage, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def run_crawl(selection: str) -> None:
        run_operation("crawl", selection)

    def run_next_batch(source_id: str) -> None:
        _, source_by_id = _source_choices(config)
        # The worker gets a second narrow guard in case the configuration is
        # edited between POST validation and thread execution.  A changed or
        # disabled source is rejected without invoking Crawler.
        if source_id not in source_by_id:
            with app.extensions["control_lock"]:
                app.extensions["control_state"].update(
                    status="rejected",
                    finished_at=_now_text(clock),
                    message="Next batch rejected: source is not enabled.",
                )
            return
        run_operation(
            "next_batch",
            source_by_id[source_id]["zoo_slug"],
            source_id=source_id,
        )

    @app.get("/")
    def index() -> tuple[str, int] | str:
        requested_source_ids = request.args.getlist("source_id")
        if requested_source_ids:
            source_id = requested_source_ids[0].strip() if len(requested_source_ids) == 1 else ""
            _, source_by_id = _source_choices(config)
            if not source_id or source_id not in source_by_id:
                return render_control(
                    status_code=400,
                    selected_source_id=None,
                    form_error="Invalid or disabled source selection.",
                )
            return render_control(selected_source_id=source_id)
        return render_control()

    @app.post("/crawl")
    def crawl() -> Any:
        token = request.form.get("csrf_token", "")
        if not hmac.compare_digest(token, app.config["CONTROL_CSRF_TOKEN"]):
            return render_control(status_code=400)

        operation = request.form.get("operation", "crawl").strip().lower()
        if operation not in {"crawl", "next_batch"}:
            return render_control(status_code=400, form_error="Invalid operation.")

        if operation == "next_batch":
            source_id = request.form.get("source_id", "").strip()
            _, source_by_id = _source_choices(config)
            if not source_id or source_id not in source_by_id:
                return render_control(
                    status_code=400,
                    selected_source_id=None,
                    form_error="Invalid or disabled source selection.",
                )
            with app.extensions["control_lock"]:
                state = app.extensions["control_state"]
                if state["status"] == "running":
                    state["message"] = "A crawl is already running."
                    return render_control(status_code=409, selected_source_id=source_id)
                state.update(
                    status="running",
                    selection=source_by_id[source_id]["zoo_slug"],
                    source_id=source_id,
                    run_id=None,
                    started_at=_now_text(clock),
                    finished_at=None,
                    summary={
                        "processed": 0,
                        "stored": 0,
                        "known": 0,
                        "duplicate": 0,
                        "errors": 0,
                        "remaining_estimate": 0,
                    },
                    message="Next batch is running.",
                )
                try:
                    worker = thread_factory(target=lambda: run_next_batch(source_id))
                    if hasattr(worker, "daemon"):
                        worker.daemon = True
                    worker.start()
                    app.extensions["control_thread"] = worker
                except Exception:
                    app.logger.exception("Control next batch failed to start")
                    state.update(
                        status="failed",
                        finished_at=_now_text(clock),
                        message="Next batch failed to start.",
                    )
                    return render_control(status_code=500, selected_source_id=source_id)
            return redirect(url_for("index", source_id=source_id), code=303)

        selection = request.form.get("selection", "all").strip()
        if selection != "all" and selection not in allowed_selections:
            return render_control(status_code=400)

        with app.extensions["control_lock"]:
            state = app.extensions["control_state"]
            if state["status"] == "running":
                state["message"] = "A crawl is already running."
                return render_control(status_code=409)
            state.update(
                status="running",
                selection=selection,
                source_id=None,
                run_id=None,
                started_at=_now_text(clock),
                finished_at=None,
                summary={"discovered": 0, "fetched": 0, "stored": 0, "errors": 0},
                message="Crawl is running.",
            )
            try:
                worker = thread_factory(target=lambda: run_crawl(selection))
                if hasattr(worker, "daemon"):
                    worker.daemon = True
                worker.start()
                app.extensions["control_thread"] = worker
            except Exception:
                app.logger.exception("Control crawl failed to start")
                state.update(status="failed", finished_at=_now_text(clock), message="Crawl failed to start.")
                return render_control(status_code=500)
        return redirect(url_for("index"), code=303)

    @app.get("/status")
    def status() -> Any:
        return jsonify(state_snapshot())

    return app


def main(
    db_path: str | os.PathLike[str] = "data/zoofan.db",
    config_path: str | os.PathLike[str] = "config/zoos.yaml",
    host: str = "127.0.0.1",
    port: int = 8001,
    dashboard_url: str = "http://127.0.0.1:8000/",
) -> None:
    """Run control locally; this surface must never bind to a public host."""
    if host != "127.0.0.1":
        raise ValueError("control must bind to 127.0.0.1")
    create_app(db_path, config_path, dashboard_url=dashboard_url).run(host="127.0.0.1", port=int(port))


if __name__ == "__main__":  # pragma: no cover
    main()
