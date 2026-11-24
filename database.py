"""Async SQLite metadata store for folders, Telegram file_ids, and tags."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

VALID_FILE_TYPES: tuple[str, ...] = (
    "document",
    "photo",
    "video",
    "audio",
    "voice",
)

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    parent_id  INTEGER NULL REFERENCES folders(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_folders_unique_sibling
    ON folders(user_id, COALESCE(parent_id, 0), LOWER(name));

CREATE INDEX IF NOT EXISTS idx_folders_user_parent
    ON folders(user_id, parent_id);

CREATE TABLE IF NOT EXISTS files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    folder_id      INTEGER NULL REFERENCES folders(id) ON DELETE CASCADE,
    file_id        TEXT    NOT NULL,
    file_unique_id TEXT    NOT NULL,
    display_name   TEXT    NOT NULL,
    file_type      TEXT    NOT NULL CHECK (file_type IN ('document', 'photo', 'video', 'audio', 'voice')),
    mime_type      TEXT    NULL,
    file_size      INTEGER NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_files_unique_in_folder
    ON files(user_id, COALESCE(folder_id, 0), file_unique_id);

CREATE INDEX IF NOT EXISTS idx_files_user_folder
    ON files(user_id, folder_id);

CREATE INDEX IF NOT EXISTS idx_files_user_type
    ON files(user_id, file_type);

CREATE INDEX IF NOT EXISTS idx_files_user_name
    ON files(user_id, display_name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_user_name
    ON tags(user_id, LOWER(name));

CREATE TABLE IF NOT EXISTS file_tags (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (file_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_file_tags_tag
    ON file_tags(tag_id);
"""


class DatabaseError(Exception):
    """Base error for database operations."""


class NotFoundError(DatabaseError):
    """Raised when a requested folder, file, or tag does not exist."""


class DuplicateError(DatabaseError):
    """Raised when a uniqueness constraint would be violated."""


class DuplicateFileError(DuplicateError):
    """Raised when the same Telegram file_unique_id already exists in a folder."""

    def __init__(self, existing_file: FileRecord) -> None:
        self.existing_file = existing_file
        super().__init__(
            f"File '{existing_file.display_name}' already exists in this folder."
        )


class InvalidMoveError(DatabaseError):
    """Raised when a folder move would create a cycle or invalid parent."""


@dataclass(frozen=True, slots=True)
class FolderRecord:
    id: int
    user_id: int
    name: str
    parent_id: int | None
    created_at: str


@dataclass(frozen=True, slots=True)
class FileRecord:
    id: int
    user_id: int
    folder_id: int | None
    file_id: str
    file_unique_id: str
    display_name: str
    file_type: str
    mime_type: str | None
    file_size: int | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TagRecord:
    id: int
    user_id: int
    name: str
    file_count: int = 0


@dataclass(frozen=True, slots=True)
class FolderCounts:
    subfolders: int
    files: int

    @property
    def is_empty(self) -> bool:
        return self.subfolders == 0 and self.files == 0


