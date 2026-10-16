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

    path: Path | None = None  # file this config was loaded from

    @property
    def root_path(self) -> Path | None:
        if not self.root_folder:
            return None
        p = (self.mount / self.root_folder).resolve()
        if not str(p).startswith(str(self.mount.resolve())):
            raise ConfigError(f"root_folder escapes the mount: {self.root_folder}")
        return p


def _parse_kv_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def load_config(explicit: str | Path | None = None) -> Config:
    """Load config.toml (plus legacy ``config`` fallback) from the cwd."""
    cwd = Path.cwd()
    cfg = Config()

    legacy_path = cwd / "config"
    if explicit is None and legacy_path.exists():
        kv = _parse_kv_file(legacy_path)
        if "token" in kv:
            cfg.token = kv["token"]
        if "base_url" in kv:
            cfg.base_url = kv["base_url"]
        if "business_slug" in kv:
            cfg.business_slug = kv["business_slug"]

    path = Path(explicit) if explicit else cwd / "config.toml"
    if path.exists():
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
        cfg.path = path
    elif explicit is not None:
        raise ConfigError(f"config file not found: {path}")

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
    )
    path.write_text(text)
    cfg.path = path
    return path
