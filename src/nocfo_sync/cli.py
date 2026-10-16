"""Command-line interface.

Commands:
  sync              run one sync pass (opens TUI pickers if not configured)
  pick-drive-root   TUI: choose the Google Drive folder to sync from
  pick-destination  TUI: choose the NoCFO destination folder
  discover-folders  print the NoCFO folder tree
  status            show config + state summary
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, ConfigError, load_config, save_config
from .nocfo import NoCfoClient, NoCfoError
from .picker import DriveFolderPicker, NoCfoFolderPicker, SyncProgressApp
from .state import State
from .sync import SyncEngine

STATE_PATH = Path(".state/sync.db")


def _client(cfg: Config) -> NoCfoClient:
    if not cfg.token:
        raise ConfigError("no NoCFO token configured (nocfo.token in config.toml)")
    if not cfg.business_slug:
        raise ConfigError("no business slug configured (nocfo.business_slug)")
    return NoCfoClient(
        cfg.base_url, cfg.token, cfg.business_slug, idempotency=cfg.idempotency
    )


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.root_folder:
        print("No Drive folder selected yet — opening picker…")
        picked = DriveFolderPicker(cfg.mount).run()
        if picked is None:
            print("cancelled.")
            return 1
        cfg.root_folder = picked.relative_to(cfg.mount.resolve()).as_posix()
        save_config(cfg)
        print(f"saved Drive root: {cfg.root_folder}")

    client = _client(cfg)
    try:
        name = client.check()
    except NoCfoError as e:
        print(f"cannot reach NoCFO: {e}", file=sys.stderr)
        return 1
    print(f"business: {name} ({cfg.business_slug})")

    if cfg.destination_folder_id is None and not args.root_destination:
        print("No NoCFO destination selected yet — opening picker…")
        picker = NoCfoFolderPicker(client)
        dest = picker.run()
        if picker.cancelled:
            print("cancelled.")
            return 1
        cfg.destination_folder_id = dest
        save_config(cfg)
        print(f"saved destination folder id: {dest}")

    root = cfg.root_path
    assert root is not None

    state = State(STATE_PATH)
    engine = SyncEngine(
        client, state, root, cfg.destination_folder_id, dry_run=args.dry_run
    )
    try:
        if args.plain:
            stats = engine.sync()
            for notice in client.notices:
                print(f"note: {notice}", file=sys.stderr)
            print(
                f"done: {stats.files_uploaded} uploaded, "
                f"{stats.files_updated} updated, "
                f"{stats.files_skipped_unchanged} unchanged, "
                f"{stats.folders_created} folders created"
            )
            for err in stats.errors:
                print(f"  error: {err}", file=sys.stderr)
        else:
            stats = SyncProgressApp(engine).run() or engine.stats
        return 1 if stats and stats.errors else 0
    finally:
        state.close()
        client.close()


def cmd_pick_drive_root(cfg: Config, args: argparse.Namespace) -> int:
    preselect = cfg.root_path if cfg.root_folder else None
    picked = DriveFolderPicker(cfg.mount, preselect).run()
    if picked is None:
        print("cancelled.")
        return 1
    cfg.root_folder = picked.relative_to(cfg.mount.resolve()).as_posix()
    path = save_config(cfg)
    print(f"saved Drive root {cfg.root_folder!r} -> {path}")
    return 0


def cmd_pick_destination(cfg: Config, args: argparse.Namespace) -> int:
    client = _client(cfg)
    try:
        picker = NoCfoFolderPicker(client, cfg.destination_folder_id)
        picked = picker.run()
    finally:
        client.close()
    if picker.cancelled:
        print("cancelled.")
        return 1
    cfg.destination_folder_id = picked
    path = save_config(cfg)
    label = "NoCFO root" if picked is None else f"folder id {picked}"
    print(f"saved destination {label} -> {path}")
    return 0


def cmd_discover_folders(cfg: Config, args: argparse.Namespace) -> int:
    client = _client(cfg)

    def show(folder_id: int | None, indent: int) -> None:
        if folder_id is None:
            children = [{"id": f.id, "name": f.name} for f in client.list_root_folders()]
        else:
            children = client.get_folder(folder_id).children or []
        for c in children:
            print("  " * indent + f"- {c['name']}  (id {c['id']})")
            if args.depth > indent:
                show(c["id"], indent + 1)

    try:
        show(None, 0)
    finally:
        client.close()
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    print(f"config:        {cfg.path or '(none — using defaults)'}")
    print(f"nocfo:         {cfg.base_url}  business={cfg.business_slug or '(unset)'}")
    print(f"token:         {'set' if cfg.token else 'MISSING'}")
    print(f"drive mount:   {cfg.mount}  {'(exists)' if cfg.mount.is_dir() else '(MISSING)'}")
    print(f"drive root:    {cfg.root_folder or '(not selected)'}")
    print(f"destination:   {cfg.destination_folder_id or 'NoCFO root'}")
    if STATE_PATH.exists():
        state = State(STATE_PATH)
        n_files = len(state.all_file_relpaths())
        n_folders = state.db.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
        print(f"state:         {n_files} files, {n_folders} folders tracked ({STATE_PATH})")
        state.close()
    else:
        print("state:         (no sync has run yet)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nocfo-sync")
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="run one sync pass")
    p.add_argument("--dry-run", action="store_true", help="don't upload anything")
    p.add_argument("--plain", action="store_true", help="no TUI; log to stdout")
    p.add_argument("--root-destination", action="store_true",
                   help="sync into the NoCFO root without asking")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("pick-drive-root", help="choose the Drive folder to sync")
    p.set_defaults(fn=cmd_pick_drive_root)

    p = sub.add_parser("pick-destination", help="choose the NoCFO destination folder")
    p.set_defaults(fn=cmd_pick_destination)

    p = sub.add_parser("discover-folders", help="print the NoCFO folder tree")
    p.add_argument("--depth", type=int, default=3)
    p.set_defaults(fn=cmd_discover_folders)

    p = sub.add_parser("status", help="show config + state summary")
    p.set_defaults(fn=cmd_status)

    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    try:
        return args.fn(cfg, args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except NoCfoError as e:
        print(f"NoCFO API error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
