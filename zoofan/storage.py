"""SQLite persistence with an adapter-shaped API.

The methods operate on domain records and keep all schema details here.  A
future PostgreSQL implementation can implement the same methods without
forcing parsers or crawlers to know about SQL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple, Union

from .models import Article, ArticleDiscovery, ArticleUpsertOutcome, CrawlRun, CrawlRunStat, Source, Zoo
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


def _json(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"


def _load_json(value: Optional[str]) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


class SQLiteStorage:
    """Transactional storage for crawl state and article records."""

    SCHEMA_VERSION = 4

    def __init__(self, path: Union[str, Path] = ":memory:", connection: Optional[sqlite3.Connection] = None) -> None:
        self.path = str(path)
        self._connection = connection or sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self.create_schema()

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
        extra_declarations = extra_declarations or {}
        columns = {
            "zoos": ["id TEXT PRIMARY KEY", "slug TEXT UNIQUE", "name TEXT", "website_url TEXT", "country_code TEXT", "language TEXT", "enabled INTEGER DEFAULT 1", "metadata_json TEXT DEFAULT '{}'", "created_at TEXT", "updated_at TEXT"],
            "sources": ["id TEXT PRIMARY KEY", "zoo_id TEXT", "url TEXT", "normalized_url TEXT", "kind TEXT DEFAULT 'rss'", "name TEXT", "language TEXT", "config_json TEXT DEFAULT '{}'", "enabled INTEGER DEFAULT 1", "status TEXT DEFAULT 'pending'", "success INTEGER", "last_checked TEXT", "last_success TEXT", "last_error TEXT", "last_http_status INTEGER", "created_at TEXT", "updated_at TEXT"],
            "articles": ["id TEXT PRIMARY KEY", "canonical_url TEXT", "normalized_url TEXT", "source_url TEXT", "title TEXT", "published_at TEXT", "updated_at_source TEXT", "author TEXT", "summary TEXT", "content TEXT", "content_hash TEXT", "html_hash TEXT", "raw_html TEXT", "language TEXT", "http_status INTEGER", "crawl_status TEXT", "last_fetched_at TEXT", "metadata_json TEXT DEFAULT '{}'", "created_at TEXT", "updated_at TEXT"],
            "article_discoveries": ["id TEXT PRIMARY KEY", "article_id TEXT", "source_id TEXT", "discovered_url TEXT", "discovered_key TEXT NOT NULL DEFAULT ''", "discovered_at TEXT", "last_discovered_at TEXT", "metadata_json TEXT DEFAULT '{}'"],
            "crawl_runs": ["id TEXT PRIMARY KEY", "batch_id TEXT UNIQUE", "started_at TEXT", "finished_at TEXT", "duration_ms INTEGER", "status TEXT DEFAULT 'running'", "error TEXT", "metadata_json TEXT DEFAULT '{}'"],
            "crawl_run_stats": ["id TEXT PRIMARY KEY", "crawl_run_id TEXT", "zoo_id TEXT", "source_id TEXT", "status TEXT DEFAULT 'running'", "discovered_count INTEGER DEFAULT 0", "fetched_count INTEGER DEFAULT 0", "stored_count INTEGER DEFAULT 0", "already_known_count INTEGER DEFAULT 0", "duplicate_candidate_count INTEGER DEFAULT 0", "error_count INTEGER DEFAULT 0", "started_at TEXT", "finished_at TEXT", "duration_ms INTEGER", "error TEXT", "errors_json TEXT DEFAULT '[]'", "metadata_json TEXT DEFAULT '{}'"],
        }
        foreign_keys = {
            "sources": [f"FOREIGN KEY(zoo_id) REFERENCES {z}(id) ON UPDATE CASCADE ON DELETE RESTRICT"],
            "article_discoveries": [f"FOREIGN KEY(article_id) REFERENCES {a}(id) ON UPDATE CASCADE ON DELETE CASCADE", f"FOREIGN KEY(source_id) REFERENCES {s}(id) ON UPDATE CASCADE ON DELETE CASCADE"],
            "crawl_run_stats": [f"FOREIGN KEY(crawl_run_id) REFERENCES {r}(id) ON UPDATE CASCADE ON DELETE CASCADE", f"FOREIGN KEY(zoo_id) REFERENCES {z}(id) ON UPDATE CASCADE ON DELETE SET NULL", f"FOREIGN KEY(source_id) REFERENCES {s}(id) ON UPDATE CASCADE ON DELETE SET NULL"],
        }
        physical = {"zoos": z, "sources": s, "articles": a, "article_discoveries": d, "crawl_runs": r, "crawl_run_stats": rs}
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
                first = min(filter(None, (existing["discovered_at"], donor["discovered_at"])), default=None)
                last = max(filter(None, (existing["last_discovered_at"], donor["last_discovered_at"], existing["discovered_at"], donor["discovered_at"])), default=None)
                metadata = {**_load_json(donor["metadata_json"]), **_load_json(existing["metadata_json"])}
                db.execute(
                    "UPDATE article_discoveries SET discovered_at=?,last_discovered_at=?,metadata_json=? WHERE id=?",
                    (first, last, _json(metadata), existing["id"]),
                )
                db.execute("DELETE FROM article_discoveries WHERE id=?", (donor["id"],))
            else:
                db.execute(f"UPDATE article_discoveries SET {column}=? WHERE id=?", (keeper_id, donor["id"]))

    @classmethod
    def _consolidate_legacy_articles(cls, db: sqlite3.Connection) -> None:
        evidence = (
            "canonical_url", "normalized_url", "source_url", "title", "published_at",
            "updated_at_source", "author", "summary", "content", "content_hash", "html_hash",
            "raw_html", "language", "http_status", "crawl_status", "last_fetched_at",
        )
        while True:
            duplicate = None
            for column in ("canonical_url", "normalized_url", "content_hash"):
                duplicate = db.execute(
                    f"SELECT {column} AS value FROM articles WHERE {column} IS NOT NULL AND {column}<>'' GROUP BY {column} HAVING COUNT(*)>1 LIMIT 1"
                ).fetchone()
                if duplicate:
                    break
            if not duplicate:
                return
            rows = db.execute(f"SELECT rowid,* FROM articles WHERE {column}=?", (duplicate["value"],)).fetchall()
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
                    if donor[name] not in (None, "") and merged[name] not in (None, "") and donor[name] != merged[name]
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

    def _migrate_schema(self, db: sqlite3.Connection) -> None:
        now = datetime.now(timezone.utc).isoformat()
        definitions = {
            "zoos": {"id": "TEXT", "slug": "TEXT", "name": "TEXT", "website_url": "TEXT", "country_code": "TEXT", "language": "TEXT", "enabled": "INTEGER DEFAULT 1", "metadata_json": "TEXT DEFAULT '{}'", "created_at": "TEXT", "updated_at": "TEXT"},
            "sources": {"id": "TEXT", "zoo_id": "TEXT", "url": "TEXT", "normalized_url": "TEXT", "kind": "TEXT DEFAULT 'rss'", "name": "TEXT", "language": "TEXT", "config_json": "TEXT DEFAULT '{}'", "enabled": "INTEGER DEFAULT 1", "status": "TEXT DEFAULT 'pending'", "success": "INTEGER", "last_checked": "TEXT", "last_success": "TEXT", "last_error": "TEXT", "last_http_status": "INTEGER", "created_at": "TEXT", "updated_at": "TEXT"},
            "articles": {"id": "TEXT", "canonical_url": "TEXT", "normalized_url": "TEXT", "source_url": "TEXT", "title": "TEXT", "published_at": "TEXT", "updated_at_source": "TEXT", "author": "TEXT", "summary": "TEXT", "content": "TEXT", "content_hash": "TEXT", "html_hash": "TEXT", "raw_html": "TEXT", "language": "TEXT", "http_status": "INTEGER", "crawl_status": "TEXT", "last_fetched_at": "TEXT", "metadata_json": "TEXT DEFAULT '{}'", "created_at": "TEXT", "updated_at": "TEXT"},
            "article_discoveries": {"id": "TEXT", "article_id": "TEXT", "source_id": "TEXT", "discovered_url": "TEXT", "discovered_key": "TEXT NOT NULL DEFAULT ''", "discovered_at": "TEXT", "last_discovered_at": "TEXT", "metadata_json": "TEXT DEFAULT '{}'"},
            "crawl_runs": {"id": "TEXT", "batch_id": "TEXT", "started_at": "TEXT", "finished_at": "TEXT", "duration_ms": "INTEGER", "status": "TEXT DEFAULT 'running'", "error": "TEXT", "metadata_json": "TEXT DEFAULT '{}'"},
            "crawl_run_stats": {"id": "TEXT", "crawl_run_id": "TEXT", "zoo_id": "TEXT", "source_id": "TEXT", "status": "TEXT DEFAULT 'running'", "discovered_count": "INTEGER DEFAULT 0", "fetched_count": "INTEGER DEFAULT 0", "stored_count": "INTEGER DEFAULT 0", "already_known_count": "INTEGER DEFAULT 0", "duplicate_candidate_count": "INTEGER DEFAULT 0", "error_count": "INTEGER DEFAULT 0", "started_at": "TEXT", "finished_at": "TEXT", "duration_ms": "INTEGER", "error": "TEXT", "errors_json": "TEXT DEFAULT '[]'", "metadata_json": "TEXT DEFAULT '{}'"},
        }
        for table, columns in definitions.items():
            self._add_columns(db, table, columns)
        for table in ("zoos", "sources", "articles", "article_discoveries", "crawl_runs", "crawl_run_stats"):
            self._ensure_ids(db, table)
        # Legacy schemas used url rather than source_url.  Do not infer either
        # hash from the other: legacy NULL evidence must remain unknown.
        article_cols = self._columns(db, "articles")
        if "url" in article_cols:
            db.execute("UPDATE articles SET source_url=COALESCE(source_url,url) WHERE source_url IS NULL")
        db.execute("UPDATE articles SET normalized_url=COALESCE(NULLIF(normalized_url,''), canonical_url, source_url)")
        for row in db.execute("SELECT rowid, canonical_url, normalized_url, source_url FROM articles").fetchall():
            canonical = normalize_url(row["canonical_url"]) if row["canonical_url"] else None
            normalized = normalize_url(row["normalized_url"] or row["source_url"]) if (row["normalized_url"] or row["source_url"]) else None
            db.execute("UPDATE articles SET canonical_url=?, normalized_url=? WHERE rowid=?", (canonical, normalized, row["rowid"]))
        db.execute("UPDATE articles SET created_at=COALESCE(created_at,?), updated_at=COALESCE(updated_at,?)", (now, now))
        source_cols = self._columns(db, "sources")
        if "source_url" in source_cols:
            db.execute("UPDATE sources SET url=COALESCE(url,source_url) WHERE url IS NULL")
        db.execute("UPDATE sources SET normalized_url=COALESCE(NULLIF(normalized_url,''),url)")
        for row in db.execute("SELECT rowid, normalized_url, url FROM sources").fetchall():
            db.execute("UPDATE sources SET normalized_url=? WHERE rowid=?", (normalize_url(row["normalized_url"] or row["url"]), row["rowid"]))
        db.execute("UPDATE sources SET created_at=COALESCE(created_at,?), updated_at=COALESCE(updated_at,?)", (now, now))
        db.execute("UPDATE article_discoveries SET discovered_key=COALESCE(NULLIF(discovered_key,''), discovered_url, '')")
        for row in db.execute("SELECT rowid, discovered_url, discovered_key FROM article_discoveries").fetchall():
            db.execute("UPDATE article_discoveries SET discovered_url=?, discovered_key=? WHERE rowid=?", (normalize_url(row["discovered_url"]) if row["discovered_url"] else None, normalize_url(row["discovered_key"]) if row["discovered_key"] else "", row["rowid"]))
        db.execute("UPDATE article_discoveries SET last_discovered_at=COALESCE(last_discovered_at, discovered_at)")
        db.execute("UPDATE crawl_runs SET started_at=COALESCE(started_at,?), status=COALESCE(status,'running'), metadata_json=COALESCE(metadata_json,'{}')", (now,))
        db.execute("UPDATE crawl_run_stats SET discovered_count=COALESCE(discovered_count,0), fetched_count=COALESCE(fetched_count,0), stored_count=COALESCE(stored_count,0), already_known_count=COALESCE(already_known_count,0), duplicate_candidate_count=COALESCE(duplicate_candidate_count,0), error_count=COALESCE(error_count,0)")
        self._backfill_durations(db, "crawl_runs")
        self._backfill_durations(db, "crawl_run_stats")
        self._consolidate_legacy_articles(db)
        self._consolidate_legacy_sources(db)
        self._rebuild_with_foreign_keys(db, definitions)
        indexes = (
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_sources_zoo_normalized_url ON sources(zoo_id, normalized_url)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_articles_canonical_url ON articles(canonical_url)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_articles_normalized_url ON articles(normalized_url)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_articles_content_hash ON articles(content_hash)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_discoveries_identity ON article_discoveries(article_id, source_id, discovered_key)",
            "CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status)",
            "CREATE INDEX IF NOT EXISTS idx_articles_normalized_url ON articles(normalized_url)",
            "CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles(content_hash)",
            "CREATE INDEX IF NOT EXISTS idx_discoveries_source ON article_discoveries(source_id)",
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
        tables = ("zoos", "sources", "articles", "article_discoveries", "crawl_runs", "crawl_run_stats")
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
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
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
                "SELECT id FROM zoos WHERE id = ? OR slug = ? LIMIT 1", (zoo.id, zoo.slug)
            ).fetchone()
            if existing:
                zoo.id = str(existing["id"])
                db.execute(
                    """UPDATE zoos SET slug=?, name=?, website_url=?, country_code=?, language=?, enabled=?, metadata_json=?, updated_at=? WHERE id=?""",
                    (zoo.slug, zoo.name, zoo.website_url, zoo.country_code, zoo.language, int(zoo.enabled), _json(zoo.metadata), now, zoo.id),
                )
            else:
                db.execute(
                    """INSERT INTO zoos(id,slug,name,website_url,country_code,language,enabled,metadata_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (zoo.id, zoo.slug, zoo.name, zoo.website_url, zoo.country_code, zoo.language, int(zoo.enabled), _json(zoo.metadata), now, now),
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
        return Zoo(
            id=row["id"], slug=row["slug"], name=row["name"], website_url=row["website_url"], country_code=row["country_code"], language=row["language"],
            enabled=bool(row["enabled"]), metadata=_load_json(row["metadata_json"]),
        )

    def upsert_source(self, source: Union[Source, Mapping[str, Any]]) -> Source:
        if not isinstance(source, Source):
            source = Source(**dict(source))
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
        return self.get_source(source.id)

    record_source_check = update_source_status

    # ---- articles and discovery ------------------------------------------

    @staticmethod
    def _article_from_row(row: sqlite3.Row) -> Article:
        return Article(
            id=row["id"], canonical_url=row["canonical_url"], normalized_url=row["normalized_url"], url=row["source_url"],
            title=row["title"], published_at=_decoded_timestamp(row["published_at"]),
            updated_at_source=_decoded_timestamp(row["updated_at_source"]), author=row["author"], summary=row["summary"],
            content=row["content"], content_hash=row["content_hash"], html_hash=row["html_hash"], language=row["language"],
            http_status=row["http_status"], crawl_status=row["crawl_status"], last_fetched_at=_decoded_timestamp(row["last_fetched_at"]),
            raw_html=row["raw_html"], metadata=_load_json(row["metadata_json"]),
        )

    def _find_article(self, db: sqlite3.Connection, article: Article) -> Optional[sqlite3.Row]:
        # Identity order is intentional: canonical, normalized, content hash.
        for column, value in (
            ("canonical_url", normalize_url(article.canonical_url) if article.canonical_url else None),
            ("normalized_url", normalize_url(article.normalized_url or article.url) if (article.normalized_url or article.url) else None),
            ("content_hash", article.content_hash),
        ):
            if value:
                row = db.execute(f"SELECT * FROM articles WHERE {column}=? LIMIT 1", (value,)).fetchone()
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
    ) -> Article:
        if not isinstance(article, Article):
            article = Article(**dict(article))
        if source is not None:
            source = self.upsert_source(source)
            source_id = source.id
        article.id = _id(article.id)
        article.canonical_url = normalize_url(article.canonical_url) if article.canonical_url else None
        article.normalized_url = normalize_url(article.normalized_url or article.url) if (article.normalized_url or article.url) else None
        if article.url:
            article.url = normalize_url(article.url)
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as db:
            existing = self._find_article(db, article)
            if existing:
                article.id = str(existing["id"])
                # Preserve richer existing values whenever this discovery only
                # has a subset of fields, while filling every missing value.
                values = {
                    "canonical_url": article.canonical_url or existing["canonical_url"],
                    "normalized_url": article.normalized_url or existing["normalized_url"],
                    # Once an article has an identity, a later discovery must
                    # not replace its canonical/source identity. This is
                    # especially important when deduplication occurs through
                    # the third-layer raw HTML hash.
                    "source_url": existing["source_url"] or article.url,
                    "title": article.title or existing["title"],
                    "published_at": _timestamp(article.published_at) or existing["published_at"],
                    "updated_at_source": _timestamp(article.updated_at_source) or existing["updated_at_source"],
                    "author": article.author or existing["author"],
                    "summary": article.summary or existing["summary"],
                    "content": article.content or existing["content"],
                    "content_hash": article.content_hash or existing["content_hash"],
                    "html_hash": article.html_hash or existing["html_hash"],
                    "language": article.language or existing["language"],
                    "http_status": article.http_status if article.http_status is not None else existing["http_status"],
                    "crawl_status": article.crawl_status or existing["crawl_status"],
                    "last_fetched_at": _timestamp(article.last_fetched_at) or existing["last_fetched_at"],
                    "raw_html": article.raw_html or existing["raw_html"],
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
                db.execute(
                    """UPDATE articles SET canonical_url=?,normalized_url=?,source_url=?,title=?,published_at=?,updated_at_source=?,author=?,summary=?,content=?,content_hash=?,html_hash=?,language=?,http_status=?,crawl_status=?,last_fetched_at=?,raw_html=?,metadata_json=?,updated_at=? WHERE id=?""",
                    (*values.values(), now, article.id),
                )
            else:
                db.execute(
                    """INSERT INTO articles(id,canonical_url,normalized_url,source_url,title,published_at,updated_at_source,author,summary,content,content_hash,html_hash,language,http_status,crawl_status,last_fetched_at,raw_html,metadata_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (article.id, article.canonical_url, article.normalized_url, article.url, article.title, _timestamp(article.published_at),
                     _timestamp(article.updated_at_source), article.author, article.summary, article.content, article.content_hash,
                     article.html_hash, article.language, article.http_status, article.crawl_status, _timestamp(article.last_fetched_at),
                     article.raw_html, _json(article.metadata), now, now),
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
        item = article if isinstance(article, Article) else Article(**dict(article))
        with self._lock:
            existing = self._find_article(self._connection, item)
            persisted = self.upsert_article(item, **kwargs)
        return ArticleUpsertOutcome(article=persisted, created=existing is None)

    def get_article(self, article_id: str) -> Optional[Article]:
        row = self._connection.execute("SELECT * FROM articles WHERE id=?", (str(article_id),)).fetchone()
        return self._article_from_row(row) if row else None

    def get_article_by_url(self, url: str) -> Optional[Article]:
        normalized = normalize_url(url)
        row = self._connection.execute(
            "SELECT * FROM articles WHERE canonical_url=? OR normalized_url=? LIMIT 1", (normalized, normalized)
        ).fetchone()
        return self._article_from_row(row) if row else None

    def list_articles(self, limit: Optional[int] = None) -> list[Article]:
        sql = "SELECT * FROM articles ORDER BY published_at DESC, created_at DESC"
        args: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (int(limit),)
        return [self._article_from_row(row) for row in self._connection.execute(sql, args).fetchall()]

    def _record_discovery_in_transaction(self, db: sqlite3.Connection, discovery: ArticleDiscovery) -> ArticleDiscovery:
        discovery.id = _id(discovery.id)
        db.execute(
            """INSERT INTO article_discoveries(id,article_id,source_id,discovered_url,discovered_key,discovered_at,last_discovered_at,metadata_json)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(article_id,source_id,discovered_key) DO UPDATE SET
               discovered_url=excluded.discovered_url, last_discovered_at=excluded.last_discovered_at,
               metadata_json=excluded.metadata_json""",
            (discovery.id, discovery.article_id, discovery.source_id, normalize_url(discovery.discovered_url) if discovery.discovered_url else None,
             normalize_url(discovery.discovered_url) if discovery.discovered_url else "",
             _timestamp(discovery.discovered_at) or datetime.now(timezone.utc).isoformat(),
             _timestamp(discovery.last_discovered_at or discovery.discovered_at) or datetime.now(timezone.utc).isoformat(), _json(discovery.metadata)),
        )
        row = db.execute(
            "SELECT * FROM article_discoveries WHERE article_id=? AND source_id=? AND discovered_key=? LIMIT 1",
            (discovery.article_id, discovery.source_id, normalize_url(discovery.discovered_url) if discovery.discovered_url else ""),
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
        return [ArticleDiscovery(id=row["id"], article_id=row["article_id"], source_id=row["source_id"], discovered_url=row["discovered_url"], discovered_at=_decoded_timestamp(row["discovered_at"]), last_discovered_at=_decoded_timestamp(row["last_discovered_at"]), metadata=_load_json(row["metadata_json"])) for row in rows]

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
