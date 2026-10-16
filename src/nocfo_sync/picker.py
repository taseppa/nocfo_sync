"""Textual TUI: Drive folder picker, NoCFO destination picker, sync progress."""

from __future__ import annotations

import os
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, ProgressBar, Static, Tree
from textual.widgets._tree import TreeNode

from .nocfo import NoCfoClient, NoCfoError
from .sync import SyncEngine, SyncStats


def _add_placeholder(node: TreeNode) -> None:
    """Give an empty folder node an expander affordance."""
    node.add("loading…")


def _is_placeholder(node: TreeNode) -> bool:
    return (
        len(node.children) == 1
        and node.children[0].data is None
        and node.children[0].label.plain == "loading…"
    )


# --------------------------------------------------------------------------
# Drive folder picker (local filesystem)
# --------------------------------------------------------------------------


class DriveFolderPicker(App[Path | None]):
    """Browse the local Drive mount, select a folder to sync from."""

    CSS = """
    #tree { height: 1fr; }
    #hint { padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        ("enter", "select", "Sync from here"),
        ("q,escape", "cancel", "Cancel"),
    ]

    def __init__(self, mount: Path, preselect: Path | None = None):
        super().__init__()
        self.mount_path = mount.resolve()
        self.preselect = preselect.resolve() if preselect else None
        self.result: Path | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(f"Select a Google Drive folder  [dim]({self.mount_path})[/dim]")
        tree: Tree[Path] = Tree("My Drive", data=self.mount_path)
        tree.root.expand()
        self._populate(tree.root)
        yield tree
        yield Label("enter = select   q/esc = cancel", id="hint")
        yield Footer()

    def _populate(self, node: TreeNode[Path]) -> None:
        """Lazily add child *directories* of node."""
        d: Path = node.data
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError:
            node.add("[red]unreadable[/red]")
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child = node.add(entry.name, data=Path(entry.path))
                    _add_placeholder(child)
            except OSError:
                continue
        node.allow_expand = bool(node.children)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        if node is not self.tree.root and node.data is None:
            return
        if _is_placeholder(node):
            node.remove_children()
            self._populate(node)

    @property
    def tree(self) -> Tree[Path]:
        return self.query_one(Tree)

    def on_mount(self) -> None:
        if self.preselect and self.preselect.is_dir():
            self._expand_to(self.preselect)
        self.tree.focus()

    def _expand_to(self, target: Path) -> None:
        """Expand nodes down to target, populating synchronously so deep
        paths work without depending on event ordering."""
        node = self.tree.root
        try:
            rel = target.relative_to(self.mount_path)
        except ValueError:
            return
        for part in rel.parts:
            match = next(
                (c for c in node.children if c.data and c.data.name == part), None
            )
            if match is None:
                # node may still hold only its placeholder
                if _is_placeholder(node):
                    node.remove_children()
                    self._populate(node)
                    match = next(
                        (c for c in node.children if c.data and c.data.name == part),
                        None,
                    )
            if match is None:
                return
            if _is_placeholder(match):
                match.remove_children()
                self._populate(match)
            match.expand()
            node = match
        # node._line is only valid after the tree has been laid out, so
        # move the cursor on the next refresh instead of inline.
        self.call_after_refresh(self.tree.select_node, node)
        self.call_after_refresh(self.tree.scroll_to_node, node)

    def action_select(self) -> None:
        node = self.tree.cursor_node
        if node is not None and node.data is not None:
            self.result = node.data
            self.exit(self.result)

    def action_cancel(self) -> None:
        self.exit(None)


# --------------------------------------------------------------------------
# NoCFO destination picker (API-backed)
# --------------------------------------------------------------------------


class NoCfoFolderPicker(App[int | None]):
    """Browse NoCFO folders, select the sync destination root.

    Returns the chosen folder id, or None for "NoCFO root" (select the root
    node itself).  Cancelling returns None too — the caller distinguishes
    via the ``cancelled`` flag.
    """

    CSS = """
    #tree { height: 1fr; }
    #hint { padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        ("enter", "select", "Sync into here"),
        ("n", "new_folder", "New folder"),
        ("q,escape", "cancel", "Cancel"),
    ]

    def __init__(self, client: NoCfoClient, preselect: int | None = None):
        super().__init__()
        self.client = client
        self.preselect = preselect
        self.result: int | None = None
        self.cancelled = True
        self._new_folder_parent: TreeNode[int | None] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("Select the NoCFO destination folder")
        tree: Tree[int | None] = Tree("NoCFO root", data=None)
        tree.root.expand()
        self._populate_root(tree.root)
        yield tree
        yield Label(
            "enter = select   n = new folder here   q/esc = cancel", id="hint"
        )
        yield Footer()

    @property
    def tree(self) -> Tree[int | None]:
        return self.query_one(Tree)

    def _populate_root(self, node: TreeNode[int | None]) -> None:
        node.remove_children()
        try:
            folders = self.client.list_root_folders()
        except NoCfoError as e:
            node.add(f"[red]error: {e}[/red]")
            node.allow_expand = False
            return
        for f in folders:
            child = node.add(f"{f.name}  [dim]({f.id})[/dim]", data=f.id)
            _add_placeholder(child)
        node.allow_expand = bool(node.children)

    def _populate_children(self, node: TreeNode[int | None]) -> None:
        node.remove_children()
        try:
            detail = self.client.get_folder(node.data)
        except NoCfoError as e:
            node.add(f"[red]error: {e}[/red]")
            node.allow_expand = False
            return
        for c in detail.children or []:
            child = node.add(f"{c['name']}  [dim]({c['id']})[/dim]", data=c["id"])
            _add_placeholder(child)
        node.allow_expand = bool(node.children)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        if node is self.tree.root:
            if _is_placeholder(node):
                node.remove_children()
                self._populate_root(node)
            return
        if node.data is None:
            return
        if _is_placeholder(node):
            self._populate_children(node)

    def action_select(self) -> None:
        node = self.tree.cursor_node
        if node is None:
            return
        self.result = node.data  # None = NoCFO root (the root node)
        self.cancelled = False
        self.exit(self.result)

    def action_cancel(self) -> None:
        self.exit(None)

    def action_new_folder(self) -> None:
        node = self.tree.cursor_node
        if node is None:
            return
        self._new_folder_parent = node

        def handle(name: str) -> None:
            name = name.strip()
            parent = self._new_folder_parent
            if not name or parent is None:
                return
            try:
                created = self.client.create_folder(name, parent.data)
            except NoCfoError as e:
                self.notify(f"create failed: {e}", severity="error")
                return
            if parent is self.tree.root:
                self._populate_root(parent)
            else:
                self._populate_children(parent)
            parent.expand()
            self.notify(f"created folder {name!r} ({created.id})")

        self.push_screen(NewFolderScreen(), handle)


