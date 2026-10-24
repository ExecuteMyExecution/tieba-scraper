from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "tieba.db"
BACKUP_KEEP = 5


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _image_list(value: Any) -> list[str]:
    return [str(url) for url in _json_list(value) if url]


def _normalized_thumbs(item: dict[str, Any]) -> list[str]:
    """Keep thumbnail URLs aligned with the original image list."""
    images = [str(url) for url in (item.get("images") or [])]
    thumbs = list(item.get("image_thumbs") or [])
    if len(thumbs) != len(images):
        return images
    return [
        str(thumb) if thumb else image
        for thumb, image in zip(thumbs, images)
    ]


def _aligned_thumbs(raw: Any, images: list[str]) -> list[str]:
    thumbs = _json_list(raw)
    if len(thumbs) != len(images):
        # Older rows have no thumbnail column value; callers use originals.
        return []
    return [
        str(thumb) if thumb else image
        for thumb, image in zip(thumbs, images)
    ]


_connection_state = threading.local()


def _get_connection() -> sqlite3.Connection:
    """Return a persistent connection for the current thread and DB path."""
    path = str(DB_PATH)
    state = getattr(_connection_state, "value", None)
    if state is not None and state[0] == path:
        return state[1]
    if state is not None:
        try:
            state[1].close()
        except sqlite3.Error:
            pass
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    _connection_state.value = (path, connection)
    return connection


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    connection = _get_connection()
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _backup_database() -> Path | None:
    """Create a consistent SQLite backup before replacing visible data."""
    if not DB_PATH.exists():
        return None
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / (
        f"auto-{DB_PATH.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
    )
    source = sqlite3.connect(DB_PATH, timeout=30)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    backups = sorted(
        backup_dir.glob(f"auto-{DB_PATH.stem}-*.db"),
        key=lambda path: path.stat().st_mtime,
    )
    for old_backup in backups[:-BACKUP_KEEP]:
        old_backup.unlink(missing_ok=True)
    return target


def count_forum_threads(forum_name: str) -> int:
    with connect() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM threads WHERE forum_name=?", (forum_name,)
        ).fetchone()
    return int(row[0]) if row else 0


