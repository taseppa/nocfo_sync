"""Config discovery and parsing tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from nocfo_sync import config as config_mod
from nocfo_sync.config import Config, ConfigError, Mapping, find_config, load_config, save_config


@pytest.fixture()
def isolated_dirs(tmp_path, monkeypatch):
    """Run with cwd and ~/.config redirected into tmp_path so discovery is
    deterministic and never touches the real repo/home.  The repo-root search
    entry is removed so the project's own config.toml can't leak in."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NOCFO_SYNC_CONFIG", raising=False)
    monkeypatch.delenv("NOCFO_TOKEN", raising=False)
    monkeypatch.setattr(
        config_mod,
        "_search_dirs",
        lambda: [work, home / ".config" / "nocfo-sync"],
    )
    return work, home


def test_explicit_missing_config_raises(isolated_dirs):
    with pytest.raises(ConfigError):
        load_config(isolated_dirs[0] / "nope.toml")


def test_finds_config_in_cwd(isolated_dirs):
    work, _ = isolated_dirs
    (work / "config.toml").write_text("[nocfo]\ntoken = \"abc\"\n")
    cfg = load_config()
    assert cfg.token == "abc"
    assert cfg.path == work / "config.toml"


def test_finds_config_in_xdg_dir(isolated_dirs):
    _, home = isolated_dirs
    xdg = home / ".config" / "nocfo-sync"
    xdg.mkdir(parents=True)
    (xdg / "config.toml").write_text("[nocfo]\ntoken = \"xdg\"\n")
    cfg = load_config()
    assert cfg.token == "xdg"
    assert cfg.path == xdg / "config.toml"


def test_env_points_at_config(isolated_dirs, monkeypatch):
    work, _ = isolated_dirs
    elsewhere = work / "custom.toml"
    elsewhere.write_text("[nocfo]\ntoken = \"envfile\"\n")
    monkeypatch.setenv("NOCFO_SYNC_CONFIG", str(elsewhere))
    cfg = load_config()
    assert cfg.token == "envfile"


def test_legacy_config_file_is_merged(isolated_dirs):
    work, _ = isolated_dirs
    (work / "config").write_text("token=legacytoken\n")
    cfg = load_config()
    assert cfg.token == "legacytoken"
    # no config.toml anywhere -> path is unset
    assert cfg.path is None


def test_env_token_overrides(isolated_dirs, monkeypatch):
    work, _ = isolated_dirs
    (work / "config.toml").write_text("[nocfo]\ntoken = \"file\"\n")
    monkeypatch.setenv("NOCFO_TOKEN", "fromenv")
    cfg = load_config()
    assert cfg.token == "fromenv"


def test_no_mappings_when_section_absent(isolated_dirs):
    work, _ = isolated_dirs
    (work / "config.toml").write_text("[nocfo]\ntoken = \"abc\"\n")
    assert load_config().mappings == []


def test_parses_mappings(isolated_dirs):
    work, _ = isolated_dirs
    (work / "config.toml").write_text(
        '[nocfo]\nbase_url = "https://api.nocfo.io/"\ntoken = "t"\n'
        "\n"
        "[[mapping]]\n"
        'business_slug = "acme"\n'
        'mount = "/mnt/drive"\n'
        'root_folder = "Receipts"\n'
        "destination_folder_id = 42\n"
        "destination_selected = true\n"
        "\n"
        "[[mapping]]\n"
        'business_slug = "acme"\n'
        'mount = "/mnt/drive"\n'
        'root_folder = "Invoices"\n'
    )
    cfg = load_config()
    assert cfg.base_url == "https://api.nocfo.io"  # trailing slash stripped
    assert len(cfg.mappings) == 2
    first, second = cfg.mappings
    assert first.label == "Receipts"
    assert first.business_slug == "acme"
    assert first.destination_folder_id == 42
    assert first.destination_chosen is True
    # no destination id and no flag -> not chosen
    assert second.destination_folder_id is None
    assert second.destination_chosen is False


def test_parses_destination_folder_name(isolated_dirs):
    work, _ = isolated_dirs
    (work / "config.toml").write_text(
        "[[mapping]]\n"
        'business_slug = "acme"\n'
        'root_folder = "Receipts"\n'
        "destination_folder_id = 42\n"
        'destination_folder_name = "Receipts 2026"\n'
        "destination_selected = true\n"
    )
    m = load_config().mappings[0]
    assert m.destination_folder_id == 42
    assert m.destination_folder_name == "Receipts 2026"
    assert m.destination_label == "Receipts 2026"


