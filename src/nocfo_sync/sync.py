"""Sync engine: mirror a local Drive-for-Desktop folder tree into NoCFO.

Design decisions (see README for rationale):
- The local mount is the source of truth; file identity = relative path.
- Change detection = (size, mtime_ns) fingerprint stored in sqlite.
- Drive deletions are NEVER propagated; changed files are re-uploaded and
  the old NoCFO copy is deleted only if it is not attached to a document.
- Native Google Docs stubs (.gsheet, .gdoc, ...) are skipped: the mount
  only stores a pointer, not file content.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .nocfo import NoCfoClient, NoCfoError, NocfoFolder, guess_mime
from .state import State

# Stubs for Google-native documents: not real file content.
GOOGLE_NATIVE_SUFFIXES = {
    ".gsheet", ".gdoc", ".gslides", ".gdraw", ".gbriefing",
    ".gmap", ".gscript", ".gsite", ".link",
}

SKIP_NAMES = {".DS_Store", ".localized", ".Trash", ".tmp", ".Encrypted",
              ".shortcut-targets-by-id", "Other computers"}


@dataclass
class SyncStats:
    folders_created: int = 0
    folders_reused: int = 0
    files_uploaded: int = 0
    files_updated: int = 0
    files_skipped_unchanged: int = 0
    files_skipped_native: int = 0
    files_failed: int = 0
    files_left_behind: int = 0  # in state but gone from Drive (never deleted)
    errors: list[str] = field(default_factory=list)


ProgressFn = Callable[[str, str], None]
# args: phase ("scan"/"folders"/"files"), human message


class SyncEngine:
    def __init__(
        self,
        client: NoCfoClient,
        state: State,
        root_path: Path,
        destination_folder_id: int | None,
        dry_run: bool = False,
        force: bool = False,
        progress: ProgressFn | None = None,
    ):
        self.client = client
        self.state = state
        self.root_path = root_path.resolve()
        self.destination_folder_id = destination_folder_id
        self.dry_run = dry_run
        # force re-uploads every file, ignoring the (size, mtime) fingerprint.
        # Used to repair files uploaded before the ``type`` field was sent.
        self.force = force
        self.progress = progress or (lambda phase, msg: None)
        self.stats = SyncStats()

    # -- scanning --------------------------------------------------------

    def _iter_local(self):
        """Walk the local tree.  Yields ("folder"|"file", Path).  Folders
        come before their contents (pre-order)."""
        stack = [self.root_path]
        while stack:
            dirpath = stack.pop(0)
            try:
                entries = sorted(os.scandir(dirpath), key=lambda e: e.name)
            except OSError as e:
                self._error(f"cannot list {dirpath}: {e}")
                continue
            dirs, files = [], []
            for entry in entries:
                if entry.name in SKIP_NAMES or entry.name.startswith("."):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(entry)
                except OSError as e:
                    self._error(f"cannot stat {entry.path}: {e}")
            # push dirs to the FRONT of the queue so the walk stays
            # breadth-first-ish and folders are processed before files
            # of shallower levels; ordering within a dir is preserved.
            stack = [Path(d.path) for d in dirs] + stack
            for d in dirs:
                yield "folder", Path(d.path)
            for f in files:
                yield "file", Path(f.path)

    # -- folder mirroring ------------------------------------------------

    def _relpath(self, p: Path) -> str:
        return p.relative_to(self.root_path).as_posix()

    def ensure_folder(self, local_dir: Path, parent_nocfo: int | None) -> int | None:
        """Return the NoCFO folder id mirroring local_dir, creating it if
        needed.  parent_nocfo None = NoCFO root."""
        if local_dir == self.root_path:
            return self.destination_folder_id

        rel = self._relpath(local_dir)
        name = local_dir.name

        # 1. cached mapping — validate it still exists
        cached = self.state.get_folder(rel)
        if cached is not None:
            try:
                self.client.get_folder(cached)
                self.stats.folders_reused += 1
                return cached
            except NoCfoError as e:
                if e.status_code != 404:
                    raise
                # cached id no longer exists in NoCFO; drop it and re-resolve
                self.state.drop_folder(rel)

        # 2. adopt an existing NoCFO folder with the same name under parent
        for child in self._nocfo_children(parent_nocfo):
            if child["name"] == name:
                self.state.set_folder(rel, child["id"])
                self.stats.folders_reused += 1
                return child["id"]

        # 3. create
        if self.dry_run:
            self.progress("folders", f"would create folder {rel!r}")
            self.stats.folders_created += 1
            return None
        self.progress("folders", f"creating folder {rel!r}")
        created = self.client.create_folder(name, parent_nocfo)
        self.state.set_folder(rel, created.id)
        self.stats.folders_created += 1
        return created.id

    def _nocfo_children(self, folder_id: int | None) -> list[dict]:
        if folder_id is None:
            return [{"id": f.id, "name": f.name} for f in self.client.list_root_folders()]
        detail = self.client.get_folder(folder_id)
        return detail.children or []

    # -- file sync -------------------------------------------------------

    def sync(self) -> SyncStats:
        self.progress("scan", f"scanning {self.root_path}")
        if not self.root_path.is_dir():
            self._error(f"root is not a directory: {self.root_path}")
            return self.stats

        used_ids: set[int] | None = None  # lazily fetched for deletions
        seen_files: set[str] = set()

        for kind, path in self._iter_local():
            if kind == "folder":
                parent = path.parent
                parent_nocfo = (
                    self.destination_folder_id
                    if parent == self.root_path
                    else self.state.get_folder(self._relpath(parent))
                )
                self.ensure_folder(path, parent_nocfo)
                continue

            rel = self._relpath(path)
            seen_files.add(rel)
            if path.suffix.lower() in GOOGLE_NATIVE_SUFFIXES:
                self.stats.files_skipped_native += 1
                continue

            try:
                st = path.stat()
            except OSError as e:
                self._error(f"cannot stat {rel}: {e}")
                continue

            known = self.state.get_file(rel)
            if (
                not self.force
                and known
                and known[2] == st.st_size
                and known[3] == st.st_mtime_ns
            ):
                self.stats.files_skipped_unchanged += 1
                continue

            folder_rel = path.parent.relative_to(self.root_path).as_posix()
            folder_id = (
                self.destination_folder_id
                if folder_rel == "."
                else self.state.get_folder(folder_rel)
            )

            if self.dry_run:
                # plan only: don't read content (Drive placeholders would be
                # downloaded) and don't touch NoCFO or the state DB
                if known:
                    self.stats.files_updated += 1
                else:
                    self.stats.files_uploaded += 1
                continue

            self.progress("files", f"uploading {rel}")
            try:
                content = path.read_bytes()
            except OSError as e:
                self._error(f"cannot read {rel}: {e}")
                continue
            try:
                new = self.client.upload_file(
                    path.name, content, folder_id=folder_id,
                    mime=guess_mime(path.name),
                )
            except NoCfoError as e:
                self._error(f"upload failed for {rel}: {e}")
                continue

            if known:  # changed file: replace
                old_id = known[0]
                self.stats.files_updated += 1
                if used_ids is None:
                    used_ids = {f["id"] for f in self.client.list_files(is_used=True)}
                if old_id in used_ids:
                    self.stats.files_left_behind += 1
                    self.progress(
                        "files",
                        f"kept old copy of {rel} (attached to a document)",
                    )
                else:
                    try:
                        self.client.delete_file(old_id)
                    except NoCfoError as e:
                        self._error(f"could not delete old copy of {rel}: {e}")
            else:
                self.stats.files_uploaded += 1

            self.state.set_file(rel, new["id"], folder_id, st.st_size, st.st_mtime_ns)

        # state entries whose source vanished: report, never delete
        for rel in sorted(self.state.all_file_relpaths() - seen_files):
            self.stats.files_left_behind += 1
            self.progress("files", f"gone from Drive, left in NoCFO: {rel}")

        return self.stats

    def _error(self, msg: str) -> None:
        self.stats.files_failed += 1
        self.stats.errors.append(msg)
        self.progress("files", f"ERROR: {msg}")
