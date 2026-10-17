"""Headless tests for the unified TUI (first run, mappings, menu, actions).

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
    MappingsScreen,
    MenuScreen,
    NocfoSyncApp,
    TokenScreen,
)
from nocfo_sync.config import Config, Mapping
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


def make_mapping(tmp_path: Path, **overrides) -> Mapping:
    return Mapping(
        business_slug=overrides.get("business_slug", "biz"),
        mount=overrides.get("mount", tmp_path).resolve(),
        root_folder=overrides.get("root_folder", "kuitit"),
        destination_folder_id=overrides.get("destination_folder_id", None),
        destination_chosen=overrides.get("destination_chosen", True),
    )


def make_cfg(tmp_path: Path, mappings: list[Mapping] | None = None, **overrides) -> Config:
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.base_url = "https://example.test"
    cfg.token = overrides.get("token", "tok")
    if mappings is None:
        mappings = [make_mapping(tmp_path, **overrides)]
    cfg.mappings = mappings
    return cfg


def make_app(tmp_path, **overrides) -> NocfoSyncApp:
    cfg = make_cfg(tmp_path, **overrides)
    return NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient())


async def _settle(pilot, screen_type, tries: int = 30) -> None:
    for _ in range(tries):
        await pilot.pause(0.05)
        if isinstance(pilot.app.screen, screen_type):
            return


# -- first run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_prompts_for_token(tmp_path):
    cfg = make_cfg(tmp_path, mappings=[], token="")
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, TokenScreen)
        # empty token is rejected
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TokenScreen)
        # a real token is saved and used; one business -> straight to Drive
        app.screen.query_one(Input).value = "new-token"
        await pilot.press("enter")
        await _settle(pilot, DrivePickerScreen)
        assert cfg.token == "new-token"
        assert cfg.path is not None and cfg.path.exists()
        assert isinstance(app.screen, DrivePickerScreen)


@pytest.mark.asyncio
async def test_first_run_full_wizard_creates_mapping(tmp_path):
    cfg = make_cfg(tmp_path, mappings=[], root_folder="")
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient())
    async with app.run_test() as pilot:
        await _settle(pilot, DrivePickerScreen)
        assert isinstance(app.screen, DrivePickerScreen)
        # choose the mount root
        await pilot.press("enter")
        await _settle(pilot, NoCfoPickerScreen)
        assert isinstance(app.screen, NoCfoPickerScreen)
        # choose "NoCFO root"
        await pilot.press("enter")
        await _settle(pilot, MenuScreen)
        assert len(cfg.mappings) == 1
        m = cfg.mappings[0]
        assert m.root_folder
        assert m.destination_chosen is True
        assert m.destination_folder_id is None
        assert m.business_slug == "biz"  # auto-selected single business
        assert isinstance(app.screen, MenuScreen)


@pytest.mark.asyncio
async def test_business_picker_when_several_businesses(tmp_path):
    businesses = [
        {"slug": "biz", "name": "Biz"},
        {"slug": "other", "name": "Other"},
    ]
    cfg = make_cfg(tmp_path, mappings=[], root_folder="")
    app = NocfoSyncApp(
        cfg, client_factory=lambda slug: FakeClient(businesses=businesses)
    )
    async with app.run_test() as pilot:
        await _settle(pilot, BusinessPickerScreen)
        assert isinstance(app.screen, BusinessPickerScreen)


@pytest.mark.asyncio
async def test_bad_token_with_mappings_lands_on_menu(tmp_path):
    """A failed probe is non-fatal when there is a mapping: its menu still
    opens (offline actions work) with a banner."""
    cfg = make_cfg(tmp_path)  # one mapping
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient(fail_auth=True))
    async with app.run_test() as pilot:
        await _settle(pilot, MenuScreen)
        assert isinstance(app.screen, MenuScreen)
        assert "unreachable" in app.screen.status_text.lower()


@pytest.mark.asyncio
async def test_bad_token_without_mappings_shows_auth_screen(tmp_path):
    cfg = make_cfg(tmp_path, mappings=[])
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient(fail_auth=True))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, AuthScreen)
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, TokenScreen)


# -- mappings screen ---------------------------------------------------------


def _two_mapping_app(tmp_path) -> NocfoSyncApp:
    (tmp_path / "kuitit").mkdir(exist_ok=True)
    (tmp_path / "laskut").mkdir(exist_ok=True)
    cfg = make_cfg(
        tmp_path,
        mappings=[
            make_mapping(tmp_path, root_folder="kuitit"),
            make_mapping(tmp_path, root_folder="laskut"),
        ],
    )
    return NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient())


@pytest.mark.asyncio
async def test_several_mappings_land_on_list(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(pilot, MappingsScreen)
        assert isinstance(app.screen, MappingsScreen)
        options = app.screen.query_one(OptionList)
        assert len(options.options) == 2


@pytest.mark.asyncio
async def test_enter_opens_mapping_and_m_returns(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(pilot, MappingsScreen)
        await pilot.press("enter")
        await _settle(pilot, MenuScreen)
        assert isinstance(app.screen, MenuScreen)
        assert app.mapping is app.cfg.mappings[0]
        await pilot.press("m")
        await _settle(pilot, MappingsScreen)
        assert isinstance(app.screen, MappingsScreen)


@pytest.mark.asyncio
async def test_remove_mapping_asks_first(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(pilot, MappingsScreen)
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        # declining keeps it
        await pilot.press("n")
        await pilot.pause()
        assert len(app.cfg.mappings) == 2
        # accepting removes it
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await _settle(pilot, MappingsScreen)
        assert len(app.cfg.mappings) == 1


@pytest.mark.asyncio
async def test_add_mapping_opens_wizard(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(pilot, MappingsScreen)
        await pilot.press("a")
        await _settle(pilot, DrivePickerScreen)
        assert isinstance(app.screen, DrivePickerScreen)


@pytest.mark.asyncio
async def test_sync_from_mappings_screen(tmp_path):
    app = _two_mapping_app(tmp_path)  # creates kuitit/ and laskut/
    (tmp_path / "kuitit" / "a.txt").write_text("a")
    (tmp_path / "laskut" / "b.txt").write_text("b")
    async with app.run_test() as pilot:
        await _settle(pilot, MappingsScreen)
        # sync the highlighted (first) mapping only
        await pilot.press("s")
        for _ in range(80):
            await pilot.pause(0.05)
            if isinstance(app.screen, ProgressScreen) and app.screen.finished:
                break
        assert isinstance(app.screen, ProgressScreen)
        assert app.screen.stats.files_uploaded == 1
        assert (tmp_path / ".state" / "kuitit.db").exists()
        assert not (tmp_path / ".state" / "laskut.db").exists()


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
        await _settle(pilot, MenuScreen)
        assert isinstance(app.screen, MenuScreen)


@pytest.mark.asyncio
async def test_menu_i_shows_info(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(pilot, MenuScreen)
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, InfoScreen)


@pytest.mark.asyncio
async def test_menu_g_opens_drive_picker(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(pilot, MenuScreen)
        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, DrivePickerScreen)


@pytest.mark.asyncio
async def test_menu_n_opens_nocfo_picker(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(pilot, MenuScreen)
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, NoCfoPickerScreen)


@pytest.mark.asyncio
async def test_menu_t_opens_readonly_tree(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(pilot, MenuScreen)
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, NoCfoPickerScreen)
        assert app.screen.choose is False


@pytest.mark.asyncio
async def test_menu_q_quits(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(pilot, MenuScreen)
        assert isinstance(app.screen, MenuScreen)
        await pilot.press("q")
        await pilot.pause()
        assert app.return_value is None  # app exited (run_test returned)


# -- force sync --------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_sync_asks_for_confirmation(tmp_path):
    app = _menu_app(tmp_path)
    async with app.run_test() as pilot:
        await _settle(pilot, MenuScreen)
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
        await _settle(pilot, MenuScreen)
        await pilot.press("s")
        for _ in range(80):
            await pilot.pause(0.05)
            if isinstance(app.screen, ProgressScreen) and app.screen.finished:
                break
        progress = app.screen
        assert isinstance(progress, ProgressScreen)
        assert progress.stats is not None
        assert progress.stats.files_uploaded == 2
        # the mapping's own state file was used
        assert (tmp_path / ".state" / "kuitit.db").exists()
        # `q` goes back to the menu and refreshes its counts
        await pilot.press("q")
        await _settle(pilot, MenuScreen)
        assert isinstance(app.screen, MenuScreen)
        assert app.last_stats is not None
        assert "2 uploaded" in app.screen.status_text


# -- config persistence ------------------------------------------------------


def test_destination_flag_persists(tmp_path):
    from nocfo_sync.config import load_config, save_config

    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.token = "t"
    cfg.mappings = [make_mapping(tmp_path, destination_folder_id=None,
                                 destination_chosen=True)]
    save_config(cfg)

    again = load_config(tmp_path / "config.toml")
    m = again.mappings[0]
    assert m.destination_chosen is True
    assert m.destination_folder_id is None
