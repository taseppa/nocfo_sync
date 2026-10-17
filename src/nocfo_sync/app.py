"""Unified ``nocfo-sync`` TUI.

One app for the whole workflow:

* first-run setup — token → business → Drive root → NoCFO destination
* running an incremental sync, or a forced re-upload
* changing either root and browsing the NoCFO folder tree

The folder pickers and the progress view live in :mod:`nocfo_sync.picker`;
this module wires them together and owns the config/state.

The menu deliberately uses letter shortcuts (``s``, ``f``, ``g``, ``n``,
``t``, ``i``, ``q``) rather than numbers.
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

from .config import Config, ConfigError, save_config
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
    """Choose which accessible business to sync into."""

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
            yield Label("Select the business to sync into")
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
# Menu
# --------------------------------------------------------------------------


class MenuScreen(Screen[None]):
    """Home screen: current setup plus the action list.

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
        self.last_stats: SyncStats | None = None
        self.user_email: str | None = None
        self._menu: MenuScreen | None = None
        self._api_ok = False
        self._api_error: str | None = None

    # -- helpers ---------------------------------------------------------

    def _client(self) -> NoCfoClient:
        return self._make_client(self.cfg.business_slug)

    def _banner(self) -> str | None:
        if self._api_error:
            return f"NoCFO unreachable: {self._api_error}"
        if not self.cfg.root_folder:
            return "Drive source not selected — press g to choose one."
        if not self.cfg.destination_chosen:
            return "NoCFO destination not selected — press n to choose one."
        return None

    def _tracked_files(self) -> int:
        path = self.cfg.state_path
        if not path.exists():
            return 0
        state = State(path)
        try:
            return len(state.all_file_relpaths())
        finally:
            state.close()

    def _status_text(self) -> str:
        cfg = self.cfg
        who = f"  [dim]({self.user_email})[/dim]" if self.user_email else ""
        lines = [
            f"[b]NoCFO[/b]  {cfg.base_url}  business=[b]{cfg.business_slug or '(unset)'}[/b]{who}"
        ]
        if cfg.root_folder:
            lines.append(f"[b]Drive[/b]  {cfg.root_folder}  [dim]({cfg.mount})[/dim]")
        else:
            lines.append("[b]Drive[/b]  [red]not selected[/red]")
        dest = (
            "NoCFO root"
            if cfg.destination_folder_id is None
            else f"folder id {cfg.destination_folder_id}"
        )
        if not cfg.destination_chosen:
            dest += "  [red]not selected[/red]"
        lines.append(f"[b]Dest[/b]   {dest}")
        lines.append(
            f"[b]State[/b]  {self._tracked_files()} files tracked  "
            f"[dim]({cfg.state_path})[/dim]"
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
        dest = (
            "NoCFO root"
            if cfg.destination_folder_id is None
            else str(cfg.destination_folder_id)
        )
        if not cfg.destination_chosen:
            dest += " (not selected)"
        return "\n".join(
            [
                f"config:      {cfg.path or '(none — using defaults)'}",
                f"state:       {cfg.state_path}",
                f"base_url:    {cfg.base_url}",
                f"business:    {cfg.business_slug or '(unset)'}",
                f"user:        {self.user_email or '(unknown)'}",
                f"token:       {'set' if cfg.token else 'MISSING'}",
                f"mount:       {cfg.mount}  "
                f"{'(exists)' if cfg.mount.is_dir() else '(MISSING)'}",
                f"drive root:  {cfg.root_folder or '(not selected)'}",
                f"destination: {dest}",
            ]
        )

    def _actions(self) -> list[tuple[str, str]]:
        return [
            ("sync", "[b]s[/b]  Sync                [dim]upload new/changed files[/dim]"),
            ("force", "[b]f[/b]  Force sync          [dim]re-upload everything[/dim]"),
            ("drive", "[b]g[/b]  Google Drive source"),
            ("nocfo", "[b]n[/b]  NoCFO destination"),
            ("tree", "[b]t[/b]  Show NoCFO folder tree"),
            ("info", "[b]i[/b]  Status / details"),
            ("quit", "[b]q[/b]  Quit"),
        ]

    # -- startup ---------------------------------------------------------

    def on_mount(self) -> None:
        self.title = "nocfo-sync"
        self.run_worker(self._bootstrap, thread=True, name="bootstrap")

    def _bootstrap(self) -> None:
        """Worker thread: validate credentials, then drive setup → menu."""
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
        self._api_ok = False
        self._api_error = msg
        if not self.cfg.business_slug:
            self.push_screen(AuthScreen(msg), self._on_auth)
            return
        # we know the business; let the user in, actions will report errors
        self._show_menu()

    def _on_auth(self, retry: bool) -> None:
        if retry:
            self.push_screen(TokenScreen(), self._on_token)
        else:
            self.exit()

    def _after_probe(self, email: str | None) -> None:
        self.user_email = email
        self._api_ok = True
        self._api_error = None
        if not self.cfg.business_slug:
            self.run_worker(self._load_businesses, thread=True, name="businesses")
            return
        self._setup_roots()

    def _load_businesses(self) -> None:
        client = self._make_client("")
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
        self.push_screen(BusinessPickerScreen(businesses), self._on_business)

    def _on_business(self, slug: str | None) -> None:
        if not slug:
            self.exit()
            return
        self.cfg.business_slug = slug
        save_config(self.cfg)
        self.notify(f"business set to {slug}")
        self._setup_roots()

    def _fatal(self, msg: str) -> None:
        self.push_screen(
            InfoScreen(f"[red]{msg}[/red]\n\nPress q to quit."),
            lambda _r: self.exit(),
        )

    # -- first-run wizard ------------------------------------------------

    def _setup_roots(self) -> None:
        if not self.cfg.root_folder:
            self.push_screen(
                DrivePickerScreen(self.cfg.mount, None), self._on_setup_drive
            )
            return
        if not self.cfg.destination_chosen and self._api_ok:
            client = self._client()
            self.push_screen(
                NoCfoPickerScreen(client),
                lambda choice: self._on_setup_dest(choice, client),
            )
            return
        self._show_menu()

    def _on_setup_drive(self, picked: Path | None) -> None:
        if picked is None:
            self._show_menu()
            return
        self.cfg.root_folder = picked.relative_to(self.cfg.mount.resolve()).as_posix()
        save_config(self.cfg)
        self.notify(f"Drive source: {self.cfg.root_folder}")
        self._setup_roots()

    def _on_setup_dest(self, choice: NocfoChoice, client: NoCfoClient) -> None:
        try:
            if choice.cancelled:
                self._show_menu()
                return
            self.cfg.destination_folder_id = choice.folder_id
            self.cfg.destination_chosen = True
            save_config(self.cfg)
            self.notify("NoCFO destination saved")
        finally:
            client.close()
        self._setup_roots()

    # -- menu ------------------------------------------------------------

    def _show_menu(self) -> None:
        if self._menu is not None:
            self._menu.set_status(self._status_text())
            return
        self._menu = MenuScreen(self._status_text(), self._actions())
        self.push_screen(self._menu)

    def _refresh_menu(self) -> None:
        if self._menu is not None:
            self._menu.set_status(self._status_text())

    def on_menu_screen_action_requested(
        self, message: MenuScreen.ActionRequested
    ) -> None:
        handlers = {
            "sync": lambda: self._start_sync(force=False),
            "force": self._confirm_force,
            "drive": self._change_drive,
            "nocfo": self._change_dest,
            "tree": self._show_tree,
            "info": lambda: self.push_screen(InfoScreen(self._info_text())),
            "quit": self.exit,
        }
        handler = handlers.get(message.action)
        if handler is not None:
            handler()

    # -- actions ---------------------------------------------------------

    def _root_path(self) -> Path | None:
        try:
            return self.cfg.root_path
        except ConfigError as e:
            self.notify(str(e), severity="error")
            return None

    def _start_sync(self, force: bool) -> None:
        root = self._root_path()
        if root is None or not root.is_dir():
            self.notify("select a Google Drive source first (press g)", severity="warning")
            return
        if not self.cfg.destination_chosen:
            self.notify("select a NoCFO destination first (press n)", severity="warning")
            return
        client = self._client()
        try:
            client.check()
        except NoCfoError as e:
            client.close()
            self.notify(f"cannot reach NoCFO: {e}", severity="error")
            return
        state = State(self.cfg.state_path)
        engine = SyncEngine(
            client, state, root, self.cfg.destination_folder_id, force=force
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
        self.push_screen(
            DrivePickerScreen(self.cfg.mount, self._root_path()),
            self._on_change_drive,
        )

    def _on_change_drive(self, picked: Path | None) -> None:
        if picked is None:
            return
        self.cfg.root_folder = picked.relative_to(self.cfg.mount.resolve()).as_posix()
        save_config(self.cfg)
        self.notify(f"Drive source: {self.cfg.root_folder}")
        self._refresh_menu()

    def _change_dest(self) -> None:
        client = self._client()
        self.push_screen(
            NoCfoPickerScreen(client, self.cfg.destination_folder_id),
            lambda choice: self._on_change_dest(choice, client),
        )

    def _on_change_dest(self, choice: NocfoChoice, client: NoCfoClient) -> None:
        try:
            if choice.cancelled:
                return
            self.cfg.destination_folder_id = choice.folder_id
            self.cfg.destination_chosen = True
            save_config(self.cfg)
            self.notify("NoCFO destination updated")
        finally:
            client.close()
        self._refresh_menu()

    def _show_tree(self) -> None:
        client = self._client()
        self.push_screen(NoCfoTreeScreen(client), lambda _choice: client.close())


def run_tui(cfg: Config) -> None:
    """Launch the unified TUI (used by the CLI when no subcommand is given)."""
    NocfoSyncApp(cfg).run()
