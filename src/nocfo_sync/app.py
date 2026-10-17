"""Unified ``nocfo-sync`` TUI.

One app for the whole workflow:

* managing several Drive-folder → NoCFO mappings (add / open / remove)
* first-run setup — token, then a first mapping (business → Drive → NoCFO)
* running an incremental sync, or a forced re-upload, for a mapping
* changing a mapping's roots and browsing the NoCFO folder tree

The folder pickers and the progress view live in :mod:`nocfo_sync.picker`;
this module wires them together and owns the config/state.

Mappings are not named by the user: a mapping is identified by its Drive
source folder, which is what the mapping list shows.

The menu deliberately uses letter shortcuts (``s``, ``f``, ``g``, ``n``,
``t``, ``m``, ``i``, ``q``) rather than numbers.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .config import Config, ConfigError, Mapping, save_config
from .nocfo import NoCfoClient, NoCfoError
from .picker import (
    DrivePickerScreen,
    NocfoChoice,
    NoCfoPickerScreen,
    NoCfoTreeScreen,
    ProgressScreen,
)
from .state import State
from .sync import SyncEngine, SyncStats


# --------------------------------------------------------------------------
# First-run / auth screens
# --------------------------------------------------------------------------


class TokenScreen(Screen[str | None]):
    """Ask for the NoCFO personal access token (first run)."""

    CSS = """
    Screen { align: center middle; }
    #box { width: 76; border: round $accent; padding: 1 2; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("NoCFO personal access token")
            yield Input(
                password=True, placeholder="paste token and press enter", id="token"
            )
            yield Label(
                "Saved to your local config.toml (git-ignored).", id="hint"
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            self.notify("token cannot be empty", severity="warning")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BusinessPickerScreen(Screen[str | None]):
    """Choose which accessible business a mapping syncs into."""

    CSS = """
    Screen { align: center middle; }
    #box { width: 76; border: round $accent; padding: 1 2; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, businesses: list[dict]):
        super().__init__()
        self.businesses = businesses

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("Select the business for this mapping")
            options = [
                Option(
                    f"{b.get('name', '?')}  [dim]({b.get('slug', '?')})[/dim]",
                    id=str(b.get("slug")),
                )
                for b in self.businesses
            ]
            yield OptionList(*options, id="biz")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AuthScreen(Screen[bool]):
    """The token was rejected or NoCFO is unreachable."""

    CSS = """
    Screen { align: center middle; }
    #box { width: 76; border: round $error; padding: 1 2; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("r", "retry", "Retry"),
        Binding("q,escape", "quit", "Quit"),
    ]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("[red]NoCFO refused the request[/red]")
            yield Label(self.message)
            yield Label(
                "Press [b]r[/b] to enter a different token, [b]q[/b] to quit.",
                id="hint",
            )
        yield Footer()

    def action_retry(self) -> None:
        self.dismiss(True)

    def action_quit(self) -> None:
        self.dismiss(False)


