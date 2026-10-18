"""Unit tests for the sync engine using a fake NoCFO client (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nocfo_sync.nocfo import NoCfoError, NocfoFolder
from nocfo_sync.state import State
from nocfo_sync.sync import SyncEngine


class FakeClient:
    """In-memory stand-in for NoCfoClient."""

    def __init__(self):
        self.folders: dict[int, dict] = {}  # id -> {name, parent, children}
        self.files: dict[int, dict] = {}  # id -> {name, folder_id, content}
        self._next_id = 100
        self._next_file = 1000
        self.deleted: list[int] = []
        self.used_file_ids: set[int] = set()
        self.fail_uploads = False

    # folders
    def list_root_folders(self):
        return [
            NocfoFolder(id=f["id"], name=f["name"], parent=f["parent"])
            for f in self.folders.values()
            if f["parent"] is None
        ]

    def get_folder(self, folder_id):
        f = self.folders.get(folder_id)
        if f is None:
            raise NoCfoError("not found", 404)
        children = [
            {"id": c["id"], "name": c["name"]}
            for c in self.folders.values()
            if c["parent"] == folder_id
        ]
        return NocfoFolder(id=f["id"], name=f["name"], parent=f["parent"],
                           children=children)

    def create_folder(self, name, parent):
        self._next_id += 1
        self.folders[self._next_id] = {"id": self._next_id, "name": name, "parent": parent}
        return NocfoFolder(id=self._next_id, name=name, parent=parent)

    # files
    def upload_file(self, name, content, folder_id=None, mime=None, notes=None):
        if hasattr(content, "read"):  # the engine streams an open file
            content = content.read()
        if self.fail_uploads:
            raise NoCfoError("boom", 500)
        self._next_file += 1
        self.files[self._next_file] = {
            "id": self._next_file, "name": name,
            "folder_id": folder_id, "content": content, "mime": mime,
        }
        return self.files[self._next_file]

    def list_files(self, folder=None, root=None, is_used=None, search=None):
        out = []
        for f in self.files.values():
            if is_used and f["id"] not in self.used_file_ids:
                continue
            out.append(f)
        return iter(out)

    def delete_file(self, file_id):
        self.deleted.append(file_id)
        self.files.pop(file_id, None)

    def folders_by_name(self, name):
        return next(f["id"] for f in self.folders.values() if f["name"] == name)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    drive = tmp_path / "drive"
    (drive / "Invoices").mkdir(parents=True)
    (drive / "Receipts" / "2026").mkdir(parents=True)
    (drive / "Invoices" / "a.txt").write_text("a")
    (drive / "Invoices" / "b.txt").write_text("b")
    (drive / "Receipts" / "2026" / "r.txt").write_text("r")
    (drive / "Receipts" / "sheet.gsheet").write_text("stub")
    (drive / ".DS_Store").write_text("junk")
    return drive


def make_engine(tmp_path, client, state_path, **kw):
    state = State(state_path)
    return SyncEngine(client, state, tmp_path, None, progress=lambda p, m: None, **kw)


def test_first_run_creates_structure(tree, tmp_path):
    client = FakeClient()
    eng = make_engine(tree, client, tmp_path / "s.db")
    stats = eng.sync()
    assert stats.files_uploaded == 3
    assert stats.folders_created == 3  # Invoices, Receipts, Receipts/2026
    assert stats.files_skipped_native == 1  # .gsheet
    # folders mirrored with correct parents
    by_name = {f["name"]: f for f in client.folders.values()}
    assert by_name["2026"]["parent"] == by_name["Receipts"]["id"]
    # every uploaded file landed in its folder
    placements = {f["name"]: f["folder_id"] for f in client.files.values()}
    assert placements["a.txt"] == by_name["Invoices"]["id"]
    assert placements["r.txt"] == by_name["2026"]["id"]


def test_second_run_is_noop(tree, tmp_path):
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    stats = make_engine(tree, client, tmp_path / "s.db").sync()
    assert stats.files_uploaded == 0
    assert stats.files_updated == 0
    assert stats.files_skipped_unchanged == 3
    assert stats.folders_created == 0
    assert stats.folders_reused == 3


def test_changed_file_is_replaced(tree, tmp_path):
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    old_id = next(f["id"] for f in client.files.values() if f["name"] == "a.txt")
    (tree / "Invoices" / "a.txt").write_text("a changed")
    stats = make_engine(tree, client, tmp_path / "s.db").sync()
    assert stats.files_updated == 1
    assert old_id in client.deleted


def test_changed_file_in_use_is_kept(tree, tmp_path):
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    old_id = next(f["id"] for f in client.files.values() if f["name"] == "a.txt")
    client.used_file_ids.add(old_id)
    (tree / "Invoices" / "a.txt").write_text("a changed again")
    stats = make_engine(tree, client, tmp_path / "s.db").sync()
    assert stats.files_updated == 1
    assert stats.files_left_behind == 1
    assert old_id not in client.deleted


def test_local_delete_never_deletes_remote(tree, tmp_path):
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    (tree / "Invoices" / "b.txt").unlink()
    stats = make_engine(tree, client, tmp_path / "s.db").sync()
    assert stats.files_left_behind == 1
    assert client.deleted == []
    assert any(f["name"] == "b.txt" for f in client.files.values())


def test_adopts_existing_remote_folder(tree, tmp_path):
    client = FakeClient()
    existing = client.create_folder("Invoices", None)
    eng = make_engine(tree, client, tmp_path / "s.db")
    stats = eng.sync()
    assert stats.folders_reused >= 1
    assert stats.folders_created == 2  # Receipts + Receipts/2026
    placements = {f["name"]: f["folder_id"] for f in client.files.values()}
    assert placements["a.txt"] == existing.id


def test_stale_folder_cache_is_recreated(tree, tmp_path):
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    # simulate someone deleting the folders in NoCFO
    client.folders.clear()
    stats = make_engine(tree, client, tmp_path / "s.db").sync()
    assert stats.files_failed == 0
    assert stats.folders_created == 3


def test_upload_failure_is_recorded_not_fatal(tree, tmp_path):
    client = FakeClient()
    client.fail_uploads = True
    stats = make_engine(tree, client, tmp_path / "s.db").sync()
    assert stats.files_failed == 3
    assert stats.files_uploaded == 0
    assert len(stats.errors) == 3


def test_dry_run_writes_nothing(tree, tmp_path):
    client = FakeClient()
    eng = make_engine(tree, client, tmp_path / "s.db", dry_run=True)
    stats = eng.sync()
    assert stats.files_uploaded == 3
    assert client.folders == {}
    assert client.files == {}


def test_dry_run_does_not_read_file_content(tree, tmp_path, monkeypatch):
    """--dry-run must not pull Drive placeholder files down just to plan."""
    def boom(self):
        raise AssertionError("dry run must not read file bytes")

    monkeypatch.setattr(Path, "read_bytes", boom)
    stats = make_engine(tree, FakeClient(), tmp_path / "s.db", dry_run=True).sync()
    assert stats.files_uploaded == 3


def test_dry_run_with_force_plans_reuploads(tree, tmp_path):
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    before = {f["id"]: f["content"] for f in client.files.values()}
    stats = make_engine(
        tree, client, tmp_path / "s.db", dry_run=True, force=True
    ).sync()
    # planned as replacements, not fresh uploads, and nothing touched
    assert stats.files_updated == 3
    assert stats.files_uploaded == 0
    assert {f["id"]: f["content"] for f in client.files.values()} == before
    assert client.deleted == []


def test_state_persists_folder_mapping(tmp_path):
    state = State(tmp_path / "s.db")
    state.set_folder("a/b", 42)
    assert state.get_folder("a/b") == 42
    state.set_file("a/b/c.txt", 7, 42, 10, 20)
    assert state.get_file("a/b/c.txt") == (7, 42, 10, 20)
    state.remove_file("a/b/c.txt")
    assert state.get_file("a/b/c.txt") is None
    state.close()


def test_uploads_carry_mime_type(tree, tmp_path):
    """NoCFO only classifies a file when the ``type`` form field is set;
    a missing type is why uploaded files were not treated as files."""
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    mimes = {f["name"]: f["mime"] for f in client.files.values()}
    assert mimes["a.txt"] == "text/plain"
    assert mimes["r.txt"] == "text/plain"
    assert all(v is not None for v in mimes.values())


def test_force_reuploads_unchanged_files(tree, tmp_path):
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    first_ids = {f["name"]: f["id"] for f in client.files.values()}

    stats = make_engine(tree, client, tmp_path / "s.db", force=True).sync()

    # nothing changed on disk, yet everything is re-uploaded and replaced
    assert stats.files_updated == 3
    assert stats.files_skipped_unchanged == 0
    for old_id in first_ids.values():
        assert old_id in client.deleted
    # state now points at the fresh copies
    second_ids = {f["name"]: f["id"] for f in client.files.values()}
    assert all(second_ids[n] != old for n, old in first_ids.items())


def test_force_off_by_default_still_skips(tree, tmp_path):
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    stats = make_engine(tree, client, tmp_path / "s.db").sync()
    assert stats.files_uploaded == 0
    assert stats.files_updated == 0
    assert stats.files_skipped_unchanged == 3


# -- destination changes ---------------------------------------------------------


def _engine_to(tree, client, state_path, dest, **kw):
    return SyncEngine(client, State(state_path), tree, dest,
                      progress=lambda p, m: None, **kw)


def test_changed_destination_mirrors_into_the_new_one(tree, tmp_path):
    client = FakeClient()
    old = client.create_folder("Old", None).id
    new = client.create_folder("New", None).id
    _engine_to(tree, client, tmp_path / "s.db", old).sync()

    stats = _engine_to(tree, client, tmp_path / "s.db", new).sync()
    assert stats.files_uploaded == 3  # nothing counted as "unchanged"

    def under(folder_id, target):
        while folder_id is not None:
            if folder_id == target:
                return True
            folder_id = client.folders[folder_id]["parent"]
        return False

    latest = sorted(client.files.values(), key=lambda f: f["id"])[-3:]
    assert all(under(f["folder_id"], new) for f in latest)
    assert client.deleted == []  # the old copies are left alone


def test_dry_run_with_changed_destination_plans_full_sync(tree, tmp_path):
    client = FakeClient()
    old = client.create_folder("Old", None).id
    new = client.create_folder("New", None).id
    _engine_to(tree, client, tmp_path / "s.db", old).sync()
    stats = _engine_to(tree, client, tmp_path / "s.db", new, dry_run=True).sync()
    assert stats.files_uploaded == 3
    assert stats.files_skipped_unchanged == 0
    # the real tracking is untouched by the dry run
    assert _engine_to(tree, client, tmp_path / "s.db", old).sync().files_skipped_unchanged == 3


# -- folder failures ---------------------------------------------------------------


def test_folder_failure_skips_its_contents_and_continues(tree, tmp_path):
    client = FakeClient()
    real_create = client.create_folder

    def create(name, parent):
        if name == "Receipts":
            raise NoCfoError("server error", 500)
        return real_create(name, parent)

    client.create_folder = create
    stats = make_engine(tree, client, tmp_path / "s.db").sync()
    assert any("Receipts" in e for e in stats.errors)
    names = {f["name"] for f in client.files.values()}
    assert names == {"a.txt", "b.txt"}  # Invoices still synced
    assert all(f["folder_id"] is not None for f in client.files.values())
    assert "r.txt" not in names  # not dumped into the NoCFO root


def test_dry_run_nested_new_folder_does_not_adopt_root_namesake(tree, tmp_path):
    client = FakeClient()
    client.create_folder("2026", None)  # a root folder named like a subfolder
    stats = make_engine(tree, client, tmp_path / "s.db", dry_run=True).sync()
    assert stats.folders_created == 3  # Invoices, Receipts, Receipts/2026
    assert stats.folders_reused == 0


def test_unreadable_folder_is_not_reported_as_gone(tree, tmp_path):
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    locked = tree / "Receipts" / "2026"
    locked.chmod(0)
    try:
        stats = make_engine(tree, client, tmp_path / "s.db").sync()
    finally:
        locked.chmod(0o755)
    assert any("cannot list" in e for e in stats.errors)
    assert stats.files_left_behind == 0
    assert stats.files_failed == 0


def test_cached_folders_validated_once_per_parent(tree, tmp_path):
    client = FakeClient()
    make_engine(tree, client, tmp_path / "s.db").sync()
    calls = []
    real_get = client.get_folder
    client.get_folder = lambda fid: (calls.append(fid), real_get(fid))[1]
    stats = make_engine(tree, client, tmp_path / "s.db").sync()
    assert stats.folders_reused == 3
    # root children come from list_root_folders; only Receipts has a cached
    # subfolder, so one fetch — not one per cached folder
    assert calls == [client.folders_by_name("Receipts")]


def test_upload_streams_file_instead_of_reading_it(tree, tmp_path, monkeypatch):
    def boom(self):
        raise AssertionError("must not read whole files into memory")

    monkeypatch.setattr(Path, "read_bytes", boom)
    client = FakeClient()
    stats = make_engine(tree, client, tmp_path / "s.db").sync()
    assert stats.files_uploaded == 3
    assert {f["content"] for f in client.files.values()} == {b"a", b"b", b"r"}
