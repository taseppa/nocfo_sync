"""SQLite state: maps local Drive paths to NoCFO ids.

Everything is keyed by the path relative to the selected Drive root, since
the local mount is our source of truth (no Drive API ids available).

Tables
------
folders: drive_relpath -> nocfo folder id
files:   drive_relpath -> nocfo file id + fingerprint (size, mtime_ns)
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    drive_relpath TEXT PRIMARY KEY,
    nocfo_id      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    drive_relpath  TEXT PRIMARY KEY,
    nocfo_id       INTEGER NOT NULL,
    nocfo_folder_id INTEGER,
    size           INTEGER NOT NULL,
    mtime_ns       INTEGER NOT NULL,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + a lock: the TUI runs SyncEngine in a
        # worker thread while State was created on the main thread.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self.db.executescript(SCHEMA)

    # -- folders ---------------------------------------------------------

    def get_folder(self, relpath: str) -> int | None:
        with self._lock:
            row = self.db.execute(
                "SELECT nocfo_id FROM folders WHERE drive_relpath = ?", (relpath,)
            ).fetchone()
        return row[0] if row else None

    def set_folder(self, relpath: str, nocfo_id: int) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO folders (drive_relpath, nocfo_id) VALUES (?, ?) "
                "ON CONFLICT(drive_relpath) DO UPDATE SET nocfo_id = excluded.nocfo_id",
                (relpath, nocfo_id),
            )
            self.db.commit()

    # -- files -----------------------------------------------------------

    def get_file(self, relpath: str) -> tuple[int, int, int, int] | None:
        """-> (nocfo_id, nocfo_folder_id|0, size, mtime_ns)"""
        with self._lock:
            row = self.db.execute(
                "SELECT nocfo_id, nocfo_folder_id, size, mtime_ns FROM files "
                "WHERE drive_relpath = ?",
                (relpath,),
            ).fetchone()
        if row is None:
            return None
        return row[0], row[1] if row[1] is not None else 0, row[2], row[3]

    def set_file(
        self, relpath: str, nocfo_id: int, nocfo_folder_id: int | None,
        size: int, mtime_ns: int,
    ) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO files (drive_relpath, nocfo_id, nocfo_folder_id, size, mtime_ns) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(drive_relpath) DO UPDATE SET "
                "nocfo_id = excluded.nocfo_id, nocfo_folder_id = excluded.nocfo_folder_id, "
                "size = excluded.size, mtime_ns = excluded.mtime_ns, "
                "updated_at = datetime('now')",
                (relpath, nocfo_id, nocfo_folder_id, size, mtime_ns),
            )
            self.db.commit()

    def all_file_relpaths(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self.db.execute("SELECT drive_relpath FROM files")}

    def remove_file(self, relpath: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM files WHERE drive_relpath = ?", (relpath,))
            self.db.commit()

    def drop_folder(self, relpath: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM folders WHERE drive_relpath = ?", (relpath,))
            self.db.commit()

    def close(self) -> None:
        self.db.close()
