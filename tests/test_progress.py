"""Headless test of the sync progress TUI."""

from __future__ import annotations

from pathlib import Path

import pytest

from nocfo_sync.picker import SyncProgressApp
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
