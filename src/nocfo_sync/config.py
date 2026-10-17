"""Configuration loading.

Primary config file is ``config.toml`` (git-ignored).  For convenience a
legacy plain ``key=value`` file (the original ``config`` file with just
``token=...``) is also supported and merged in.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MOUNT = (
    "~/Library/CloudStorage/GoogleDrive-tapio.seppala@gmail.com/My Drive"
)


class ConfigError(Exception):
    pass


@dataclass
class Config:
    # [nocfo]
    base_url: str = "https://api.nocfo.io"
    token: str = ""
    business_slug: str = ""
    idempotency: bool = True  # send Idempotency-Key on uploads

    # [gdrive]
    mount: Path = field(default_factory=lambda: Path(DEFAULT_MOUNT).expanduser())
    root_folder: str = ""  # relative path inside the mount; "" = not selected
    destination_folder_id: int | None = None  # None = NoCFO root
    # Whether a destination was ever chosen.  Needed because None means both
    # "not chosen yet" and "the NoCFO root" — without this the setup wizard
    # would re-run on every launch after choosing the root.
    destination_chosen: bool = False

    path: Path | None = None  # file this config was loaded from

    @property
    def root_path(self) -> Path | None:
        if not self.root_folder:
            return None
        p = (self.mount / self.root_folder).resolve()
        if not str(p).startswith(str(self.mount.resolve())):
            raise ConfigError(f"root_folder escapes the mount: {self.root_folder}")
        return p

    @property
    def state_path(self) -> Path:
        """Where the sync database lives: next to the config file, so the
        tool works no matter which directory it is run from."""
        base = self.path.parent if self.path else Path.cwd()
        return base / ".state" / "sync.db"


def _parse_kv_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _search_dirs() -> list[Path]:
    """Directories searched for ``config.toml`` / ``config``, in order.

    The repo root is included so the checked-out project's config is found no
    matter which directory the command is run from.
    """
    dirs = [Path.cwd(), Path.home() / ".config" / "nocfo-sync"]
    # src/nocfo_sync/config.py -> [0] nocfo_sync, [1] src, [2] repo root
    repo_root = Path(__file__).resolve().parents[2]
    dirs.append(repo_root)
    # de-duplicate, keep order
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def find_config(explicit: str | Path | None = None) -> Path | None:
    """Return the config.toml to use, or None if none exists."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        return path
    env = os.environ.get("NOCFO_SYNC_CONFIG")
    if env:
        path = Path(env).expanduser()
        if not path.exists():
            raise ConfigError(f"NOCFO_SYNC_CONFIG points at a missing file: {path}")
        return path
    for d in _search_dirs():
        candidate = d / "config.toml"
        if candidate.exists():
            return candidate
    return None


def _legacy_config() -> Path | None:
    for d in _search_dirs():
        candidate = d / "config"
        if candidate.exists():
            return candidate
    return None


def load_config(explicit: str | Path | None = None) -> Config:
    """Load ``config.toml``, searching the cwd, ``~/.config/nocfo-sync`` and
    the repo root.  A legacy plain ``config`` file (``token=…``) is merged in
    as a fallback."""
    cfg = Config()

    legacy_path = _legacy_config()
    if legacy_path is not None:
        kv = _parse_kv_file(legacy_path)
        if "token" in kv:
            cfg.token = kv["token"]
        if "base_url" in kv:
            cfg.base_url = kv["base_url"]
        if "business_slug" in kv:
            cfg.business_slug = kv["business_slug"]

    path = find_config(explicit)
    if path is not None:
        data = tomllib.loads(path.read_text())
        nocfo = data.get("nocfo", {})
        gdrive = data.get("gdrive", {})
        cfg.base_url = nocfo.get("base_url", cfg.base_url).rstrip("/")
        cfg.token = nocfo.get("token", cfg.token)
        cfg.business_slug = nocfo.get("business_slug", cfg.business_slug)
        cfg.idempotency = bool(nocfo.get("idempotency", cfg.idempotency))
        if "mount" in gdrive:
            cfg.mount = Path(gdrive["mount"]).expanduser()
        else:
            cfg.mount = cfg.mount.expanduser()
        cfg.root_folder = gdrive.get("root_folder", cfg.root_folder)
        dest = gdrive.get("destination_folder_id")
        cfg.destination_folder_id = int(dest) if dest else None
        # older configs predate this flag: an explicit id means it was chosen
        cfg.destination_chosen = bool(
            gdrive.get("destination_selected", dest is not None)
        )
        cfg.path = path
    else:
        cfg.mount = cfg.mount.expanduser()

    # env override for the token (handy for CI / testing)
    if os.environ.get("NOCFO_TOKEN"):
        cfg.token = os.environ["NOCFO_TOKEN"]

    return cfg


def save_config(cfg: Config) -> Path:
    """Persist the config as TOML.  Creates ``config.toml`` next to the
    loaded file (or in the cwd)."""
    path = cfg.path or (Path.cwd() / "config.toml")
    dest_line = (
        ""
        if cfg.destination_folder_id is None
        else f"destination_folder_id = {cfg.destination_folder_id}\n"
    )
    text = (
        "[nocfo]\n"
        f'base_url = "{cfg.base_url}"\n'
        f'token = "{cfg.token}"\n'
        f'business_slug = "{cfg.business_slug}"\n'
        f"idempotency = {'true' if cfg.idempotency else 'false'}\n"
        "\n"
        "[gdrive]\n"
        f'mount = "{cfg.mount}"\n'
        f'root_folder = "{cfg.root_folder}"\n'
        f"{dest_line}"
        f"destination_selected = {'true' if cfg.destination_chosen else 'false'}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    cfg.path = path
    return path
