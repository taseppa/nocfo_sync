"""Headless tests for the unified TUI.

The whole app is one screen — the mappings list — so these tests cover first
run, the list's rows, and every shortcut on it.  Everything runs against a
fake NoCFO client; no network, no real Drive.
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
    NocfoSyncApp,
    TokenScreen,
)
from nocfo_sync.config import Config, Mapping
from nocfo_sync.nocfo import NoCfoError, NocfoFolder
from nocfo_sync.picker import (
    DrivePickerScreen,
    NoCfoPickerScreen,
    ProgressScreen,
    SyncAllProgressScreen,
)


class FakeClient:
    """Minimal stand-in covering the calls the app makes."""

    def __init__(self, fail_auth: bool = False, businesses=None, folders=None):
        self.fail_auth = fail_auth
        self.businesses = (
            businesses
            if businesses is not None
            else [{"slug": "biz", "name": "Biz"}]
        )
        self.folders = folders if folders is not None else {
            1: {"id": 1, "name": "Receipts", "parent": None}
        }
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
        if folder_id not in self.folders:
            raise NoCfoError("folder not found", 404)
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
        destination_folder_name=overrides.get("destination_folder_name", None),
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


async def _settle(pilot, screen_type, tries: int = 40) -> None:
    for _ in range(tries):
        await pilot.pause(0.05)
        if isinstance(pilot.app.screen, screen_type):
            return


async def _land_on_list(pilot, app: NocfoSyncApp) -> MappingsScreen:
    await _settle(pilot, MappingsScreen)
    screen = app.screen
    assert isinstance(screen, MappingsScreen)
    return screen


def _rows(app: NocfoSyncApp) -> list[str]:
    listing = app.screen.query_one(OptionList)
    return [str(option.prompt) for option in listing.options]


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
        # choose the mount root
        await pilot.press("enter")
        await _settle(pilot, NoCfoPickerScreen)
        # choose "NoCFO root"
        await pilot.press("enter")
        await _settle(pilot, MappingsScreen)
        assert len(cfg.mappings) == 1
        m = cfg.mappings[0]
        assert m.root_folder
        assert m.destination_chosen is True
        assert m.destination_folder_id is None
        assert m.business_slug == "biz"  # auto-selected single business


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
async def test_bad_token_with_mappings_still_shows_list(tmp_path):
    """A failed probe is non-fatal when there is a mapping: the list still
    opens (offline actions work) with a banner."""
    cfg = make_cfg(tmp_path)  # one mapping
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient(fail_auth=True))
    async with app.run_test() as pilot:
        screen = await _land_on_list(pilot, app)
        assert "unreachable" in screen.status_text.lower()


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


# -- the mappings list -------------------------------------------------------


def _two_mapping_app(tmp_path, **overrides) -> NocfoSyncApp:
    (tmp_path / "kuitit").mkdir(exist_ok=True)
    (tmp_path / "laskut").mkdir(exist_ok=True)
    cfg = make_cfg(
        tmp_path,
        mappings=[
            make_mapping(tmp_path, root_folder="kuitit"),
            make_mapping(tmp_path, root_folder="laskut"),
        ],
        **overrides,
    )
    return NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient())


@pytest.mark.asyncio
async def test_mappings_list_is_the_home_screen(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        screen = await _land_on_list(pilot, app)
        assert len(screen.query_one(OptionList).options) == 2


@pytest.mark.asyncio
async def test_single_mapping_also_lands_on_list(tmp_path):
    app = make_app(tmp_path, root_folder="kuitit")
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)


@pytest.mark.asyncio
async def test_rows_show_drive_source_and_destination_name(tmp_path):
    cfg = make_cfg(
        tmp_path,
        mappings=[
            make_mapping(tmp_path, root_folder="kuitit"),
            make_mapping(
                tmp_path,
                root_folder="laskut",
                destination_folder_id=1,
                destination_folder_name="Receipts 2026",
            ),
        ],
    )
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient())
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        rows = _rows(app)
        assert "kuitit" in rows[0] and "NoCFO root" in rows[0]
        assert "laskut" in rows[1] and "Receipts 2026" in rows[1]


@pytest.mark.asyncio
async def test_missing_destination_name_is_looked_up(tmp_path):
    """A hand-edited config with only an id gets its name filled in on view."""
    cfg = make_cfg(
        tmp_path,
        mappings=[
            make_mapping(tmp_path, root_folder="kuitit", destination_folder_id=1)
        ],
    )
    app = NocfoSyncApp(cfg, client_factory=lambda slug: FakeClient())
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        for _ in range(40):
            await pilot.pause(0.05)
            if cfg.mappings[0].destination_folder_name:
                break
        assert cfg.mappings[0].destination_folder_name == "Receipts"
        assert "Receipts" in _rows(app)[0]


@pytest.mark.asyncio
async def test_remove_mapping_asks_first(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
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
        await _land_on_list(pilot, app)
        assert len(app.cfg.mappings) == 1


@pytest.mark.asyncio
async def test_add_mapping_opens_wizard(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("a")
        await _settle(pilot, DrivePickerScreen)
        assert isinstance(app.screen, DrivePickerScreen)


@pytest.mark.asyncio
async def test_add_mapping_full_flow_appends(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("a")
        await _settle(pilot, DrivePickerScreen)
        await pilot.press("enter")  # pick the mount root
        await _settle(pilot, NoCfoPickerScreen)
        await pilot.press("enter")  # pick the NoCFO root
        await _land_on_list(pilot, app)
        assert len(app.cfg.mappings) == 3


# -- per-row shortcuts -------------------------------------------------------


@pytest.mark.asyncio
async def test_i_shows_info(tmp_path):
    app = make_app(tmp_path, root_folder="kuitit")
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, InfoScreen)


@pytest.mark.asyncio
async def test_g_opens_drive_picker_for_highlighted(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("down")  # highlight the second mapping
        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, DrivePickerScreen)


@pytest.mark.asyncio
async def test_n_opens_nocfo_picker_for_highlighted(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, NoCfoPickerScreen)


@pytest.mark.asyncio
async def test_t_opens_readonly_tree(tmp_path):
    app = make_app(tmp_path, root_folder="kuitit")
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, NoCfoPickerScreen)
        assert app.screen.choose is False


@pytest.mark.asyncio
async def test_q_quits(tmp_path):
    app = make_app(tmp_path, root_folder="kuitit")
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("q")
        await pilot.pause()
        assert app.return_value is None  # app exited (run_test returned)


# -- syncing -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_s_syncs_the_highlighted_mapping(tmp_path):
    app = _two_mapping_app(tmp_path)  # creates kuitit/ and laskut/
    (tmp_path / "kuitit" / "a.txt").write_text("a")
    (tmp_path / "laskut" / "b.txt").write_text("b")
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("s")
        for _ in range(80):
            await pilot.pause(0.05)
            if isinstance(app.screen, ProgressScreen) and app.screen.finished:
                break
        assert isinstance(app.screen, ProgressScreen)
        assert app.screen.stats.files_uploaded == 1
        assert (tmp_path / ".state" / "kuitit.db").exists()
        assert not (tmp_path / ".state" / "laskut.db").exists()


@pytest.mark.asyncio
async def test_sync_updates_status_on_return(tmp_path):
    (tmp_path / "kuitit").mkdir()
    (tmp_path / "kuitit" / "a.txt").write_text("hello")
    (tmp_path / "kuitit" / "b.txt").write_text("b")

    app = make_app(tmp_path, root_folder="kuitit")
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("s")
        for _ in range(80):
            await pilot.pause(0.05)
            if isinstance(app.screen, ProgressScreen) and app.screen.finished:
                break
        progress = app.screen
        assert isinstance(progress, ProgressScreen)
        assert progress.stats.files_uploaded == 2
        assert (tmp_path / ".state" / "kuitit.db").exists()
        await pilot.press("q")
        screen = await _land_on_list(pilot, app)
        assert app.last_stats is not None
        assert "2 uploaded" in screen.status_text


@pytest.mark.asyncio
async def test_S_syncs_all_mappings(tmp_path):
    app = _two_mapping_app(tmp_path)
    (tmp_path / "kuitit" / "a.txt").write_text("a")
    (tmp_path / "laskut" / "b.txt").write_text("b")
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("S")
        for _ in range(80):
            await pilot.pause(0.05)
            if isinstance(app.screen, SyncAllProgressScreen) and app.screen.finished:
                break
        assert isinstance(app.screen, SyncAllProgressScreen)
        assert app.screen.stats.files_uploaded == 2
        assert (tmp_path / ".state" / "kuitit.db").exists()
        assert (tmp_path / ".state" / "laskut.db").exists()


@pytest.mark.asyncio
async def test_f_force_sync_asks_for_confirmation(tmp_path):
    app = make_app(tmp_path, root_folder="kuitit")
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)


@pytest.mark.asyncio
async def test_F_force_sync_all_asks_for_confirmation(tmp_path):
    app = _two_mapping_app(tmp_path)
    async with app.run_test() as pilot:
        await _land_on_list(pilot, app)
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        for _ in range(80):
            await pilot.pause(0.05)
            if isinstance(app.screen, SyncAllProgressScreen) and app.screen.finished:
                break
        assert isinstance(app.screen, SyncAllProgressScreen)


# -- config persistence ------------------------------------------------------


def test_destination_flag_and_name_persist(tmp_path):
    from nocfo_sync.config import load_config, save_config

    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.token = "t"
    cfg.mappings = [
        make_mapping(
            tmp_path,
            destination_folder_id=7,
            destination_folder_name="Receipts",
            destination_chosen=True,
        )
    ]
    save_config(cfg)

    again = load_config(tmp_path / "config.toml")
    m = again.mappings[0]
    assert m.destination_chosen is True
    assert m.destination_folder_id == 7
    assert m.destination_folder_name == "Receipts"
    assert m.destination_label == "Receipts"


def test_destination_label_handles_root_and_missing_name(tmp_path):
    assert make_mapping(tmp_path, destination_folder_id=None).destination_label == (
        "NoCFO root"
    )
    assert make_mapping(tmp_path, destination_folder_id=7).destination_label == "7"
    assert make_mapping(
        tmp_path, destination_folder_id=7, destination_folder_name="X"
    ).destination_label == "X"
    assert make_mapping(
        tmp_path, destination_chosen=False
    ).destination_label == "(not selected)"
