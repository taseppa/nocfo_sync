"""Config discovery and parsing tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from nocfo_sync import config as config_mod
from nocfo_sync.config import ConfigError, find_config, load_config


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
    (work / "config.toml").write_text(
        "[nocfo]\ntoken = \"abc\"\nbusiness_slug = \"biz\"\n"
    )
    cfg = load_config()
    assert cfg.token == "abc"
    assert cfg.business_slug == "biz"
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


def test_state_path_sits_next_to_config(isolated_dirs):
    work, _ = isolated_dirs
    (work / "config.toml").write_text("[nocfo]\ntoken = \"abc\"\n")
    cfg = load_config()
    assert cfg.state_path == work / ".state" / "sync.db"


def test_state_path_falls_back_to_cwd(isolated_dirs):
    work, _ = isolated_dirs
    cfg = load_config()
    assert cfg.path is None
    assert cfg.state_path == work / ".state" / "sync.db"


def test_root_path_rejects_escape(isolated_dirs, tmp_path):
    work, _ = isolated_dirs
    (work / "config.toml").write_text(
        "[gdrive]\nroot_folder = \"../../etc\"\n"
    )
    cfg = load_config()
    with pytest.raises(ConfigError):
        _ = cfg.root_path


def test_find_config_returns_none_when_nothing_exists(isolated_dirs):
    assert find_config() is None


def test_search_dirs_includes_repo_root():
    """The checked-out project's config must be discoverable from anywhere,
    which is what lets `.venv/bin/nocfo-sync` run outside the repo dir."""
    repo_root = Path(config_mod.__file__).resolve().parents[2]
    assert repo_root in config_mod._search_dirs()
    # repo root is discoverable even when the cwd is somewhere else
    assert config_mod._search_dirs()[0] == Path.cwd()
