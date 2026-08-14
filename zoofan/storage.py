"""SQLite persistence with an adapter-shaped API.

The methods operate on domain records and keep all schema details here.  A
future PostgreSQL implementation can implement the same methods without
forcing parsers or crawlers to know about SQL.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Union, cast

from .models import (
    Article,
    ArticleDiscovery,
    ArticleReadModel,
    ArticleUpsertOutcome,
    CrawlRun,
    CrawlRunStat,
    CrawlZooResult,
    Source,
    Zoo,
)
from .normalization import normalize_url


def _id(value: Optional[str]) -> str:
    return str(value) if value else uuid.uuid4().hex


def _timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _decoded_timestamp(value: Optional[str]) -> Optional[Union[datetime, str]]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return value


def _discovery_timestamp(value: Any) -> Optional[str]:
    """Canonicalize a discovery timestamp without discarding bad legacy text."""

    raw = _timestamp(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _parsed_discovery_timestamp(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _select_discovery_timestamp(values: Any, *, latest: bool) -> Optional[str]:
    """Select min/max chronologically, with deterministic raw-text fallback.

    An unparseable legacy value remains intact.  When all candidates parse,
    compare aware UTC datetimes; when any candidate is opaque, lexical order
    is only a deterministic fallback because no honest chronology is known.
    """

    candidates = [
        normalized
        for value in values
        if (normalized := _discovery_timestamp(value)) is not None
    ]
    if not candidates:
        return None
    parsed = [(candidate, _parsed_discovery_timestamp(candidate)) for candidate in candidates]
    if all(timestamp is not None for _, timestamp in parsed):
        selected = max if latest else min
        return selected(
            parsed,
            key=lambda item: item[1] or datetime.min.replace(tzinfo=timezone.utc),
        )[0]
    return (max if latest else min)(candidates)


def _json(value: Any) -> str:
    try:
        # Preserve empty lists/tuples.  New zoo registry fields use JSON list
        # values, and turning [] into {} would make a fresh round-trip lossy.
        return json.dumps({} if value is None else value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"


def _load_json(value: Optional[str]) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _load_json_value(value: Optional[str], default: Any = None) -> Any:
    """Decode an arbitrary JSON value while keeping malformed legacy data safe."""

    if value in (None, ""):
        return default
    try:
        raw_value = str(value)
        return json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _normalize_title(value: Optional[str]) -> str:
    """Return a deterministic, human-title identity key.

    URL identity remains the primary global key.  This key is only used in
    the zoo-scoped identity relation, so two zoos can independently publish
    an article with the same title.
    """

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _normalize_legacy_url(value: Any) -> Optional[str]:
    """Normalize a legacy URL without letting corrupt evidence abort startup.

    New writes intentionally continue to call :func:`normalize_url` directly
    and therefore retain its validation behavior.  A legacy database may
    contain values that were accepted before URL parsing was made strict,
    though, so migration treats an unparseable value as an unknown identity
    while retaining the original value in its raw-evidence column.
    """

    if value in (None, ""):
        return None
    try:
        normalized = normalize_url(str(value))
    except (TypeError, UnicodeError, ValueError):
        return None
    return normalized or None


def _first_legacy_url(*values: Any) -> Optional[str]:
    """Return the first usable normalized URL from legacy fallbacks."""

    for value in values:
        normalized = _normalize_legacy_url(value)
        if normalized is not None:
            return normalized
    return None


# Public spelling for adapters that need to build the same title key before
# calling storage; the underscored implementation remains the local helper.
normalize_title = _normalize_title


def _content_identity_key(content_hash: Optional[str], title: Optional[str]) -> Optional[str]:
    """Build the stable, collision-scoped content identity.

    A parsed-content hash alone is not globally unique: boilerplate cards can
    produce the same digest for unrelated articles.  Scoping it by the
    normalized title keeps the database-level uniqueness guarantee while
    allowing those distinct titles to coexist.
    """

    if not content_hash:
        return None
    title_key = _normalize_title(title)
    if not title_key:
        return None
    identity = f"{content_hash}\x00{title_key}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


class SQLiteStorage:
    """Transactional storage for crawl state and article records."""

    SCHEMA_VERSION = 7
    _INIT_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)

    def __init__(self, path: Union[str, Path] = ":memory:", connection: Optional[sqlite3.Connection] = None) -> None:
        self.path = str(path)
        self._connection = connection or sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._initialize_database()

    @staticmethod
    def _is_lock_error(error: sqlite3.OperationalError) -> bool:
        message = str(error).lower()
        return "database is locked" in message or "database is busy" in message

    def _initialize_database(self) -> None:
        """Set up a file database with bounded retries for startup races.

        Two workers can open the same new database at the same time.  WAL
        negotiation and the first schema transaction both contend on SQLite's
        schema lock, so retry the complete initialization as one unit.  The
        retry count is deliberately finite: a permanently unusable database
        must still fail promptly and visibly.
        """

        attempts = len(self._INIT_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                if self.path != ":memory:":
                    self._connection.execute("PRAGMA journal_mode = WAL")
                self.create_schema()
                return
            except sqlite3.OperationalError as error:
                if not self._is_lock_error(error) or attempt >= attempts - 1:
                    raise
                time.sleep(self._INIT_RETRY_DELAYS[attempt])

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def create_schema(self) -> None:
        """Create or transactionally migrate and verify the SQLite schema."""
        with self._lock:
            db = self._connection
            if db.in_transaction:
                # A schema migration may rebuild tables.  Refusing to nest it
                # inside an arbitrary caller transaction avoids rolling back
                # unrelated caller writes on migration failure.
                raise RuntimeError("create_schema cannot run inside an active caller transaction")
            # SQLite cannot add FK constraints with ALTER TABLE.  Disable FK
            # enforcement only for the bounded rebuild transaction, then run
            # both database checks before committing and restore enforcement.
            db.execute("PRAGMA foreign_keys = OFF")
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                self._create_tables(db)
                self._migrate_schema(db)
                db.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
                db.execute(
                    "INSERT INTO schema_meta(key,value) VALUES('version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(self.SCHEMA_VERSION),),
                )
                integrity = db.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise RuntimeError(f"SQLite integrity_check failed: {integrity[0] if integrity else 'no result'}")
                foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
                if foreign_keys:
                    details = [tuple(row) for row in foreign_keys]
                    raise RuntimeError(f"SQLite foreign_key_check failed: {details}")
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.execute("PRAGMA foreign_keys = ON")
            if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise RuntimeError("SQLite foreign key enforcement could not be enabled")

    @staticmethod
    def _create_tables(
        db: sqlite3.Connection,
        suffix: str = "",
        extra_declarations: Optional[Mapping[str, list[str]]] = None,
    ) -> None:
        z, s, a = f"zoos{suffix}", f"sources{suffix}", f"articles{suffix}"
        d, r, rs = f"article_discoveries{suffix}", f"crawl_runs{suffix}", f"crawl_run_stats{suffix}"
        azi, zr = f"article_zoo_identities{suffix}", f"crawl_zoo_results{suffix}"
        extra_declarations = extra_declarations or {}
        columns = {
            "zoos": ["id TEXT PRIMARY KEY", "slug TEXT UNIQUE", "name TEXT", "website_url TEXT", "country_code TEXT", "language TEXT", "groups_json TEXT DEFAULT '[]'", "region TEXT", "city TEXT", "source_status TEXT", "list_provenance_json TEXT DEFAULT '[]'", "enabled INTEGER DEFAULT 1", "metadata_json TEXT DEFAULT '{}'", "created_at TEXT", "updated_at TEXT"],
            "sources": ["id TEXT PRIMARY KEY", "zoo_id TEXT", "url TEXT", "normalized_url TEXT", "kind TEXT DEFAULT 'rss'", "name TEXT", "language TEXT", "config_json TEXT DEFAULT '{}'", "enabled INTEGER DEFAULT 1", "status TEXT DEFAULT 'pending'", "success INTEGER", "last_checked TEXT", "last_success TEXT", "last_error TEXT", "last_http_status INTEGER", "created_at TEXT", "updated_at TEXT"],
            "articles": ["id TEXT PRIMARY KEY", "canonical_url TEXT", "normalized_url TEXT", "source_url TEXT", "source_url_raw TEXT", "title TEXT", "published_at TEXT", "published_at_raw TEXT", "updated_at_source TEXT", "author TEXT", "summary TEXT", "content TEXT", "content_html TEXT", "image_url TEXT", "parse_status TEXT", "content_hash TEXT", "content_identity_key TEXT", "html_hash TEXT", "raw_html TEXT", "language TEXT", "http_status INTEGER", "crawl_status TEXT", "last_fetched_at TEXT", "metadata_json TEXT DEFAULT '{}'", "created_at TEXT", "updated_at TEXT"],
            "article_discoveries": ["id TEXT PRIMARY KEY", "article_id TEXT", "source_id TEXT", "discovered_url TEXT", "discovered_url_raw TEXT", "discovered_key TEXT NOT NULL DEFAULT ''", "discovered_at TEXT", "last_discovered_at TEXT", "metadata_json TEXT DEFAULT '{}'"],
            "crawl_runs": ["id TEXT PRIMARY KEY", "batch_id TEXT UNIQUE", "started_at TEXT", "finished_at TEXT", "duration_ms INTEGER", "status TEXT DEFAULT 'running'", "error TEXT", "metadata_json TEXT DEFAULT '{}'"],
            "crawl_run_stats": ["id TEXT PRIMARY KEY", "crawl_run_id TEXT", "zoo_id TEXT", "source_id TEXT", "status TEXT DEFAULT 'running'", "discovered_count INTEGER DEFAULT 0", "fetched_count INTEGER DEFAULT 0", "stored_count INTEGER DEFAULT 0", "already_known_count INTEGER DEFAULT 0", "duplicate_candidate_count INTEGER DEFAULT 0", "error_count INTEGER DEFAULT 0", "started_at TEXT", "finished_at TEXT", "duration_ms INTEGER", "error TEXT", "errors_json TEXT DEFAULT '[]'", "metadata_json TEXT DEFAULT '{}'"],
            "article_zoo_identities": ["article_id TEXT NOT NULL", "zoo_id TEXT NOT NULL", "title_key TEXT NOT NULL", "created_at TEXT", "updated_at TEXT", "PRIMARY KEY(article_id,zoo_id)"],
            "crawl_zoo_results": ["id TEXT PRIMARY KEY", "crawl_run_id TEXT NOT NULL", "zoo_id TEXT NOT NULL", "zoo_slug TEXT", "zoo_name TEXT", "status TEXT DEFAULT 'running'", "source_status TEXT", "discovered INTEGER DEFAULT 0", "parsed INTEGER DEFAULT 0", "inserted INTEGER DEFAULT 0", "updated INTEGER DEFAULT 0", "failed INTEGER DEFAULT 0", "duplicate_filtered INTEGER DEFAULT 0", "duration_ms INTEGER", "source_url TEXT", "http_status INTEGER", "error_category TEXT", "error_summary TEXT", "started_at TEXT", "finished_at TEXT", "metadata_json TEXT DEFAULT '{}'", "created_at TEXT", "updated_at TEXT"],
        }
        foreign_keys = {
            "sources": [f"FOREIGN KEY(zoo_id) REFERENCES {z}(id) ON UPDATE CASCADE ON DELETE RESTRICT"],
            "article_discoveries": [f"FOREIGN KEY(article_id) REFERENCES {a}(id) ON UPDATE CASCADE ON DELETE CASCADE", f"FOREIGN KEY(source_id) REFERENCES {s}(id) ON UPDATE CASCADE ON DELETE CASCADE"],
            "crawl_run_stats": [f"FOREIGN KEY(crawl_run_id) REFERENCES {r}(id) ON UPDATE CASCADE ON DELETE CASCADE", f"FOREIGN KEY(zoo_id) REFERENCES {z}(id) ON UPDATE CASCADE ON DELETE SET NULL", f"FOREIGN KEY(source_id) REFERENCES {s}(id) ON UPDATE CASCADE ON DELETE SET NULL"],
            "article_zoo_identities": [f"FOREIGN KEY(article_id) REFERENCES {a}(id) ON UPDATE CASCADE ON DELETE CASCADE", f"FOREIGN KEY(zoo_id) REFERENCES {z}(id) ON UPDATE CASCADE ON DELETE CASCADE"],
            "crawl_zoo_results": [f"FOREIGN KEY(crawl_run_id) REFERENCES {r}(id) ON UPDATE CASCADE ON DELETE CASCADE", f"FOREIGN KEY(zoo_id) REFERENCES {z}(id) ON UPDATE CASCADE ON DELETE CASCADE"],
        }
        physical = {"zoos": z, "sources": s, "articles": a, "article_discoveries": d, "crawl_runs": r, "crawl_run_stats": rs, "article_zoo_identities": azi, "crawl_zoo_results": zr}
        for logical, table in physical.items():
            declarations = columns[logical] + list(extra_declarations.get(logical, [])) + foreign_keys.get(logical, [])
            db.execute(f"CREATE TABLE IF NOT EXISTS {table} ({','.join(declarations)})")

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row["name"]) for row in db.execute("PRAGMA table_info(" + table + ")").fetchall()}

    @staticmethod
    def _quoted_identifier(value: str) -> str:
        return '"' + str(value).replace('"', '""') + '"'

    @classmethod
    def _extra_column_declaration(cls, row: sqlite3.Row) -> str:
        """Recreate a compatible extension column from SQLite metadata.

        Generated/hidden and additional primary-key columns cannot safely be
        reconstructed from PRAGMA metadata alone, so migration aborts instead
        of silently weakening or discarding those definitions.
        """
        name = cls._quoted_identifier(str(row["name"]))
        if int(row["pk"] or 0) or int(row["hidden"] or 0):
            raise RuntimeError(f"unsupported legacy extension column: {row['name']}")
        declared_type = str(row["type"] or "").strip()
        if "\x00" in declared_type or ";" in declared_type:
            raise RuntimeError(f"unsafe declared type for legacy extension column: {row['name']}")
        parts = [name]
        if declared_type:
            parts.append(declared_type)
        if int(row["notnull"] or 0):
            parts.append("NOT NULL")
        if row["dflt_value"] is not None:
            default = str(row["dflt_value"])
            if "\x00" in default or ";" in default:
                raise RuntimeError(f"unsafe default for legacy extension column: {row['name']}")
            # PRAGMA table_xinfo reports the expression without its outer
            # parentheses. Parenthesizing every default is valid for literal
            # defaults too and preserves expression-default compatibility in
            # the replacement CREATE TABLE statement.
            parts.extend(("DEFAULT", f"({default})"))
        return " ".join(parts)

    @staticmethod
    def _table_definition_parts(sql: str) -> list[str]:
        """Split a CREATE TABLE body at top-level commas.

        SQLite does not expose the original declaration for an extension
        column through ``table_xinfo``.  This small tokenizer is intentionally
        limited to the SQL grammar needed for declarations, but handles
        quoted identifiers, string defaults, and nested default expressions so
        an ordinary type/default is not mistaken for a constraint.
        """

        opening = sql.find("(")
        if opening < 0:
            return []
        parts: list[str] = []
        start = opening + 1
        depth = 1
        quote: Optional[str] = None
        index = start
        while index < len(sql) and depth:
            char = sql[index]
            if quote is not None:
                if char == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
                elif quote == "]" and char == "]":
                    quote = None
            else:
                if char in ("'", '"', "`"):
                    quote = char
                elif char == "[":
                    quote = "]"
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        parts.append(sql[start:index].strip())
                        break
                elif char == "," and depth == 1:
                    parts.append(sql[start:index].strip())
                    start = index + 1
            index += 1
        return [part for part in parts if part]

    @classmethod
    def _table_column_declarations(cls, sql: str) -> dict[str, str]:
        declarations: dict[str, str] = {}
        for part in cls._table_definition_parts(sql):
            match = re.match(
                r"^\s*(?:\"((?:\"\"|[^\"])*)\"|`((?:``|[^`])*)`|\[((?:\]\]|[^\]])*)\]|([^\s]+))",
                part,
            )
            if not match:
                continue
            quoted_name = next((group for group in match.groups()[:3] if group is not None), None)
            name = (
                quoted_name.replace('""', '"').replace("``", "`").replace("]]", "]")
                if quoted_name is not None
                else str(match.group(4))
            )
            declarations[name] = part
        return declarations

    @staticmethod
    def _sql_without_quoted_text(value: str) -> str:
        """Mask quoted SQL text before checking declaration keywords."""

        output: list[str] = []
        quote: Optional[str] = None
        index = 0
        while index < len(value):
            char = value[index]
            if quote is not None:
                if char == quote:
                    if index + 1 < len(value) and value[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
                elif quote == "]" and char == "]":
                    quote = None
                output.append(" ")
            elif char in ("'", '"', "`"):
                quote = char
                output.append(" ")
            elif char == "[":
                quote = "]"
                output.append(" ")
            else:
                output.append(char)
            index += 1
        return "".join(output)

    @staticmethod
    def _add_columns(db: sqlite3.Connection, table: str, definitions: Mapping[str, str]) -> None:
        columns = SQLiteStorage._columns(db, table)
        for name, definition in definitions.items():
            if name not in columns:
                db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)

    @staticmethod
    def _ensure_ids(db: sqlite3.Connection, table: str) -> None:
        rows = db.execute("SELECT rowid, id FROM " + table).fetchall()
        for row in rows:
            if not row["id"]:
                db.execute("UPDATE " + table + " SET id=? WHERE rowid=?", (_id(None), row["rowid"]))

    @staticmethod
    def _dedup_by_column(db: sqlite3.Connection, table: str, column: str, ref_table: Optional[str] = None) -> None:
        rows = db.execute(
            "SELECT " + column + ", MIN(rowid) AS keep_rowid FROM " + table +
            " WHERE " + column + " IS NOT NULL AND " + column + " <> '' GROUP BY " + column + " HAVING COUNT(*) > 1"
        ).fetchall()
        for row in rows:
            duplicates = db.execute("SELECT rowid, id FROM " + table + " WHERE " + column + "=? AND rowid<>?", (row[column], row["keep_rowid"])).fetchall()
            for duplicate in duplicates:
                if ref_table and duplicate["id"]:
                    db.execute("UPDATE " + ref_table + " SET " + ("article_id" if table == "articles" else "source_id") + "= (SELECT id FROM " + table + " WHERE rowid=?) WHERE " + ("article_id" if table == "articles" else "source_id") + "=?", (row["keep_rowid"], duplicate["id"]))
                db.execute("DELETE FROM " + table + " WHERE rowid=?", (duplicate["rowid"],))

    @staticmethod
    def _merge_discovery_reference(db: sqlite3.Connection, *, column: str, donor_id: str, keeper_id: str) -> None:
        """Retarget discoveries, coalescing relationship collisions safely."""
        for donor in db.execute(f"SELECT * FROM article_discoveries WHERE {column}=?", (donor_id,)).fetchall():
            article_id = keeper_id if column == "article_id" else donor["article_id"]
            source_id = keeper_id if column == "source_id" else donor["source_id"]
            existing = db.execute(
                "SELECT * FROM article_discoveries WHERE article_id IS ? AND source_id IS ? AND discovered_key=? AND id<>? LIMIT 1",
                (article_id, source_id, donor["discovered_key"], donor["id"]),
            ).fetchone()
            if existing:
                first = _select_discovery_timestamp(
                    (existing["discovered_at"], donor["discovered_at"]), latest=False
                )
                last = _select_discovery_timestamp(
                    (
                        existing["last_discovered_at"], donor["last_discovered_at"],
                        existing["discovered_at"], donor["discovered_at"],
                    ),
                    latest=True,
                )
                metadata = {**_load_json(donor["metadata_json"]), **_load_json(existing["metadata_json"])}
                discovered_url = existing["discovered_url"] or donor["discovered_url"]
                discovered_url_raw = existing["discovered_url_raw"] or donor["discovered_url_raw"]
                db.execute(
                    "UPDATE article_discoveries SET discovered_url=?,discovered_url_raw=?,discovered_at=?,last_discovered_at=?,metadata_json=? WHERE id=?",
                    (discovered_url, discovered_url_raw, first, last, _json(metadata), existing["id"]),
                )
                db.execute("DELETE FROM article_discoveries WHERE id=?", (donor["id"],))
            else:
                db.execute(f"UPDATE article_discoveries SET {column}=? WHERE id=?", (keeper_id, donor["id"]))

    @staticmethod
    def _merge_article_zoo_identity_reference(
        db: sqlite3.Connection, *, donor_id: str, keeper_id: str
    ) -> None:
        """Retarget article/zoo identities while coalescing PK collisions."""

        if not SQLiteStorage._columns(db, "article_zoo_identities"):
            return
        identities = db.execute(
            "SELECT zoo_id,title_key FROM article_zoo_identities WHERE article_id=?",
            (donor_id,),
        ).fetchall()
        for identity in identities:
            existing = db.execute(
                "SELECT 1 FROM article_zoo_identities WHERE article_id=? AND zoo_id=?",
                (keeper_id, identity["zoo_id"]),
            ).fetchone()
            if existing:
                db.execute(
                    "DELETE FROM article_zoo_identities WHERE article_id=? AND zoo_id=?",
                    (donor_id, identity["zoo_id"]),
                )
            else:
                db.execute(
                    "UPDATE article_zoo_identities SET article_id=? WHERE article_id=? AND zoo_id=?",
                    (keeper_id, donor_id, identity["zoo_id"]),
                )

    @classmethod
    def _merge_runtime_article_conflict(
        cls, db: sqlite3.Connection, *, donor_id: str, keeper_id: str
    ) -> None:
        """Resolve a URL/hash-vs-title collision deterministically.

        The globally identified URL/hash row is the keeper.  The title-only
        row is removed only after its discoveries, zoo identities, metadata,
        and non-conflicting evidence have been retained on the keeper.
        """

        if donor_id == keeper_id:
            return
        keeper = db.execute("SELECT * FROM articles WHERE id=?", (keeper_id,)).fetchone()
        donor = db.execute("SELECT * FROM articles WHERE id=?", (donor_id,)).fetchone()
        if not keeper or not donor:
            return
        evidence = (
            "canonical_url", "normalized_url", "source_url", "source_url_raw", "title", "published_at",
            "published_at_raw", "updated_at_source", "author", "summary", "content",
            "content_html", "image_url", "parse_status", "content_hash", "html_hash",
            "raw_html", "language", "http_status", "crawl_status", "last_fetched_at",
        )
        merged = {name: keeper[name] for name in evidence}
        metadata = _load_json(keeper["metadata_json"])
        donor_metadata = _load_json(donor["metadata_json"])
        for key, value in donor_metadata.items():
            metadata.setdefault(key, value)
        conflicts = {
            name: donor[name]
            for name in evidence
            if name != "source_url_raw"
            and donor[name] not in (None, "")
            and merged[name] not in (None, "")
            and donor[name] != merged[name]
        }
        provenance = metadata.setdefault("_runtime_merge_provenance", [])
        if not isinstance(provenance, list):
            provenance = []
            metadata["_runtime_merge_provenance"] = provenance
        provenance.append(
            {
                "donor_article_id": donor_id,
                "conflicting_evidence": conflicts,
                "donor_metadata": donor_metadata,
            }
        )
        for name in evidence:
            if merged[name] in (None, "") and donor[name] not in (None, ""):
                # Never introduce a donor URL/hash that collides with a third
                # article; identity ownership remains deterministic.
                if name in {"canonical_url", "normalized_url", "content_hash"}:
                    collision = db.execute(
                        f"SELECT id FROM articles WHERE {name}=? AND id NOT IN (?,?) LIMIT 1",
                        (donor[name], keeper_id, donor_id),
                    ).fetchone()
                    if collision:
                        continue
                merged[name] = donor[name]
        cls._merge_discovery_reference(db, column="article_id", donor_id=donor_id, keeper_id=keeper_id)
        cls._merge_article_zoo_identity_reference(db, donor_id=donor_id, keeper_id=keeper_id)
        db.execute("DELETE FROM articles WHERE id=?", (donor_id,))
        assignments = ",".join(f"{name}=?" for name in evidence)
        db.execute(
            f"UPDATE articles SET {assignments},metadata_json=? WHERE id=?",
            (*[merged[name] for name in evidence], _json(metadata), keeper_id),
        )

    @classmethod
    def _consolidate_legacy_articles(cls, db: sqlite3.Connection) -> None:
        evidence = (
            "canonical_url", "normalized_url", "source_url", "source_url_raw", "title", "published_at",
            "published_at_raw", "updated_at_source", "author", "summary", "content",
            "content_html", "image_url", "parse_status", "content_hash", "html_hash",
            "raw_html", "language", "http_status", "crawl_status", "last_fetched_at",
        )
        while True:
            duplicate_column: Optional[str] = None
            duplicate_value: Optional[str] = None
            for column in ("canonical_url", "normalized_url"):
                duplicate = db.execute(
                    f"SELECT {column} AS value FROM articles WHERE {column} IS NOT NULL AND {column}<>'' GROUP BY {column} HAVING COUNT(*)>1 LIMIT 1"
                ).fetchone()
                if duplicate:
                    duplicate_column = column
                    duplicate_value = str(duplicate["value"])
                    break
            if duplicate_column is None:
                # A content hash may collide across unrelated pages.  It is
                # safe as a migration merge signal only when the normalized
                # titles agree; otherwise leave both rows for the non-unique
                # evidence index and preserve distinct article records.
                hash_groups = db.execute(
                    "SELECT content_hash AS value FROM articles "
                    "WHERE content_hash IS NOT NULL AND content_hash<>'' "
                    "GROUP BY content_hash HAVING COUNT(*)>1"
                ).fetchall()
                for group in hash_groups:
                    hash_rows = db.execute(
                        "SELECT title FROM articles WHERE content_hash=?",
                        (group["value"],),
                    ).fetchall()
                    title_keys = {_normalize_title(row["title"]) for row in hash_rows}
                    if len(title_keys) == 1 and "" not in title_keys:
                        duplicate_column = "content_hash"
                        duplicate_value = str(group["value"])
                        break
            if duplicate_column is None or duplicate_value is None:
                return
            rows = db.execute(
                f"SELECT rowid,* FROM articles WHERE {duplicate_column}=?",
                (duplicate_value,),
            ).fetchall()
            keeper = max(rows, key=lambda row: (sum(row[name] not in (None, "") for name in evidence), len(row["content"] or "") + len(row["raw_html"] or ""), -row["rowid"]))
            merged = {name: keeper[name] for name in evidence}
            metadata = _load_json(keeper["metadata_json"])
            merged_ids = list(metadata.get("merged_legacy_article_ids", []))
            provenance = list(metadata.get("_migration_provenance", []))
            for donor in rows:
                if donor["id"] == keeper["id"]:
                    continue
                merged_ids.append(donor["id"])
                donor_metadata = _load_json(donor["metadata_json"])
                donor_merged_ids = donor_metadata.get("merged_legacy_article_ids", [])
                if isinstance(donor_merged_ids, list):
                    merged_ids.extend(str(value) for value in donor_merged_ids if value)
                donor_provenance = donor_metadata.get("_migration_provenance", [])
                if isinstance(donor_provenance, list):
                    for snapshot in donor_provenance:
                        if not isinstance(snapshot, dict) or not snapshot.get("donor_article_id"):
                            continue
                        flattened = dict(snapshot)
                        nested_metadata = flattened.get("donor_metadata")
                        if isinstance(nested_metadata, dict):
                            flattened["donor_metadata"] = {
                                key: value for key, value in nested_metadata.items()
                                if key not in {"_migration_provenance", "merged_legacy_article_ids"}
                            }
                        provenance.append(flattened)
                # Keep the surviving row's primary evidence unchanged, but
                # retain every conflicting donor value in reserved migration
                # provenance.  Reserved provenance keys are intentionally
                # stripped from nested donor metadata to prevent recursion if
                # an already-migrated database is imported later.
                donor_user_metadata = {
                    key: value for key, value in donor_metadata.items()
                    if key not in {"_migration_provenance", "merged_legacy_article_ids"}
                }
                conflicts = {
                    name: donor[name] for name in evidence
                    if name != "source_url_raw"
                    and donor[name] not in (None, "") and merged[name] not in (None, "") and donor[name] != merged[name]
                }
                provenance.append({
                    "donor_article_id": donor["id"],
                    "conflicting_evidence": conflicts,
                    "donor_metadata": donor_user_metadata,
                })
                # Non-conflicting donor metadata remains directly available;
                # conflicting keys remain namespaced in the snapshot above.
                for key, value in donor_user_metadata.items():
                    metadata.setdefault(key, value)
                for name in evidence:
                    if merged[name] in (None, "") and donor[name] not in (None, ""):
                        merged[name] = donor[name]
                cls._merge_discovery_reference(db, column="article_id", donor_id=donor["id"], keeper_id=keeper["id"])
                cls._merge_article_zoo_identity_reference(db, donor_id=donor["id"], keeper_id=keeper["id"])
                db.execute("DELETE FROM articles WHERE id=?", (donor["id"],))
            metadata["merged_legacy_article_ids"] = sorted(set(merged_ids))
            # Transitive merges may present the same historical donor through
            # multiple paths. Preserve one deterministic flat snapshot per ID.
            by_donor_id = {
                str(snapshot["donor_article_id"]): snapshot
                for snapshot in provenance
                if isinstance(snapshot, dict) and snapshot.get("donor_article_id")
            }
            metadata["_migration_provenance"] = [by_donor_id[key] for key in sorted(by_donor_id)]
            assignments = ",".join(f"{name}=?" for name in evidence)
            db.execute(
                f"UPDATE articles SET {assignments},metadata_json=? WHERE id=?",
                (*[merged[name] for name in evidence], _json(metadata), keeper["id"]),
            )

    @classmethod
    def _consolidate_legacy_sources(cls, db: sqlite3.Connection) -> None:
        groups = db.execute(
            "SELECT zoo_id,normalized_url FROM sources WHERE normalized_url IS NOT NULL AND normalized_url<>'' GROUP BY zoo_id,normalized_url HAVING COUNT(*)>1"
        ).fetchall()
        evidence = ("last_checked", "last_success", "last_error", "last_http_status")
        for group in groups:
            rows = db.execute("SELECT rowid,* FROM sources WHERE zoo_id IS ? AND normalized_url=?", (group["zoo_id"], group["normalized_url"])).fetchall()
            keeper = max(rows, key=lambda row: (sum(row[name] is not None for name in evidence), -row["rowid"]))
            values = {name: keeper[name] for name in evidence}
            for donor in rows:
                if donor["id"] == keeper["id"]:
                    continue
                for name in evidence:
                    if values[name] is None and donor[name] is not None:
                        values[name] = donor[name]
                cls._merge_discovery_reference(db, column="source_id", donor_id=donor["id"], keeper_id=keeper["id"])
                db.execute("UPDATE crawl_run_stats SET source_id=? WHERE source_id=?", (keeper["id"], donor["id"]))
                db.execute("DELETE FROM sources WHERE id=?", (donor["id"],))
            db.execute(
                "UPDATE sources SET last_checked=?,last_success=?,last_error=?,last_http_status=? WHERE id=?",
                (*[values[name] for name in evidence], keeper["id"]),
            )

    @classmethod
    def _consolidate_legacy_discoveries(cls, db: sqlite3.Connection) -> None:
        """Coalesce normalized duplicate discovery identities before indexing."""

        while True:
            duplicate = db.execute(
                """
                SELECT article_id,source_id,discovered_key
                FROM article_discoveries
                GROUP BY article_id,source_id,discovered_key
                HAVING COUNT(*)>1 LIMIT 1
                """
            ).fetchone()
            if not duplicate:
                return
            rows = db.execute(
                """
                SELECT rowid,* FROM article_discoveries
                WHERE article_id IS ? AND source_id IS ? AND discovered_key IS ?
                ORDER BY rowid
                """,
                (duplicate["article_id"], duplicate["source_id"], duplicate["discovered_key"]),
            ).fetchall()
            keeper = rows[0]
            first_values = [row["discovered_at"] for row in rows]
            last_values = [
                value for row in rows
                for value in (row["discovered_at"], row["last_discovered_at"])
            ]
            metadata: dict[str, Any] = {}
            discovered_url = None
            discovered_url_raw = None
            for row in rows:
                metadata.update(_load_json(row["metadata_json"]))
                if discovered_url is None and row["discovered_url"]:
                    discovered_url = row["discovered_url"]
                if discovered_url_raw is None and row["discovered_url_raw"]:
                    discovered_url_raw = row["discovered_url_raw"]
            # Keeper metadata wins on conflicts, while all donor-only keys
            # survive.  This mirrors the existing article/source merge rule.
            metadata = {
                **{key: value for row in rows[1:] for key, value in _load_json(row["metadata_json"]).items()},
                **_load_json(keeper["metadata_json"]),
            }
            first = _select_discovery_timestamp(first_values, latest=False)
            last = _select_discovery_timestamp(last_values, latest=True) or first
            for donor in rows[1:]:
                db.execute("DELETE FROM article_discoveries WHERE id=?", (donor["id"],))
            db.execute(
                """
                UPDATE article_discoveries
                SET discovered_url=?,discovered_url_raw=?,discovered_at=?,last_discovered_at=?,metadata_json=?
                WHERE id=?
                """,
                (discovered_url, discovered_url_raw, first, last, _json(metadata), keeper["id"]),
            )

    @classmethod
    def _backfill_article_zoo_identities(cls, db: sqlite3.Connection) -> None:
        """Backfill zoo-scoped title identities from discovery provenance.

        Legacy article rows did not carry a zoo column.  The source relation is
        the only authoritative way to recover that scope, so rows without a
        discovery are intentionally left without a fabricated identity.
        """

        now = datetime.now(timezone.utc).isoformat()
        rows = db.execute(
            """
            SELECT DISTINCT a.id AS article_id, s.zoo_id, a.title
            FROM articles AS a
            JOIN article_discoveries AS d ON d.article_id=a.id
            JOIN sources AS s ON s.id=d.source_id
            WHERE s.zoo_id IS NOT NULL AND a.title IS NOT NULL AND TRIM(a.title)<>''
            """
        ).fetchall()
        for row in rows:
            title_key = _normalize_title(row["title"])
            if not title_key:
                continue
            db.execute(
                """
                INSERT OR IGNORE INTO article_zoo_identities(article_id,zoo_id,title_key,created_at,updated_at)
                VALUES(?,?,?,?,?)
                """,
                (row["article_id"], row["zoo_id"], title_key, now, now),
            )

    @classmethod
    def _consolidate_legacy_title_identities(cls, db: sqlite3.Connection) -> None:
        """Merge pre-existing duplicate zoo/title identities before indexing."""

        evidence = (
            "canonical_url", "normalized_url", "source_url", "source_url_raw", "title", "published_at",
            "published_at_raw", "updated_at_source", "author", "summary", "content",
            "content_html", "image_url", "parse_status", "content_hash", "html_hash",
            "raw_html", "language", "http_status", "crawl_status", "last_fetched_at",
        )
        while True:
            duplicate = db.execute(
                """
                SELECT zoo_id,title_key
                FROM article_zoo_identities
                WHERE zoo_id IS NOT NULL AND title_key IS NOT NULL AND title_key<>''
                GROUP BY zoo_id,title_key HAVING COUNT(*)>1 LIMIT 1
                """
            ).fetchone()
            if not duplicate:
                return
            rows = db.execute(
                """
                SELECT a.rowid AS article_rowid,a.*,i.zoo_id,i.title_key
                FROM article_zoo_identities AS i
                JOIN articles AS a ON a.id=i.article_id
                WHERE i.zoo_id=? AND i.title_key=?
                """,
                (duplicate["zoo_id"], duplicate["title_key"]),
            ).fetchall()
            if len(rows) < 2:
                continue
            keeper = max(
                rows,
                key=lambda row: (
                    sum(row[name] not in (None, "") for name in evidence),
                    len(row["content"] or "") + len(row["raw_html"] or ""),
                    -row["article_rowid"],
                ),
            )
            merged = {name: keeper[name] for name in evidence}
            metadata = _load_json(keeper["metadata_json"])
            merged_ids = list(metadata.get("merged_legacy_article_ids", []))
            provenance = list(metadata.get("_migration_provenance", []))
            for donor in rows:
                if donor["id"] == keeper["id"]:
                    continue
                merged_ids.append(donor["id"])
                donor_metadata = _load_json(donor["metadata_json"])
                donor_user_metadata = {
                    key: value
                    for key, value in donor_metadata.items()
                    if key not in {"_migration_provenance", "merged_legacy_article_ids"}
                }
                conflicts = {
                    name: donor[name]
                    for name in evidence
                    if donor[name] not in (None, "")
                    and merged[name] not in (None, "")
                    and donor[name] != merged[name]
                }
                provenance.append(
                    {
                        "donor_article_id": donor["id"],
                        "conflicting_evidence": conflicts,
                        "donor_metadata": donor_user_metadata,
                    }
                )
                for key, value in donor_user_metadata.items():
                    metadata.setdefault(key, value)
                for name in evidence:
                    if merged[name] in (None, "") and donor[name] not in (None, ""):
                        merged[name] = donor[name]

                cls._merge_discovery_reference(
                    db, column="article_id", donor_id=donor["id"], keeper_id=keeper["id"]
                )
                cls._merge_article_zoo_identity_reference(
                    db, donor_id=donor["id"], keeper_id=keeper["id"]
                )
                db.execute("DELETE FROM articles WHERE id=?", (donor["id"],))
            metadata["merged_legacy_article_ids"] = sorted(set(merged_ids))
            by_donor_id = {
                str(snapshot["donor_article_id"]): snapshot
                for snapshot in provenance
                if isinstance(snapshot, dict) and snapshot.get("donor_article_id")
            }
            metadata["_migration_provenance"] = [
                by_donor_id[key] for key in sorted(by_donor_id)
            ]
            assignments = ",".join(f"{name}=?" for name in evidence)
            db.execute(
                f"UPDATE articles SET {assignments},metadata_json=? WHERE id=?",
                (*[merged[name] for name in evidence], _json(metadata), keeper["id"]),
            )

    @classmethod
    def _consolidate_legacy_zoo_results(cls, db: sqlite3.Connection) -> None:
        """Merge duplicate legacy run/zoo rows before the unique index exists."""

        fields = (
            "status", "source_status", "discovered", "parsed", "inserted", "updated",
            "failed", "duplicate_filtered", "duration_ms", "source_url", "http_status",
            "error_category", "error_summary", "started_at", "finished_at",
        )
        while True:
            duplicate = db.execute(
                """
                SELECT crawl_run_id,zoo_id FROM crawl_zoo_results
                WHERE crawl_run_id IS NOT NULL AND zoo_id IS NOT NULL
                GROUP BY crawl_run_id,zoo_id HAVING COUNT(*)>1 LIMIT 1
                """
            ).fetchone()
            if not duplicate:
                return
            rows = db.execute(
                "SELECT rowid,* FROM crawl_zoo_results WHERE crawl_run_id=? AND zoo_id=?",
                (duplicate["crawl_run_id"], duplicate["zoo_id"]),
            ).fetchall()
            keeper = max(
                rows,
                key=lambda row: (
                    sum(row[name] not in (None, "") for name in fields),
                    str(row["finished_at"] or row["started_at"] or ""),
                    -row["rowid"],
                ),
            )
            merged = {name: keeper[name] for name in fields}
            metadata = _load_json(keeper["metadata_json"])
            merged_ids = list(metadata.get("merged_legacy_result_ids", []))
            for donor in rows:
                if donor["id"] == keeper["id"]:
                    continue
                merged_ids.append(donor["id"])
                donor_metadata = _load_json(donor["metadata_json"])
                for key, value in donor_metadata.items():
                    metadata.setdefault(key, value)
                for name in fields:
                    if merged[name] in (None, "") and donor[name] not in (None, ""):
                        merged[name] = donor[name]
                db.execute("DELETE FROM crawl_zoo_results WHERE id=?", (donor["id"],))
            metadata["merged_legacy_result_ids"] = sorted(set(merged_ids))
            assignments = ",".join(f"{name}=?" for name in fields)
            db.execute(
                f"UPDATE crawl_zoo_results SET {assignments},metadata_json=? WHERE id=?",
                (*[merged[name] for name in fields], _json(metadata), keeper["id"]),
            )

    @staticmethod
    def _backfill_content_identity_keys(db: sqlite3.Connection) -> None:
        """Derive composite content identities after legacy merges."""

        for row in db.execute("SELECT rowid,content_hash,title FROM articles").fetchall():
            key = _content_identity_key(row["content_hash"], row["title"])
            db.execute(
                "UPDATE articles SET content_identity_key=? WHERE rowid=?",
                (key, row["rowid"]),
            )

    def _migrate_schema(self, db: sqlite3.Connection) -> None:
        now = datetime.now(timezone.utc).isoformat()
        definitions = {
            "zoos": {"id": "TEXT", "slug": "TEXT", "name": "TEXT", "website_url": "TEXT", "country_code": "TEXT", "language": "TEXT", "groups_json": "TEXT DEFAULT '[]'", "region": "TEXT", "city": "TEXT", "source_status": "TEXT", "list_provenance_json": "TEXT DEFAULT '[]'", "enabled": "INTEGER DEFAULT 1", "metadata_json": "TEXT DEFAULT '{}'", "created_at": "TEXT", "updated_at": "TEXT"},
            "sources": {"id": "TEXT", "zoo_id": "TEXT", "url": "TEXT", "normalized_url": "TEXT", "kind": "TEXT DEFAULT 'rss'", "name": "TEXT", "language": "TEXT", "config_json": "TEXT DEFAULT '{}'", "enabled": "INTEGER DEFAULT 1", "status": "TEXT DEFAULT 'pending'", "success": "INTEGER", "last_checked": "TEXT", "last_success": "TEXT", "last_error": "TEXT", "last_http_status": "INTEGER", "created_at": "TEXT", "updated_at": "TEXT"},
            "articles": {"id": "TEXT", "canonical_url": "TEXT", "normalized_url": "TEXT", "source_url": "TEXT", "source_url_raw": "TEXT", "title": "TEXT", "published_at": "TEXT", "published_at_raw": "TEXT", "updated_at_source": "TEXT", "author": "TEXT", "summary": "TEXT", "content": "TEXT", "content_html": "TEXT", "image_url": "TEXT", "parse_status": "TEXT", "content_hash": "TEXT", "content_identity_key": "TEXT", "html_hash": "TEXT", "raw_html": "TEXT", "language": "TEXT", "http_status": "INTEGER", "crawl_status": "TEXT", "last_fetched_at": "TEXT", "metadata_json": "TEXT DEFAULT '{}'", "created_at": "TEXT", "updated_at": "TEXT"},
            "article_discoveries": {"id": "TEXT", "article_id": "TEXT", "source_id": "TEXT", "discovered_url": "TEXT", "discovered_url_raw": "TEXT", "discovered_key": "TEXT NOT NULL DEFAULT ''", "discovered_at": "TEXT", "last_discovered_at": "TEXT", "metadata_json": "TEXT DEFAULT '{}'"},
            "crawl_runs": {"id": "TEXT", "batch_id": "TEXT", "started_at": "TEXT", "finished_at": "TEXT", "duration_ms": "INTEGER", "status": "TEXT DEFAULT 'running'", "error": "TEXT", "metadata_json": "TEXT DEFAULT '{}'"},
            "crawl_run_stats": {"id": "TEXT", "crawl_run_id": "TEXT", "zoo_id": "TEXT", "source_id": "TEXT", "status": "TEXT DEFAULT 'running'", "discovered_count": "INTEGER DEFAULT 0", "fetched_count": "INTEGER DEFAULT 0", "stored_count": "INTEGER DEFAULT 0", "already_known_count": "INTEGER DEFAULT 0", "duplicate_candidate_count": "INTEGER DEFAULT 0", "error_count": "INTEGER DEFAULT 0", "started_at": "TEXT", "finished_at": "TEXT", "duration_ms": "INTEGER", "error": "TEXT", "errors_json": "TEXT DEFAULT '[]'", "metadata_json": "TEXT DEFAULT '{}'"},
            "article_zoo_identities": {"article_id": "TEXT", "zoo_id": "TEXT", "title_key": "TEXT", "created_at": "TEXT", "updated_at": "TEXT"},
            "crawl_zoo_results": {"id": "TEXT", "crawl_run_id": "TEXT", "zoo_id": "TEXT", "zoo_slug": "TEXT", "zoo_name": "TEXT", "status": "TEXT DEFAULT 'running'", "source_status": "TEXT", "discovered": "INTEGER DEFAULT 0", "parsed": "INTEGER DEFAULT 0", "inserted": "INTEGER DEFAULT 0", "updated": "INTEGER DEFAULT 0", "failed": "INTEGER DEFAULT 0", "duplicate_filtered": "INTEGER DEFAULT 0", "duration_ms": "INTEGER", "source_url": "TEXT", "http_status": "INTEGER", "error_category": "TEXT", "error_summary": "TEXT", "started_at": "TEXT", "finished_at": "TEXT", "metadata_json": "TEXT DEFAULT '{}'", "created_at": "TEXT", "updated_at": "TEXT"},
        }
        legacy_zoo_columns = self._columns(db, "zoos")
        for table, columns in definitions.items():
            self._add_columns(db, table, columns)
        for table in ("zoos", "sources", "articles", "article_discoveries", "crawl_runs", "crawl_run_stats", "crawl_zoo_results"):
            self._ensure_ids(db, table)
        # Legacy schemas used url rather than source_url.  Do not infer either
        # hash from the other: legacy NULL evidence must remain unknown.
        article_cols = self._columns(db, "articles")
        if "url" in article_cols:
            db.execute("UPDATE articles SET source_url=COALESCE(source_url,url) WHERE source_url IS NULL")
        # Preserve legacy spellings before canonical identity cleanup.  Fresh
        # rows write these columns directly; old rows use their best available
        # URL field as the one-time provenance backfill.
        db.execute(
            "UPDATE articles SET source_url_raw=COALESCE(source_url_raw,source_url,canonical_url) "
            "WHERE source_url_raw IS NULL"
        )
        # Empty legacy identity values mean "unknown", not a real shared
        # identity.  Convert them to NULL before the unique indexes are
        # created; SQLite permits multiple NULLs and the migration does not
        # invent a URL or content hash from another field.
        db.execute(
            "UPDATE articles SET canonical_url=NULLIF(canonical_url,''), "
            "normalized_url=NULLIF(normalized_url,''), content_hash=NULLIF(content_hash,'')"
        )
        db.execute(
            "UPDATE articles SET normalized_url=COALESCE(normalized_url, canonical_url, NULLIF(source_url,''))"
        )
        for row in db.execute("SELECT rowid, canonical_url, normalized_url, source_url FROM articles").fetchall():
            canonical = _normalize_legacy_url(row["canonical_url"])
            normalized = _first_legacy_url(
                row["normalized_url"], row["canonical_url"], row["source_url"]
            )
            db.execute("UPDATE articles SET canonical_url=?, normalized_url=? WHERE rowid=?", (canonical, normalized, row["rowid"]))
        db.execute("UPDATE articles SET created_at=COALESCE(created_at,?), updated_at=COALESCE(updated_at,?)", (now, now))
        # Accept an early/hand-written schema that used direct JSON columns
        # before the project settled on the ``*_json`` naming convention.
        if "groups" in legacy_zoo_columns and "groups_json" not in legacy_zoo_columns:
            db.execute(
                "UPDATE zoos SET groups_json=CASE WHEN groups_json IS NULL OR groups_json IN ('','[]','{}') THEN groups ELSE groups_json END"
            )
        if "list_provenance" in legacy_zoo_columns and "list_provenance_json" not in legacy_zoo_columns:
            db.execute(
                "UPDATE zoos SET list_provenance_json=CASE WHEN list_provenance_json IS NULL OR list_provenance_json IN ('','[]','{}') THEN list_provenance ELSE list_provenance_json END"
            )
        source_cols = self._columns(db, "sources")
        if "source_url" in source_cols:
            db.execute("UPDATE sources SET url=COALESCE(url,source_url) WHERE url IS NULL")
        for row in db.execute("SELECT rowid, normalized_url, url FROM sources").fetchall():
            normalized = _first_legacy_url(row["normalized_url"], row["url"])
            db.execute("UPDATE sources SET normalized_url=? WHERE rowid=?", (normalized, row["rowid"]))
        db.execute("UPDATE sources SET created_at=COALESCE(created_at,?), updated_at=COALESCE(updated_at,?)", (now, now))
        db.execute(
            "UPDATE article_discoveries SET discovered_url_raw=COALESCE(discovered_url_raw,discovered_url) "
            "WHERE discovered_url_raw IS NULL"
        )
        db.execute("UPDATE article_discoveries SET discovered_key=COALESCE(NULLIF(discovered_key,''), discovered_url, '')")
        for row in db.execute("SELECT rowid, discovered_url, discovered_key FROM article_discoveries").fetchall():
            discovered_url = _normalize_legacy_url(row["discovered_url"])
            discovered_key = _first_legacy_url(row["discovered_key"], row["discovered_url"]) or ""
            db.execute(
                "UPDATE article_discoveries SET discovered_url=?, discovered_key=? WHERE rowid=?",
                (discovered_url, discovered_key, row["rowid"]),
            )
        db.execute("UPDATE article_discoveries SET last_discovered_at=COALESCE(last_discovered_at, discovered_at)")
        for row in db.execute(
            "SELECT rowid,discovered_at,last_discovered_at FROM article_discoveries"
        ).fetchall():
            discovered_at = _discovery_timestamp(row["discovered_at"])
            last_discovered_at = _discovery_timestamp(row["last_discovered_at"])
            db.execute(
                "UPDATE article_discoveries SET discovered_at=?,last_discovered_at=? WHERE rowid=?",
                (discovered_at, last_discovered_at, row["rowid"]),
            )
        self._consolidate_legacy_discoveries(db)
        db.execute("UPDATE crawl_runs SET started_at=COALESCE(started_at,?), status=COALESCE(status,'running'), metadata_json=COALESCE(metadata_json,'{}')", (now,))
        db.execute("UPDATE crawl_run_stats SET discovered_count=COALESCE(discovered_count,0), fetched_count=COALESCE(fetched_count,0), stored_count=COALESCE(stored_count,0), already_known_count=COALESCE(already_known_count,0), duplicate_candidate_count=COALESCE(duplicate_candidate_count,0), error_count=COALESCE(error_count,0)")
        self._backfill_durations(db, "crawl_runs")
        self._backfill_durations(db, "crawl_run_stats")
        self._consolidate_legacy_articles(db)
        self._consolidate_legacy_sources(db)
        self._consolidate_legacy_discoveries(db)
        self._backfill_article_zoo_identities(db)
        self._consolidate_legacy_title_identities(db)
        self._consolidate_legacy_zoo_results(db)
        # Recompute keys from authoritative hash/title fields even on an
        # already-v6 database.  Dropping the old derived index first lets a
        # repair fix stale keys atomically before recreating the constraint.
        db.execute("DROP INDEX IF EXISTS ux_articles_content_identity_key")
        self._backfill_content_identity_keys(db)
        self._rebuild_with_foreign_keys(db, definitions)
        # Content hashes are evidence, not globally unique identity.  Older
        # schema versions created a unique index here; replace it within the
        # migration transaction so benign boilerplate collisions can coexist.
        db.execute("DROP INDEX IF EXISTS ux_articles_content_hash")
        indexes = (
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_sources_zoo_normalized_url ON sources(zoo_id, normalized_url)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_articles_canonical_url ON articles(canonical_url)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_articles_normalized_url ON articles(normalized_url)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_articles_content_identity_key ON articles(content_identity_key) WHERE content_identity_key IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_discoveries_identity ON article_discoveries(article_id, source_id, discovered_key)",
            "CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status)",
            "CREATE INDEX IF NOT EXISTS idx_articles_normalized_url ON articles(normalized_url)",
            "CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles(content_hash)",
            "CREATE INDEX IF NOT EXISTS idx_discoveries_source ON article_discoveries(source_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_article_zoo_title ON article_zoo_identities(zoo_id,title_key)",
            "CREATE INDEX IF NOT EXISTS idx_article_zoo_article ON article_zoo_identities(article_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_crawl_zoo_results_run_zoo ON crawl_zoo_results(crawl_run_id,zoo_id)",
            "CREATE INDEX IF NOT EXISTS idx_crawl_zoo_results_zoo ON crawl_zoo_results(zoo_id)",
        )
        for statement in indexes:
            db.execute(statement)

    @staticmethod
    def _duration_ms(started: Optional[str], finished: Optional[str]) -> Optional[int]:
        if not started or not finished:
            return None
        try:
            start = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            finish = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
            milliseconds = round((finish - start).total_seconds() * 1000)
            return milliseconds if milliseconds >= 0 else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _backfill_durations(cls, db: sqlite3.Connection, table: str) -> None:
        for row in db.execute(f"SELECT rowid, started_at, finished_at, duration_ms FROM {table}").fetchall():
            if row["duration_ms"] is None:
                duration = cls._duration_ms(row["started_at"], row["finished_at"])
                if duration is not None:
                    db.execute(f"UPDATE {table} SET duration_ms=? WHERE rowid=?", (duration, row["rowid"]))

    @classmethod
    def _rebuild_with_foreign_keys(cls, db: sqlite3.Connection, definitions: Mapping[str, Mapping[str, str]]) -> None:
        tables = (
            "zoos", "sources", "articles", "article_discoveries", "crawl_runs",
            "crawl_run_stats", "article_zoo_identities", "crawl_zoo_results",
        )
        expected = {
            "sources": {("zoo_id", "zoos", "id", "CASCADE", "RESTRICT")},
            "article_discoveries": {
                ("article_id", "articles", "id", "CASCADE", "CASCADE"),
                ("source_id", "sources", "id", "CASCADE", "CASCADE"),
            },
            "crawl_run_stats": {
                ("crawl_run_id", "crawl_runs", "id", "CASCADE", "CASCADE"),
                ("zoo_id", "zoos", "id", "CASCADE", "SET NULL"),
                ("source_id", "sources", "id", "CASCADE", "SET NULL"),
            },
            "article_zoo_identities": {
                ("article_id", "articles", "id", "CASCADE", "CASCADE"),
                ("zoo_id", "zoos", "id", "CASCADE", "CASCADE"),
            },
            "crawl_zoo_results": {
                ("crawl_run_id", "crawl_runs", "id", "CASCADE", "CASCADE"),
                ("zoo_id", "zoos", "id", "CASCADE", "CASCADE"),
            },
        }
        actual = {
            table: {
                (row["from"], row["table"], row["to"], row["on_update"], row["on_delete"])
                for row in db.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            }
            for table in expected
        }
        if actual == expected:
            return
        before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        extra_columns = {
            table: [
                row for row in db.execute(f"PRAGMA table_xinfo({cls._quoted_identifier(table)})").fetchall()
                if str(row["name"]) not in definitions[table]
            ]
            for table in tables
        }
        # sqlite_autoindex objects have NULL SQL and are recreated by table
        # constraints. Every explicit index and trigger is replayed verbatim
        # after the replacement tables exist; any failure rolls back the whole
        # migration transaction.
        placeholders = ",".join("?" for _ in tables)
        schema_objects = db.execute(
            f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('index','trigger') AND tbl_name IN ({placeholders}) AND sql IS NOT NULL ORDER BY type,name",
            tables,
        ).fetchall()
        # Extension column constraints are not represented by the
        # ``table_xinfo`` fields used by ``_extra_column_declaration``.  If we
        # copied only the type/default, a rebuild would silently turn e.g.
        # ``extra TEXT UNIQUE`` or ``extra_id TEXT REFERENCES ...`` into an
        # unconstrained column.  Inspect the original declaration and abort
        # before any rename when it cannot be represented safely.
        for table in tables:
            table_sql_row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            table_sql = str(table_sql_row["sql"] or "") if table_sql_row else ""
            declarations = cls._table_column_declarations(table_sql)
            extension_names = {str(row["name"]) for row in extra_columns[table]}
            for name in extension_names:
                declaration = declarations.get(name)
                if declaration is None:
                    raise RuntimeError(f"unsupported legacy extension column: {table}.{name}")
                declaration_sql = cls._sql_without_quoted_text(declaration)
                if re.search(r"\b(?:CHECK|COLLATE|REFERENCES|UNIQUE)\b", declaration_sql, flags=re.IGNORECASE):
                    raise RuntimeError(
                        f"unsupported legacy extension column constraint: {table}.{name}"
                    )
            # Table-level constraints are likewise not included in the
            # canonical declarations.  Reject only actual top-level
            # constraint clauses; quoted/default text such as DEFAULT
            # ('CHECK') is intentionally ignored by the masked SQL.
            for part in cls._table_definition_parts(table_sql):
                masked = cls._sql_without_quoted_text(part)
                constraint = re.match(
                    r"^\s*(CHECK|UNIQUE|FOREIGN|PRIMARY)\b", masked, flags=re.IGNORECASE
                )
                if not constraint:
                    continue
                mentions_extension = any(
                    name.casefold() in part.casefold() for name in extension_names
                )
                kind = constraint.group(1).upper()
                if mentions_extension or kind in {"CHECK", "UNIQUE"}:
                    raise RuntimeError(f"unsupported legacy table constraint in {table}")
                if kind == "FOREIGN":
                    foreign = re.match(
                        r"^\s*FOREIGN\s+KEY\s*\(\s*([^)]*?)\s*\)\s+"
                        r"REFERENCES\s+([\w\"`\[\]]+)\s*\(\s*([^)]*?)\s*\)"
                        r"(?P<actions>.*)$",
                        part,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    if not foreign:
                        raise RuntimeError(f"unsupported legacy table constraint in {table}")
                    local = foreign.group(1).strip().strip('"`[]')
                    target_table = foreign.group(2).strip().strip('"`[]')
                    target = foreign.group(3).strip().strip('"`[]')
                    actions = foreign.group("actions")
                    update_match = re.search(
                        r"ON\s+UPDATE\s+(SET\s+NULL|NO\s+ACTION|\w+)",
                        actions,
                        flags=re.IGNORECASE,
                    )
                    delete_match = re.search(
                        r"ON\s+DELETE\s+(SET\s+NULL|NO\s+ACTION|\w+)",
                        actions,
                        flags=re.IGNORECASE,
                    )
                    on_update = update_match.group(1).upper() if update_match else "NO ACTION"
                    on_delete = delete_match.group(1).upper() if delete_match else "NO ACTION"
                    if (local, target_table, target, on_update, on_delete) not in expected.get(table, set()):
                        raise RuntimeError(f"unsupported legacy table constraint in {table}")
        for table in reversed(tables):
            db.execute(f"ALTER TABLE {table} RENAME TO {table}__legacy")
        cls._create_tables(
            db,
            extra_declarations={
                table: [cls._extra_column_declaration(row) for row in extra_columns[table]]
                for table in tables
            },
        )
        for table in tables:
            columns = list(definitions[table]) + [str(row["name"]) for row in extra_columns[table]]
            names = ",".join(cls._quoted_identifier(name) for name in columns)
            db.execute(
                f"INSERT INTO {cls._quoted_identifier(table)} ({names}) SELECT {names} FROM {cls._quoted_identifier(table + '__legacy')}"
            )
        for table in reversed(tables):
            db.execute(f"DROP TABLE {cls._quoted_identifier(table + '__legacy')}")
        for obj in schema_objects:
            db.execute(str(obj["sql"]))
        after = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        if before != after:
            raise RuntimeError(f"migration row-count mismatch: before={before}, after={after}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            # Nested storage calls (notably outcome-aware article upserts that
            # resolve a Source) must stay in one write transaction.  A
            # savepoint also prevents an inner failure from rolling back a
            # caller-owned outer transaction.
            nested = bool(self._connection.in_transaction)
            savepoint = f"zoofan_sp_{uuid.uuid4().hex}" if nested else None
            if nested:
                self._connection.execute(f"SAVEPOINT {savepoint}")
            else:
                self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                if nested:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._connection.rollback()
                raise
            else:
                if nested:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._connection.commit()

    # ---- zoos and sources -------------------------------------------------

    def upsert_zoo(self, zoo: Union[Zoo, Mapping[str, Any]]) -> Zoo:
        if not isinstance(zoo, Zoo):
            zoo = Zoo(**dict(zoo))
        zoo.id = _id(zoo.id)
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as db:
            existing = db.execute(
                "SELECT * FROM zoos WHERE id = ? OR slug = ? LIMIT 1", (zoo.id, zoo.slug)
            ).fetchone()
            if existing:
                zoo.id = str(existing["id"])
                # A legacy caller that does not know the registry fields must
                # not erase values written by a newer registry import.
                groups = (
                    zoo.groups
                    if zoo._groups_provided
                    else tuple(_load_json_value(existing["groups_json"], []) or [])
                )
                region = zoo.region if zoo.region is not None else existing["region"]
                city = zoo.city if zoo.city is not None else existing["city"]
                source_status = zoo.source_status if zoo.source_status is not None else existing["source_status"]
                list_provenance = (
                    zoo.list_provenance
                    if zoo._list_provenance_provided
                    else list(_load_json_value(existing["list_provenance_json"], []) or [])
                )
                zoo.groups = tuple(str(value) for value in groups)
                zoo.region = region
                zoo.city = city
                zoo.source_status = source_status
                zoo.list_provenance = list(list_provenance)
                db.execute(
                    """UPDATE zoos SET slug=?, name=?, website_url=?, country_code=?, language=?, groups_json=?, region=?, city=?, source_status=?, list_provenance_json=?, enabled=?, metadata_json=?, updated_at=? WHERE id=?""",
                    (zoo.slug, zoo.name, zoo.website_url, zoo.country_code, zoo.language, _json(list(zoo.groups)), zoo.region, zoo.city, zoo.source_status, _json(zoo.list_provenance), int(zoo.enabled), _json(zoo.metadata), now, zoo.id),
                )
            else:
                db.execute(
                    """INSERT INTO zoos(id,slug,name,website_url,country_code,language,groups_json,region,city,source_status,list_provenance_json,enabled,metadata_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (zoo.id, zoo.slug, zoo.name, zoo.website_url, zoo.country_code, zoo.language, _json(list(zoo.groups)), zoo.region, zoo.city, zoo.source_status, _json(zoo.list_provenance), int(zoo.enabled), _json(zoo.metadata), now, now),
                )
        return zoo

    save_zoo = upsert_zoo

    def get_zoo(self, zoo_id: str) -> Optional[Zoo]:
        row = self._connection.execute("SELECT * FROM zoos WHERE id=? OR slug=? LIMIT 1", (str(zoo_id), str(zoo_id))).fetchone()
        return self._zoo_from_row(row) if row else None

    def list_zoos(self, enabled_only: bool = False) -> list[Zoo]:
        sql = "SELECT * FROM zoos"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY slug"
        return [self._zoo_from_row(row) for row in self._connection.execute(sql).fetchall()]

    @staticmethod
    def _zoo_from_row(row: sqlite3.Row) -> Zoo:
        groups_value = _load_json_value(row["groups_json"], [])
        provenance_value = _load_json_value(row["list_provenance_json"], [])
        return Zoo(
            id=row["id"], slug=row["slug"], name=row["name"], website_url=row["website_url"], country_code=row["country_code"], language=row["language"],
            groups=tuple(groups_value) if isinstance(groups_value, (list, tuple)) else (),
            region=row["region"], city=row["city"], source_status=row["source_status"],
            list_provenance=list(provenance_value) if isinstance(provenance_value, (list, tuple)) else [],
            enabled=bool(row["enabled"]), metadata=_load_json(row["metadata_json"]),
        )

    def upsert_source(self, source: Union[Source, Mapping[str, Any]]) -> Source:
        if not isinstance(source, Source):
            source = Source(**dict(source))
        source.normalized_url = normalize_url(source.normalized_url or source.url) or None
        if not source.zoo_id:
            raise ValueError("source.zoo_id is required")
        zoo = self.get_zoo(source.zoo_id)
        if zoo is None:
            raise ValueError(f"unknown zoo: {source.zoo_id}")
        source.zoo_id = zoo.id
        source.id = _id(source.id)
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as db:
            existing = db.execute(
                    "SELECT id FROM sources WHERE id=? OR (zoo_id=? AND normalized_url=?) LIMIT 1",
                (source.id, source.zoo_id, source.normalized_url),
            ).fetchone()
            if existing:
                source.id = str(existing["id"])
                # Registry/config re-registration owns descriptive metadata,
                # not operational check state. update_source_status is the
                # sole API for replacing status and HTTP evidence.
                db.execute(
                    """UPDATE sources SET zoo_id=?,url=?,normalized_url=?,kind=?,name=?,language=?,config_json=?,enabled=?,updated_at=? WHERE id=?""",
                    (source.zoo_id, source.url, source.normalized_url, source.kind, source.name, source.language,
                     _json(source.config), int(source.enabled), now, source.id),
                )
            else:
                db.execute(
                    """INSERT INTO sources(id,zoo_id,url,normalized_url,kind,name,language,config_json,enabled,status,success,last_checked,last_success,last_error,last_http_status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source.id, source.zoo_id, source.url, source.normalized_url, source.kind, source.name, source.language, _json(source.config), int(source.enabled), source.status,
                     None if source.success is None else int(source.success), _timestamp(source.last_checked), _timestamp(source.last_success), source.last_error or source.error, source.last_http_status, now, now),
                )
        return source

    save_source = upsert_source

    def get_source(self, source_id: str) -> Optional[Source]:
        row = self._connection.execute("SELECT * FROM sources WHERE id=?", (str(source_id),)).fetchone()
        return self._source_from_row(row) if row else None

    def list_sources(self, zoo_id: Optional[str] = None, enabled_only: bool = False) -> list[Source]:
        clauses: list[str] = []
        args: list[Any] = []
        if zoo_id:
            zoo = self.get_zoo(zoo_id)
            clauses.append("zoo_id=?")
            args.append(zoo.id if zoo else zoo_id)
        if enabled_only:
            clauses.append("enabled=1")
        sql = "SELECT * FROM sources" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY url"
        return [self._source_from_row(row) for row in self._connection.execute(sql, args).fetchall()]

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> Source:
        return Source(
            id=row["id"], zoo_id=row["zoo_id"], url=row["url"], normalized_url=row["normalized_url"], kind=row["kind"], name=row["name"], language=row["language"],
            config=_load_json(row["config_json"]), enabled=bool(row["enabled"]), status=row["status"],
            success=None if row["success"] is None else bool(row["success"]), error=row["last_error"],
            last_checked=_decoded_timestamp(row["last_checked"]), last_success=_decoded_timestamp(row["last_success"]),
            last_error=row["last_error"], last_http_status=row["last_http_status"],
        )

    def update_source_status(
        self,
        source_id: str,
        *,
        status: str,
        checked_at: Any = None,
        success: Optional[bool] = None,
        error: Optional[str] = None,
        http_status: Optional[int] = None,
    ) -> Optional[Source]:
        source = self.get_source(source_id)
        if source is None:
            return None
        checked_at = checked_at or datetime.now(timezone.utc)
        with self._transaction() as db:
            db.execute(
                """UPDATE sources SET status=?,success=?,last_checked=?,last_success=CASE WHEN ? THEN ? ELSE last_success END,
                   last_error=?,last_http_status=?,updated_at=? WHERE id=?""",
                (status, source.success if success is None else int(bool(success)), _timestamp(checked_at), int(bool(success)), _timestamp(checked_at) if success else None,
                 error, http_status, datetime.now(timezone.utc).isoformat(), source.id),
            )
        loaded_source_id = source.id
        if loaded_source_id is None:
            return None
        return self.get_source(loaded_source_id)

    record_source_check = update_source_status

    # ---- articles and discovery ------------------------------------------

    @staticmethod
    def _article_from_row(row: sqlite3.Row) -> Article:
        return Article(
            id=row["id"], canonical_url=row["canonical_url"], normalized_url=row["normalized_url"], url=row["source_url"],
            title=row["title"], published_at=_decoded_timestamp(row["published_at"]), published_at_raw=row["published_at_raw"],
            updated_at_source=_decoded_timestamp(row["updated_at_source"]), author=row["author"], summary=row["summary"],
            content=row["content"], content_html=row["content_html"], image_url=row["image_url"], parse_status=row["parse_status"],
            content_hash=row["content_hash"], content_identity_key=row["content_identity_key"], html_hash=row["html_hash"], language=row["language"],
            http_status=row["http_status"], crawl_status=row["crawl_status"], last_fetched_at=_decoded_timestamp(row["last_fetched_at"]),
            raw_html=row["raw_html"], metadata=_load_json(row["metadata_json"]),
            source_url_raw=row["source_url_raw"],
        )

    @staticmethod
    def _article_read_model(article: Article, discoveries: list[ArticleDiscovery], row: sqlite3.Row) -> ArticleReadModel:
        """Build a joined, read-only article view without losing provenance."""

        first = _select_discovery_timestamp(
            [item.discovered_at for item in discoveries], latest=False
        )
        last = _select_discovery_timestamp(
            [
                value
                for item in discoveries
                for value in (item.discovered_at, item.last_discovered_at)
            ],
            latest=True,
        ) or first
        values = dict(article.__dict__)
        values.update(
            first_discovered_at=first,
            last_discovered_at=last,
            created_at=_discovery_timestamp(row["created_at"]),
            storage_updated_at=_discovery_timestamp(row["updated_at"]),
            discoveries=discoveries,
        )
        return ArticleReadModel(**values)

    def _find_article(
        self,
        db: sqlite3.Connection,
        article: Article,
        zoo_id: Optional[str] = None,
    ) -> Optional[sqlite3.Row]:
        # Global URL identity wins over every other signal.  Keep the two URL
        # forms separate so we can distinguish an incoming URL that failed to
        # match from an article which genuinely has no URL identity.
        canonical_url = normalize_url(article.canonical_url) if article.canonical_url else None
        normalized_url = (
            normalize_url(article.normalized_url or article.url)
            if (article.normalized_url or article.url)
            else None
        )
        for column, value in (("canonical_url", canonical_url), ("normalized_url", normalized_url)):
            if value:
                row = db.execute(f"SELECT * FROM articles WHERE {column}=? LIMIT 1", (value,)).fetchone()
                if row:
                    return row

        # Parsed content hashes are useful evidence, but generic boilerplate
        # can give unrelated pages the same digest.  If an incoming URL exists
        # but did not match, only a hash row with the same normalized title is
        # eligible.  A URL-less legacy/discovery record may still use its hash
        # as the fallback identity.  The query is intentionally non-unique:
        # more than one article may legitimately share a hash.
        title_key = _normalize_title(article.title)
        if article.content_hash and (title_key or canonical_url or normalized_url):
            hash_rows = db.execute(
                "SELECT * FROM articles WHERE content_hash=? ORDER BY rowid",
                (article.content_hash,),
            ).fetchall()
            if not canonical_url and not normalized_url and hash_rows:
                # Without a URL there is no global identity.  Never pick an
                # arbitrary same-hash row: boilerplate hashes are expected to
                # collide across unrelated titles.  Only a matching composite
                # hash/title identity is eligible for a merge.
                return next(
                    (row for row in hash_rows if _normalize_title(row["title"]) == title_key),
                    None,
                )
            for row in hash_rows:
                existing_title_key = _normalize_title(row["title"])
                if title_key and existing_title_key == title_key:
                    return row
                # Preserve the historical same-content behavior for fetched
                # URL records that have no parsed title, while keeping the
                # URL-less path strict (it cannot choose an arbitrary row).
                if not title_key and not existing_title_key and (canonical_url or normalized_url):
                    return row
        if zoo_id and title_key:
            row = db.execute(
                """
                SELECT a.* FROM articles AS a
                JOIN article_zoo_identities AS i ON i.article_id=a.id
                WHERE i.zoo_id=? AND i.title_key=? LIMIT 1
                """,
                (zoo_id, title_key),
            ).fetchone()
            if row:
                return row
        return None

    def upsert_article(
        self,
        article: Union[Article, Mapping[str, Any]],
        source_id: Optional[str] = None,
        discovered_url: Optional[str] = None,
        discovered_at: Any = None,
        source: Optional[Source] = None,
        zoo_id: Optional[str] = None,
    ) -> Article:
        if not isinstance(article, Article):
            values = dict(article)
            zoo_id = zoo_id or values.pop("zoo_id", None)
            article = Article(**values)
        if source is not None:
            source = self.upsert_source(source)
            source_id = source.id
        if zoo_id:
            zoo = self.get_zoo(str(zoo_id))
            if zoo is None:
                raise ValueError(f"unknown zoo: {zoo_id}")
            zoo_id = zoo.id
        elif source_id:
            source_row = self._connection.execute(
                "SELECT zoo_id FROM sources WHERE id=?", (str(source_id),)
            ).fetchone()
            if source_row:
                zoo_id = source_row["zoo_id"]
        article.id = _id(article.id)
        # Capture the caller's URL lexeme before normalizing identity fields.
        # The first stored lexeme is authoritative provenance; later retries
        # for the same normalized URL do not overwrite it with tracking noise.
        article.source_url_raw = article.source_url_raw or article.url or article.canonical_url
        article.canonical_url = normalize_url(article.canonical_url) if article.canonical_url else None
        article.normalized_url = normalize_url(article.normalized_url or article.url) if (article.normalized_url or article.url) else None
        if article.url:
            article.url = normalize_url(article.url)
        article.content_hash = article.content_hash or None
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as db:
            existing = self._find_article(db, article, zoo_id=zoo_id)
            if existing:
                article.id = str(existing["id"])
                # A title/content change can move the composite key onto an
                # existing article.  Merge that row first so the database
                # unique index remains authoritative while preserving its
                # provenance and discoveries.
                candidate_key = _content_identity_key(
                    article.content_hash or existing["content_hash"],
                    article.title or existing["title"],
                )
                if candidate_key:
                    key_conflict = db.execute(
                        "SELECT id FROM articles WHERE content_identity_key=? AND id<>? LIMIT 1",
                        (candidate_key, article.id),
                    ).fetchone()
                    if key_conflict:
                        self._merge_runtime_article_conflict(
                            db, donor_id=str(key_conflict["id"]), keeper_id=article.id
                        )
                        existing = db.execute(
                            "SELECT * FROM articles WHERE id=?", (article.id,)
                        ).fetchone()
                # Preserve richer existing values whenever this discovery only
                # has a subset of fields, while filling every missing value.
                values = {
                    # Identity values are immutable once established.  A
                    # title/hash hit may arrive with a different URL; retain
                    # the existing canonical/normalized/source identity and
                    # only fill a genuinely missing legacy value.
                    "canonical_url": existing["canonical_url"] or article.canonical_url,
                    "normalized_url": existing["normalized_url"] or article.normalized_url,
                    # Once an article has an identity, a later discovery must
                    # not replace its canonical/source identity. This is
                    # especially important when deduplication occurs through
                    # the third-layer raw HTML hash.
                    "source_url": existing["source_url"] or article.url,
                    "source_url_raw": existing["source_url_raw"] or article.source_url_raw or article.url,
                    "title": article.title or existing["title"],
                    "published_at": _timestamp(article.published_at) or existing["published_at"],
                    "published_at_raw": article.published_at_raw or existing["published_at_raw"],
                    "updated_at_source": _timestamp(article.updated_at_source) or existing["updated_at_source"],
                    "author": article.author or existing["author"],
                    "summary": article.summary or existing["summary"],
                    "content": article.content or existing["content"],
                    "content_html": article.content_html or existing["content_html"],
                    "image_url": article.image_url or existing["image_url"],
                    "parse_status": article.parse_status or existing["parse_status"],
                    "content_hash": article.content_hash or existing["content_hash"],
                    "content_identity_key": _content_identity_key(
                        article.content_hash or existing["content_hash"],
                        article.title or existing["title"],
                    ),
                    # Raw response evidence is operational and must track the
                    # latest fetch exactly, including an explicitly empty
                    # response.  ``None`` means the caller did not provide a
                    # replacement (partial updates still preserve evidence).
                    "html_hash": article.html_hash if article.html_hash is not None else existing["html_hash"],
                    "language": article.language or existing["language"],
                    "http_status": article.http_status if article.http_status is not None else existing["http_status"],
                    "crawl_status": article.crawl_status or existing["crawl_status"],
                    "last_fetched_at": _timestamp(article.last_fetched_at) if article.last_fetched_at is not None else existing["last_fetched_at"],
                    "raw_html": article.raw_html if article.raw_html is not None else existing["raw_html"],
                    "metadata_json": _json({**_load_json(existing["metadata_json"]), **article.metadata}),
                }
                # Avoid a new identity colliding with a different row; such a
                # collision can only happen when two identity keys disagree.
                for key in ("canonical_url", "normalized_url", "content_hash"):
                    candidate = values[key]
                    if candidate:
                        conflict = db.execute(f"SELECT id FROM articles WHERE {key}=? AND id<>?", (candidate, article.id)).fetchone()
                        if conflict:
                            values[key] = existing[key]
                # These fields represent the parsed/article business state.
                # Raw response capture can legitimately change on every
                # recheck (for example, a rotating CSRF token) without being
                # an article update.
                business_fields = (
                    "canonical_url", "normalized_url", "source_url", "source_url_raw", "title", "published_at",
                    "published_at_raw", "updated_at_source", "author", "summary", "content",
                    "content_html", "image_url", "parse_status", "content_hash",
                    "content_identity_key", "language", "http_status", "crawl_status", "metadata_json",
                )
                business_changed = any(
                    values[name] != existing[name] for name in business_fields
                )
                db.execute(
                    """UPDATE articles SET canonical_url=?,normalized_url=?,source_url=?,source_url_raw=?,title=?,published_at=?,published_at_raw=?,updated_at_source=?,author=?,summary=?,content=?,content_html=?,image_url=?,parse_status=?,content_hash=?,content_identity_key=?,html_hash=?,language=?,http_status=?,crawl_status=?,last_fetched_at=?,raw_html=?,metadata_json=?,updated_at=? WHERE id=?""",
                    (*values.values(), now if business_changed else existing["updated_at"], article.id),
                )
            else:
                db.execute(
                    """INSERT INTO articles(id,canonical_url,normalized_url,source_url,source_url_raw,title,published_at,published_at_raw,updated_at_source,author,summary,content,content_html,image_url,parse_status,content_hash,content_identity_key,html_hash,language,http_status,crawl_status,last_fetched_at,raw_html,metadata_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (article.id, article.canonical_url, article.normalized_url, article.url, article.source_url_raw, article.title, _timestamp(article.published_at), article.published_at_raw,
                     _timestamp(article.updated_at_source), article.author, article.summary, article.content, article.content_html, article.image_url, article.parse_status, article.content_hash,
                     _content_identity_key(article.content_hash, article.title),
                     article.html_hash, article.language, article.http_status, article.crawl_status, _timestamp(article.last_fetched_at), article.raw_html, _json(article.metadata), now, now),
                )
            title_key = _normalize_title(article.title)
            if zoo_id and title_key:
                conflict = db.execute(
                    "SELECT article_id FROM article_zoo_identities WHERE zoo_id=? AND title_key=? AND article_id<>? LIMIT 1",
                    (zoo_id, title_key, article.id),
                ).fetchone()
                if conflict:
                    # URL/hash identity is already authoritative for this
                    # upsert. Merge the title-only row into it before adding
                    # the current zoo identity, rather than attaching the
                    # URL to the title row or raising a uniqueness error.
                    self._merge_runtime_article_conflict(
                        db, donor_id=str(conflict["article_id"]), keeper_id=article.id
                    )
                db.execute(
                    """
                    INSERT INTO article_zoo_identities(article_id,zoo_id,title_key,created_at,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(article_id,zoo_id) DO UPDATE SET title_key=excluded.title_key,updated_at=excluded.updated_at
                    """,
                    (article.id, zoo_id, title_key, now, now),
                )
            if source_id:
                self._record_discovery_in_transaction(
                    db,
                    ArticleDiscovery(
                        article_id=article.id,
                        source_id=str(source_id),
                        discovered_url=discovered_url or article.url or article.canonical_url,
                        discovered_at=discovered_at or datetime.now(timezone.utc),
                    ),
                )
        row = self._connection.execute("SELECT * FROM articles WHERE id=?", (article.id,)).fetchone()
        return self._article_from_row(row)

    save_article = upsert_article
    upsert = upsert_article

    def upsert_article_with_outcome(self, article: Union[Article, Mapping[str, Any]], **kwargs: Any) -> ArticleUpsertOutcome:
        """Upsert and report whether a new article row was created.

        ``upsert_article`` remains source compatible for existing consumers.
        """
        values = dict(article) if not isinstance(article, Article) else None
        scope_zoo_id = kwargs.get("zoo_id")
        source_value = kwargs.get("source")
        if scope_zoo_id is None and source_value is not None:
            if isinstance(source_value, Source):
                scope_zoo_id = source_value.zoo_id
            elif isinstance(source_value, Mapping):
                scope_zoo_id = source_value.get("zoo_id")
        if values is not None:
            scope_zoo_id = scope_zoo_id or values.get("zoo_id")
            values.pop("zoo_id", None)
            item = Article(**values)
        else:
            item = cast(Article, article)
        if scope_zoo_id is None and kwargs.get("source_id"):
            source_row = self._connection.execute(
                "SELECT zoo_id FROM sources WHERE id=?", (str(kwargs["source_id"]),)
            ).fetchone()
            scope_zoo_id = source_row["zoo_id"] if source_row else None
        if scope_zoo_id is not None:
            scope_zoo = self.get_zoo(str(scope_zoo_id))
            scope_zoo_id = scope_zoo.id if scope_zoo else str(scope_zoo_id)
        if scope_zoo_id is not None and "zoo_id" not in kwargs:
            kwargs["zoo_id"] = scope_zoo_id
        # The identity lookup and write are intentionally one BEGIN IMMEDIATE
        # transaction.  Two SQLiteStorage instances therefore cannot both
        # report ``created=True`` for the same identity.
        with self._transaction() as db:
            existing = self._find_article(db, item, zoo_id=scope_zoo_id)
            before = self._article_state(existing) if existing else None
            persisted = self.upsert_article(item, **kwargs)
            after = self._article_state(persisted)
        return ArticleUpsertOutcome(
            article=persisted,
            created=existing is None,
            updated=existing is not None and before != after,
        )

    @staticmethod
    def _article_state(value: Union[sqlite3.Row, Article]) -> tuple[Any, ...]:
        """Business state used by ``ArticleUpsertOutcome.updated``.

        Raw response capture and ``last_fetched_at`` are operational
        observations, not article changes; deliberately exclude them so a
        repeated recent recheck with identical parsed evidence is
        ``unchanged`` even when response-only tokens rotate.
        """

        names = (
            "canonical_url", "normalized_url", "source_url", "source_url_raw", "title", "published_at",
            "published_at_raw", "updated_at_source", "author", "summary", "content",
            "content_html", "image_url", "parse_status", "content_hash", "content_identity_key",
            "language", "http_status", "crawl_status", "metadata_json",
        )
        if isinstance(value, sqlite3.Row):
            return tuple(value[name] for name in names)
        return (
            value.canonical_url,
            value.normalized_url,
            value.url,
            value.source_url_raw,
            value.title,
            _timestamp(value.published_at),
            value.published_at_raw,
            _timestamp(value.updated_at_source),
            value.author,
            value.summary,
            value.content,
            value.content_html,
            value.image_url,
            value.parse_status,
            value.content_hash,
            value.content_identity_key or _content_identity_key(value.content_hash, value.title),
            value.language,
            value.http_status,
            value.crawl_status,
            _json(value.metadata),
        )

    def get_article(self, article_id: str) -> Optional[Article]:
        row = self._connection.execute("SELECT * FROM articles WHERE id=?", (str(article_id),)).fetchone()
        return self._article_from_row(row) if row else None

    def get_article_by_url(self, url: str) -> Optional[Article]:
        normalized = normalize_url(url)
        row = self._connection.execute(
            "SELECT * FROM articles WHERE canonical_url=? OR normalized_url=? LIMIT 1", (normalized, normalized)
        ).fetchone()
        return self._article_from_row(row) if row else None

    def list_articles(self, limit: Optional[int] = None, zoo_id: Optional[str] = None) -> list[Article]:
        args_list: list[Any] = []
        if zoo_id:
            zoo = self.get_zoo(zoo_id)
            scope = zoo.id if zoo else str(zoo_id)
            sql = "SELECT DISTINCT a.* FROM articles AS a JOIN article_zoo_identities AS i ON i.article_id=a.id WHERE i.zoo_id=? ORDER BY a.published_at DESC, a.created_at DESC"
            args_list.append(scope)
        else:
            sql = "SELECT * FROM articles ORDER BY published_at DESC, created_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args_list.append(int(limit))
        return [self._article_from_row(row) for row in self._connection.execute(sql, args_list).fetchall()]

    def get_article_read_model(self, article_id: str) -> Optional[ArticleReadModel]:
        """Return one article joined with all discovery provenance and bounds."""

        row = self._connection.execute(
            "SELECT * FROM articles WHERE id=?", (str(article_id),)
        ).fetchone()
        if row is None:
            return None
        article = self._article_from_row(row)
        discoveries = self.list_discoveries(article_id=article.id)
        return self._article_read_model(article, discoveries, row)

    def list_article_read_models(
        self, limit: Optional[int] = None, zoo_id: Optional[str] = None
    ) -> list[ArticleReadModel]:
        """Return joined article views ordered like :meth:`list_articles`."""

        args_list: list[Any] = []
        if zoo_id:
            zoo = self.get_zoo(zoo_id)
            scope = zoo.id if zoo else str(zoo_id)
            sql = (
                "SELECT DISTINCT a.* FROM articles AS a "
                "JOIN article_zoo_identities AS i ON i.article_id=a.id "
                "WHERE i.zoo_id=? ORDER BY a.published_at DESC, a.created_at DESC"
            )
            args_list.append(scope)
        else:
            sql = "SELECT * FROM articles ORDER BY published_at DESC, created_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args_list.append(int(limit))
        rows = self._connection.execute(sql, args_list).fetchall()
        return [
            self._article_read_model(
                self._article_from_row(row), self.list_discoveries(article_id=row["id"]), row
            )
            for row in rows
        ]

    # Compatibility spellings used by read-model consumers.
    get_article_with_discoveries = get_article_read_model
    list_article_with_discoveries = list_article_read_models
    list_article_read_model = list_article_read_models

    def get_article_by_title(self, title: str, zoo_id: str) -> Optional[Article]:
        """Read an article by its normalized title within one zoo."""

        zoo = self.get_zoo(zoo_id)
        scope = zoo.id if zoo else str(zoo_id)
        row = self._connection.execute(
            """
            SELECT a.* FROM articles AS a
            JOIN article_zoo_identities AS i ON i.article_id=a.id
            WHERE i.zoo_id=? AND i.title_key=? LIMIT 1
            """,
            (scope, _normalize_title(title)),
        ).fetchone()
        return self._article_from_row(row) if row else None

    def list_article_zoo_identities(
        self, article_id: Optional[str] = None, zoo_id: Optional[str] = None
    ) -> list[dict[str, str]]:
        clauses: list[str] = []
        args: list[Any] = []
        if article_id:
            clauses.append("article_id=?")
            args.append(str(article_id))
        if zoo_id:
            zoo = self.get_zoo(zoo_id)
            clauses.append("zoo_id=?")
            args.append(zoo.id if zoo else str(zoo_id))
        sql = "SELECT article_id,zoo_id,title_key FROM article_zoo_identities"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY zoo_id,article_id"
        return [dict(row) for row in self._connection.execute(sql, args).fetchall()]

    def _record_discovery_in_transaction(self, db: sqlite3.Connection, discovery: ArticleDiscovery) -> ArticleDiscovery:
        discovery.id = _id(discovery.id)
        discovery.discovered_url_raw = discovery.discovered_url_raw or discovery.discovered_url
        normalized_discovered_url = normalize_url(discovery.discovered_url) if discovery.discovered_url else None
        discovered_key = normalized_discovered_url or ""
        incoming_discovered_at = _discovery_timestamp(discovery.discovered_at) or datetime.now(timezone.utc).isoformat()
        incoming_last_seen = _discovery_timestamp(
            discovery.last_discovered_at or discovery.discovered_at
        ) or incoming_discovered_at
        existing = db.execute(
            "SELECT * FROM article_discoveries WHERE article_id IS ? AND source_id IS ? AND discovered_key=? LIMIT 1",
            (discovery.article_id, discovery.source_id, discovered_key),
        ).fetchone()
        if existing:
            first = _select_discovery_timestamp(
                (existing["discovered_at"], incoming_discovered_at), latest=False
            )
            last = _select_discovery_timestamp(
                (
                    existing["discovered_at"], existing["last_discovered_at"],
                    incoming_discovered_at, incoming_last_seen,
                ),
                latest=True,
            ) or first
            merged_metadata = {
                **_load_json(existing["metadata_json"]),
                **dict(discovery.metadata or {}),
            }
            db.execute(
                """
                UPDATE article_discoveries
                SET discovered_url=COALESCE(discovered_url,?),discovered_url_raw=COALESCE(discovered_url_raw,?),discovered_at=?,last_discovered_at=?,metadata_json=?
                WHERE id=?
                """,
                (normalized_discovered_url, discovery.discovered_url_raw, first, last, _json(merged_metadata), existing["id"]),
            )
            discovery.id = existing["id"]
            discovery.discovered_url = existing["discovered_url"] or normalized_discovered_url
            discovery.discovered_url_raw = existing["discovered_url_raw"] or discovery.discovered_url_raw
            discovery.discovered_at = _decoded_timestamp(first)
            discovery.last_discovered_at = _decoded_timestamp(last)
            discovery.metadata = merged_metadata
            return discovery
        db.execute(
            """INSERT INTO article_discoveries(id,article_id,source_id,discovered_url,discovered_url_raw,discovered_key,discovered_at,last_discovered_at,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (discovery.id, discovery.article_id, discovery.source_id, normalized_discovered_url, discovery.discovered_url_raw,
             discovered_key, incoming_discovered_at, incoming_last_seen, _json(discovery.metadata)),
        )
        row = db.execute(
            "SELECT * FROM article_discoveries WHERE article_id=? AND source_id=? AND discovered_key=? LIMIT 1",
            (discovery.article_id, discovery.source_id, discovered_key),
        ).fetchone()
        if row:
            discovery.id = row["id"]
        return discovery

    def record_discovery(
        self,
        discovery: Optional[ArticleDiscovery] = None,
        *,
        article_id: Optional[str] = None,
        source_id: Optional[str] = None,
        discovered_url: Optional[str] = None,
        discovered_at: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ArticleDiscovery:
        item = discovery or ArticleDiscovery(
            article_id=article_id, source_id=source_id, discovered_url=discovered_url,
            discovered_at=discovered_at, metadata=dict(metadata or {}),
        )
        if not item.article_id or not item.source_id:
            raise ValueError("article_id and source_id are required")
        with self._transaction() as db:
            return self._record_discovery_in_transaction(db, item)

    add_discovery = record_discovery
    record_article_discovery = record_discovery

    def list_discoveries(self, article_id: Optional[str] = None, source_id: Optional[str] = None) -> list[ArticleDiscovery]:
        clauses: list[str] = []
        args: list[Any] = []
        if article_id:
            clauses.append("article_id=?")
            args.append(article_id)
        if source_id:
            clauses.append("source_id=?")
            args.append(source_id)
        sql = "SELECT * FROM article_discoveries" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY discovered_at"
        rows = self._connection.execute(sql, args).fetchall()
        return [ArticleDiscovery(id=row["id"], article_id=row["article_id"], source_id=row["source_id"], discovered_url=row["discovered_url"], discovered_url_raw=row["discovered_url_raw"], discovered_at=_decoded_timestamp(row["discovered_at"]), last_discovered_at=_decoded_timestamp(row["last_discovered_at"]), metadata=_load_json(row["metadata_json"])) for row in rows]

    # ---- crawl runs -------------------------------------------------------

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> CrawlRun:
        return CrawlRun(id=row["id"], batch_id=row["batch_id"], started_at=_decoded_timestamp(row["started_at"]), finished_at=_decoded_timestamp(row["finished_at"]), duration_ms=row["duration_ms"], status=row["status"], error=row["error"], metadata=_load_json(row["metadata_json"]))

    def start_crawl_run(self, run: Optional[CrawlRun] = None, **kwargs: Any) -> CrawlRun:
        run = run or CrawlRun(**kwargs)
        run.id = _id(run.id)
        run.started_at = run.started_at or datetime.now(timezone.utc)
        with self._transaction() as db:
            db.execute(
                "INSERT INTO crawl_runs(id,batch_id,started_at,finished_at,duration_ms,status,error,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
                (run.id, run.batch_id, _timestamp(run.started_at), _timestamp(run.finished_at), run.duration_ms, run.status, run.error, _json(run.metadata)),
            )
        return run

    create_crawl_run = start_crawl_run
    start_run = start_crawl_run

    def finish_crawl_run(self, run_id: str, *, status: str = "completed", finished_at: Any = None, error: Optional[str] = None) -> Optional[CrawlRun]:
        finish_value = _timestamp(finished_at or datetime.now(timezone.utc))
        with self._transaction() as db:
            row = db.execute("SELECT started_at FROM crawl_runs WHERE id=?", (run_id,)).fetchone()
            duration = self._duration_ms(row["started_at"], finish_value) if row else None
            db.execute("UPDATE crawl_runs SET status=?,finished_at=?,duration_ms=?,error=? WHERE id=?", (status, finish_value, duration, error, run_id))
        return self.get_crawl_run(run_id)

    def get_crawl_run(self, run_id: str) -> Optional[CrawlRun]:
        row = self._connection.execute("SELECT * FROM crawl_runs WHERE id=?", (str(run_id),)).fetchone()
        return self._run_from_row(row) if row else None

    def record_run_stat(self, stat: Union[CrawlRunStat, Mapping[str, Any]]) -> CrawlRunStat:
        if not isinstance(stat, CrawlRunStat):
            stat = CrawlRunStat(**dict(stat))
        stat.id = _id(stat.id)
        with self._transaction() as db:
            existing = db.execute("SELECT id FROM crawl_run_stats WHERE id=? OR (crawl_run_id=? AND zoo_id IS ? AND source_id IS ?) LIMIT 1", (stat.id, stat.crawl_run_id, stat.zoo_id, stat.source_id)).fetchone()
            if existing:
                stat.id = existing["id"]
                db.execute(
                    """UPDATE crawl_run_stats SET crawl_run_id=?,zoo_id=?,source_id=?,status=?,discovered_count=?,fetched_count=?,stored_count=?,already_known_count=?,duplicate_candidate_count=?,error_count=?,started_at=?,finished_at=?,duration_ms=?,error=?,errors_json=?,metadata_json=? WHERE id=?""",
                    (stat.crawl_run_id, stat.zoo_id, stat.source_id, stat.status, stat.discovered_count, stat.fetched_count, stat.stored_count, stat.already_known_count, stat.duplicate_candidate_count, stat.error_count, _timestamp(stat.started_at), _timestamp(stat.finished_at), stat.duration_ms if stat.duration_ms is not None else self._duration_ms(_timestamp(stat.started_at), _timestamp(stat.finished_at)), stat.error, json.dumps(stat.errors, ensure_ascii=False), _json(stat.metadata), stat.id),
                )
            else:
                db.execute(
                    """INSERT INTO crawl_run_stats(id,crawl_run_id,zoo_id,source_id,status,discovered_count,fetched_count,stored_count,already_known_count,duplicate_candidate_count,error_count,started_at,finished_at,duration_ms,error,errors_json,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (stat.id, stat.crawl_run_id, stat.zoo_id, stat.source_id, stat.status, stat.discovered_count, stat.fetched_count, stat.stored_count, stat.already_known_count, stat.duplicate_candidate_count, stat.error_count, _timestamp(stat.started_at), _timestamp(stat.finished_at), stat.duration_ms if stat.duration_ms is not None else self._duration_ms(_timestamp(stat.started_at), _timestamp(stat.finished_at)), stat.error, json.dumps(stat.errors, ensure_ascii=False), _json(stat.metadata)),
                )
        return self.get_run_stat(stat.id) or stat

    save_run_stat = record_run_stat
    record_crawl_stat = record_run_stat

    def get_run_stat(self, stat_id: str) -> Optional[CrawlRunStat]:
        row = self._connection.execute("SELECT * FROM crawl_run_stats WHERE id=?", (str(stat_id),)).fetchone()
        return self._stat_from_row(row) if row else None

    @staticmethod
    def _stat_from_row(row: sqlite3.Row) -> CrawlRunStat:
        try:
            errors = json.loads(row["errors_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            errors = []
        return CrawlRunStat(id=row["id"], crawl_run_id=row["crawl_run_id"], zoo_id=row["zoo_id"], source_id=row["source_id"], status=row["status"], discovered_count=row["discovered_count"], fetched_count=row["fetched_count"], stored_count=row["stored_count"], already_known_count=row["already_known_count"], duplicate_candidate_count=row["duplicate_candidate_count"], error_count=row["error_count"], started_at=_decoded_timestamp(row["started_at"]), finished_at=_decoded_timestamp(row["finished_at"]), duration_ms=row["duration_ms"], error=row["error"], errors=errors if isinstance(errors, list) else [], metadata=_load_json(row["metadata_json"]))

    def list_run_stats(self, crawl_run_id: Optional[str] = None) -> list[CrawlRunStat]:
        if crawl_run_id:
            rows = self._connection.execute("SELECT * FROM crawl_run_stats WHERE crawl_run_id=? ORDER BY id", (crawl_run_id,)).fetchall()
        else:
            rows = self._connection.execute("SELECT * FROM crawl_run_stats ORDER BY id").fetchall()
        return [self._stat_from_row(row) for row in rows]

    # ---- zoo-level crawl results ----------------------------------------

    @staticmethod
    def _zoo_result_from_row(row: sqlite3.Row) -> CrawlZooResult:
        return CrawlZooResult(
            id=row["id"],
            crawl_run_id=row["crawl_run_id"],
            zoo_id=row["zoo_id"],
            zoo_slug=row["zoo_slug"],
            zoo_name=row["zoo_name"],
            status=row["status"],
            source_status=row["source_status"],
            discovered=row["discovered"],
            parsed=row["parsed"],
            inserted=row["inserted"],
            updated=row["updated"],
            failed=row["failed"],
            duplicate_filtered=row["duplicate_filtered"],
            duration_ms=row["duration_ms"],
            source_url=row["source_url"],
            http_status=row["http_status"],
            error_category=row["error_category"],
            error_summary=row["error_summary"],
            started_at=_decoded_timestamp(row["started_at"]),
            finished_at=_decoded_timestamp(row["finished_at"]),
            metadata=_load_json(row["metadata_json"]),
        )

    def upsert_zoo_run_result(
        self, result: Union[CrawlZooResult, Mapping[str, Any]]
    ) -> CrawlZooResult:
        """Insert or update the unique ``(crawl_run_id, zoo_id)`` result."""

        if not isinstance(result, CrawlZooResult):
            result = CrawlZooResult(**dict(result))
        run_id = result.crawl_run_id
        zoo_id = result.zoo_id
        if not run_id:
            raise ValueError("result.crawl_run_id (or run_id) is required")
        if not zoo_id:
            raise ValueError("result.zoo_id is required")
        zoo = self.get_zoo(zoo_id)
        if zoo is not None:
            zoo_id = zoo.id or zoo_id
            result.zoo_id = zoo_id
        result.id = _id(result.id)
        started = _timestamp(result.started_at)
        finished = _timestamp(result.finished_at)
        duration = result.duration_ms if result.duration_ms is not None else self._duration_ms(started, finished)
        now = datetime.now(timezone.utc).isoformat()
        source_url = normalize_url(result.source_url) if result.source_url else None
        with self._transaction() as db:
            existing = db.execute(
                "SELECT id FROM crawl_zoo_results WHERE id=? OR (crawl_run_id=? AND zoo_id=?) LIMIT 1",
                (result.id, run_id, zoo_id),
            ).fetchone()
            if existing:
                result.id = str(existing["id"])
                db.execute(
                    """
                    UPDATE crawl_zoo_results SET crawl_run_id=?,zoo_id=?,zoo_slug=?,zoo_name=?,status=?,source_status=?,discovered=?,parsed=?,inserted=?,updated=?,failed=?,duplicate_filtered=?,duration_ms=?,source_url=?,http_status=?,error_category=?,error_summary=?,started_at=?,finished_at=?,metadata_json=?,updated_at=? WHERE id=?
                    """,
                    (run_id, zoo_id, result.zoo_slug, result.zoo_name, result.status, result.source_status, result.discovered, result.parsed, result.inserted, result.updated, result.failed, result.duplicate_filtered, duration, source_url, result.http_status, result.error_category, result.error_summary, started, finished, _json(result.metadata), now, result.id),
                )
            else:
                db.execute(
                    """
                    INSERT INTO crawl_zoo_results(id,crawl_run_id,zoo_id,zoo_slug,zoo_name,status,source_status,discovered,parsed,inserted,updated,failed,duplicate_filtered,duration_ms,source_url,http_status,error_category,error_summary,started_at,finished_at,metadata_json,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (result.id, run_id, zoo_id, result.zoo_slug, result.zoo_name, result.status, result.source_status, result.discovered, result.parsed, result.inserted, result.updated, result.failed, result.duplicate_filtered, duration, source_url, result.http_status, result.error_category, result.error_summary, started, finished, _json(result.metadata), now, now),
                )
        return self.get_zoo_run_result(run_id, zoo_id) or result

    save_zoo_run_result = upsert_zoo_run_result
    record_zoo_run_result = upsert_zoo_run_result
    upsert_crawl_zoo_result = upsert_zoo_run_result
    record_crawl_zoo_result = upsert_zoo_run_result

    def get_zoo_run_result(self, run_id: str, zoo_id: str) -> Optional[CrawlZooResult]:
        zoo = self.get_zoo(zoo_id)
        scope = zoo.id if zoo else str(zoo_id)
        row = self._connection.execute(
            "SELECT * FROM crawl_zoo_results WHERE crawl_run_id=? AND zoo_id=? LIMIT 1",
            (str(run_id), scope),
        ).fetchone()
        return self._zoo_result_from_row(row) if row else None

    def get_zoo_run_results(
        self, run_id: Optional[str] = None, zoo_id: Optional[str] = None
    ) -> list[CrawlZooResult]:
        clauses: list[str] = []
        args: list[Any] = []
        if run_id:
            clauses.append("crawl_run_id=?")
            args.append(str(run_id))
        if zoo_id:
            zoo = self.get_zoo(zoo_id)
            clauses.append("zoo_id=?")
            args.append(zoo.id if zoo else str(zoo_id))
        sql = "SELECT * FROM crawl_zoo_results"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at,id"
        return [
            self._zoo_result_from_row(row)
            for row in self._connection.execute(sql, args).fetchall()
        ]

    list_zoo_run_results = get_zoo_run_results
    list_zoo_results = get_zoo_run_results
