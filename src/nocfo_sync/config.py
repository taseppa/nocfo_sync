"""Configuration loading.

Primary config file is ``config.toml`` (git-ignored).  It has one shared
``[nocfo]`` section (base_url / token / idempotency) and one ``[[mapping]]``
block per Google-Drive-folder → NoCFO-destination mapping.  For convenience a
legacy plain ``key=value`` file (the original ``config`` file with just
``token=...``) is also supported and merged in for the token/base_url.

Mappings are not named by the user: a mapping is identified by the Drive
source folder it syncs (``root_folder``), which is what the CLI ``--mapping``
selector and the TUI labels use.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MOUNT = (
    "~/Library/CloudStorage/GoogleDrive-tapio.seppala@gmail.com/My Drive"
)


class ConfigError(Exception):
    pass


def _slug(text: str) -> str:
    """Filesystem-safe, deterministic key from a mapping's Drive source."""
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._").lower()
    return out or "mapping"


@dataclass
class Mapping:
    """One Google Drive folder → NoCFO destination mapping."""

    business_slug: str = ""
    mount: Path = field(default_factory=lambda: Path(DEFAULT_MOUNT).expanduser())
    root_folder: str = ""  # relative path inside the mount; "" = not selected
    destination_folder_id: int | None = None  # None = NoCFO root
    # The destination's display name, remembered when it is picked so the TUI
    # can show it without another API call.  Refreshed lazily when missing.
    destination_folder_name: str | None = None
    # Whether a destination was ever chosen.  Needed because None means both
    # "not chosen yet" and "the NoCFO root" — without this the setup wizard
    # would re-run on every launch after choosing the root.
    destination_chosen: bool = False

    @property
    def root_path(self) -> Path | None:
        if not self.root_folder:
            return None
        p = (self.mount / self.root_folder).resolve()
        if not str(p).startswith(str(self.mount.resolve())):
            raise ConfigError(f"root_folder escapes the mount: {self.root_folder}")
        return p

    @property
    def label(self) -> str:
        """Human label: the Drive source folder (fallback: the business)."""
        return self.root_folder or self.business_slug or "(unmapped)"

    @property
    def state_key(self) -> str:
        """Stem of this mapping's state DB, derived from its Drive source."""
        return _slug(self.root_folder or self.business_slug or "mapping")

    @property
    def destination_label(self) -> str:
        """Human name for the NoCFO destination (falls back to its id)."""
        if not self.destination_chosen:
            return "(not selected)"
        if self.destination_folder_id is None:
            return "NoCFO root"
        return self.destination_folder_name or str(self.destination_folder_id)


@dataclass
class Config:
    # [nocfo] — shared across every mapping
    base_url: str = "https://api.nocfo.io"
    token: str = ""
    idempotency: bool = True  # send Idempotency-Key on uploads

    mappings: list[Mapping] = field(default_factory=list)

    path: Path | None = None  # file this config was loaded from

    @property
    def state_dir(self) -> Path:
        """The sync databases live next to the config file, so the tool works
        no matter which directory it is run from."""
        base = self.path.parent if self.path else Path.cwd()
        return base / ".state"

    def state_path_for(self, mapping: Mapping) -> Path:
        """One DB per mapping, named after its Drive source folder.

        Two mappings with the same source key would otherwise share a DB and
        corrupt each other's change tracking, so duplicates get a numeric
        suffix (deterministic: by position in ``self.mappings``)."""
        key = mapping.state_key
        same = [m for m in self.mappings if m.state_key == key]
        if len(same) > 1:
            key = f"{key}-{same.index(mapping) + 1}"
        return self.state_dir / f"{key}.db"

    def find_mapping(self, spec: str) -> Mapping | None:
        """Resolve a ``--mapping`` selector.

        Matches the Drive source folder exactly, then by basename, then by
        case-insensitive substring — so ``sync --mapping Receipts`` works and
        people don't have to type the full relative path.
        """
        spec = spec.strip().strip("/")
        if not spec:
            return None
        for m in self.mappings:
            if m.root_folder == spec:
                return m
        for m in self.mappings:
            if Path(m.root_folder).name == spec:
                return m
        low = spec.lower()
        matches = [m for m in self.mappings if low in m.root_folder.lower()]
        if len(matches) == 1:
            return matches[0]
        return matches[0] if matches else None


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


def _load_mapping(raw: dict) -> Mapping:
    m = Mapping(
        business_slug=str(raw.get("business_slug", "")),
        mount=Path(str(raw.get("mount", DEFAULT_MOUNT))).expanduser(),
        root_folder=str(raw.get("root_folder", "")),
    )
    dest = raw.get("destination_folder_id")
    m.destination_folder_id = int(dest) if dest else None
    name = raw.get("destination_folder_name")
    m.destination_folder_name = str(name) if name else None
    # older configs predate this flag: an explicit id means it was chosen
    m.destination_chosen = bool(raw.get("destination_selected", dest is not None))
    return m


def load_config(explicit: str | Path | None = None) -> Config:
    """Load ``config.toml``, searching the cwd, ``~/.config/nocfo-sync`` and
    the repo root.  A legacy plain ``config`` file (``token=…``) is merged in
    as a fallback for the token/base_url."""
    cfg = Config()

    legacy_path = _legacy_config()
    if legacy_path is not None:
        kv = _parse_kv_file(legacy_path)
        if "token" in kv:
            cfg.token = kv["token"]
        if "base_url" in kv:
            cfg.base_url = kv["base_url"]

    path = find_config(explicit)
    if path is not None:
        data = tomllib.loads(path.read_text())
        nocfo = data.get("nocfo", {})
        cfg.base_url = nocfo.get("base_url", cfg.base_url).rstrip("/")
        cfg.token = nocfo.get("token", cfg.token)
        cfg.idempotency = bool(nocfo.get("idempotency", cfg.idempotency))
        cfg.mappings = [_load_mapping(raw) for raw in data.get("mapping", [])]
        cfg.path = path

    # env override for the token (handy for CI / testing)
    if os.environ.get("NOCFO_TOKEN"):
        cfg.token = os.environ["NOCFO_TOKEN"]

    return cfg


def _toml_str(value: str) -> str:
    """A quoted TOML basic string (escapes backslashes and quotes)."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_config(cfg: Config) -> Path:
    """Persist the config as TOML.  Creates ``config.toml`` next to the
    loaded file (or in the cwd)."""
    path = cfg.path or (Path.cwd() / "config.toml")
    lines = [
        "[nocfo]",
        f"base_url = {_toml_str(cfg.base_url)}",
        f"token = {_toml_str(cfg.token)}",
        f"idempotency = {'true' if cfg.idempotency else 'false'}",
    ]
    for m in cfg.mappings:
        lines.append("")
        lines.append("[[mapping]]")
        lines.append(f"business_slug = {_toml_str(m.business_slug)}")
        lines.append(f"mount = {_toml_str(str(m.mount))}")
        lines.append(f"root_folder = {_toml_str(m.root_folder)}")
        if m.destination_folder_id is not None:
            lines.append(f"destination_folder_id = {m.destination_folder_id}")
        if m.destination_folder_name:
            lines.append(
                f"destination_folder_name = {_toml_str(m.destination_folder_name)}"
            )
        lines.append(
            f"destination_selected = "
            f"{'true' if m.destination_chosen else 'false'}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    cfg.path = path
    return path
