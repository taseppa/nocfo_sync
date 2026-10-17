"""Headless test of the sync progress TUI."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App

from nocfo_sync.picker import SyncAllProgressScreen, SyncProgressApp
from nocfo_sync.state import State
from nocfo_sync.sync import SyncEngine
from tests.test_sync import FakeClient


@pytest.mark.asyncio
async def test_progress_app_runs_sync(tmp_path: Path):
    drive = tmp_path / "drive"
    (drive / "Invoices").mkdir(parents=True)
    (drive / "Invoices" / "a.txt").write_text("a")
    (drive / "Invoices" / "b.txt").write_text("b")

    client = FakeClient()
    state = State(tmp_path / "state.db")
    engine = SyncEngine(client, state, drive, None)

    app = SyncProgressApp(engine)
    async with app.run_test() as pilot:
        # wait for the worker thread to finish
        for _ in range(50):
            await pilot.pause(0.05)
            if app.stats is not None:
                break
        assert app.stats is not None
        assert app.stats.files_uploaded == 2
        assert app.stats.folders_created == 1
        assert len(client.files) == 2
    state.close()


class _MultiApp(App[None]):
    """Tiny harness that pushes a SyncAllProgressScreen (no full nocfo app)."""

    def __init__(self, jobs):
        super().__init__()
        self._screen = SyncAllProgressScreen(jobs)

    @property
    def stats(self):
        return self._screen.stats

    def on_mount(self) -> None:
        self.push_screen(self._screen)


@pytest.mark.asyncio
async def test_sync_all_runs_every_engine_and_totals(tmp_path: Path):
    drive = tmp_path / "drive"
    (drive / "Invoices").mkdir(parents=True)
    (drive / "Invoices" / "a.txt").write_text("a")
    (drive / "Receipts").mkdir()
    (drive / "Receipts" / "b.txt").write_text("b")
    (drive / "Receipts" / "c.txt").write_text("c")

    c1, s1 = FakeClient(), State(tmp_path / "s1.db")
    c2, s2 = FakeClient(), State(tmp_path / "s2.db")
    e1 = SyncEngine(c1, s1, drive / "Invoices", None)
    e2 = SyncEngine(c2, s2, drive / "Receipts", None)

    app = _MultiApp([("Invoices", e1), ("Receipts", e2)])
    async with app.run_test() as pilot:
        for _ in range(60):
            await pilot.pause(0.05)
            if app.stats is not None:
                break
        assert app.stats is not None
        assert app.stats.files_uploaded == 3  # 1 + 2 across both mappings
        assert app.stats.folders_created == 0
    s1.close()
    s2.close()


@pytest.mark.asyncio
async def test_sync_all_keeps_going_after_a_failure(tmp_path: Path):
    """A mapping that crashes is reported but does not stop the rest."""
    drive = tmp_path / "drive"
    (drive / "a").mkdir(parents=True)
    (drive / "a" / "x.txt").write_text("x")
    (drive / "b").mkdir()
    (drive / "b" / "y.txt").write_text("y")

    class BoomClient(FakeClient):
        def upload_file(self, *a, **k):
            raise RuntimeError("model down")

    bad, s_bad = BoomClient(), State(tmp_path / "bad.db")
    good, s_good = FakeClient(), State(tmp_path / "good.db")
    e_bad = SyncEngine(bad, s_bad, drive / "a", None)
    e_good = SyncEngine(good, s_good, drive / "b", None)

    app = _MultiApp([("a", e_bad), ("b", e_good)])
    async with app.run_test() as pilot:
        for _ in range(60):
            await pilot.pause(0.05)
            if app.stats is not None:
                break
        assert app.stats is not None
        # the second mapping still uploaded its file
        assert app.stats.files_uploaded == 1
        assert len(good.files) == 1
    s_bad.close()
    s_good.close()
