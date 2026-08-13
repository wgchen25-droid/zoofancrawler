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


_PUBLIC_STATUSES = {"idle", "running", "completed", "completed_with_errors", "failed"}


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
    except (TypeError, ValueError):
        return 0


def _summary(result: Any) -> dict[str, int]:
    return {
        "discovered": _number(getattr(result, "discovered_count", 0)),
        "fetched": _number(getattr(result, "fetched_count", 0)),
        "stored": _number(getattr(result, "stored_count", 0)),
        "errors": _number(getattr(result, "error_count", 0)),
    }


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
    return {
        "status": status if status in _PUBLIC_STATUSES else "failed",
        "selection": state.get("selection"),
        "run_id": state.get("run_id"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "summary": dict(state.get("summary") or {}),
        "message": state.get("message", "Ready to start a crawl."),
    }


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
        "run_id": None,
        "started_at": None,
        "finished_at": None,
        "summary": {"discovered": 0, "fetched": 0, "stored": 0, "errors": 0},
        "message": "Ready to start a crawl.",
    }

    def state_snapshot() -> dict[str, Any]:
        with app.extensions["control_lock"]:
            return _public_state(app.extensions["control_state"])

    def render_control(*, status_code: int = 200) -> tuple[str, int] | str:
        return render_template(
            "control.html",
            state=state_snapshot(),
            zoos=zoo_choices,
            csrf_token=app.config["CONTROL_CSRF_TOKEN"],
            dashboard_url=app.config["CONTROL_DASHBOARD_URL"],
        ), status_code if status_code != 200 else 200

    def run_crawl(selection: str) -> None:
        storage: Any = None
        try:
            storage = storage_factory(app.config["CONTROL_DB_PATH"])
            result = crawler_factory(config, storage).crawl(selection)
            result_status = getattr(result, "status", "completed")
            status = result_status if result_status in {"completed", "completed_with_errors"} else "failed"
            message = "Crawl completed." if status == "completed" else (
                "Crawl completed with errors." if status == "completed_with_errors" else "Crawl failed."
            )
            with app.extensions["control_lock"]:
                app.extensions["control_state"].update(
                    status=status,
                    run_id=_run_id(result),
                    finished_at=_now_text(clock),
                    summary=_summary(result),
                    message=message,
                )
        except Exception:
            # Deliberately do not return exception text; it may include local
            # paths, URLs, or data returned by a remote source.
            app.logger.exception("Control crawl failed")
            with app.extensions["control_lock"]:
                app.extensions["control_state"].update(
                    status="failed",
                    finished_at=_now_text(clock),
                    message="Crawl failed.",
                )
        finally:
            close = getattr(storage, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    @app.get("/")
    def index() -> tuple[str, int] | str:
        return render_control()

    @app.post("/crawl")
    def crawl() -> Any:
        token = request.form.get("csrf_token", "")
        if not hmac.compare_digest(token, app.config["CONTROL_CSRF_TOKEN"]):
            return render_control(status_code=400)

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
