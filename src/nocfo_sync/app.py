"""Unified ``nocfo-sync`` TUI.

One screen does the work: the **mappings list**.  Each row is a Google-Drive
source folder → NoCFO destination pair, shown by name on both sides, and the
whole workflow lives on that screen:

* ``s`` / ``S``  sync the highlighted mapping, or every mapping
* ``f`` / ``F``  force sync (re-upload everything) one, or all
* ``g`` / ``n``  change the highlighted mapping's Drive source / NoCFO destination
* ``t``          browse the NoCFO folder tree for the highlighted mapping
* ``a`` / ``d``  add / remove a mapping
* ``i`` / ``q``  status details / quit

First run (no config) walks you through the token, then a first mapping
(business → Drive → NoCFO).  The folder pickers and the progress views live in
:mod:`nocfo_sync.picker`; this module wires them together and owns the
config/state.

Mappings are not named by the user: a mapping is identified by its Drive
source folder, which is what the list shows.
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
    SyncAllProgressScreen,
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
# The mappings list — the app's single main screen
# --------------------------------------------------------------------------


class MappingsScreen(Screen[None]):
    """The list of Drive → NoCFO mappings and every action on them.

    Each action is bound to a key and can also be invoked through the posted
    :class:`ActionRequested` message, so the app owns the behaviour while the
    screen owns the keys.  Actions that target one mapping carry its row
    index; ``index`` is None for whole-list actions.
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
        Binding("S", "sync_all", "Sync all"),
        Binding("f", "force", "Force"),
        Binding("F", "force_all", "Force all"),
        Binding("g", "drive", "Drive"),
        Binding("n", "dest", "Destination"),
        Binding("t", "tree", "Tree"),
        Binding("a", "add", "Add"),
        Binding("d", "remove", "Remove"),
        Binding("i", "info", "Info"),
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

    def set_options(self, options: list[Option]) -> None:
        """Replace the rows, keeping the cursor where it was if possible."""
        listing = self.query_one(OptionList)
        previous = listing.highlighted
        listing.clear_options()
        listing.add_options(options)
        if options:
            listing.highlighted = min(previous or 0, len(options) - 1)

    def _highlighted(self) -> int | None:
        return self.query_one(OptionList).highlighted

    def _request(self, action: str, index: int | None = None) -> None:
        self.post_message(self.ActionRequested(action, index))

    def action_sync(self) -> None:
        self._request("sync", self._highlighted())

    def action_sync_all(self) -> None:
        self._request("sync_all")

    def action_force(self) -> None:
        self._request("force", self._highlighted())

    def action_force_all(self) -> None:
        self._request("force_all")

    def action_drive(self) -> None:
        self._request("drive", self._highlighted())

    def action_dest(self) -> None:
        self._request("dest", self._highlighted())

    def action_tree(self) -> None:
        self._request("tree", self._highlighted())

    def action_add(self) -> None:
        self._request("add")

    def action_remove(self) -> None:
        self._request("remove", self._highlighted())

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
        self._new_mapping: Mapping | None = None  # being built by the wizard
        self.last_stats: SyncStats | None = None
        self.user_email: str | None = None
        self._api_error: str | None = None
        self._resolving_names = False

    # -- helpers ---------------------------------------------------------

    def _client(self, slug: str) -> NoCfoClient:
        return self._make_client(slug)

    def _root_path(self, mapping: Mapping) -> Path | None:
        try:
            return mapping.root_path
        except ConfigError as e:
            self.notify(str(e), severity="error")
            return None

    def _row_label(self, mapping: Mapping, width: int) -> str:
        """One list row: ``<drive source>  →  <business> · <NoCFO folder>``."""
        plain = mapping.root_folder or "(no Drive source)"
        pad = " " * max(1, width - len(plain) + 2)
        drive = plain if mapping.root_folder else f"[red]{plain}[/red]"
        dest = (
            mapping.destination_label
            if mapping.destination_chosen
            else "[red](no destination)[/red]"
        )
        return f"{drive}{pad}[dim]→[/dim] {mapping.business_slug or '?'} › {dest}"

    def _mapping_options(self) -> list[Option]:
        labels = [m.root_folder or "(no Drive source)" for m in self.cfg.mappings]
        width = min(max((len(x) for x in labels), default=0), 40)
        return [
            Option(self._row_label(m, width), id=str(i))
            for i, m in enumerate(self.cfg.mappings)
        ]

    def _mappings_status(self) -> str:
        header = f"[b]NoCFO[/b]  {self.cfg.base_url}"
        if self.user_email:
            header += f"  [dim]({self.user_email})[/dim]"
        count = len(self.cfg.mappings)
        lines = [header, "No mappings yet." if count == 0 else f"[b]{count}[/b] mapping(s)"]
        if self.last_stats:
            s = self.last_stats
            lines.append(
                f"[dim]last run: {s.files_uploaded} uploaded, "
                f"{s.files_updated} updated, {s.files_skipped_unchanged} unchanged, "
                f"{len(s.errors)} errors[/dim]"
            )
        if self._api_error:
            lines.append(f"[yellow]NoCFO unreachable: {self._api_error}[/yellow]")
        incomplete = [
            m for m in self.cfg.mappings
            if not m.root_folder or not m.destination_chosen
        ]
        if incomplete:
            lines.append(
                f"[yellow]{len(incomplete)} mapping(s) still need a Drive source "
                "or a NoCFO destination[/yellow]"
            )
        return "\n".join(lines)

    def _info_text(self) -> str:
        cfg = self.cfg
        rows = [
            f"config:      {cfg.path or '(none — using defaults)'}",
            f"base_url:    {cfg.base_url}",
            f"user:        {self.user_email or '(unknown)'}",
            f"token:       {'set' if cfg.token else 'MISSING'}",
        ]
        if not cfg.mappings:
            rows.append("mappings:    (none)")
            return "\n".join(rows)
        rows.append("")
        for m in cfg.mappings:
            rows += [
                f"[b]{m.label}[/b]",
                f"  drive:       {m.root_folder or '(not selected)'}   ({m.mount})",
                f"  business:    {m.business_slug or '(unset)'}",
                f"  destination: {m.destination_label}",
                f"  state:       {cfg.state_path_for(m)}",
            ]
        return "\n".join(rows)

    # -- startup ---------------------------------------------------------

    def on_mount(self) -> None:
        self.title = "nocfo-sync"
        self.run_worker(self._bootstrap, thread=True, name="bootstrap")

    def _land(self) -> None:
        """Show the right starting screen for the current config."""
        if not self.cfg.mappings:
            self._add_mapping()
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

    # -- navigation ------------------------------------------------------

    def _home(self, screen: Screen) -> None:
        """Show the main screen.

        Textual can't ``switch_screen`` the base default screen (it has no
        result callback), so the first home screen is *pushed*; after that we
        swap the current top screen."""
        if len(self.screen_stack) > 1:
            self.switch_screen(screen)
        else:
            self.push_screen(screen)

    def _show_mappings(self) -> None:
        self._home(MappingsScreen(self._mappings_status(), self._mapping_options()))
        self._resolve_missing_names()

    def _refresh_mappings(self) -> None:
        screen = self.screen
        if isinstance(screen, MappingsScreen):
            screen.set_status(self._mappings_status())
            screen.set_options(self._mapping_options())

    # -- live destination-name lookup ------------------------------------

    def _resolve_missing_names(self) -> None:
        """Best-effort: fill in destination names we don't have cached.

        Hand-edited configs store only the folder id, so the list can't show a
        name for them.  Look each missing one up once, in the background, and
        cache it back into the config."""
        todo = [
            m for m in self.cfg.mappings
            if m.destination_chosen
            and m.destination_folder_id is not None
            and not m.destination_folder_name
        ]
        if not todo or self._resolving_names:
            return
        self._resolving_names = True
        self.run_worker(
            lambda: self._resolve_names_worker(todo), thread=True, name="resolve-names"
        )

    def _resolve_names_worker(self, todo: list[Mapping]) -> None:
        changed = False
        for m in todo:
            client = self._client(m.business_slug)
            try:
                name = client.get_folder(m.destination_folder_id).name
            except NoCfoError:
                continue
            finally:
                client.close()
            if name:
                m.destination_folder_name = name
                changed = True
        self.call_from_thread(self._names_resolved, changed)

    def _names_resolved(self, changed: bool) -> None:
        self._resolving_names = False
        if not changed:
            return
        save_config(self.cfg)
        self._refresh_mappings()

    # -- mapping actions -------------------------------------------------

    def on_mappings_screen_action_requested(
        self, message: MappingsScreen.ActionRequested
    ) -> None:
        action, index = message.action, message.index
        if action == "add":
            self._add_mapping()
            return
        if action == "sync_all":
            self._start_sync_all(force=False)
            return
        if action == "force_all":
            self._start_sync_all(force=True)
            return
        if action == "info":
            self.push_screen(InfoScreen(self._info_text()))
            return
        if index is None or not (0 <= index < len(self.cfg.mappings)):
            self.notify("no mapping selected", severity="warning")
            return
        mapping = self.cfg.mappings[index]
        if action == "sync":
            self._start_sync(mapping, force=False)
        elif action == "force":
            self._start_sync(mapping, force=True)
        elif action == "drive":
            self._change_drive(mapping)
        elif action == "dest":
            self._change_dest(mapping)
        elif action == "tree":
            self._show_tree(mapping)
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
        save_config(self.cfg)
        self.notify(f"removed mapping {mapping.label}")
        if not self.cfg.mappings:
            self.exit()
            return
        self._show_mappings()

    # -- syncing ---------------------------------------------------------

    def _prepare_sync(
        self, mapping: Mapping, force: bool, quiet: bool = False
    ) -> tuple[NoCfoClient, State, SyncEngine] | None:
        """Validate a mapping and build its client/state/engine, or None."""
        root = self._root_path(mapping)
        if root is None or not root.is_dir():
            if not quiet:
                self.notify(
                    f"{mapping.label}: choose a Google Drive source first (g)",
                    severity="warning",
                )
            return None
        if not mapping.destination_chosen:
            if not quiet:
                self.notify(
                    f"{mapping.label}: choose a NoCFO destination first (n)",
                    severity="warning",
                )
            return None
        client = self._client(mapping.business_slug)
        try:
            client.check()
        except NoCfoError as e:
            client.close()
            if not quiet:
                self.notify(
                    f"{mapping.label}: cannot reach NoCFO: {e}", severity="error"
                )
            return None
        state = State(self.cfg.state_path_for(mapping))
        engine = SyncEngine(
            client, state, root, mapping.destination_folder_id, force=force
        )
        return client, state, engine

    def _start_sync(self, mapping: Mapping, force: bool) -> None:
        prepared = self._prepare_sync(mapping, force)
        if prepared is None:
            return
        client, state, engine = prepared
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
        self._refresh_mappings()

    def _start_sync_all(self, force: bool) -> None:
        jobs: list[tuple[str, SyncEngine]] = []
        resources: list[tuple[NoCfoClient, State]] = []
        skipped: list[str] = []
        for mapping in self.cfg.mappings:
            prepared = self._prepare_sync(mapping, force, quiet=True)
            if prepared is None:
                skipped.append(mapping.label)
                continue
            client, state, engine = prepared
            jobs.append((mapping.label, engine))
            resources.append((client, state))
        if not jobs:
            for client, state in resources:
                client.close()
                state.close()
            self.notify(
                "nothing to sync — check each mapping's Drive source and "
                "destination",
                severity="warning",
                timeout=8,
            )
            return
        if skipped:
            self.notify("skipped: " + ", ".join(skipped), severity="warning", timeout=8)
        self.push_screen(
            SyncAllProgressScreen(jobs, force=force),
            lambda stats: self._sync_all_done(stats, resources),
        )

    def _sync_all_done(
        self, stats: SyncStats | None, resources: list[tuple[NoCfoClient, State]]
    ) -> None:
        self.last_stats = stats
        if stats is not None and stats.errors:
            for err in stats.errors[:8]:
                self.notify(err, severity="error", timeout=8)
        for client, state in resources:
            try:
                client.close()
            finally:
                state.close()
        self._refresh_mappings()

    # -- editing a mapping -----------------------------------------------

    def _change_drive(self, mapping: Mapping) -> None:
        self.push_screen(
            DrivePickerScreen(mapping.mount, self._root_path(mapping)),
            lambda picked: self._on_change_drive(mapping, picked),
        )

    def _on_change_drive(self, mapping: Mapping, picked: Path | None) -> None:
        if picked is None:
            return
        mapping.root_folder = picked.relative_to(mapping.mount.resolve()).as_posix()
        save_config(self.cfg)
        self.notify(f"Drive source: {mapping.root_folder}")
        self._refresh_mappings()

    def _change_dest(self, mapping: Mapping) -> None:
        client = self._client(mapping.business_slug)
        self.push_screen(
            NoCfoPickerScreen(client, mapping.destination_folder_id),
            lambda choice: self._on_change_dest(mapping, choice, client),
        )

    def _on_change_dest(
        self, mapping: Mapping, choice: NocfoChoice, client: NoCfoClient
    ) -> None:
        try:
            if choice.cancelled:
                return
            mapping.destination_folder_id = choice.folder_id
            mapping.destination_folder_name = choice.folder_name
            mapping.destination_chosen = True
            save_config(self.cfg)
            self.notify(f"destination → {mapping.destination_label}")
        finally:
            client.close()
        self._refresh_mappings()

    def _show_tree(self, mapping: Mapping) -> None:
        client = self._client(mapping.business_slug)
        self.push_screen(NoCfoTreeScreen(client), lambda _choice: client.close())

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
            self._new_mapping.destination_folder_name = choice.folder_name
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


def run_tui(cfg: Config) -> None:
    """Launch the unified TUI (used by the CLI when no subcommand is given)."""
    NocfoSyncApp(cfg).run()
