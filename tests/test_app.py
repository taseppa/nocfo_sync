"""Headless tests for the unified TUI (first run, menu, actions).

Everything runs against a fake NoCFO client; no network, no real Drive.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input, OptionList

from nocfo_sync.app import (
    AuthScreen,
    BusinessPickerScreen,
    ConfirmScreen,
    InfoScreen,
    MenuScreen,
    NocfoSyncApp,
    TokenScreen,
)
from nocfo_sync.config import Config
from nocfo_sync.nocfo import NoCfoError, NocfoFolder
from nocfo_sync.picker import DrivePickerScreen, NoCfoPickerScreen, ProgressScreen


class FakeClient:
    """Minimal stand-in covering the calls the app makes."""

    def __init__(self, fail_auth: bool = False, businesses=None):
        self.fail_auth = fail_auth
        self.businesses = (
            businesses
            if businesses is not None
            else [{"slug": "biz", "name": "Biz"}]
        )
        self.notices: list[str] = []
        self.folders = {1: {"id": 1, "name": "Receipts", "parent": None}}
        self.files: dict[int, dict] = {}
        self._next_file = 5000
        self.deleted: list[int] = []
        self.closed = 0

    # auth / discovery
    def current_user(self):
        if self.fail_auth:
            raise NoCfoError("invalid token", 401)
        return {"id": 1, "email": "me@example.com"}

    def list_businesses(self):
        return self.businesses

    def check(self):
        if self.fail_auth:
            raise NoCfoError("invalid token", 401)
        return "Biz"

    # folders
    def list_root_folders(self):
        return [
            NocfoFolder(id=f["id"], name=f["name"], parent=f["parent"])
            for f in self.folders.values()
            if f["parent"] is None
        ]

    def get_folder(self, folder_id):
        f = self.folders[folder_id]
        children = [
            {"id": c["id"], "name": c["name"]}
            for c in self.folders.values()
            if c["parent"] == folder_id
        ]
        return NocfoFolder(id=f["id"], name=f["name"], parent=f["parent"],
                           children=children)

    def create_folder(self, name, parent):
        new_id = max(self.folders) + 1
        self.folders[new_id] = {"id": new_id, "name": name, "parent": parent}
        return NocfoFolder(id=new_id, name=name, parent=parent)

    # files (enough for the sync engine)
    def upload_file(self, name, content, folder_id=None, mime=None, notes=None):
        self._next_file += 1
        self.files[self._next_file] = {
            "id": self._next_file, "name": name, "folder_id": folder_id,
            "content": content, "mime": mime,
        }
        return self.files[self._next_file]

    def list_files(self, folder=None, root=None, is_used=None, search=None):
        if is_used:
            return iter([])
        return iter(list(self.files.values()))

    def delete_file(self, file_id):
        self.deleted.append(file_id)
        self.files.pop(file_id, None)

    def close(self):
        self.closed += 1


def make_cfg(tmp_path: Path, **overrides) -> Config:
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.base_url = "https://example.test"
    cfg.token = overrides.get("token", "tok")
    cfg.business_slug = overrides.get("business_slug", "biz")
    cfg.mount = overrides.get("mount", tmp_path).resolve()
    cfg.root_folder = overrides.get("root_folder", "")
    cfg.destination_folder_id = overrides.get("destination_folder_id", None)
    cfg.destination_chosen = overrides.get("destination_chosen", False)
    return cfg


def make_app(tmp_path, **overrides) -> NocfoSyncApp:
    cfg = make_cfg(tmp_path, **overrides)
    return NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient())


# -- first run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_prompts_for_token(tmp_path):
    cfg = make_cfg(tmp_path, token="", business_slug="")
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, TokenScreen)
        # empty token is rejected
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TokenScreen)
        # a real token is saved and used
        app.screen.query_one(Input).value = "new-token"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause(0.05)
            if not isinstance(app.screen, TokenScreen):
                break
        assert cfg.token == "new-token"
        assert cfg.path is not None and cfg.path.exists()
        # no business slug yet -> business picker
        assert isinstance(app.screen, BusinessPickerScreen)


@pytest.mark.asyncio
async def test_first_run_full_wizard(tmp_path):
    (tmp_path / "kuitit").mkdir()
    cfg = make_cfg(tmp_path, root_folder="", destination_chosen=False)
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        # slug is set, drive root is not -> drive picker comes up first
        assert isinstance(app.screen, DrivePickerScreen)
        # choose the mount root
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause(0.05)
            if not isinstance(app.screen, DrivePickerScreen):
                break
        assert cfg.root_folder  # saved
        assert isinstance(app.screen, NoCfoPickerScreen)
        # choose "NoCFO root"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause(0.05)
            if not isinstance(app.screen, NoCfoPickerScreen):
                break
        assert cfg.destination_chosen is True
        assert cfg.destination_folder_id is None
        assert isinstance(app.screen, MenuScreen)


@pytest.mark.asyncio
async def test_bad_token_with_known_business_lands_on_menu(tmp_path):
    """A failed probe is non-fatal when the business is known: the menu still
    opens (offline actions like changing the Drive root work) with a banner."""
    cfg = make_cfg(tmp_path)  # token + slug set
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient(fail_auth=True))
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, MenuScreen):
                break
        assert isinstance(app.screen, MenuScreen)
        assert "unreachable" in app.screen.status_text.lower()


@pytest.mark.asyncio
async def test_bad_token_without_business_shows_auth_screen(tmp_path):
    cfg = make_cfg(tmp_path, business_slug="")
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient(fail_auth=True))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, AuthScreen)
        # 'r' offers a fresh token
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, TokenScreen)


# -- menu --------------------------------------------------------------------


def _menu_app(tmp_path, **overrides) -> NocfoSyncApp:
    (tmp_path / "kuitit").mkdir(exist_ok=True)
    overrides.setdefault("root_folder", "kuitit")
    overrides.setdefault("destination_chosen", True)
    return make_app(tmp_path, **overrides)


@pytest.mark.asyncio
async def test_menu_is_reached_when_configured(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, MenuScreen):
                break
        assert isinstance(app.screen, MenuScreen)


@pytest.mark.asyncio
async def test_menu_i_shows_info(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, MenuScreen):
                break
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, InfoScreen)


@pytest.mark.asyncio
async def test_menu_g_opens_drive_picker(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, MenuScreen):
                break
        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, DrivePickerScreen)


@pytest.mark.asyncio
async def test_menu_n_opens_nocfo_picker(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, MenuScreen):
                break
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, NoCfoPickerScreen)


@pytest.mark.asyncio
async def test_menu_t_opens_readonly_tree(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, MenuScreen):
                break
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, NoCfoPickerScreen)
        assert app.screen.choose is False


@pytest.mark.asyncio
async def test_menu_q_quits(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, MenuScreen):
                break
        assert isinstance(app.screen, MenuScreen)
        await pilot.press("q")
        await pilot.pause()
        assert app.return_value is None  # app exited (run_test returned)


# -- force sync --------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_sync_asks_for_confirmation(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, MenuScreen):
                break
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        # declining does nothing
        await pilot.press("n")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)


@pytest.mark.asyncio
async def test_menu_s_syncs_and_updates_menu(tmp_path):
    """Pressing `s` runs the engine against the real SyncEngine + fake client,
    then `q` returns to the menu with fresh counts."""
    (tmp_path / "kuitit" / "sub").mkdir(parents=True)
    (tmp_path / "kuitit" / "sub" / "a.txt").write_text("hello")
    (tmp_path / "kuitit" / "b.txt").write_text("b")

    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause(0.05)
            if isinstance(app.screen, MenuScreen):
                break
        await pilot.press("s")
        for _ in range(80):
            await pilot.pause(0.05)
            if isinstance(app.screen, ProgressScreen) and app.screen.finished:
                break
        progress = app.screen
        assert isinstance(progress, ProgressScreen)
        assert progress.stats is not None
        assert progress.stats.files_uploaded == 2
        # `q` goes back to the menu and refreshes its counts
        await pilot.press("q")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, MenuScreen):
                break
        assert isinstance(app.screen, MenuScreen)
        assert app.last_stats is not None
        assert "2 uploaded" in app.screen.status_text


# -- config flag -------------------------------------------------------------


def test_destination_flag_persists(tmp_path):
    from nocfo_sync.config import load_config, save_config

    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.token = "t"
    cfg.business_slug = "b"
    cfg.root_folder = "kuitit"
    cfg.destination_folder_id = None
    cfg.destination_chosen = True  # chose the NoCFO root
    save_config(cfg)

    again = load_config(tmp_path / "config.toml")
    assert again.destination_chosen is True
    assert again.destination_folder_id is None