def initialize() -> None:
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                forum_name TEXT NOT NULL,
                requested_pages INTEGER NOT NULL,
                include_images INTEGER NOT NULL,
                image_filter TEXT NOT NULL DEFAULT 'all',
                start_date TEXT,
                end_date TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                progress INTEGER NOT NULL DEFAULT 0,
                found_count INTEGER NOT NULL DEFAULT 0,
                saved_count INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                detail_cache_count INTEGER NOT NULL DEFAULT 0,
                image_cache_count INTEGER NOT NULL DEFAULT 0,
                image_failed_count INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS threads (
                tid INTEGER PRIMARY KEY,
                forum_name TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                author_name TEXT NOT NULL DEFAULT '',
                author_id INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL DEFAULT 0,
                last_reply_at INTEGER NOT NULL DEFAULT 0,
                reply_count INTEGER NOT NULL DEFAULT 0,
                view_count INTEGER NOT NULL DEFAULT 0,
                agree_count INTEGER NOT NULL DEFAULT 0,
                has_images INTEGER NOT NULL DEFAULT 0,
                images_json TEXT NOT NULL DEFAULT '[]',
                image_thumbs_json TEXT NOT NULL DEFAULT '[]',
                source_url TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS job_threads (
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                tid INTEGER NOT NULL,
                forum_name TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                author_name TEXT NOT NULL DEFAULT '',
                author_id INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL DEFAULT 0,
                last_reply_at INTEGER NOT NULL DEFAULT 0,
                reply_count INTEGER NOT NULL DEFAULT 0,
                view_count INTEGER NOT NULL DEFAULT 0,
                agree_count INTEGER NOT NULL DEFAULT 0,
                has_images INTEGER NOT NULL DEFAULT 0,
                images_json TEXT NOT NULL DEFAULT '[]',
                image_thumbs_json TEXT NOT NULL DEFAULT '[]',
                portrait_images_json TEXT NOT NULL DEFAULT '[]',
                portrait_thumbs_json TEXT NOT NULL DEFAULT '[]',
                source_url TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (job_id, tid)
            );

            CREATE TABLE IF NOT EXISTS image_portrait_cache (
                url TEXT PRIMARY KEY,
                is_portrait INTEGER NOT NULL,
                detector_version TEXT NOT NULL,
                checked_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_threads_forum ON threads(forum_name);
            CREATE INDEX IF NOT EXISTS idx_threads_created ON threads(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_threads_author ON threads(author_name);
            CREATE INDEX IF NOT EXISTS idx_threads_created_tid
                ON threads(created_at DESC, tid DESC);
            CREATE INDEX IF NOT EXISTS idx_threads_forum_created_tid
                ON threads(forum_name, created_at DESC, tid DESC);
            CREATE INDEX IF NOT EXISTS idx_threads_images_created_tid
                ON threads(has_images, created_at DESC, tid DESC);
            CREATE INDEX IF NOT EXISTS idx_threads_replies_created_tid
                ON threads(reply_count DESC, created_at DESC, tid DESC);
            CREATE INDEX IF NOT EXISTS idx_threads_views_created_tid
                ON threads(view_count DESC, created_at DESC, tid DESC);
            CREATE INDEX IF NOT EXISTS idx_job_threads_job ON job_threads(job_id);
            CREATE INDEX IF NOT EXISTS idx_job_threads_tid_fetched
                ON job_threads(tid, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_job_threads_forum_tid_fetched
                ON job_threads(forum_name, tid, fetched_at DESC);
            """
        )
        job_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "image_filter" not in job_columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN image_filter TEXT NOT NULL DEFAULT 'all'"
            )
        if "retry_count" not in job_columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
            )
        if "failed_count" not in job_columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN failed_count INTEGER NOT NULL DEFAULT 0"
            )
        if "skipped_count" not in job_columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN skipped_count INTEGER NOT NULL DEFAULT 0"
            )
        if "detail_cache_count" not in job_columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN detail_cache_count INTEGER NOT NULL DEFAULT 0"
            )
        if "image_cache_count" not in job_columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN image_cache_count INTEGER NOT NULL DEFAULT 0"
            )
        if "image_failed_count" not in job_columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN image_failed_count INTEGER NOT NULL DEFAULT 0"
            )
        table_columns = {
            table: {
                row["name"]
                for row in db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for table in ("threads", "job_threads")
        }
        for table in ("threads", "job_threads"):
            if "image_thumbs_json" not in table_columns[table]:
                db.execute(
                    f"ALTER TABLE {table} "
                    "ADD COLUMN image_thumbs_json TEXT NOT NULL DEFAULT '[]'"
                )
        if "portrait_thumbs_json" not in table_columns["job_threads"]:
            db.execute(
                "ALTER TABLE job_threads "
                "ADD COLUMN portrait_thumbs_json TEXT NOT NULL DEFAULT '[]'"
            )
        db.execute(
            "UPDATE jobs SET status='failed', message='服务重启，任务已中断', "
            "finished_at=datetime('now', 'localtime') WHERE status IN ('pending', 'running')"
        )


def create_job(payload: dict[str, Any]) -> int:
    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO jobs (
                forum_name, requested_pages, include_images, image_filter,
                start_date, end_date
            ) VALUES (?, ?, 1, ?, ?, ?)
            """,
            (
                payload["forum_name"],
                payload["pages"],
                payload["first_floor_image_filter"],
                payload.get("start_date"),
                payload.get("end_date"),
            ),
        )
        return int(cursor.lastrowid)


def update_job(job_id: int, **values: Any) -> None:
    allowed = {
        "status", "progress", "found_count", "saved_count", "retry_count",
        "failed_count", "skipped_count", "detail_cache_count",
        "image_cache_count", "image_failed_count", "message", "finished_at",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key}=?" for key in values)
    with connect() as db:
        db.execute(
            f"UPDATE jobs SET {assignments} WHERE id=?",  # columns come from allow-list
            (*values.values(), job_id),
        )


def get_job(job_id: int) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(limit: int = 8) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def get_cached_thread(
    tid: int, forum_name: str, max_age_hours: int = 24
) -> dict[str, Any] | None:
    """Return a recent successful first-floor snapshot for the same topic."""
    return get_cached_threads([tid], forum_name, max_age_hours).get(tid)


def get_cached_threads(
    tids: list[int], forum_name: str, max_age_hours: int = 24
) -> dict[int, dict[str, Any]]:
    """Bulk-load each topic's latest recent first-floor snapshot."""
    if not tids or max_age_hours <= 0:
        return {}
    result: dict[int, dict[str, Any]] = {}
    unique_tids = list(dict.fromkeys(int(tid) for tid in tids))
    with connect() as db:
        for offset in range(0, len(unique_tids), 400):
            batch = unique_tids[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = db.execute(
                f"""
                SELECT tid, content, author_name, has_images,
                       images_json, image_thumbs_json, fetched_at
                FROM (
                    SELECT tid, content, author_name, has_images,
                           images_json, image_thumbs_json, fetched_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY tid
                               ORDER BY fetched_at DESC, job_id DESC
                           ) AS row_number
                    FROM job_threads
                    WHERE forum_name=?
                      AND fetched_at >= datetime('now', 'localtime', ?)
                      AND tid IN ({placeholders})
                )
                WHERE row_number=1
                """,
                (forum_name, f"-{max_age_hours} hours", *batch),
            ).fetchall()
            for row in rows:
                item = dict(row)
                tid = int(item.pop("tid"))
                item["images"] = _image_list(item.pop("images_json"))
                item["image_thumbs"] = _aligned_thumbs(
                    item.pop("image_thumbs_json"), item["images"]
                )
                item["has_images"] = bool(item["has_images"])
                result[tid] = item
    return result


def get_portrait_cache(
    urls: list[str], detector_version: str, max_age_days: int = 14
) -> dict[str, bool]:
    if not urls or max_age_days <= 0:
        return {}
    result: dict[str, bool] = {}
    with connect() as db:
        for offset in range(0, len(urls), 400):
            batch = urls[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = db.execute(
                f"""
                SELECT url, is_portrait FROM image_portrait_cache
                WHERE detector_version=?
                  AND checked_at >= datetime('now', 'localtime', ?)
                  AND url IN ({placeholders})
                """,
                (detector_version, f"-{max_age_days} days", *batch),
            ).fetchall()
            result.update({row["url"]: bool(row["is_portrait"]) for row in rows})
    return result


def set_portrait_cache(
    outcomes: dict[str, bool], detector_version: str
) -> None:
    if not outcomes:
        return
    with connect() as db:
        db.executemany(
            """
            INSERT INTO image_portrait_cache (
                url, is_portrait, detector_version, checked_at
            ) VALUES (?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(url) DO UPDATE SET
                is_portrait=excluded.is_portrait,
                detector_version=excluded.detector_version,
                checked_at=excluded.checked_at
            """,
            [(url, int(value), detector_version) for url, value in outcomes.items()],
        )


def upsert_thread(item: dict[str, Any]) -> None:
    with connect() as db:
        db.execute(
            """
            INSERT INTO threads (
                tid, forum_name, title, content, author_name, author_id,
                created_at, last_reply_at, reply_count, view_count, agree_count,
                has_images, images_json, image_thumbs_json, source_url,
                fetched_at, job_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), ?)
            ON CONFLICT(tid) DO UPDATE SET
                forum_name=excluded.forum_name,
                title=excluded.title,
                content=excluded.content,
                author_name=excluded.author_name,
                author_id=excluded.author_id,
                created_at=excluded.created_at,
                last_reply_at=excluded.last_reply_at,
                reply_count=excluded.reply_count,
                view_count=excluded.view_count,
                agree_count=excluded.agree_count,
                has_images=excluded.has_images,
                images_json=excluded.images_json,
                image_thumbs_json=excluded.image_thumbs_json,
                source_url=excluded.source_url,
                fetched_at=excluded.fetched_at,
                job_id=excluded.job_id
            """,
            (
                item["tid"], item["forum_name"], item["title"], item["content"],
                item["author_name"], item["author_id"], item["created_at"],
                item["last_reply_at"], item["reply_count"], item["view_count"],
                item["agree_count"], int(item["has_images"]),
                json.dumps(item["images"], ensure_ascii=False),
                json.dumps(_normalized_thumbs(item), ensure_ascii=False),
                item["source_url"], item["job_id"],
            ),
        )


def stage_threads(job_id: int, items: list[dict[str, Any]]) -> None:
    """Store an unfiltered crawl snapshot without changing the visible library."""
    if not items:
        return
    values = [
        (
            job_id, item["tid"], item["forum_name"], item["title"], item["content"],
            item["author_name"], item["author_id"], item["created_at"],
            item["last_reply_at"], item["reply_count"], item["view_count"],
            item["agree_count"], int(item["has_images"]),
            json.dumps(item["images"], ensure_ascii=False),
            json.dumps(_normalized_thumbs(item), ensure_ascii=False),
            item["source_url"],
        )
        for item in items
    ]
    with connect() as db:
        db.executemany(
            """
            INSERT INTO job_threads (
                job_id, tid, forum_name, title, content, author_name, author_id,
                created_at, last_reply_at, reply_count, view_count, agree_count,
                has_images, images_json, image_thumbs_json, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(job_id, tid) DO UPDATE SET
                forum_name=excluded.forum_name,
                title=excluded.title,
                content=excluded.content,
                author_name=excluded.author_name,
                author_id=excluded.author_id,
                created_at=excluded.created_at,
                last_reply_at=excluded.last_reply_at,
                reply_count=excluded.reply_count,
                view_count=excluded.view_count,
                agree_count=excluded.agree_count,
                has_images=excluded.has_images,
                images_json=excluded.images_json,
                image_thumbs_json=excluded.image_thumbs_json,
                source_url=excluded.source_url,
                fetched_at=excluded.fetched_at
            """,
            values,
        )


def set_portrait_matches(
    job_id: int,
    matches: dict[int, list[str]],
    thumbs: dict[int, list[str]] | None = None,
) -> None:
    """Attach portrait results to the exact snapshot that was classified."""
    thumbs = thumbs or {}
    values = []
    for tid, urls in matches.items():
        portrait_thumbs = list(thumbs.get(tid) or [])
        if len(portrait_thumbs) != len(urls):
            portrait_thumbs = list(urls)
        values.append((
            json.dumps(urls, ensure_ascii=False),
            json.dumps(portrait_thumbs, ensure_ascii=False),
            job_id,
            tid,
        ))
    if not values:
        return
    with connect() as db:
        db.executemany(
            "UPDATE job_threads "
            "SET portrait_images_json=?, portrait_thumbs_json=? "
            "WHERE job_id=? AND tid=?",
            values,
        )


def publish_job_threads(
    job_id: int,
    image_filter: str,
    preserve_tids: set[int] | None = None,
) -> int:
    """Replace this forum's visible rows while keeping unresolved old rows.

    ``preserve_tids`` is used for topics whose detail could not be fetched in
    this run. Keeping their previous snapshots avoids deleting data merely
    because a request was rate-limited or failed temporarily.
    """
    filters = {
        "all": "1=1",
        "with_images": "has_images=1",
        "without_images": "has_images=0",
        "portrait_images": "portrait_images_json != '[]'",
    }
    condition = filters.get(image_filter, filters["all"])
    portrait_mode = image_filter == "portrait_images"
    images_expression = "portrait_images_json" if portrait_mode else "images_json"
    thumbs_expression = "portrait_thumbs_json" if portrait_mode else "image_thumbs_json"
    has_images_expression = "1" if portrait_mode else "has_images"
    preserve_tids = sorted({int(tid) for tid in (preserve_tids or [])})
    with connect() as db:
        job = db.execute(
            "SELECT forum_name FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None:
            raise ValueError(f"任务 #{job_id} 不存在")
        forum_name = job["forum_name"]
        existing_count = db.execute(
            "SELECT COUNT(*) FROM threads WHERE forum_name=?", (forum_name,)
        ).fetchone()[0]
        new_count = db.execute(
            f"SELECT COUNT(*) FROM job_threads WHERE job_id=? AND {condition}",
            (job_id,),
        ).fetchone()[0]
        if existing_count and new_count == 0:
            raise ValueError("本次没有可发布的主题，已保留该吧原数据")
        if existing_count:
            _backup_database()
        if preserve_tids:
            db.execute("DROP TABLE IF EXISTS temp.preserve_tids")
            db.execute(
                "CREATE TEMP TABLE preserve_tids (tid INTEGER PRIMARY KEY)"
            )
            db.executemany(
                "INSERT OR IGNORE INTO preserve_tids(tid) VALUES (?)",
                [(tid,) for tid in preserve_tids],
            )
            db.execute("DROP TABLE IF EXISTS temp.preserved_threads")
            db.execute(
                """
                CREATE TEMP TABLE preserved_threads AS
                SELECT threads.*
                FROM threads
                JOIN preserve_tids ON preserve_tids.tid = threads.tid
                WHERE threads.forum_name=?
                """,
                (forum_name,),
            )
        db.execute("DELETE FROM threads WHERE forum_name=?", (forum_name,))
        cursor = db.execute(
            f"""
            INSERT INTO threads (
                tid, forum_name, title, content, author_name, author_id,
                created_at, last_reply_at, reply_count, view_count, agree_count,
                has_images, images_json, image_thumbs_json, source_url, fetched_at, job_id
            )
            SELECT
                tid, forum_name, title, content, author_name, author_id,
                created_at, last_reply_at, reply_count, view_count, agree_count,
                {has_images_expression}, {images_expression}, {thumbs_expression},
                source_url, datetime('now', 'localtime'), job_id
            FROM job_threads
            WHERE job_id=? AND {condition}
            """,
            (job_id,),
        )
        if preserve_tids:
            db.execute(
                "INSERT OR IGNORE INTO threads SELECT * FROM preserved_threads"
            )
            db.execute("DROP TABLE preserved_threads")
            db.execute("DROP TABLE preserve_tids")
        return max(cursor.rowcount, 0)


def retain_threads_for_job(job_id: int) -> int:
    """Keep only this job's rows for its forum, preserving other forums."""
    with connect() as db:
        job = db.execute(
            "SELECT forum_name FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None:
            return 0
        cursor = db.execute(
            """
            DELETE FROM threads
            WHERE forum_name=? AND (job_id IS NULL OR job_id != ?)
            """,
            (job["forum_name"], job_id),
        )
        return max(cursor.rowcount, 0)


def list_forums() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT forum_name, COUNT(*) thread_count, MAX(job_id) job_id,
                   MAX(fetched_at) fetched_at
            FROM threads
            GROUP BY forum_name
            ORDER BY MAX(fetched_at) DESC, forum_name
            """
        ).fetchall()
    return [dict(row) for row in rows]


SORT_COLUMNS = {
    "newest": "created_at DESC, tid DESC",
    "oldest": "created_at ASC, tid ASC",
    "replies": "reply_count DESC, created_at DESC, tid DESC",
    "views": "view_count DESC, created_at DESC, tid DESC",
}


def search_threads(
    *,
    query: str = "",
    forum: str = "",
    author: str = "",
    exclude_authors: list[str] | None = None,
    has_images: str = "all",
    start_date: str = "",
    end_date: str = "",
    sort: str = "newest",
    page: int = 1,
    page_size: int = 20,
    anchor_tid: int | None = None,
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if query:
        where.append("(title LIKE ? OR content LIKE ?)")
        needle = f"%{query}%"
        params.extend([needle, needle])
    if forum:
        where.append("forum_name = ?")
        params.append(forum)
    if author:
        where.append("author_name LIKE ?")
        params.append(f"%{author}%")
    excluded_authors = list(dict.fromkeys(
        name.strip() for name in (exclude_authors or []) if name.strip()
    ))
    if excluded_authors:
        placeholders = ",".join("?" for _ in excluded_authors)
        where.append(f"author_name NOT IN ({placeholders})")
        params.extend(excluded_authors)
    if has_images in {"yes", "no"}:
        where.append("has_images=?")
        params.append(1 if has_images == "yes" else 0)
    if start_date:
        where.append("created_at >= ?")
        params.append(int(datetime.fromisoformat(start_date).timestamp()))
    if end_date:
        where.append("created_at < ?")
        end_exclusive = datetime.fromisoformat(end_date) + timedelta(days=1)
        params.append(int(end_exclusive.timestamp()))

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order_by = SORT_COLUMNS.get(sort, SORT_COLUMNS["newest"])
    with connect() as db:
        summary_row = db.execute(
            f"""
            SELECT COUNT(*) total,
                   COUNT(DISTINCT forum_name) forum_count,
                   COUNT(DISTINCT job_id) job_count,
                   MIN(job_id) only_job_id
            FROM threads {clause}
            """,
            params,
        ).fetchone()
        total = summary_row["total"]
        if anchor_tid is not None:
            # Keep the item the user was looking at on screen after filters
            # changed. The client sends the nearest non-ignored card's tid.
            anchor_row = db.execute(
                f"""
                WITH filtered AS (
                    SELECT tid, ROW_NUMBER() OVER (ORDER BY {order_by}) AS position
                    FROM threads {clause}
                )
                SELECT position FROM filtered WHERE tid=?
                """,
                (*params, anchor_tid),
            ).fetchone()
            if anchor_row is not None:
                position = int(anchor_row["position"])
                page = max(1, (position + page_size - 1) // page_size)
        offset = (page - 1) * page_size
        rows = db.execute(
            f"SELECT * FROM threads {clause} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["images"] = _image_list(item.pop("images_json"))
        item["image_thumbs"] = _aligned_thumbs(
            item.pop("image_thumbs_json", None), item["images"]
        )
        items.append(item)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "forum_count": summary_row["forum_count"],
        "job_id": (
            summary_row["only_job_id"] if summary_row["job_count"] == 1 else None
        ),
    }


def get_stats() -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            """
            SELECT COUNT(*) total,
                   COUNT(DISTINCT forum_name) forums,
                   COALESCE(SUM(has_images), 0) with_images,
                   COALESCE(MAX(created_at), 0) latest
            FROM threads
            """
        ).fetchone()
    return dict(row)