class NewFolderScreen(Screen[str | None]):
    """Tiny input screen; returns the entered name (or None on cancel)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    Screen { align: center middle; }
    #box { width: 60; border: round $accent; padding: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("New folder name:")
            yield Input(placeholder="e.g. Receipts 2026", id="name")
        yield Footer()

    def on_input_submitted(self, event) -> None:  # type: ignore[no-untyped-def]
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# Sync progress
# --------------------------------------------------------------------------


class SyncProgressApp(App[SyncStats | None]):
    """Progress bar + live log while SyncEngine runs in a worker thread."""

    CSS = """
    #log { height: 1fr; border: round $accent; margin: 0 1; padding: 0 1; }
    #summary { padding: 0 1; color: $success; }
    #bar { height: 1; margin: 0 1; }
    """

    BINDINGS = [("q", "quit", "Exit")]

    def __init__(self, engine: SyncEngine):
        super().__init__()
        self.engine = engine
        self.stats: SyncStats | None = None
        self._log_lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield ProgressBar(show_eta=True, id="bar")
        yield Static(id="summary")
        yield Static(id="log")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "nocfo-sync"
        self.sub_title = "syncing…"
        self.run_worker(self._run, thread=True)

    def _run(self) -> None:
        def progress(phase: str, msg: str) -> None:
            self.call_from_thread(self._append_log, msg)

        self.engine.progress = progress
        try:
            stats = self.engine.sync()
        except Exception as e:  # surface crashes in the UI, not just stderr
            self.call_from_thread(self._crash, str(e))
            return
        self.call_from_thread(self._done, stats)

    def _append_log(self, msg: str) -> None:
        self._log_lines.append(msg)
        del self._log_lines[:-200]
        self.query_one("#log", Static).update("\n".join(self._log_lines))

    def _done(self, stats: SyncStats) -> None:
        self.stats = stats
        self.query_one("#bar", ProgressBar).update(total=100, progress=100)
        s = stats
        summary = (
            f"done: {s.files_uploaded} uploaded, {s.files_updated} updated, "
            f"{s.files_skipped_unchanged} unchanged, "
            f"{s.folders_created} folders created, {s.folders_reused} reused, "
            f"{len(s.errors)} errors"
        )
        if s.files_skipped_native:
            summary += f", {s.files_skipped_native} Google-native skipped"
        if s.files_left_behind:
            summary += f", {s.files_left_behind} left in NoCFO (gone from Drive)"
        self.query_one("#summary", Static).update(summary)
        self.sub_title = "done — press q to exit"

    def _crash(self, msg: str) -> None:
        self.query_one("#summary", Static).update(f"[red]sync crashed: {msg}[/red]")
        self.sub_title = "crashed — press q to exit"
