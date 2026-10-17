"""CLI tests for multi-mapping behaviour (plain/scriptable paths)."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from nocfo_sync import cli
from nocfo_sync.config import Config, ConfigError, Mapping
from nocfo_sync.nocfo import NoCfoError, NocfoFolder


class FakeClient:
    def __init__(self):
        self.folders: dict[int, dict] = {}
        self.files: dict[int, dict] = {}
        self._next_folder = 100
        self._next_file = 1000
        self.notices: list[str] = []
        self.closed = 0
        self.checks = 0

    def check(self):
        self.checks += 1
        return "Biz"

    def list_root_folders(self):
        return [
            NocfoFolder(id=f["id"], name=f["name"], parent=f["parent"])
            for f in self.folders.values()
            if f["parent"] is None
        ]

    def get_folder(self, folder_id):
        if folder_id not in self.folders:
            raise NoCfoError("not found", 404)
        f = self.folders[folder_id]
        children = [
            {"id": c["id"], "name": c["name"]}
            for c in self.folders.values()
            if c["parent"] == folder_id
        ]
        return NocfoFolder(id=f["id"], name=f["name"], parent=f["parent"], children=children)

    def create_folder(self, name, parent):
        self._next_folder += 1
        self.folders[self._next_folder] = {
            "id": self._next_folder, "name": name, "parent": parent
        }
        return NocfoFolder(id=self._next_folder, name=name, parent=parent)

    def upload_file(self, name, content, folder_id=None, mime=None, notes=None):
        self._next_file += 1
        self.files[self._next_file] = {
            "id": self._next_file, "name": name, "folder_id": folder_id,
            "content": content, "mime": mime,
        }
        return self.files[self._next_file]

    def list_files(self, folder=None, root=None, is_used=None, search=None):
        return iter(list(self.files.values()))

    def delete_file(self, file_id):
        self.files.pop(file_id, None)

    def close(self):
        self.closed += 1


def _sync_args(**overrides) -> Namespace:
    base = dict(dry_run=False, force=False, plain=True, root_destination=False, mapping=None)
    base.update(overrides)
    return Namespace(**base)


def _cfg(tmp_path, names) -> Config:
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.token = "tok"
    for name in names:
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
        (tmp_path / name / f"{name.split('/')[-1]}.txt").write_text(name)
    cfg.mappings = [
        Mapping(business_slug="biz", mount=tmp_path, root_folder=name,
                destination_chosen=True)
        for name in names
    ]
    return cfg


@pytest.fixture()
def fake_clients(monkeypatch):
    made: list[FakeClient] = []

    def factory(cfg, mapping):
        c = FakeClient()
        made.append(c)
        return c

    monkeypatch.setattr(cli, "_client", factory)
    return made


def test_sync_without_mapping_runs_every_mapping(tmp_path, fake_clients, capsys):
    cfg = _cfg(tmp_path, ["Receipts", "Invoices"])
    code = cli.cmd_sync(cfg, _sync_args())
    assert code == 0
    assert len(fake_clients) == 2
    assert all(c.closed for c in fake_clients)
    out = capsys.readouterr().out
    assert "[Receipts]" in out and "[Invoices]" in out
    # one state DB per mapping, both populated
    assert (tmp_path / ".state" / "receipts.db").exists()
    assert (tmp_path / ".state" / "invoices.db").exists()


def test_sync_mapping_selects_one(tmp_path, fake_clients):
    cfg = _cfg(tmp_path, ["Receipts", "Invoices"])
    code = cli.cmd_sync(cfg, _sync_args(mapping="Invoices"))
    assert code == 0
    assert len(fake_clients) == 1
    assert (tmp_path / ".state" / "invoices.db").exists()
    assert not (tmp_path / ".state" / "receipts.db").exists()


def test_state_is_isolated_per_mapping(tmp_path, fake_clients):
    """The same relative filename in two mappings must not collide."""
    cfg = _cfg(tmp_path, ["a", "b"])
    for name in ("a", "b"):
        (tmp_path / name / "same.txt").write_text(f"content-{name}")
    cli.cmd_sync(cfg, _sync_args())
    contents = {f["content"] for c in fake_clients for f in c.files.values()}
    assert b"content-a" in contents and b"content-b" in contents


def test_select_mapping_requires_choice_when_several(tmp_path):
    cfg = _cfg(tmp_path, ["a", "b"])
    with pytest.raises(ConfigError):
        cli._select_mapping(cfg, _sync_args())


def test_select_mapping_errors_when_none(tmp_path):
    cfg = Config()
    with pytest.raises(ConfigError):
        cli._select_mapping(cfg, _sync_args())


def test_select_mapping_by_substring(tmp_path):
    cfg = _cfg(tmp_path, ["Receipts/2026", "Invoices"])
    m = cli._select_mapping(cfg, _sync_args(mapping="2026"))
    assert m.root_folder == "Receipts/2026"


def test_status_lists_mappings(tmp_path, capsys):
    cfg = _cfg(tmp_path, ["Receipts", "Invoices"])
    assert cli.cmd_status(cfg, Namespace()) == 0
    out = capsys.readouterr().out
    assert "mappings:      2" in out
    assert "- Receipts" in out
    assert "- Invoices" in out


def test_sync_plain_with_no_mappings_errors(tmp_path, capsys):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.token = "tok"
    code = cli.cmd_sync(cfg, _sync_args())
    assert code == 1
    assert "no mappings configured" in capsys.readouterr().err


def test_mappings_subcommand(tmp_path, capsys):
    cfg = _cfg(tmp_path, ["Receipts"])
    assert cli.cmd_mappings(cfg, Namespace()) == 0
    assert "Receipts" in capsys.readouterr().out
