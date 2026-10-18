"""Sync engine: mirror a local Drive-for-Desktop folder tree into NoCFO.

Design decisions (see README for rationale):
- The local mount is the source of truth; file identity = relative path.
- Change detection = (size, mtime_ns) fingerprint stored in sqlite.
- Drive deletions are NEVER propagated; changed files are re-uploaded and
  the old NoCFO copy is deleted only if it is not attached to a document.
- Native Google Docs stubs (.gsheet, .gdoc, ...) are skipped: the mount
  only stores a pointer, not file content.
- The state DB remembers which NoCFO destination its ids live under; when
  the mapping's destination changes, the tracking is reset so the tree is
  mirrored afresh into the new destination (nothing is deleted).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .nocfo import NoCfoClient, NoCfoError, guess_mime
from .state import State

# Stubs for Google-native documents: not real file content.
GOOGLE_NATIVE_SUFFIXES = {
    ".gsheet", ".gdoc", ".gslides", ".gdraw", ".gbriefing",
    ".gmap", ".gscript", ".gsite", ".link",
}

SKIP_NAMES = {".DS_Store", ".localized", ".Trash", ".tmp", ".Encrypted",
              ".shortcut-targets-by-id", "Other computers"}

# state meta key: the NoCFO destination the cached ids belong to
DESTINATION_KEY = "destination"


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


def _destination_key(folder_id: int | None) -> str:
    return "root" if folder_id is None else str(folder_id)


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
        # True when the state belongs to another destination but this is a
        # dry run, so it can't be reset: plan as if it were empty.
        self._ignore_state = False
        # parent NoCFO folder id -> its children, fetched once per run
        self._children_cache: dict[int | None, list[dict]] = {}
        # local dirs the walk could not list (their files aren't "gone")
        self._unlisted: list[str] = []

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
                self._unlisted.append(self._relpath(Path(dirpath)))
                self._error(f"cannot list {dirpath}: {e}", file=False)
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

    # -- state access (honours _ignore_state) ----------------------------

    def _cached_folder(self, rel: str) -> int | None:
        return None if self._ignore_state else self.state.get_folder(rel)

    def _known_file(self, rel: str) -> tuple[int, int, int, int] | None:
        return None if self._ignore_state else self.state.get_file(rel)

    def _check_destination(self) -> None:
        """Reset the tracking when it was built for another destination.

        Otherwise cached folder ids would keep sending files into the old
        destination's tree, and unchanged files would never reach the new
        one."""
        current = _destination_key(self.destination_folder_id)
        stored = self.state.get_meta(DESTINATION_KEY)
        if stored is not None and stored != current:
            msg = "destination changed — mirroring everything into the new one"
            if self.dry_run:
                self._ignore_state = True
                self.progress("scan", f"would reset: {msg}")
            else:
                self.state.reset_tracking()
                self.progress("scan", msg)
        if not self.dry_run:
            self.state.set_meta(DESTINATION_KEY, current)

    # -- folder mirroring ------------------------------------------------

    def _relpath(self, p: Path) -> str:
        return p.relative_to(self.root_path).as_posix()

    def ensure_folder(self, local_dir: Path, parent_nocfo: int | None) -> int | None:
        """Return the NoCFO folder id mirroring local_dir, creating it if
        needed.  parent_nocfo None = NoCFO root.  Returns None for a folder
        a dry run would create."""
        if local_dir == self.root_path:
            return self.destination_folder_id

        rel = self._relpath(local_dir)
        name = local_dir.name
        children = self._nocfo_children(parent_nocfo)

        # 1. cached mapping — still valid if it is still under the parent
        cached = self._cached_folder(rel)
        if cached is not None:
            if any(c["id"] == cached for c in children):
                self.stats.folders_reused += 1
                return cached
            # deleted or moved away in NoCFO; drop it and re-resolve
            if not self.dry_run:
                self.state.drop_folder(rel)

        # 2. adopt an existing NoCFO folder with the same name under parent
        for child in children:
            if child["name"] == name:
                if not self.dry_run:
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
        children.append({"id": created.id, "name": created.name})
        self.stats.folders_created += 1
        return created.id

    def _nocfo_children(self, folder_id: int | None) -> list[dict]:
        if folder_id not in self._children_cache:
            if folder_id is None:
                children = [
                    {"id": f.id, "name": f.name}
                    for f in self.client.list_root_folders()
                ]
            else:
                children = list(self.client.get_folder(folder_id).children or [])
            self._children_cache[folder_id] = children
        return self._children_cache[folder_id]

    # -- file sync -------------------------------------------------------

    def sync(self) -> SyncStats:
        self.progress("scan", f"scanning {self.root_path}")
        if not self.root_path.is_dir():
            self._error(f"root is not a directory: {self.root_path}", file=False)
            return self.stats

        self._check_destination()

        used_ids: set[int] | None = None  # lazily fetched for deletions
        seen_files: set[str] = set()
        # local dir -> its NoCFO folder id for this run
        resolved: dict[Path, int | None] = {self.root_path: self.destination_folder_id}
        planned: set[Path] = set()  # dry run: folders that would be created
        failed: set[Path] = set()  # folders that could not be mirrored

        for kind, path in self._iter_local():
            if kind == "folder":
                parent = path.parent
                if parent in failed:
                    failed.add(path)
                    continue
                if parent in planned:
                    # dry run: the parent doesn't exist yet, so neither can
                    # anything inside it
                    self.progress("folders", f"would create folder {self._relpath(path)!r}")
                    self.stats.folders_created += 1
                    planned.add(path)
                    continue
                try:
                    folder_id = self.ensure_folder(path, resolved[parent])
                except NoCfoError as e:
                    self._error(
                        f"cannot mirror folder {self._relpath(path)}: {e} "
                        "(skipping its contents)",
                        file=False,
                    )
                    failed.add(path)
                    continue
                if folder_id is None:
                    planned.add(path)
                else:
                    resolved[path] = folder_id
                continue

            rel = self._relpath(path)
            seen_files.add(rel)
            if path.parent in failed:
                continue  # reported once, for the folder
            if path.suffix.lower() in GOOGLE_NATIVE_SUFFIXES:
                self.stats.files_skipped_native += 1
                continue

            try:
                st = path.stat()
            except OSError as e:
                self._error(f"cannot stat {rel}: {e}")
                continue

            known = self._known_file(rel)
            if (
                not self.force
                and known
                and known[2] == st.st_size
                and known[3] == st.st_mtime_ns
            ):
                self.stats.files_skipped_unchanged += 1
                continue

            if self.dry_run:
                # plan only: don't read content (Drive placeholders would be
                # downloaded) and don't touch NoCFO or the state DB
                if known:
                    self.stats.files_updated += 1
                else:
                    self.stats.files_uploaded += 1
                continue

            folder_id = resolved[path.parent]
            self.progress("files", f"uploading {rel}")
            try:
                with path.open("rb") as fh:  # streamed, not read into memory
                    new = self.client.upload_file(
                        path.name, fh, folder_id=folder_id,
                        mime=guess_mime(path.name),
                    )
            except OSError as e:
                self._error(f"cannot read {rel}: {e}")
                continue
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
        tracked = set() if self._ignore_state else self.state.all_file_relpaths()
        for rel in sorted(tracked - seen_files):
            if self._under_unlisted(rel):
                continue  # not gone — its folder just couldn't be read
            self.stats.files_left_behind += 1
            self.progress("files", f"gone from Drive, left in NoCFO: {rel}")

        return self.stats

    def _under_unlisted(self, rel: str) -> bool:
        return any(u == "." or rel.startswith(u + "/") for u in self._unlisted)

    def _error(self, msg: str, file: bool = True) -> None:
        """Record an error; ``file`` counts it as a failed file."""
        if file:
            self.stats.files_failed += 1
        self.stats.errors.append(msg)
        self.progress("files", f"ERROR: {msg}")
