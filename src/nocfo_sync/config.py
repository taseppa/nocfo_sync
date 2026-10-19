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
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Drive for Desktop mounts each account at GoogleDrive-<email>/My Drive.
DRIVE_MOUNT_GLOB = "Library/CloudStorage/GoogleDrive-*/My Drive"
DEFAULT_MOUNT = "~/Library/CloudStorage/GoogleDrive/My Drive"


def default_mount() -> Path:
    """The Drive for Desktop mount to use when a mapping doesn't name one:
    the first signed-in account found, else a generic placeholder path."""
    found = sorted(p for p in Path.home().glob(DRIVE_MOUNT_GLOB) if p.is_dir())
    return found[0] if found else Path(DEFAULT_MOUNT).expanduser()


class ConfigError(Exception):
    pass


def _slug(text: str) -> str:
    """Filesystem-safe, deterministic key from a mapping's Drive source."""
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._").lower()
    return out or "mapping"


@dataclass(eq=False)  # identity, not field equality: two mappings may look alike
class Mapping:
    """One Google Drive folder → NoCFO destination mapping."""

    business_slug: str = ""
    mount: Path = field(default_factory=default_mount)
    root_folder: str = ""  # relative path inside the mount; "" = not selected
    destination_folder_id: int | None = None  # None = NoCFO root
    # The destination's display name, remembered when it is picked so the TUI
    # can show it without another API call.  Refreshed lazily when missing.
    destination_folder_name: str | None = None
    # Whether a destination was ever chosen.  Needed because None means both
    # "not chosen yet" and "the NoCFO root" — without this the setup wizard
    # would re-run on every launch after choosing the root.
    destination_chosen: bool = False
    # Stem of this mapping's state DB, persisted so it never changes when
    # other mappings are added, removed or reordered.  "" = not assigned yet
    # (see ``Config.assign_state_files``).
    state_file: str = ""

    @property
    def root_path(self) -> Path | None:
        if not self.root_folder:
            return None
        p = (self.mount / self.root_folder).resolve()
        if not p.is_relative_to(self.mount.resolve()):
            raise ConfigError(f"root_folder escapes the mount: {self.root_folder}")
        return p

    def set_root_folder(self, root_folder: str) -> None:
        """Point the mapping at a new Drive source.  A different source is a
        different tree, so it gets a fresh state DB instead of inheriting
        the old one's path -> id cache."""
        if root_folder != self.root_folder:
            self.root_folder = root_folder
            self.state_file = ""

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
    # The token as stored in config.toml, and the token that came from
    # somewhere else (NOCFO_TOKEN / the legacy ``config`` file).  While
    # ``token`` is still that override, saving writes ``file_token`` back so
    # an override is never persisted into config.toml.
    file_token: str = ""
    override_token: str = ""

    mappings: list[Mapping] = field(default_factory=list)

    path: Path | None = None  # file this config was loaded from

    @property
    def state_dir(self) -> Path:
        """The sync databases live next to the config file, so the tool works
        no matter which directory it is run from."""
        base = self.path.parent if self.path else Path.cwd()
        return base / ".state"

    @property
    def _last_drive_folder_path(self) -> Path:
        return self.state_dir / "last_drive_folder"

    def last_drive_folder(self) -> Path | None:
        """The Drive folder last chosen in the picker, so the picker can
        reopen there.  None when nothing is remembered."""
        try:
            text = self._last_drive_folder_path.read_text().strip()
        except OSError:
            return None
        return Path(text) if text else None

    def remember_drive_folder(self, folder: Path) -> None:
        """Record the picker's choice; best effort, a failure is not fatal."""
        path = self._last_drive_folder_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{folder}\n")
        except OSError:
            pass

    def drive_preselect(self, mapping: Mapping) -> Path | None:
        """Where the Drive picker should open for ``mapping``: its own source
        folder once it has one, else the last folder picked."""
        if mapping.root_folder:
            try:
                return mapping.root_path
            except ConfigError:
                pass
        return self.last_drive_folder()

    def _legacy_state_key(self, mapping: Mapping) -> str:
        """The DB stem older versions derived from the list position, used
        once to adopt an existing DB for a mapping without ``state_file``."""
        key = mapping.state_key
        same = [m for m in self.mappings if m.state_key == key]
        if len(same) > 1:
            index = next(i for i, m in enumerate(same) if m is mapping)
            key = f"{key}-{index + 1}"
        return key

    def assign_state_files(self) -> None:
        """Give every mapping a unique, persistent ``state_file``.

        Mappings from older configs adopt the DB name they used to get, so
        their change tracking carries over; clashes get a numeric suffix."""
        taken = {m.state_file for m in self.mappings if m.state_file}
        for m in self.mappings:
            if m.state_file:
                continue
            base = self._legacy_state_key(m)
            key, n = base, 2
            while key in taken:
                key, n = f"{base}-{n}", n + 1
            m.state_file = key
            taken.add(key)

    def state_path_for(self, mapping: Mapping) -> Path:
        """One DB per mapping, named by its persistent ``state_file``.

        Two mappings must never share a DB — they would corrupt each other's
        change tracking — and a mapping must keep its DB when others are
        removed, or it would re-upload everything as duplicates."""
        if any(m is mapping for m in self.mappings):
            self.assign_state_files()
        key = mapping.state_file or self._legacy_state_key(mapping)
        return self.state_dir / f"{key}.db"

    def find_mapping(self, spec: str) -> Mapping | None:
        """Resolve a ``--mapping`` selector.

        Matches the Drive source folder exactly, then by basename, then by
        case-insensitive substring — so ``sync --mapping Receipts`` works and
        people don't have to type the full relative path.  A selector that
        matches several mappings raises rather than guessing one.
        """
        spec = spec.strip().strip("/")
        if not spec:
            return None
        low = spec.lower()
        for matches in (
            [m for m in self.mappings if m.root_folder == spec],
            [m for m in self.mappings if Path(m.root_folder).name == spec],
            [m for m in self.mappings if low in m.root_folder.lower()],
        ):
            if len(matches) == 1:
                return matches[0]
            if matches:
                names = ", ".join(m.label for m in matches)
                raise ConfigError(
                    f"{spec!r} matches several mappings ({names}) — "
                    "use the full Drive path"
                )
        return None


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
        mount=(
            Path(str(raw["mount"])).expanduser() if raw.get("mount") else default_mount()
        ),
        root_folder=str(raw.get("root_folder", "")),
        state_file=_slug(str(raw["state_file"])) if raw.get("state_file") else "",
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
        if kv.get("token"):
            cfg.token = cfg.override_token = kv["token"]
        if "base_url" in kv:
            cfg.base_url = kv["base_url"].rstrip("/")

    path = find_config(explicit)
    if path is not None:
        data = tomllib.loads(path.read_text())
        nocfo = data.get("nocfo", {})
        cfg.base_url = nocfo.get("base_url", cfg.base_url).rstrip("/")
        if nocfo.get("token"):
            cfg.token = cfg.file_token = str(nocfo["token"])
            cfg.override_token = ""
        cfg.idempotency = bool(nocfo.get("idempotency", cfg.idempotency))
        cfg.mappings = [_load_mapping(raw) for raw in data.get("mapping", [])]
        cfg.path = path

    # env override for the token (handy for CI / testing)
    if os.environ.get("NOCFO_TOKEN"):
        cfg.token = cfg.override_token = os.environ["NOCFO_TOKEN"]

    return cfg