def _escape_like(term: str) -> str:
    """Escape SQLite LIKE metacharacters using backslash as the escape char."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_folder(row: sqlite3.Row) -> FolderRecord:
    return FolderRecord(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        parent_id=row["parent_id"],
        created_at=row["created_at"],
    )


def _row_to_file(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"],
        user_id=row["user_id"],
        folder_id=row["folder_id"],
        file_id=row["file_id"],
        file_unique_id=row["file_unique_id"],
        display_name=row["display_name"],
        file_type=row["file_type"],
        mime_type=row["mime_type"],
        file_size=row["file_size"],
        created_at=row["created_at"],
    )


def _row_to_tag(row: sqlite3.Row) -> TagRecord:
    keys = row.keys()
    count = row["file_count"] if "file_count" in keys else 0
    return TagRecord(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        file_count=count,
    )


class Database:
    """Non-blocking async facade over SQLite metadata storage.

    All blocking SQLite calls are serialized via an internal lock and executed
    in a worker thread via ``asyncio.to_thread`` so the Telegram event loop
    remains responsive.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    async def init(self) -> None:
        """Open the SQLite connection and ensure schema/indexes exist."""
        await self._run(self._init_sync)

    def _init_sync(self) -> None:
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        if self._db_path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        """Close the SQLite connection cleanly."""
        await self._run(self._close_sync)

    def _close_sync(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseError("Database is not initialized. Call init() first.")
        return self._conn

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        def _wrapped() -> T:
            with self._lock:
                return fn(*args, **kwargs)

        return await asyncio.to_thread(_wrapped)

    # ------------------------------------------------------------------
    # Folder Operations
    # ------------------------------------------------------------------

    async def get_folder(
        self, user_id: int, folder_id: int
    ) -> FolderRecord | None:
        return await self._run(self._get_folder_sync, user_id, folder_id)

    def _get_folder_sync(
        self, user_id: int, folder_id: int
    ) -> FolderRecord | None:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT id, user_id, name, parent_id, created_at "
            "FROM folders WHERE id = ? AND user_id = ?",
            (folder_id, user_id),
        ).fetchone()
        return _row_to_folder(row) if row else None

    async def get_breadcrumb(
        self, user_id: int, folder_id: int | None
    ) -> list[FolderRecord]:
        if folder_id is None:
            return []
        return await self._run(self._get_breadcrumb_sync, user_id, folder_id)

    def _get_breadcrumb_sync(
        self, user_id: int, folder_id: int
    ) -> list[FolderRecord]:
        conn = self._require_conn()
        rows = conn.execute(
            """
            WITH RECURSIVE ancestors(id, user_id, name, parent_id, created_at, depth) AS (
                SELECT id, user_id, name, parent_id, created_at, 0
                FROM folders
                WHERE id = ? AND user_id = ?
                UNION ALL
                SELECT f.id, f.user_id, f.name, f.parent_id, f.created_at, a.depth + 1
                FROM folders f
                JOIN ancestors a ON f.id = a.parent_id
                WHERE f.user_id = ?
            )
            SELECT id, user_id, name, parent_id, created_at
            FROM ancestors
            ORDER BY depth DESC
            """,
            (folder_id, user_id, user_id),
        ).fetchall()
        return [_row_to_folder(r) for r in rows]

    async def list_subfolders(
        self, user_id: int, parent_id: int | None
    ) -> list[FolderRecord]:
        return await self._run(self._list_subfolders_sync, user_id, parent_id)

    def _list_subfolders_sync(
        self, user_id: int, parent_id: int | None
    ) -> list[FolderRecord]:
        conn = self._require_conn()
        if parent_id is None:
            rows = conn.execute(
                "SELECT id, user_id, name, parent_id, created_at "
                "FROM folders WHERE user_id = ? AND parent_id IS NULL "
                "ORDER BY LOWER(name) ASC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, user_id, name, parent_id, created_at "
                "FROM folders WHERE user_id = ? AND parent_id = ? "
                "ORDER BY LOWER(name) ASC",
                (user_id, parent_id),
            ).fetchall()
        return [_row_to_folder(r) for r in rows]

    async def create_folder(
        self, user_id: int, name: str, parent_id: int | None
    ) -> FolderRecord:
        return await self._run(
            self._create_folder_sync, user_id, name.strip(), parent_id
        )

    def _create_folder_sync(
        self, user_id: int, name: str, parent_id: int | None
    ) -> FolderRecord:
        conn = self._require_conn()
        if parent_id is not None and self._get_folder_sync(user_id, parent_id) is None:
            raise NotFoundError("Parent folder no longer exists.")
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO folders (user_id, name, parent_id) VALUES (?, ?, ?)",
                    (user_id, name, parent_id),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(
                f"A folder named '{name}' already exists here."
            ) from exc

        folder = self._get_folder_sync(user_id, int(cur.lastrowid))
        assert folder is not None
        return folder

    async def rename_folder(
        self, user_id: int, folder_id: int, new_name: str
    ) -> FolderRecord:
        return await self._run(
            self._rename_folder_sync, user_id, folder_id, new_name.strip()
        )

    def _rename_folder_sync(
        self, user_id: int, folder_id: int, new_name: str
    ) -> FolderRecord:
        conn = self._require_conn()
        existing = self._get_folder_sync(user_id, folder_id)
        if existing is None:
            raise NotFoundError("Folder not found.")
        try:
            with conn:
                conn.execute(
                    "UPDATE folders SET name = ? WHERE id = ? AND user_id = ?",
                    (new_name, folder_id, user_id),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(
                f"A sibling folder named '{new_name}' already exists."
            ) from exc

        updated = self._get_folder_sync(user_id, folder_id)
        assert updated is not None
        return updated

    async def get_folder_descendant_ids(
        self, user_id: int, folder_id: int
    ) -> set[int]:
        return await self._run(
            self._get_folder_descendant_ids_sync, user_id, folder_id
        )

    def _get_folder_descendant_ids_sync(
        self, user_id: int, folder_id: int
    ) -> set[int]:
        conn = self._require_conn()
        rows = conn.execute(
            """
            WITH RECURSIVE subtree(id) AS (
                SELECT id FROM folders WHERE id = ? AND user_id = ?
                UNION ALL
                SELECT f.id FROM folders f
                JOIN subtree s ON f.parent_id = s.id
                WHERE f.user_id = ?
            )
            SELECT id FROM subtree
            """,
            (folder_id, user_id, user_id),
        ).fetchall()
        return {int(r["id"]) for r in rows}

    async def move_folder(
        self, user_id: int, folder_id: int, new_parent_id: int | None
    ) -> FolderRecord:
        return await self._run(
            self._move_folder_sync, user_id, folder_id, new_parent_id
        )

    def _move_folder_sync(
        self, user_id: int, folder_id: int, new_parent_id: int | None
    ) -> FolderRecord:
        conn = self._require_conn()
        folder = self._get_folder_sync(user_id, folder_id)
        if folder is None:
            raise NotFoundError("Folder not found.")

        if new_parent_id is not None:
            if self._get_folder_sync(user_id, new_parent_id) is None:
                raise NotFoundError("Destination folder not found.")
            descendants = self._get_folder_descendant_ids_sync(user_id, folder_id)
            if new_parent_id in descendants:
                raise InvalidMoveError(
                    "Cannot move a folder into itself or one of its subfolders."
                )

        if folder.parent_id == new_parent_id:
            return folder

        try:
            with conn:
                conn.execute(
                    "UPDATE folders SET parent_id = ? WHERE id = ? AND user_id = ?",
                    (new_parent_id, folder_id, user_id),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError(
                f"Destination already contains a folder named '{folder.name}'."
            ) from exc

        updated = self._get_folder_sync(user_id, folder_id)
        assert updated is not None
        return updated

    async def get_folder_counts(
        self, user_id: int, folder_id: int
    ) -> FolderCounts:
        return await self._run(self._get_folder_counts_sync, user_id, folder_id)

    def _get_folder_counts_sync(
        self, user_id: int, folder_id: int
    ) -> FolderCounts:
        conn = self._require_conn()
        row = conn.execute(
            """
            WITH RECURSIVE subtree(id) AS (
                SELECT id FROM folders WHERE id = ? AND user_id = ?
                UNION ALL
                SELECT f.id FROM folders f
                JOIN subtree s ON f.parent_id = s.id
                WHERE f.user_id = ?
            )
            SELECT
                (SELECT COUNT(*) FROM subtree WHERE id != ?) AS subfolder_count,
                (SELECT COUNT(*) FROM files WHERE user_id = ? AND folder_id IN (SELECT id FROM subtree)) AS file_count
            """,
            (folder_id, user_id, user_id, folder_id, user_id),
        ).fetchone()
        return FolderCounts(
            subfolders=int(row["subfolder_count"]),
            files=int(row["file_count"]),
        )

    async def delete_folder(self, user_id: int, folder_id: int) -> FolderRecord:
        return await self._run(self._delete_folder_sync, user_id, folder_id)

    def _delete_folder_sync(self, user_id: int, folder_id: int) -> FolderRecord:
        conn = self._require_conn()
        folder = self._get_folder_sync(user_id, folder_id)
        if folder is None:
            raise NotFoundError("Folder not found.")
        with conn:
            conn.execute(
                "DELETE FROM folders WHERE id = ? AND user_id = ?",
                (folder_id, user_id),
            )
        return folder

    # ------------------------------------------------------------------
    # File Operations
    # ------------------------------------------------------------------

    async def add_file(
        self,
        *,
        user_id: int,
        folder_id: int | None,
        file_id: str,
        file_unique_id: str,
        display_name: str,
        file_type: str,
        mime_type: str | None,
        file_size: int | None,
    ) -> FileRecord:
        if file_type not in VALID_FILE_TYPES:
            raise ValueError(f"Unsupported file_type: {file_type}")
        return await self._run(
            self._add_file_sync,
            user_id,
            folder_id,
            file_id,
            file_unique_id,
            display_name.strip(),
            file_type,
            mime_type,
            file_size,
        )

    def _find_duplicate_file_sync(
        self, user_id: int, folder_id: int | None, file_unique_id: str
    ) -> FileRecord | None:
        conn = self._require_conn()
        if folder_id is None:
            row = conn.execute(
                "SELECT * FROM files "
                "WHERE user_id = ? AND folder_id IS NULL AND file_unique_id = ?",
                (user_id, file_unique_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM files "
                "WHERE user_id = ? AND folder_id = ? AND file_unique_id = ?",
                (user_id, folder_id, file_unique_id),
            ).fetchone()
        return _row_to_file(row) if row else None

    def _add_file_sync(
        self,
        user_id: int,
        folder_id: int | None,
        file_id: str,
        file_unique_id: str,
        display_name: str,
        file_type: str,
        mime_type: str | None,
        file_size: int | None,
    ) -> FileRecord:
        conn = self._require_conn()
        if folder_id is not None and self._get_folder_sync(user_id, folder_id) is None:
            folder_id = None

        existing = self._find_duplicate_file_sync(user_id, folder_id, file_unique_id)
        if existing is not None:
            raise DuplicateFileError(existing)

        try:
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO files (
                        user_id, folder_id, file_id, file_unique_id,
                        display_name, file_type, mime_type, file_size
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        folder_id,
                        file_id,
                        file_unique_id,
                        display_name,
                        file_type,
                        mime_type,
                        file_size,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            existing = self._find_duplicate_file_sync(
                user_id, folder_id, file_unique_id
            )
            if existing is not None:
                raise DuplicateFileError(existing) from exc
            raise DatabaseError(str(exc)) from exc

        record = self._get_file_sync(user_id, int(cur.lastrowid))
        assert record is not None
        return record

    async def get_file(self, user_id: int, file_row_id: int) -> FileRecord | None:
        return await self._run(self._get_file_sync, user_id, file_row_id)

    def _get_file_sync(self, user_id: int, file_row_id: int) -> FileRecord | None:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM files WHERE id = ? AND user_id = ?",
            (file_row_id, user_id),
        ).fetchone()
        return _row_to_file(row) if row else None

    async def list_files_in_folder(
        self, user_id: int, folder_id: int | None
    ) -> list[FileRecord]:
        return await self._run(self._list_files_in_folder_sync, user_id, folder_id)

    def _list_files_in_folder_sync(
        self, user_id: int, folder_id: int | None
    ) -> list[FileRecord]:
        conn = self._require_conn()
        if folder_id is None:
            rows = conn.execute(
                "SELECT * FROM files WHERE user_id = ? AND folder_id IS NULL "
                "ORDER BY LOWER(display_name) ASC, id DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM files WHERE user_id = ? AND folder_id = ? "
                "ORDER BY LOWER(display_name) ASC, id DESC",
                (user_id, folder_id),
            ).fetchall()
        return [_row_to_file(r) for r in rows]

    async def rename_file(
        self, user_id: int, file_row_id: int, new_name: str
    ) -> FileRecord:
        return await self._run(
            self._rename_file_sync, user_id, file_row_id, new_name.strip()
        )

    def _rename_file_sync(
        self, user_id: int, file_row_id: int, new_name: str
    ) -> FileRecord:
        conn = self._require_conn()
        if self._get_file_sync(user_id, file_row_id) is None:
            raise NotFoundError("File not found.")
        with conn:
            conn.execute(
                "UPDATE files SET display_name = ? WHERE id = ? AND user_id = ?",
                (new_name, file_row_id, user_id),
            )
        updated = self._get_file_sync(user_id, file_row_id)
        assert updated is not None
        return updated

    async def move_file(
        self, user_id: int, file_row_id: int, new_folder_id: int | None
    ) -> FileRecord:
        return await self._run(
            self._move_file_sync, user_id, file_row_id, new_folder_id
        )

    def _move_file_sync(
        self, user_id: int, file_row_id: int, new_folder_id: int | None
    ) -> FileRecord:
        conn = self._require_conn()
        file_rec = self._get_file_sync(user_id, file_row_id)
        if file_rec is None:
            raise NotFoundError("File not found.")
        if new_folder_id is not None and self._get_folder_sync(user_id, new_folder_id) is None:
            raise NotFoundError("Destination folder not found.")
        if file_rec.folder_id == new_folder_id:
            return file_rec

        existing = self._find_duplicate_file_sync(
            user_id, new_folder_id, file_rec.file_unique_id
        )
        if existing is not None:
            raise DuplicateFileError(existing)

        with conn:
            conn.execute(
                "UPDATE files SET folder_id = ? WHERE id = ? AND user_id = ?",
                (new_folder_id, file_row_id, user_id),
            )
        updated = self._get_file_sync(user_id, file_row_id)
        assert updated is not None
        return updated

    async def delete_file(self, user_id: int, file_row_id: int) -> FileRecord:
        return await self._run(self._delete_file_sync, user_id, file_row_id)

    def _delete_file_sync(self, user_id: int, file_row_id: int) -> FileRecord:
        conn = self._require_conn()
        file_rec = self._get_file_sync(user_id, file_row_id)
        if file_rec is None:
            raise NotFoundError("File not found.")
        with conn:
            conn.execute(
                "DELETE FROM files WHERE id = ? AND user_id = ?",
                (file_row_id, user_id),
            )
        return file_rec

    async def search_files(self, user_id: int, query: str) -> list[FileRecord]:
        return await self._run(self._search_files_sync, user_id, query.strip())

    def _search_files_sync(self, user_id: int, query: str) -> list[FileRecord]:
        conn = self._require_conn()
        pattern = f"%{_escape_like(query)}%"
        rows = conn.execute(
            """
            SELECT DISTINCT f.*
            FROM files f
            LEFT JOIN file_tags ft ON ft.file_id = f.id
            LEFT JOIN tags t ON t.id = ft.tag_id AND t.user_id = f.user_id
            WHERE f.user_id = ?
              AND (
                  f.display_name LIKE ? ESCAPE '\\'
                  OR t.name LIKE ? ESCAPE '\\'
              )
            ORDER BY LOWER(f.display_name) ASC, f.id DESC
            """,
            (user_id, pattern, pattern),
        ).fetchall()
        return [_row_to_file(r) for r in rows]

    async def list_files_by_type(
        self, user_id: int, file_type: str
    ) -> list[FileRecord]:
        return await self._run(self._list_files_by_type_sync, user_id, file_type)

    def _list_files_by_type_sync(
        self, user_id: int, file_type: str
    ) -> list[FileRecord]:
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT * FROM files WHERE user_id = ? AND file_type = ? "
            "ORDER BY LOWER(display_name) ASC, id DESC",
            (user_id, file_type),
        ).fetchall()
        return [_row_to_file(r) for r in rows]

    async def count_files_by_type(self, user_id: int) -> dict[str, int]:
        return await self._run(self._count_files_by_type_sync, user_id)

    def _count_files_by_type_sync(self, user_id: int) -> dict[str, int]:
        conn = self._require_conn()
        rows = conn.execute(
            "SELECT file_type, COUNT(*) AS cnt "
            "FROM files WHERE user_id = ? GROUP BY file_type",
            (user_id,),
        ).fetchall()
        counts = {ft: 0 for ft in VALID_FILE_TYPES}
        for row in rows:
            counts[str(row["file_type"])] = int(row["cnt"])
        return counts

    # ------------------------------------------------------------------
    # Tag Operations
    # ------------------------------------------------------------------

    async def list_tags(
        self, user_id: int, *, only_used: bool = False
    ) -> list[TagRecord]:
        return await self._run(self._list_tags_sync, user_id, only_used)

    def _list_tags_sync(self, user_id: int, only_used: bool) -> list[TagRecord]:
        conn = self._require_conn()
        having = "HAVING COUNT(ft.file_id) > 0" if only_used else ""
        rows = conn.execute(
            f"""
            SELECT t.id, t.user_id, t.name, COUNT(ft.file_id) AS file_count
            FROM tags t
            LEFT JOIN file_tags ft ON ft.tag_id = t.id
            WHERE t.user_id = ?
            GROUP BY t.id
            {having}
            ORDER BY LOWER(t.name) ASC
            """,
            (user_id,),
        ).fetchall()
        return [_row_to_tag(r) for r in rows]

    async def get_tag(self, user_id: int, tag_id: int) -> TagRecord | None:
        return await self._run(self._get_tag_sync, user_id, tag_id)

    def _get_tag_sync(self, user_id: int, tag_id: int) -> TagRecord | None:
        conn = self._require_conn()
        row = conn.execute(
            """
            SELECT t.id, t.user_id, t.name, COUNT(ft.file_id) AS file_count
            FROM tags t
            LEFT JOIN file_tags ft ON ft.tag_id = t.id
            WHERE t.id = ? AND t.user_id = ?
            GROUP BY t.id
            """,
            (tag_id, user_id),
        ).fetchone()
        return _row_to_tag(row) if row else None

    async def get_file_tags(
        self, user_id: int, file_row_id: int
    ) -> list[TagRecord]:
        return await self._run(self._get_file_tags_sync, user_id, file_row_id)

    def _get_file_tags_sync(
        self, user_id: int, file_row_id: int
    ) -> list[TagRecord]:
        conn = self._require_conn()
        rows = conn.execute(
            """
            SELECT t.id, t.user_id, t.name
            FROM tags t
            JOIN file_tags ft ON ft.tag_id = t.id
            JOIN files f ON f.id = ft.file_id
            WHERE ft.file_id = ? AND f.user_id = ? AND t.user_id = ?
            ORDER BY LOWER(t.name) ASC
            """,
            (file_row_id, user_id, user_id),
        ).fetchall()
        return [_row_to_tag(r) for r in rows]

    async def list_files_by_tag(
        self, user_id: int, tag_id: int
    ) -> list[FileRecord]:
        return await self._run(self._list_files_by_tag_sync, user_id, tag_id)

    def _list_files_by_tag_sync(
        self, user_id: int, tag_id: int
    ) -> list[FileRecord]:
        conn = self._require_conn()
        rows = conn.execute(
            """
            SELECT f.*
            FROM files f
            JOIN file_tags ft ON ft.file_id = f.id
            JOIN tags t ON t.id = ft.tag_id
            WHERE f.user_id = ? AND t.user_id = ? AND t.id = ?
            ORDER BY LOWER(f.display_name) ASC, f.id DESC
            """,
            (user_id, user_id, tag_id),
        ).fetchall()
        return [_row_to_file(r) for r in rows]

    async def toggle_file_tag(
        self, user_id: int, file_row_id: int, tag_id: int
    ) -> bool:
        """Attach tag if not present, detach if present. Returns True if attached."""
        return await self._run(
            self._toggle_file_tag_sync, user_id, file_row_id, tag_id
        )

    def _toggle_file_tag_sync(
        self, user_id: int, file_row_id: int, tag_id: int
    ) -> bool:
        conn = self._require_conn()
        if self._get_file_sync(user_id, file_row_id) is None:
            raise NotFoundError("File not found.")
        if self._get_tag_sync(user_id, tag_id) is None:
            raise NotFoundError("Tag not found.")

        exists = conn.execute(
            "SELECT 1 FROM file_tags WHERE file_id = ? AND tag_id = ?",
            (file_row_id, tag_id),
        ).fetchone()
        with conn:
            if exists:
                conn.execute(
                    "DELETE FROM file_tags WHERE file_id = ? AND tag_id = ?",
                    (file_row_id, tag_id),
                )
                return False
            conn.execute(
                "INSERT INTO file_tags (file_id, tag_id) VALUES (?, ?)",
                (file_row_id, tag_id),
            )
            return True

    async def apply_tag_changes(
        self,
        user_id: int,
        file_row_id: int,
        add_names: list[str],
        remove_names: list[str],
    ) -> tuple[list[str], list[str]]:
        """Add and/or remove tags by name for a file. Returns (added, removed)."""
        return await self._run(
            self._apply_tag_changes_sync,
            user_id,
            file_row_id,
            add_names,
            remove_names,
        )

    def _apply_tag_changes_sync(
        self,
        user_id: int,
        file_row_id: int,
        add_names: list[str],
        remove_names: list[str],
    ) -> tuple[list[str], list[str]]:
        conn = self._require_conn()
        if self._get_file_sync(user_id, file_row_id) is None:
            raise NotFoundError("File not found.")

        added: list[str] = []
        removed: list[str] = []

        with conn:
            for raw_name in add_names:
                clean = raw_name.strip().lstrip("#")
                if not clean:
                    continue
                row = conn.execute(
                    "SELECT id, name FROM tags WHERE user_id = ? AND LOWER(name) = LOWER(?)",
                    (user_id, clean),
                ).fetchone()
                if row is None:
                    cur = conn.execute(
                        "INSERT INTO tags (user_id, name) VALUES (?, ?)",
                        (user_id, clean),
                    )
                    tag_id = int(cur.lastrowid)
                    canonical_name = clean
                else:
                    tag_id = int(row["id"])
                    canonical_name = str(row["name"])

                cur = conn.execute(
                    "INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?, ?)",
                    (file_row_id, tag_id),
                )
                if cur.rowcount > 0:
                    added.append(canonical_name)

            for raw_name in remove_names:
                clean = raw_name.strip().lstrip("#")
                if not clean:
                    continue
                row = conn.execute(
                    "SELECT id, name FROM tags WHERE user_id = ? AND LOWER(name) = LOWER(?)",
                    (user_id, clean),
                ).fetchone()
                if row is None:
                    continue
                tag_id = int(row["id"])
                canonical_name = str(row["name"])
                cur = conn.execute(
                    "DELETE FROM file_tags WHERE file_id = ? AND tag_id = ?",
                    (file_row_id, tag_id),
                )
                if cur.rowcount > 0:
                    removed.append(canonical_name)

        return added, removed
