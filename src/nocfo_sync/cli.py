"""Command-line interface.

Run with no arguments for the unified TUI.  Subcommands remain for
scripting/cron:

  sync              run one pass for every mapping (or one with --mapping)
  mappings          list the configured mappings
  pick-drive-root   TUI: choose a mapping's Google Drive source
  pick-destination  TUI: choose a mapping's NoCFO destination
  discover-folders  print the NoCFO folder tree
  status            show config + per-mapping summary

Mappings are not named; ``--mapping`` selects one by its Drive source folder
(exact relative path, folder name, or a case-insensitive substring).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, ConfigError, Mapping, load_config, save_config
from .nocfo import NoCfoClient, NoCfoError
from .picker import DriveFolderPicker, NoCfoFolderPicker, SyncProgressApp
from .state import State
from .sync import SyncEngine


def _client(cfg: Config, mapping: Mapping) -> NoCfoClient:
    if not cfg.token:
        raise ConfigError("no NoCFO token configured (nocfo.token in config.toml)")
    if not mapping.business_slug:
        raise ConfigError(
            f"mapping {mapping.label!r} has no business configured "
            "(business_slug in config.toml)"
        )
    return NoCfoClient(
        cfg.base_url, cfg.token, mapping.business_slug, idempotency=cfg.idempotency
    )


def _select_mapping(cfg: Config, args: argparse.Namespace) -> Mapping:
    """Resolve the mapping a single-mapping command applies to."""
    if not cfg.mappings:
        raise ConfigError(
            "no mappings configured — run `nocfo-sync` (TUI) to add one"
        )
    spec = getattr(args, "mapping", None)
    if spec:
        mapping = cfg.find_mapping(spec)
        if mapping is None:
            known = ", ".join(m.label for m in cfg.mappings)
            raise ConfigError(f"no mapping matches {spec!r} (known: {known})")
        return mapping
    if len(cfg.mappings) == 1:
        return cfg.mappings[0]
    known = ", ".join(m.label for m in cfg.mappings)
    raise ConfigError(
        f"multiple mappings configured — pass --mapping (known: {known})"
    )


def _sync_one(cfg: Config, mapping: Mapping, args: argparse.Namespace) -> int:
    """Sync a single mapping; returns a process exit code."""
    if not mapping.root_folder:
        if args.plain:
            print(
                f"mapping {mapping.label!r} has no Drive source configured",
                file=sys.stderr,
            )
            return 1
        print("No Drive folder selected yet — opening picker…")
        picked = DriveFolderPicker(mapping.mount).run()
        if picked is None:
            print("cancelled.")
            return 1
        mapping.set_root_folder(picked.relative_to(mapping.mount.resolve()).as_posix())
        save_config(cfg)
        print(f"saved Drive root: {mapping.root_folder}")

    client = _client(cfg, mapping)
    try:
        name = client.check()
    except NoCfoError as e:
        print(f"[{mapping.label}] cannot reach NoCFO: {e}", file=sys.stderr)
        client.close()
        return 1
    print(f"[{mapping.label}] business: {name} ({mapping.business_slug})")

    if not mapping.destination_chosen and not args.root_destination:
        if args.plain:
            print(
                f"mapping {mapping.label!r} has no NoCFO destination configured",
                file=sys.stderr,
            )
            client.close()
            return 1
        print("No NoCFO destination selected yet — opening picker…")
        picker = NoCfoFolderPicker(client)
        dest = picker.run()
        if picker.cancelled:
            print("cancelled.")
            client.close()
            return 1
        mapping.destination_folder_id = dest
        mapping.destination_folder_name = picker.result_name
        mapping.destination_chosen = True
        save_config(cfg)
        label = "NoCFO root" if dest is None else f"folder id {dest}"
        print(f"saved destination {label}")

    root = mapping.root_path
    assert root is not None

    state = State(cfg.state_path_for(mapping))
    engine = SyncEngine(
        client, state, root, mapping.destination_folder_id,
        dry_run=args.dry_run, force=args.force,
    )
    try:
        if args.plain:
            stats = engine.sync()
            for notice in client.notices:
                print(f"note: {notice}", file=sys.stderr)
            print(
                f"[{mapping.label}] done: {stats.files_uploaded} uploaded, "
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


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    if args.mapping:
        mappings = [_select_mapping(cfg, args)]
    else:
        mappings = list(cfg.mappings)

    if not mappings:
        if args.plain:
            print(
                "no mappings configured — run `nocfo-sync` (TUI) to add one",
                file=sys.stderr,
            )
            return 1
        # first run: hand off to the TUI, which creates the first mapping
        from .app import run_tui

        run_tui(cfg)
        return 0

    code = 0
    for mapping in mappings:
        # one broken mapping must not stop the others from syncing
        try:
            code |= _sync_one(cfg, mapping, args)
        except (ConfigError, NoCfoError) as e:
            print(f"[{mapping.label}] error: {e}", file=sys.stderr)
            code = 1
    return code


def cmd_mappings(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.mappings:
        print("(no mappings configured)")
        return 0
    for m in cfg.mappings:
        print(
            f"{m.label}\t{m.business_slug or '(unset)'}\t"
            f"{m.root_folder or '(not selected)'}\t{m.destination_label}"
        )
    return 0


def cmd_pick_drive_root(cfg: Config, args: argparse.Namespace) -> int:
    mapping = _select_mapping(cfg, args)
    preselect = mapping.root_path if mapping.root_folder else None
    picked = DriveFolderPicker(mapping.mount, preselect).run()
    if picked is None:
        print("cancelled.")
        return 1
    mapping.set_root_folder(picked.relative_to(mapping.mount.resolve()).as_posix())
    path = save_config(cfg)
    print(f"saved Drive root {mapping.root_folder!r} -> {path}")
    return 0


def cmd_pick_destination(cfg: Config, args: argparse.Namespace) -> int:
    mapping = _select_mapping(cfg, args)
    client = _client(cfg, mapping)
    try:
        picker = NoCfoFolderPicker(client, mapping.destination_folder_id)
        picked = picker.run()
    finally:
        client.close()
    if picker.cancelled:
        print("cancelled.")
        return 1
    mapping.destination_folder_id = picked
    mapping.destination_folder_name = picker.result_name
    mapping.destination_chosen = True
    path = save_config(cfg)
    label = "NoCFO root" if picked is None else f"folder id {picked}"
    print(f"saved destination {label} -> {path}")
    return 0


def cmd_discover_folders(cfg: Config, args: argparse.Namespace) -> int:
    mapping = _select_mapping(cfg, args)
    client = _client(cfg, mapping)

    def show(folder_id: int | None, indent: int) -> None:
        if folder_id is None:
            children = [
                {"id": f.id, "name": f.name} for f in client.list_root_folders()
            ]
        else:
            children = client.get_folder(folder_id).children or []
        for c in children:
            print("  " * indent + f"- {c['name']}  (id {c['id']})")
            if indent + 1 < args.depth:
                show(c["id"], indent + 1)

    try:
        show(None, 0)
    finally:
        client.close()
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    print(f"config:        {cfg.path or '(none — using defaults)'}")
    print(f"nocfo:         {cfg.base_url}")
    print(f"token:         {'set' if cfg.token else 'MISSING'}")
    if not cfg.mappings:
        print("mappings:      (none configured)")
        return 0
    print(f"mappings:      {len(cfg.mappings)}")
    for m in cfg.mappings:
        root = m.root_folder or "(not selected)"
        if m.root_folder:
            try:
                p = m.root_path
            except ConfigError:
                p = None
            root += " (exists)" if p and p.is_dir() else " (MISSING)"
        print(f"  - {m.label}")
        print(f"      business:    {m.business_slug or '(unset)'}")
        print(f"      drive:       {root}   ({m.mount})")
        print(f"      destination: {m.destination_label}")
        state_path = cfg.state_path_for(m)
        if state_path.exists():
            state = State(state_path)
            try:
                n_files = len(state.all_file_relpaths())
                n_folders = state.count_folders()
            finally:
                state.close()
            print(
                f"      state:       {n_files} files, {n_folders} folders "
                f"tracked ({state_path})"
            )
        else:
            print(f"      state:       (no sync yet; {state_path})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nocfo-sync",
        description="Sync Google Drive folders into NoCFO. "
                    "Run with no command for the interactive TUI.",
    )
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=False)

    p = sub.add_parser("sync", help="run sync passes")
    p.add_argument("--mapping", help="sync only the mapping with this Drive "
                                     "source (default: all mappings)")
    p.add_argument("--dry-run", action="store_true", help="don't upload anything")
    p.add_argument("--force", action="store_true",
                   help="re-upload every file, ignoring the unchanged check "
                        "(repairs files uploaded without a type)")
    p.add_argument("--plain", action="store_true", help="no TUI; log to stdout")
    p.add_argument("--root-destination", action="store_true",
                   help="sync into the NoCFO root without asking")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("mappings", help="list the configured mappings")
    p.set_defaults(fn=cmd_mappings)

    p = sub.add_parser("pick-drive-root", help="choose a mapping's Drive source")
    p.add_argument("--mapping", help="which mapping (default: the only one)")
    p.set_defaults(fn=cmd_pick_drive_root)

    p = sub.add_parser("pick-destination",
                       help="choose a mapping's NoCFO destination folder")
    p.add_argument("--mapping", help="which mapping (default: the only one)")
    p.set_defaults(fn=cmd_pick_destination)

    p = sub.add_parser("discover-folders", help="print the NoCFO folder tree")
    p.add_argument("--mapping", help="which mapping (default: the only one)")
    p.add_argument("--depth", type=int, default=3,
                   help="number of folder levels to print (default: 3)")
    p.set_defaults(fn=cmd_discover_folders)

    p = sub.add_parser("status", help="show config + per-mapping summary")
    p.set_defaults(fn=cmd_status)

    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    try:
        if not args.command:
            from .app import run_tui

            run_tui(cfg)
            return 0
        return args.fn(cfg, args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except NoCfoError as e:
        print(f"NoCFO API error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
