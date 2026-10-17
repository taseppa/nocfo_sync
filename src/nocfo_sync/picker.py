"""Textual TUI building blocks: folder pickers and sync progress.

Each piece is a ``Screen`` so it can live inside the unified ``nocfo-sync``
app (see ``app.py``).  Thin ``App`` wrappers are kept for the standalone CLI
commands (``pick-drive-root``, ``pick-destination``, ``sync``) and their
tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
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


class FolderTree(Tree):
    """A Tree with predictable folder-picker keys.

    Textual's stock ``Tree`` binds ``enter`` to expand/collapse and does not
    bind the left/right arrows at all, which makes it impossible to both
    navigate *into* folders and *choose* one.  Here:

    - up/down   move the cursor
    - right     open the folder (and step into its first child)
    - left      close the folder (or jump to the parent)
    - space     toggle open/closed
    - enter     choose the folder under the cursor

    The ``enter`` binding lives on the tree (not the app) so it can't shadow
    the new-folder input screen's own ``enter``.
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("right", "open_folder", "Open", show=True),
        Binding("left", "close_folder", "Close", show=True),
        Binding("space", "toggle_node", "Toggle", show=False),
        Binding("enter", "choose", "Select", show=True),
    ]

    on_choose: Callable[[], None] | None = None

    def action_open_folder(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and not node.is_expanded:
            node.expand()
        elif node.children and not _is_placeholder(node):
            # already open — step into the first child
            self.move_cursor(node.children[0])

    def action_close_folder(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.is_expanded and node.children:
            node.collapse()
        elif node.parent is not None:
            self.move_cursor(node.parent)

    def action_choose(self) -> None:
        if self.on_choose is not None:
            self.on_choose()


# --------------------------------------------------------------------------
# Drive folder picker (local filesystem)
# --------------------------------------------------------------------------


class DrivePickerScreen(Screen[Path | None]):
    """Browse the local Drive mount, select a folder to sync from.

    Dismisses with the chosen path, or None when cancelled.
    """

    CSS = """
    #tree { height: 1fr; }
    #hint { padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        ("q,escape", "cancel", "Cancel"),
    ]

    def __init__(self, mount: Path, preselect: Path | None = None):
        super().__init__()
        self.mount_path = mount.resolve()
        self.preselect = preselect.resolve() if preselect else None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        if not self.mount_path.is_dir():
            yield Static(
                "[red]Drive mount not found[/red] at "
                f"{self.mount_path}\n"
                "Is Google Drive for Desktop installed, running and synced?"
            )
        yield Static(f"Select a Google Drive folder  [dim]({self.mount_path})[/dim]")
        tree: FolderTree[Path] = FolderTree("My Drive", data=self.mount_path)
        tree.on_choose = self.action_select
        tree.root.expand()
        self._populate(tree.root)
        yield tree
        yield Label(
            "→ open   ← close   enter = sync from here   q/esc = cancel", id="hint"
        )
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
    def tree(self) -> FolderTree[Path]:
        return self.query_one(FolderTree)

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

    def current_choice(self) -> Path | None | bool:
        """The path under the cursor, or ``None`` if the cursor is invalid.

        Returns ``False`` to mean "no valid choice" so callers can tell an
        empty cursor apart from a cancelled pick.
        """
        node = self.tree.cursor_node
        if node is None or node.data is None:
            return False
        return node.data

    def action_select(self) -> None:
        choice = self.current_choice()
        if choice is not False:
            self.dismiss(choice)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DriveFolderPicker(App[Path | None]):
    """Standalone wrapper around :class:`DrivePickerScreen` (CLI + tests)."""

    def __init__(self, mount: Path, preselect: Path | None = None):
        super().__init__()
        self.result: Path | None = None
        self._screen = DrivePickerScreen(mount, preselect)

    def on_mount(self) -> None:
        self.push_screen(self._screen, self._chosen)

    def _chosen(self, result: Path | None) -> None:
        self.result = result
        self.exit(result)

    @property
    def tree(self) -> FolderTree[Path]:
        return self._screen.tree

    def action_select(self) -> None:
        choice = self._screen.current_choice()
        if choice is not False:
            self.result = choice
            self._screen.dismiss(choice)

    def action_cancel(self) -> None:
        self.result = None
        self._screen.dismiss(None)


# --------------------------------------------------------------------------
# NoCFO destination picker (API-backed)
# --------------------------------------------------------------------------


@dataclass
class NocfoChoice:
    """Result of the NoCFO picker.

    ``folder_id`` is None both for "the NoCFO root" and for a cancel, so
    ``cancelled`` carries the distinction (selecting the root is a real
    choice, not a cancel).  ``folder_name`` is the human label we can show in
    the UI without another API call; it is None for the root.
    """

    folder_id: int | None = None
    folder_name: str | None = None
    cancelled: bool = False


class NoCfoPickerScreen(Screen[NocfoChoice]):
    """Browse NoCFO folders.

    With ``choose=True`` (the default) ``enter`` dismisses with the folder
    under the cursor and ``n`` creates a new one.  With ``choose=False`` the
    tree is read-only, which is how the TUI shows the whole folder tree.
    """

    CSS = """
    #tree { height: 1fr; }
    #hint { padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        ("n", "new_folder", "New folder"),
        ("q,escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        client: NoCfoClient,
        preselect: int | None = None,
        choose: bool = True,
    ):
        super().__init__()
        self.client = client
        self.preselect = preselect
        self.choose = choose
        self._new_folder_parent: TreeNode[int | None] | None = None
        # folder id -> display name, filled in as the tree is populated so we
        # can report the chosen folder's name to the caller for free.
        self._names: dict[int, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(
            "Select the NoCFO destination folder"
            if self.choose
            else "NoCFO folders"
        )
        tree: FolderTree[int | None] = FolderTree("NoCFO root", data=None)
        tree.on_choose = self.action_select if self.choose else None
        tree.root.expand()
        self._populate_root(tree.root)
        yield tree
        if self.choose:
            hint = "→ open   ← close   enter = sync into here   n = new folder   q/esc = cancel"
        else:
            hint = "→ open   ← close   q/esc = back"
        yield Label(hint, id="hint")
        yield Footer()

    @property
    def tree(self) -> FolderTree[int | None]:
        return self.query_one(FolderTree)

    def _populate_root(self, node: TreeNode[int | None]) -> None:
        node.remove_children()
        try:
            folders = self.client.list_root_folders()
        except NoCfoError as e:
            node.add(f"[red]error: {e}[/red]")
            node.allow_expand = False
            return
        for f in folders:
            self._names[f.id] = f.name
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
            self._names[c["id"]] = c["name"]
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

    def refresh_node(self, node: TreeNode[int | None]) -> None:
        """Re-read a node's children (used after creating a folder)."""
        if node is self.tree.root:
            self._populate_root(node)
        else:
            self._populate_children(node)

    def current_choice(self) -> NocfoChoice | None:
        node = self.tree.cursor_node
        if node is None:
            return None
        name = None if node.data is None else self._names.get(node.data)
        return NocfoChoice(folder_id=node.data, folder_name=name, cancelled=False)

    def action_select(self) -> None:
        choice = self.current_choice()
        if choice is not None:
            self.dismiss(choice)

    def action_cancel(self) -> None:
        self.dismiss(NocfoChoice(folder_id=None, folder_name=None, cancelled=True))

    def action_new_folder(self) -> None:
        if not self.choose:
            return
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
            self.refresh_node(parent)
            parent.expand()
            self.notify(f"created folder {name!r} ({created.id})")

        self.app.push_screen(NewFolderScreen(), handle)


class NoCfoTreeScreen(NoCfoPickerScreen):
    """Read-only view of the whole NoCFO folder tree (the TUI's 'show tree')."""

    BINDINGS = [("q,escape", "back", "Back")]

    def __init__(self, client: NoCfoClient):
        super().__init__(client, choose=False)

    def action_new_folder(self) -> None:  # `n` is not bound here
        return

    def action_back(self) -> None:
        self.dismiss(NocfoChoice(folder_id=None, folder_name=None, cancelled=True))


class NoCfoFolderPicker(App[int | None]):
    """Standalone wrapper around :class:`NoCfoPickerScreen` (CLI + tests)."""

    def __init__(self, client: NoCfoClient, preselect: int | None = None):
        super().__init__()
        self.result: int | None = None
        self.result_name: str | None = None
        self.cancelled = True
        self._screen = NoCfoPickerScreen(client, preselect)

    def on_mount(self) -> None:
        self.push_screen(self._screen, self._chosen)

    def _chosen(self, choice: NocfoChoice) -> None:
        self.result = choice.folder_id
        self.result_name = choice.folder_name
        self.cancelled = choice.cancelled
        self.exit(self.result)

    @property
    def tree(self) -> FolderTree[int | None]:
        return self._screen.tree

    def action_select(self) -> None:
        choice = self._screen.current_choice()
        if choice is None:
            return
        self.result = choice.folder_id
        self.result_name = choice.folder_name
        self.cancelled = choice.cancelled
        self._screen.dismiss(choice)

    def action_cancel(self) -> None:
        self.result = None
        self.result_name = None
        self.cancelled = True
        self._screen.dismiss(NocfoChoice(folder_id=None, folder_name=None, cancelled=True))


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


def _summary_text(s: SyncStats) -> str:
    """One-line human summary of a sync run (shared by the progress screens)."""
    text = (
        f"done: {s.files_uploaded} uploaded, {s.files_updated} updated, "
        f"{s.files_skipped_unchanged} unchanged, "
        f"{s.folders_created} folders created, {s.folders_reused} reused, "
        f"{len(s.errors)} errors"
    )
    if s.files_skipped_native:
        text += f", {s.files_skipped_native} Google-native skipped"
    if s.files_left_behind:
        text += f", {s.files_left_behind} left in NoCFO (gone from Drive)"
    return text


def _accumulate(total: SyncStats, part: SyncStats, label: str) -> None:
    """Fold one run's stats into a running total (errors get a prefix)."""
    total.folders_created += part.folders_created
    total.folders_reused += part.folders_reused
    total.files_uploaded += part.files_uploaded
    total.files_updated += part.files_updated
    total.files_skipped_unchanged += part.files_skipped_unchanged
    total.files_skipped_native += part.files_skipped_native
    total.files_failed += part.files_failed
    total.files_left_behind += part.files_left_behind
    total.errors.extend(f"[{label}] {e}" for e in part.errors)


class ProgressScreen(Screen[SyncStats | None]):
    """Progress bar + live log while SyncEngine runs in a worker thread.

    Dismisses with the stats once the run finishes.  ``q``/``esc`` only
    return once the run is over, so a sync can't be abandoned halfway.
    """

    CSS = """
    #log { height: 1fr; border: round $accent; margin: 0 1; padding: 0 1; }
    #summary { padding: 0 1; color: $success; }
    #bar { height: 1; margin: 0 1; }
    """

    BINDINGS = [
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self, engine: SyncEngine, force: bool = False):
        super().__init__()
        self.engine = engine
        self.stats: SyncStats | None = None
        self.finished = False
        self.force = force
        self._log_lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield ProgressBar(show_eta=True, id="bar")
        yield Static(id="summary")
        yield Static(id="log")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "force sync — re-uploading everything" if self.force else "syncing…"
        self.run_worker(self._run, thread=True)

    def _run(self) -> None:
        def progress(phase: str, msg: str) -> None:
            self.app.call_from_thread(self._append_log, msg)

        self.engine.progress = progress
        try:
            stats = self.engine.sync()
        except Exception as e:  # surface crashes in the UI, not just stderr
            self.app.call_from_thread(self._crash, str(e))
            return
        self.app.call_from_thread(self._done, stats)

    def _append_log(self, msg: str) -> None:
        self._log_lines.append(msg)
        del self._log_lines[:-200]
        self.query_one("#log", Static).update("\n".join(self._log_lines))

    def _done(self, stats: SyncStats) -> None:
        self.stats = stats
        self.finished = True
        self.query_one("#bar", ProgressBar).update(total=100, progress=100)
        self.query_one("#summary", Static).update(_summary_text(stats))
        self.sub_title = "done — press q to go back"

    def _crash(self, msg: str) -> None:
        self.finished = True
        self.query_one("#summary", Static).update(f"[red]sync crashed: {msg}[/red]")
        self.sub_title = "crashed — press q to go back"

    def action_back(self) -> None:
        if not self.finished:
            self.notify("sync is still running — wait for it to finish", severity="warning")
            return
        self.dismiss(self.stats)


class SyncProgressApp(App[SyncStats | None]):
    """Standalone wrapper around :class:`ProgressScreen` (CLI + tests)."""

    def __init__(self, engine: SyncEngine):
        super().__init__()
        self._screen = ProgressScreen(engine)

    @property
    def engine(self) -> SyncEngine:
        return self._screen.engine

    @property
    def stats(self) -> SyncStats | None:
        return self._screen.stats

    def on_mount(self) -> None:
        self.push_screen(self._screen, self.exit)


class SyncAllProgressScreen(Screen[SyncStats | None]):
    """Run several mappings' syncs one after another in a single view.

    ``jobs`` is a list of ``(label, SyncEngine)``.  The engines run in a
    worker thread; each one's progress lines are prefixed with its label and
    the final stats are the sum of every run.  Dismisses with those totals.
    """

    CSS = """
    #log { height: 1fr; border: round $accent; margin: 0 1; padding: 0 1; }
    #summary { padding: 0 1; color: $success; }
    #bar { height: 1; margin: 0 1; }
    """

    BINDINGS = [
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self, jobs: list[tuple[str, SyncEngine]], force: bool = False):
        super().__init__()
        self.jobs = jobs
        self.stats: SyncStats | None = None
        self.finished = False
        self.force = force
        self._log_lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield ProgressBar(total=max(1, len(self.jobs)), show_eta=False, id="bar")
        yield Static(id="summary")
        yield Static(id="log")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = (
            "force sync — re-uploading everything"
            if self.force
            else "syncing all mappings…"
        )
        self.run_worker(self._run, thread=True)

    def _run(self) -> None:
        total = SyncStats()
        for i, (label, engine) in enumerate(self.jobs, start=1):
            self.app.call_from_thread(self._append_log, f"── {label} ──")
            engine.progress = (
                lambda phase, msg, label=label: self.app.call_from_thread(
                    self._append_log, f"[{label}] {msg}"
                )
            )
            try:
                part = engine.sync()
            except Exception as e:  # surface crashes per mapping, keep going
                self.app.call_from_thread(
                    self._append_log, f"[{label}] crashed: {e}"
                )
                total.errors.append(f"[{label}] {e}")
                self.app.call_from_thread(self._step, i)
                continue
            _accumulate(total, part, label)
            self.app.call_from_thread(self._step, i)
        self.app.call_from_thread(self._done, total)

    def _append_log(self, msg: str) -> None:
        self._log_lines.append(msg)
        del self._log_lines[:-300]
        self.query_one("#log", Static).update("\n".join(self._log_lines))

    def _step(self, done: int) -> None:
        self.query_one("#bar", ProgressBar).update(
            total=max(1, len(self.jobs)), progress=done
        )

    def _done(self, stats: SyncStats) -> None:
        self.stats = stats
        self.finished = True
        self.query_one("#bar", ProgressBar).update(
            total=max(1, len(self.jobs)), progress=len(self.jobs)
        )
        self.query_one("#summary", Static).update(_summary_text(stats))
        self.sub_title = "done — press q to go back"

    def action_back(self) -> None:
        if not self.finished:
            self.notify(
                "sync is still running — wait for it to finish", severity="warning"
            )
            return
        self.dismiss(self.stats)