class ConfirmScreen(Screen[bool]):
    """Yes/no confirmation.  Enter does nothing, so ``y`` is deliberate."""

    CSS = """
    Screen { align: center middle; }
    #box { width: 76; border: round $accent; padding: 1 2; }
    #hint { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n,escape", "no", "No"),
    ]

    def __init__(self, title: str, detail: str = ""):
        super().__init__()
        self.title_text = title
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"[b]{self.title_text}[/b]")
            if self.detail:
                yield Label(self.detail)
            yield Label("[b]y[/b] yes     [b]n[/b] no", id="hint")
        yield Footer()

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class InfoScreen(Screen[None]):
    """Plain text panel (status / errors), dismissed with q, esc or enter."""

    CSS = "#info { padding: 1 2; }"

    BINDINGS = [Binding("q,escape,enter", "close", "Back")]

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(self.text, id="info")
        yield Footer()

    def action_close(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# Mapping list
# --------------------------------------------------------------------------


class MappingsScreen(Screen[None]):
    """The list of Drive → NoCFO mappings.

    ``enter`` opens the highlighted mapping; ``a`` adds one, ``d`` removes
    one, ``s`` syncs one.  Every action is also clickable in the list.
    """

    class ActionRequested(Message):
        def __init__(self, action: str, index: int | None = None):
            self.action = action
            self.index = index
            super().__init__()

    CSS = """
    #status { padding: 1 2; border: round $accent; margin: 1 2 0 2; height: auto; }
    #mappings { margin: 1 2; height: auto; }
    """

    BINDINGS = [
        Binding("s", "sync", "Sync"),
        Binding("a", "add", "Add"),
        Binding("d", "remove", "Remove"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, status_text: str, options: list[Option]):
        super().__init__()
        self.status_text = status_text
        self.options = options

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(self.status_text, id="status")
        yield OptionList(*self.options, id="mappings")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.query_one("#status", Static).update(text)

    def _highlighted(self) -> int | None:
        return self.query_one(OptionList).highlighted

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        index = int(event.option.id) if event.option.id is not None else None
        self.post_message(self.ActionRequested("open", index))

    def action_sync(self) -> None:
        self.post_message(self.ActionRequested("sync", self._highlighted()))

    def action_add(self) -> None:
        self.post_message(self.ActionRequested("add"))

    def action_remove(self) -> None:
        self.post_message(self.ActionRequested("remove", self._highlighted()))

    def action_quit(self) -> None:
        self.app.exit()


# --------------------------------------------------------------------------
# Menu
# --------------------------------------------------------------------------


class MenuScreen(Screen[None]):
    """Per-mapping home screen: current setup plus the action list.

    Keys are letters; each action is also clickable in the list.
    """

    class ActionRequested(Message):
        def __init__(self, action: str):
            self.action = action
            super().__init__()

    CSS = """
    #status { padding: 1 2; border: round $accent; margin: 1 2 0 2; height: auto; }
    #actions { margin: 1 2; height: auto; }
    """

    BINDINGS = [
        Binding("s", "sync", "Sync"),
        Binding("f", "force", "Force sync"),
        Binding("g", "drive", "Drive source"),
        Binding("n", "nocfo", "NoCFO dest"),
        Binding("t", "tree", "Folder tree"),
        Binding("m", "mappings", "Mappings"),
        Binding("i", "info", "Status"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, status_text: str, actions: list[tuple[str, str]]):
        super().__init__()
        self.status_text = status_text
        self.actions = actions

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(self.status_text, id="status")
        yield OptionList(
            *[Option(label, id=key) for key, label in self.actions], id="actions"
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.query_one("#status", Static).update(text)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.post_message(self.ActionRequested(event.option.id))

    def _request(self, action: str) -> None:
        self.post_message(self.ActionRequested(action))

    def action_sync(self) -> None:
        self._request("sync")

    def action_force(self) -> None:
        self._request("force")

    def action_drive(self) -> None:
        self._request("drive")

    def action_nocfo(self) -> None:
        self._request("nocfo")

    def action_tree(self) -> None:
        self._request("tree")

    def action_mappings(self) -> None:
        self._request("mappings")

    def action_info(self) -> None:
        self._request("info")

    def action_quit(self) -> None:
        self.app.exit()


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


class NocfoSyncApp(App[None]):
    """The single ``nocfo-sync`` interface."""

    CSS = """
    #status { padding: 1 2; border: round $accent; margin: 1 2 0 2; height: auto; }
    #actions { margin: 1 2; height: auto; }
    #info { padding: 1 2; }
    """

    def __init__(self, cfg: Config, client_factory=None):
        super().__init__()
        self.cfg = cfg
        # ``client_factory(slug) -> NoCfoClient`` — injectable for tests
        self._make_client = client_factory or (
            lambda slug: NoCfoClient(
                cfg.base_url, cfg.token, slug, idempotency=cfg.idempotency
            )
        )
        self.mapping: Mapping | None = None  # the mapping opened this session
        self._new_mapping: Mapping | None = None  # being built by the wizard
        self.last_stats: SyncStats | None = None
        self.user_email: str | None = None
        self._api_error: str | None = None

    # -- helpers ---------------------------------------------------------

    def _client(self, slug: str) -> NoCfoClient:
        return self._make_client(slug)

    def _banner(self) -> str | None:
        if self._api_error:
            return f"NoCFO unreachable: {self._api_error}"
        m = self.mapping
        if m is None:
            return None
        if not m.root_folder:
            return "Drive source not selected — press g to choose one."
        if not m.destination_chosen:
            return "NoCFO destination not selected — press n to choose one."
        return None

    def _tracked_files(self, mapping: Mapping) -> int:
        path = self.cfg.state_path_for(mapping)
        if not path.exists():
            return 0
        state = State(path)
        try:
            return len(state.all_file_relpaths())
        finally:
            state.close()

    def _mapping_options(self) -> list[Option]:
        options: list[Option] = []
        for i, m in enumerate(self.cfg.mappings):
            root = m.root_folder or "(no Drive source)"
            options.append(
                Option(
                    f"{root}  [dim]→ {m.business_slug or '?'} · "
                    f"{m.destination_label}[/dim]",
                    id=str(i),
                )
            )
        return options

    def _mappings_status(self) -> str:
        who = f"  [dim]({self.user_email})[/dim]" if self.user_email else ""
        lines = [f"[b]NoCFO[/b]  {self.cfg.base_url}{who}"]
        if not self.cfg.mappings:
            lines.append("No mappings yet.")
        else:
            lines.append(
                f"[b]{len(self.cfg.mappings)}[/b] mapping(s) — "
                "[b]enter[/b] open   [b]s[/b] sync   [b]a[/b] add   [b]d[/b] remove"
            )
        banner = self._banner()
        if banner:
            lines.append(f"[yellow]{banner}[/yellow]")
        return "\n".join(lines)

    def _status_text(self) -> str:
        cfg = self.cfg
        m = self.mapping
        who = f"  [dim]({self.user_email})[/dim]" if self.user_email else ""
        lines = [f"[b]NoCFO[/b]  {cfg.base_url}{who}"]
        if m is not None:
            lines.append(
                f"[b]Mapping[/b] {m.label}  "
                f"business=[b]{m.business_slug or '(unset)'}[/b]"
            )
            if m.root_folder:
                lines.append(
                    f"[b]Drive[/b]  {m.root_folder}  [dim]({m.mount})[/dim]"
                )
            else:
                lines.append("[b]Drive[/b]  [red]not selected[/red]")
            dest = m.destination_label
            if not m.destination_chosen:
                dest += "  [red]not selected[/red]"
            lines.append(f"[b]Dest[/b]   {dest}")
            lines.append(
                f"[b]State[/b]  {self._tracked_files(m)} files tracked  "
                f"[dim]({cfg.state_path_for(m)})[/dim]"
            )
        if self.last_stats:
            s = self.last_stats
            lines.append(
                f"[b]Last[/b]   {s.files_uploaded} uploaded, {s.files_updated} updated, "
                f"{s.files_skipped_unchanged} unchanged, {len(s.errors)} errors"
            )
        banner = self._banner()
        if banner:
            lines.append(f"[yellow]{banner}[/yellow]")
        return "\n".join(lines)

    def _info_text(self) -> str:
        cfg = self.cfg
        m = self.mapping
        rows = [
            f"config:      {cfg.path or '(none — using defaults)'}",
            f"base_url:    {cfg.base_url}",
            f"user:        {self.user_email or '(unknown)'}",
            f"token:       {'set' if cfg.token else 'MISSING'}",
        ]
        if m is not None:
            rows += [
                f"mapping:     {m.label}",
                f"state:       {cfg.state_path_for(m)}",
                f"business:    {m.business_slug or '(unset)'}",
                f"mount:       {m.mount}  "
                f"{'(exists)' if m.mount.is_dir() else '(MISSING)'}",
                f"drive root:  {m.root_folder or '(not selected)'}",
                f"destination: {m.destination_label}",
            ]
        return "\n".join(rows)

    def _actions(self) -> list[tuple[str, str]]:
        return [
            ("sync", "[b]s[/b]  Sync                [dim]upload new/changed files[/dim]"),
            ("force", "[b]f[/b]  Force sync          [dim]re-upload everything[/dim]"),
            ("drive", "[b]g[/b]  Google Drive source"),
            ("nocfo", "[b]n[/b]  NoCFO destination"),
            ("tree", "[b]t[/b]  Show NoCFO folder tree"),
            ("mappings", "[b]m[/b]  All mappings"),
            ("info", "[b]i[/b]  Status / details"),
            ("quit", "[b]q[/b]  Quit"),
        ]

    # -- startup ---------------------------------------------------------

    def on_mount(self) -> None:
        self.title = "nocfo-sync"
        self.run_worker(self._bootstrap, thread=True, name="bootstrap")

    def _land(self) -> None:
        """Show the right starting screen for the current config."""
        if not self.cfg.mappings:
            self._add_mapping()
        elif len(self.cfg.mappings) == 1:
            self._open_mapping(self.cfg.mappings[0])
        else:
            self._show_mappings()

    def _bootstrap(self) -> None:
        """Worker thread: validate credentials, then land on the right screen."""
        if not self.cfg.token:
            self.call_from_thread(self._ask_token)
            return
        client = self._make_client("")
        try:
            user = client.current_user()
        except NoCfoError as e:
            self.call_from_thread(self._auth_failed, str(e))
            return
        finally:
            client.close()
        self.call_from_thread(self._after_probe, user.get("email"))

    def _ask_token(self) -> None:
        self.push_screen(TokenScreen(), self._on_token)

    def _on_token(self, token: str | None) -> None:
        if not token:
            self.exit()
            return
        self.cfg.token = token
        save_config(self.cfg)
        self.notify("token saved")
        self.run_worker(self._bootstrap, thread=True, name="bootstrap")

    def _auth_failed(self, msg: str) -> None:
        self._api_error = msg
        if not self.cfg.mappings:
            self.push_screen(AuthScreen(msg), self._on_auth)
            return
        # there is something to show; let the user in, actions report errors
        self._land()

    def _on_auth(self, retry: bool) -> None:
        if retry:
            self.push_screen(TokenScreen(), self._on_token)
        else:
            self.exit()

    def _after_probe(self, email: str | None) -> None:
        self.user_email = email
        self._api_error = None
        self._land()

    def _fatal(self, msg: str) -> None:
        self.push_screen(
            InfoScreen(f"[red]{msg}[/red]\n\nPress q to quit."),
            lambda _r: self.exit(),
        )

    # -- mapping list ----------------------------------------------------

    # -- navigation ------------------------------------------------------

    def _home(self, screen: Screen) -> None:
        """Show a top-level screen (mapping list or a mapping's menu).

        Textual can't ``switch_screen`` the base default screen (it has no
        result callback), so the first home screen is *pushed*; after that we
        swap the current top screen."""
        if len(self.screen_stack) > 1:
            self.switch_screen(screen)
        else:
            self.push_screen(screen)

    def _show_mappings(self) -> None:
        self._home(MappingsScreen(self._mappings_status(), self._mapping_options()))

    def _open_mapping(self, mapping: Mapping) -> None:
        self.mapping = mapping
        self._home(MenuScreen(self._status_text(), self._actions()))

    def on_mappings_screen_action_requested(
        self, message: MappingsScreen.ActionRequested
    ) -> None:
        action, index = message.action, message.index
        if action == "add":
            self._add_mapping()
            return
        if index is None or not (0 <= index < len(self.cfg.mappings)):
            return
        mapping = self.cfg.mappings[index]
        if action == "open":
            self._open_mapping(mapping)
        elif action == "sync":
            self._open_mapping(mapping)
            self._start_sync(force=False)
        elif action == "remove":
            self._confirm_remove(mapping)

    def _confirm_remove(self, mapping: Mapping) -> None:
        self.push_screen(
            ConfirmScreen(
                "Remove this mapping?",
                f"{mapping.label} — its state file is kept on disk; nothing in "
                "NoCFO or Google Drive is deleted.",
            ),
            lambda ok: self._do_remove(mapping) if ok else None,
        )

    def _do_remove(self, mapping: Mapping) -> None:
        self.cfg.mappings.remove(mapping)
        if self.mapping is mapping:
            self.mapping = None
        save_config(self.cfg)
        self.notify(f"removed mapping {mapping.label}")
        if not self.cfg.mappings:
            self.exit()
            return
        self._land()

    # -- add-mapping wizard ----------------------------------------------

    def _add_mapping(self) -> None:
        self._new_mapping = Mapping()
        if self.cfg.mappings:
            # reuse an existing mount so the picker opens somewhere sensible
            self._new_mapping.mount = self.cfg.mappings[0].mount
        self.run_worker(self._load_businesses, thread=True, name="businesses")

    def _load_businesses(self) -> None:
        client = self._client("")
        try:
            businesses = client.list_businesses()
        except NoCfoError as e:
            self.call_from_thread(self._fatal, f"could not list businesses: {e}")
            return
        finally:
            client.close()
        self.call_from_thread(self._pick_business, businesses)

    def _pick_business(self, businesses: list[dict]) -> None:
        if not businesses:
            self._fatal("this token has no accessible businesses")
            return
        if len(businesses) == 1:
            self._on_business(businesses[0].get("slug"))
            return
        self.push_screen(BusinessPickerScreen(businesses), self._on_business)

    def _on_business(self, slug: str | None) -> None:
        if not slug or self._new_mapping is None:
            self._cancel_add()
            return
        self._new_mapping.business_slug = slug
        self.push_screen(
            DrivePickerScreen(self._new_mapping.mount, None), self._on_new_drive
        )

    def _on_new_drive(self, picked: Path | None) -> None:
        if picked is None or self._new_mapping is None:
            self._cancel_add()
            return
        self._new_mapping.root_folder = picked.relative_to(
            self._new_mapping.mount.resolve()
        ).as_posix()
        client = self._client(self._new_mapping.business_slug)
        self.push_screen(
            NoCfoPickerScreen(client),
            lambda choice: self._on_new_dest(choice, client),
        )

    def _on_new_dest(self, choice: NocfoChoice, client: NoCfoClient) -> None:
        try:
            if choice.cancelled or self._new_mapping is None:
                self._cancel_add()
                return
            self._new_mapping.destination_folder_id = choice.folder_id
            self._new_mapping.destination_chosen = True
            self.cfg.mappings.append(self._new_mapping)
            save_config(self.cfg)
            self.notify(f"added mapping: {self._new_mapping.label}")
            self._new_mapping = None
        finally:
            client.close()
        self._land()

    def _cancel_add(self) -> None:
        self._new_mapping = None
        if not self.cfg.mappings:
            self.exit()
            return
        self._land()

    # -- menu actions ----------------------------------------------------

    def on_menu_screen_action_requested(
        self, message: MenuScreen.ActionRequested
    ) -> None:
        handlers = {
            "sync": lambda: self._start_sync(force=False),
            "force": self._confirm_force,
            "drive": self._change_drive,
            "nocfo": self._change_dest,
            "tree": self._show_tree,
            "mappings": self._show_mappings,
            "info": lambda: self.push_screen(InfoScreen(self._info_text())),
            "quit": self.exit,
        }
        handler = handlers.get(message.action)
        if handler is not None:
            handler()

    def _root_path(self, mapping: Mapping) -> Path | None:
        try:
            return mapping.root_path
        except ConfigError as e:
            self.notify(str(e), severity="error")
            return None

    def _start_sync(self, force: bool) -> None:
        m = self.mapping
        if m is None:
            self.notify("no mapping selected", severity="warning")
            return
        root = self._root_path(m)
        if root is None or not root.is_dir():
            self.notify("select a Google Drive source first (press g)", severity="warning")
            return
        if not m.destination_chosen:
            self.notify("select a NoCFO destination first (press n)", severity="warning")
            return
        client = self._client(m.business_slug)
        try:
            client.check()
        except NoCfoError as e:
            client.close()
            self.notify(f"cannot reach NoCFO: {e}", severity="error")
            return
        state = State(self.cfg.state_path_for(m))
        engine = SyncEngine(
            client, state, root, m.destination_folder_id, force=force
        )
        self.push_screen(
            ProgressScreen(engine, force=force),
            lambda stats: self._sync_done(stats, client, state),
        )

    def _sync_done(
        self, stats: SyncStats | None, client: NoCfoClient, state: State
    ) -> None:
        self.last_stats = stats
        if stats is not None and stats.errors:
            for err in stats.errors[:5]:
                self.notify(err, severity="error", timeout=8)
        try:
            client.close()
        finally:
            state.close()
        self._refresh_menu()

    def _confirm_force(self) -> None:
        self.push_screen(
            ConfirmScreen(
                "Force sync?",
                "Every file is re-uploaded, ignoring the unchanged check. Old "
                "copies are deleted only when they are not attached to a document.",
            ),
            lambda ok: self._start_sync(force=True) if ok else None,
        )

    def _change_drive(self) -> None:
        m = self.mapping
        if m is None:
            return
        self.push_screen(
            DrivePickerScreen(m.mount, self._root_path(m)),
            self._on_change_drive,
        )

    def _on_change_drive(self, picked: Path | None) -> None:
        if picked is None or self.mapping is None:
            return
        m = self.mapping
        m.root_folder = picked.relative_to(m.mount.resolve()).as_posix()
        save_config(self.cfg)
        self.notify(f"Drive source: {m.root_folder}")
        self._refresh_menu()

    def _change_dest(self) -> None:
        m = self.mapping
        if m is None:
            return
        client = self._client(m.business_slug)
        self.push_screen(
            NoCfoPickerScreen(client, m.destination_folder_id),
            lambda choice: self._on_change_dest(choice, client),
        )

    def _on_change_dest(self, choice: NocfoChoice, client: NoCfoClient) -> None:
        try:
            if choice.cancelled or self.mapping is None:
                return
            self.mapping.destination_folder_id = choice.folder_id
            self.mapping.destination_chosen = True
            save_config(self.cfg)
            self.notify("NoCFO destination updated")
        finally:
            client.close()
        self._refresh_menu()

    def _show_tree(self) -> None:
        m = self.mapping
        if m is None:
            return
        client = self._client(m.business_slug)
        self.push_screen(NoCfoTreeScreen(client), lambda _choice: client.close())

    def _refresh_menu(self) -> None:
        screen = self.screen
        if isinstance(screen, MenuScreen):
            screen.set_status(self._status_text())


def run_tui(cfg: Config) -> None:
    """Launch the unified TUI (used by the CLI when no subcommand is given)."""
    NocfoSyncApp(cfg).run()