def _toml_str(value: str) -> str:
    """A quoted TOML basic string (escapes backslashes, quotes and control
    characters, which TOML does not allow raw)."""
    out = []
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, readable by the owner only.

    The config holds the API token, so it must not be world-readable, and a
    crash mid-write must never leave a truncated config behind."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:  # mkstemp creates the file as 0600
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_config(cfg: Config) -> Path:
    """Persist the config as TOML.  Creates ``config.toml`` next to the
    loaded file (or in the cwd)."""
    path = cfg.path or (Path.cwd() / "config.toml")
    # an override (NOCFO_TOKEN / legacy file) stays out of config.toml; a
    # token set in the app replaces it and is saved
    if cfg.override_token and cfg.token == cfg.override_token:
        token = cfg.file_token
    else:
        token = cfg.file_token = cfg.token
        cfg.override_token = ""
    cfg.assign_state_files()
    lines = ["[nocfo]", f"base_url = {_toml_str(cfg.base_url)}"]
    if token:
        lines.append(f"token = {_toml_str(token)}")
    lines.append(f"idempotency = {'true' if cfg.idempotency else 'false'}")
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
        lines.append(f"state_file = {_toml_str(m.state_file)}")
    _write_private(path, "\n".join(lines) + "\n")
    cfg.path = path
    return path