def test_root_destination_flag_makes_it_chosen(isolated_dirs):
    work, _ = isolated_dirs
    (work / "config.toml").write_text(
        "[[mapping]]\n"
        'business_slug = "acme"\n'
        'root_folder = "Invoices"\n'
        "destination_selected = true\n"
    )
    m = load_config().mappings[0]
    assert m.destination_folder_id is None
    assert m.destination_chosen is True


def test_config_round_trip(isolated_dirs, tmp_path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.token = "tok"
    cfg.base_url = "https://api.nocfo.io"
    cfg.mappings = [
        Mapping(
            business_slug="acme",
            mount=tmp_path / "drive",
            root_folder="Receipts",
            destination_folder_id=1234,
            destination_chosen=True,
        ),
        Mapping(
            business_slug="acme",
            mount=tmp_path / "drive",
            root_folder="Invoices",
            destination_folder_id=None,
            destination_chosen=True,  # chose the NoCFO root
        ),
    ]
    save_config(cfg)

    again = load_config(tmp_path / "config.toml")
    assert again.token == "tok"
    assert len(again.mappings) == 2
    assert again.mappings[0].root_folder == "Receipts"
    assert again.mappings[0].destination_folder_id == 1234
    assert again.mappings[1].destination_folder_id is None
    assert again.mappings[1].destination_chosen is True


def test_state_path_is_per_mapping(isolated_dirs, tmp_path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.mappings = [
        Mapping(root_folder="Receipts"),
        Mapping(root_folder="Invoices"),
    ]
    assert cfg.state_path_for(cfg.mappings[0]) == tmp_path / ".state" / "receipts.db"
    assert cfg.state_path_for(cfg.mappings[1]) == tmp_path / ".state" / "invoices.db"


def test_state_path_disambiguates_duplicate_sources(isolated_dirs, tmp_path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.mappings = [
        Mapping(root_folder="Receipts", business_slug="a"),
        Mapping(root_folder="Receipts", business_slug="b"),
    ]
    assert cfg.state_path_for(cfg.mappings[0]) == tmp_path / ".state" / "receipts-1.db"
    assert cfg.state_path_for(cfg.mappings[1]) == tmp_path / ".state" / "receipts-2.db"


def test_state_path_falls_back_to_cwd(isolated_dirs):
    work, _ = isolated_dirs
    cfg = Config()
    assert cfg.path is None
    assert cfg.state_path_for(Mapping(root_folder="X")) == work / ".state" / "x.db"


def test_find_mapping_by_path_name_and_substring():
    cfg = Config()
    cfg.mappings = [
        Mapping(root_folder="Receipts/2026"),
        Mapping(root_folder="Invoices"),
    ]
    assert cfg.find_mapping("Receipts/2026") is cfg.mappings[0]
    assert cfg.find_mapping("Invoices") is cfg.mappings[1]
    assert cfg.find_mapping("receipts") is cfg.mappings[0]
    assert cfg.find_mapping("nope") is None


def test_root_path_rejects_escape(isolated_dirs):
    work, _ = isolated_dirs
    (work / "config.toml").write_text(
        "[[mapping]]\n"
        f'mount = "{work}"\n'
        'root_folder = "../../etc"\n'
    )
    cfg = load_config()
    with pytest.raises(ConfigError):
        _ = cfg.mappings[0].root_path


def test_find_config_returns_none_when_nothing_exists(isolated_dirs):
    assert find_config() is None


def test_search_dirs_includes_repo_root():
    """The checked-out project's config must be discoverable from anywhere,
    which is what lets `.venv/bin/nocfo-sync` run outside the repo dir."""
    repo_root = Path(config_mod.__file__).resolve().parents[2]
    assert repo_root in config_mod._search_dirs()
    # repo root is discoverable even when the cwd is somewhere else
    assert config_mod._search_dirs()[0] == Path.cwd()


# -- token handling (overrides never persisted) ------------------------------


def test_env_token_is_not_written_into_config(isolated_dirs, monkeypatch):
    work, _ = isolated_dirs
    path = work / "config.toml"
    path.write_text('[nocfo]\ntoken = "file-token"\n')
    monkeypatch.setenv("NOCFO_TOKEN", "env-secret")
    cfg = load_config()
    assert cfg.token == "env-secret"
    save_config(cfg)
    text = path.read_text()
    assert "env-secret" not in text
    assert 'token = "file-token"' in text


def test_token_entered_in_app_replaces_env_override(isolated_dirs, monkeypatch):
    work, _ = isolated_dirs
    (work / "config.toml").write_text('[nocfo]\ntoken = "old"\n')
    monkeypatch.setenv("NOCFO_TOKEN", "env-secret")
    cfg = load_config()
    cfg.token = "typed-in"  # what the TUI's token screen does
    save_config(cfg)
    assert 'token = "typed-in"' in (work / "config.toml").read_text()


def test_legacy_token_is_not_copied_into_config(isolated_dirs):
    work, _ = isolated_dirs
    (work / "config").write_text("token=legacytoken\n")
    cfg = load_config()
    save_config(cfg)
    assert "legacytoken" not in (work / "config.toml").read_text()
    # still picked up from the legacy file next time
    assert load_config().token == "legacytoken"


def test_empty_token_in_file_does_not_hide_legacy_token(isolated_dirs):
    work, _ = isolated_dirs
    (work / "config").write_text("token=legacytoken\n")
    (work / "config.toml").write_text('[nocfo]\ntoken = ""\n')
    assert load_config().token == "legacytoken"


def test_saved_config_is_private_and_written_atomically(isolated_dirs):
    work, _ = isolated_dirs
    path = work / "config.toml"
    path.write_text('[nocfo]\ntoken = "t"\n')
    path.chmod(0o644)
    save_config(load_config())
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in work.iterdir()] == ["config.toml"]  # no temp left


def test_control_characters_round_trip(isolated_dirs, tmp_path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.token = 'a"b\\c\nd\te\x01'
    save_config(cfg)
    assert load_config(cfg.path).token == cfg.token


# -- stable per-mapping state DBs ----------------------------------------------


def test_identical_mappings_get_separate_state_dbs(tmp_path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.mappings = [Mapping(root_folder="A/Kuitit"), Mapping(root_folder="A/Kuitit")]
    paths = {cfg.state_path_for(m) for m in cfg.mappings}
    assert len(paths) == 2


def test_state_db_survives_removing_another_mapping(isolated_dirs, tmp_path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.mappings = [Mapping(root_folder="Foo/Bar"), Mapping(root_folder="foo-bar")]
    before = cfg.state_path_for(cfg.mappings[1])
    save_config(cfg)
    again = load_config(cfg.path)
    again.mappings.pop(0)
    save_config(again)
    assert load_config(cfg.path).state_path_for(again.mappings[0]) == before


def test_new_mapping_does_not_take_an_existing_db(tmp_path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.mappings = [Mapping(root_folder="Receipts")]
    first = cfg.state_path_for(cfg.mappings[0])
    cfg.mappings.insert(0, Mapping(root_folder="Receipts"))
    assert cfg.state_path_for(cfg.mappings[1]) == first
    assert cfg.state_path_for(cfg.mappings[0]) != first


def test_changing_drive_source_gets_a_new_db(tmp_path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    m = Mapping(root_folder="Receipts")
    cfg.mappings = [m]
    old = cfg.state_path_for(m)
    m.set_root_folder("Invoices")
    assert cfg.state_path_for(m) == tmp_path / ".state" / "invoices.db" != old
    m.set_root_folder("Invoices")  # same source again: keeps it
    assert cfg.state_path_for(m) == tmp_path / ".state" / "invoices.db"


# -- --mapping selection ---------------------------------------------------------


def test_find_mapping_rejects_ambiguous_selector():
    cfg = Config()
    cfg.mappings = [
        Mapping(root_folder="Receipts 2025"),
        Mapping(root_folder="Receipts 2026"),
        Mapping(root_folder="x/2025"),
        Mapping(root_folder="y/2025"),
    ]
    with pytest.raises(ConfigError, match="several"):
        cfg.find_mapping("receipts")
    with pytest.raises(ConfigError, match="several"):
        cfg.find_mapping("2025")
    assert cfg.find_mapping("x/2025") is cfg.mappings[2]
    assert cfg.find_mapping("Receipts 2026") is cfg.mappings[1]


def test_root_path_rejects_sibling_with_same_prefix(tmp_path):
    mount = tmp_path / "My Drive"
    (tmp_path / "My Drive2").mkdir(parents=True)
    mount.mkdir()
    m = Mapping(mount=mount, root_folder="../My Drive2")
    with pytest.raises(ConfigError):
        _ = m.root_path


def test_default_mount_finds_signed_in_account(isolated_dirs):
    _, home = isolated_dirs
    drive = home / "Library" / "CloudStorage" / "GoogleDrive-me@example.com" / "My Drive"
    drive.mkdir(parents=True)
    assert config_mod.default_mount() == drive
    assert Mapping().mount == drive
