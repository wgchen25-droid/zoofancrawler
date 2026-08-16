"""Structured crawl events and the small sink boundary used to persist them.

The crawler can emit operational events without knowing whether the sink is
SQLite, an in-memory test double, or a future transport.  Metadata is treated
as untrusted evidence: it is reduced to bounded JSON-safe values before it
reaches a sink so an exception, response object, or response body cannot be
written to the event store accidentally.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Optional, Protocol, Union, runtime_checkable
from urllib.parse import urlsplit, urlunsplit


_MAX_METADATA_DEPTH = 8
_MAX_METADATA_ITEMS = 100
_MAX_METADATA_STRING = 4096
_MAX_METADATA_KEY = 128
_HTML_KEY = re.compile(r"(?:^|_)(?:raw_)?html(?:$|_)", re.IGNORECASE)
_HTML_VALUE = re.compile(
    r"<(?:!doctype\b|html\b|head\b|body\b|script\b|style\b|iframe\b|"
    r"[a-z][^>]{0,200}>)",
    re.IGNORECASE,
)
_RESPONSE_KEY = re.compile(r"(?:^|_)(?:raw_)?(?:response|request)(?:$|_)", re.IGNORECASE)
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:token|password|passwd|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|private[_-]?key|bearer|auth|"
    r"authorization|signature|sig|credential|cookie|set_cookie)(?:$|_)",
    re.IGNORECASE,
)
_URL_KEY = re.compile(r"(?:^|_)(?:url|uri|target|location|source|link)(?:$|_)", re.IGNORECASE)
_URL_VALUE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SECRET_TEXT = re.compile(
    r"(?i)(\b(?:token|password|passwd|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|private[_-]?key|bearer|auth|"
    r"authorization|signature|sig|credential|cookie)\s*[=:]\s*)[^\s,;&]+"
)


def _type_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _short_text(value: Any, *, limit: int = _MAX_METADATA_STRING) -> str:
    try:
        text = str(value)
    except Exception:
        text = f"<{_type_name(value)}>"
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…"


def _safe_url(value: str) -> str:
    """Keep only the non-sensitive HTTP URL identity for event metadata."""

    try:
        parsed = urlsplit(value.strip())
        if not parsed.netloc:
            return "<invalid-url>"
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return "<invalid-url>"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = host
        if ":" in host and not host.startswith("["):
            netloc = f"[{host}]"
        if port is not None and not (
            (parsed.scheme.lower() == "http" and port == 80)
            or (parsed.scheme.lower() == "https" and port == 443)
        ):
            netloc += f":{port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


def _safe_text(value: Any, *, key_hint: str = "") -> Any:
    """Bound and redact a scalar string before it becomes persisted evidence."""

    if isinstance(value, str):
        text = value[:_MAX_METADATA_STRING]
        if len(value) > _MAX_METADATA_STRING:
            text += "…"
    elif isinstance(value, BaseException):
        try:
            text = str(value)
        except Exception:
            text = f"<{_type_name(value)}>"
    else:
        # Do not invoke repr()/str() on an untrusted object merely because it
        # arrived in a text-shaped field.
        text = f"<{_type_name(value)}>"
    if len(text) > _MAX_METADATA_STRING:
        text = f"{text[:_MAX_METADATA_STRING]}…"
    if _HTML_KEY.search(key_hint) or _HTML_VALUE.search(text):
        return _omitted("html", text)
    if _URL_KEY.search(key_hint) and _URL_VALUE.fullmatch(text.strip()):
        return _safe_url(text)
    # Exception messages and free-form diagnostics may contain embedded URLs
    # or key/value credentials.  Redact both before retaining the short text.
    text = _URL_VALUE.sub(lambda match: _safe_url(match.group(0)), text)
    text = _SECRET_TEXT.sub(r"\1<redacted>", text)
    return text


def _omitted(kind: str, value: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"__omitted__": kind}
    if value is not None:
        try:
            result["length"] = len(value)
        except (TypeError, ValueError):
            pass
    return result


def _response_summary(value: Any) -> dict[str, Any]:
    """Keep scalar response facts while excluding body/content attributes."""

    result: dict[str, Any] = {"__type__": _type_name(value)}
    for attribute in ("status_code", "status", "url", "reason"):
        try:
            candidate = getattr(value, attribute)
        except Exception:
            continue
        if candidate is None or isinstance(candidate, (bool, int)):
            result[attribute] = candidate
        elif isinstance(candidate, str):
            if attribute == "url":
                result[attribute] = _safe_url(candidate)
            else:
                safe = _safe_text(candidate)
                result[attribute] = safe if isinstance(safe, str) else _omitted("value")
        elif isinstance(candidate, float) and math.isfinite(candidate):
            result[attribute] = candidate
    return result


def _looks_like_response(value: Any) -> bool:
    value_type = type(value)
    name = f"{value_type.__module__}.{value_type.__qualname__}".lower()
    if "response" in name or "httpresponse" in name:
        return True
    try:
        return hasattr(value, "status_code") and hasattr(value, "url")
    except Exception:
        return False


def _sanitize_value(
    value: Any,
    *,
    key_hint: str = "",
    seen: Optional[set[int]] = None,
    depth: int = 0,
) -> Any:
    seen = seen if seen is not None else set()
    key_hint = str(key_hint).replace("-", "_").lower()

    if _SECRET_KEY.search(key_hint):
        return _omitted("secret")
    if _HTML_KEY.search(key_hint):
        return _omitted("html", value)
    if isinstance(value, BaseException):
        result: dict[str, Any] = {
            "__type__": _type_name(value),
        }
        # str(exception) is untrusted (it can contain HTML, response bodies,
        # credentials, or a full URL). It is only passed through the same
        # scalar sanitizer used for ordinary text, never stored raw.
        try:
            message = str(value)
        except Exception:
            message = ""
        if message:
            result["message"] = _safe_text(
                message[:_MAX_METADATA_STRING], key_hint="exception_message"
            )
        return result
    if isinstance(value, str) and _HTML_VALUE.search(value):
        return _omitted("html", value)
    if _RESPONSE_KEY.search(key_hint):
        if isinstance(value, (str, bytes, bytearray)):
            return _omitted("response_body", value)
        if _looks_like_response(value):
            return _response_summary(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _safe_text(value, key_hint=key_hint)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _omitted("binary", value)
    if _looks_like_response(value):
        return _response_summary(value)
    if depth >= _MAX_METADATA_DEPTH:
        return _omitted("max_depth")

    value_id = id(value)
    if isinstance(value, Mapping):
        if value_id in seen:
            return _omitted("cyclic")
        seen.add(value_id)
        mapping_result: dict[str, Any] = {}
        omitted_keys = 0
        try:
            items = value.items()
            for index, (key, item) in enumerate(items):
                if index >= _MAX_METADATA_ITEMS:
                    omitted_keys += 1
                    break
                if not isinstance(key, str):
                    omitted_keys += 1
                    continue
                safe_key = key[:_MAX_METADATA_KEY]
                if len(key) > _MAX_METADATA_KEY:
                    safe_key = f"{safe_key}…"
                mapping_result[safe_key] = _sanitize_value(
                    item,
                    key_hint=key,
                    seen=seen,
                    depth=depth + 1,
                )
        except Exception:
            return {"__omitted__": "malformed_mapping"}
        finally:
            seen.discard(value_id)
        if omitted_keys:
            mapping_result["_omitted_keys"] = omitted_keys
        return mapping_result
    if isinstance(value, (list, tuple, set, frozenset)):
        if value_id in seen:
            return _omitted("cyclic")
        seen.add(value_id)
        try:
            values = list(value)
            sequence_result: list[Any] = [
                _sanitize_value(item, seen=seen, depth=depth + 1)
                for item in values[:_MAX_METADATA_ITEMS]
            ]
            if len(values) > _MAX_METADATA_ITEMS:
                sequence_result.append(_omitted("items", len(values) - _MAX_METADATA_ITEMS))
            return sequence_result
        except Exception:
            return _omitted("malformed_sequence")
        finally:
            seen.discard(value_id)

    # Unknown objects are represented by their type only.  In particular, do
    # not call repr()/str() here: either may contain a full HTML body or a
    # response payload.
    return {"__type__": _type_name(value)}


def sanitize_metadata(metadata: Any) -> dict[str, Any]:
    """Return a bounded, JSON-compatible metadata object.

    Event metadata is intentionally best-effort.  A malformed root value is
    replaced with an empty object; malformed nested values are represented by
    small type/omission markers so the event itself can still be recorded.
    """

    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        return {}
    result = _sanitize_value(metadata)
    return result if isinstance(result, dict) else {}


def sanitize_json_value(value: Any) -> Any:
    """Sanitize an arbitrary JSON value with the event safety policy.

    Event roots are objects, but storage also has legacy JSON list/scalar
    columns. This helper lets those columns reuse exactly the same
    finite/depth/length/cycle/HTML/URL boundary without weakening their shape.
    """

    return _sanitize_value(value)


def serialize_metadata(metadata: Any) -> str:
    """Serialize event metadata without allowing bad evidence to abort a write."""

    try:
        return json.dumps(
            sanitize_metadata(metadata),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        return "{}"


# A discoverable alias for integrations that prefer the shorter spelling.
safe_serialize_metadata = serialize_metadata
safe_metadata = sanitize_metadata


@dataclass(frozen=True)
class CrawlEvent:
    """One operational event emitted during a crawl run."""

    run_id: str
    zoo_id: Optional[str] = None
    source_id: Optional[str] = None
    created_at: Any = None
    level: str = "INFO"
    component: str = "crawler"
    event_type: str = "event"
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", sanitize_metadata(self.metadata))
        object.__setattr__(self, "level", str(self.level or "INFO").strip().upper())
        object.__setattr__(self, "component", str(self.component or "crawler").strip())
        object.__setattr__(self, "event_type", str(self.event_type or "event").strip())
        object.__setattr__(self, "message", _short_text(self.message, limit=16384))

    @property
    def crawl_run_id(self) -> str:
        """Compatibility alias matching the longer storage vocabulary."""

        return self.run_id

    @property
    def metadata_json(self) -> str:
        return serialize_metadata(self.metadata)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "zoo_id": self.zoo_id,
            "source_id": self.source_id,
            "created_at": self.created_at,
            "level": self.level,
            "component": self.component,
            "event_type": self.event_type,
            "message": self.message,
            "metadata": dict(self.metadata),
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


@dataclass(frozen=True)
class CrawlRunArticle:
    """Durable association between a crawl run and an article outcome."""

    run_id: str
    article_id: str
    source_id: Optional[str] = None
    outcome: str = "stored"
    created_at: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: Optional[int] = None
    updated_at: Any = None
    zoo_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", str(self.run_id or "").strip())
        object.__setattr__(self, "article_id", str(self.article_id or "").strip())
        object.__setattr__(
            self,
            "source_id",
            None if self.source_id is None else str(self.source_id).strip() or None,
        )
        object.__setattr__(
            self,
            "zoo_id",
            None if self.zoo_id is None else str(self.zoo_id).strip() or None,
        )
        object.__setattr__(self, "outcome", str(self.outcome or "stored").strip())
        object.__setattr__(self, "metadata", sanitize_metadata(self.metadata))

    @property
    def crawl_run_id(self) -> str:
        return self.run_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "article_id": self.article_id,
            "zoo_id": self.zoo_id,
            "source_id": self.source_id,
            "outcome": self.outcome,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


@runtime_checkable
class EventSink(Protocol):
    """Storage-shaped boundary consumed by :class:`CrawlEventRecorder`."""

    def record_crawl_event(self, event: CrawlEvent) -> CrawlEvent:
        ...


class CrawlEventRecorder:
    """Emit structured events through an injected storage-shaped sink."""

    def __init__(self, sink: EventSink) -> None:
        self.sink = sink

    def emit(
        self,
        event: Optional[Union[CrawlEvent, Mapping[str, Any]]] = None,
        *,
        run_id: Optional[str] = None,
        zoo_id: Optional[str] = None,
        source_id: Optional[str] = None,
        created_at: Any = None,
        level: Optional[str] = None,
        component: Optional[str] = None,
        event_type: Optional[str] = None,
        message: Optional[str] = None,
        metadata: Any = None,
    ) -> CrawlEvent:
        """Build and persist an event, tolerating malformed metadata."""

        if isinstance(event, str) and run_id is None:
            # A small convenience for ``emit("run-id", event_type=...)``.
            run_id, event = event, None

        if event is None:
            values: dict[str, Any] = {
                "run_id": run_id,
                "zoo_id": zoo_id,
                "source_id": source_id,
                "created_at": created_at,
                "level": level or "INFO",
                "component": component or "crawler",
                "event_type": event_type or "event",
                "message": message or "",
                "metadata": metadata,
            }
        elif isinstance(event, CrawlEvent):
            values = event.as_dict()
            values.pop("id", None)
            values["metadata"] = event.metadata
            for key, value in {
                "run_id": run_id,
                "zoo_id": zoo_id,
                "source_id": source_id,
                "created_at": created_at,
                "level": level,
                "component": component,
                "event_type": event_type,
                "message": message,
                "metadata": metadata,
            }.items():
                if value is not None:
                    values[key] = value
        elif isinstance(event, Mapping):
            values = dict(event)
            if "run_id" not in values and "crawl_run_id" in values:
                values["run_id"] = values.pop("crawl_run_id")
            else:
                values.pop("crawl_run_id", None)
            if "metadata" not in values and "metadata_json" in values:
                try:
                    decoded_metadata = json.loads(values["metadata_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    decoded_metadata = {}
                values["metadata"] = decoded_metadata if isinstance(decoded_metadata, Mapping) else {}
            for key, value in {
                "run_id": run_id,
                "zoo_id": zoo_id,
                "source_id": source_id,
                "created_at": created_at,
                "level": level,
                "component": component,
                "event_type": event_type,
                "message": message,
                "metadata": metadata,
            }.items():
                if value is not None:
                    values[key] = value
        else:
            raise TypeError("event must be a CrawlEvent or mapping")

        values.pop("id", None)
        values.pop("metadata_json", None)
        return self.sink.record_crawl_event(CrawlEvent(**values))

    record = emit
    record_event = emit
    record_crawl_event = emit


__all__ = [
    "CrawlEvent",
    "CrawlEventRecorder",
    "CrawlRunArticle",
    "EventSink",
    "safe_metadata",
    "safe_serialize_metadata",
    "sanitize_json_value",
    "sanitize_metadata",
    "serialize_metadata",
]
