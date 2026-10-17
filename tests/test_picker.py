"""Headless TUI smoke tests using Textual's run_test pilot."""

from __future__ import annotations

from pathlib import Path

import pytest

from nocfo_sync.nocfo import NocfoFolder
from nocfo_sync.picker import DriveFolderPicker, NoCfoFolderPicker


class FakeClient:
    def __init__(self):
        self.folders = {1: {"id": 1, "name": "Receipts", "parent": None}}
        self.created = []

    def list_root_folders(self):
        return [NocfoFolder(id=f["id"], name=f["name"], parent=f["parent"])
                for f in self.folders.values() if f["parent"] is None]

    def get_folder(self, folder_id):
        children = [{"id": c["id"], "name": c["name"]}
                    for c in self.folders.values() if c["parent"] == folder_id]
        f = self.folders[folder_id]
        return NocfoFolder(id=f["id"], name=f["name"], parent=f["parent"],
                           children=children)

    def create_folder(self, name, parent):
        new_id = max(self.folders) + 1
        self.folders[new_id] = {"id": new_id, "name": name, "parent": parent}
        self.created.append((name, parent))
        return NocfoFolder(id=new_id, name=name, parent=parent)


@pytest.mark.asyncio
async def test_drive_picker_lists_dirs(tmp_path: Path):
    (tmp_path / "Invoices").mkdir()
    (tmp_path / "Receipts").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    app = DriveFolderPicker(tmp_path)
    async with app.run_test() as pilot:
        tree = app.tree
        labels = [c.label.plain for c in tree.root.children]
        assert "Invoices" in labels
        assert "notes.txt" not in labels  # files are not shown
        await pilot.pause()


@pytest.mark.asyncio
async def test_drive_picker_preselect_expands(tmp_path: Path):
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    app = DriveFolderPicker(tmp_path, preselect=deep)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.tree.cursor_node is not None
        assert app.tree.cursor_node.data == deep


@pytest.mark.asyncio
async def test_nocfo_picker_selects_root_as_none(tmp_path: Path):
    app = NoCfoFolderPicker(FakeClient())  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.tree.root.data is None


@pytest.mark.asyncio
async def test_nocfo_picker_expands_children():
    app = NoCfoFolderPicker(FakeClient())  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        tree = app.tree
        root_child = tree.root.children[0]
        root_child.expand()
        await pilot.pause()
        assert root_child.data == 1


@pytest.mark.asyncio
async def test_nocfo_picker_root_selection_is_not_cancel():
    """Selecting the root node yields None but must not look like cancel."""
    app = NoCfoFolderPicker(FakeClient())  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_select()
        assert app.cancelled is False
        assert app.result is None


@pytest.mark.asyncio
async def test_nocfo_picker_cancel_sets_flag():
    app = NoCfoFolderPicker(FakeClient())  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_cancel()
        assert app.cancelled is True


@pytest.mark.asyncio
async def test_drive_picker_enter_selects_via_keypress(tmp_path: Path):
    """Regression: the Tree's own `enter` binding used to swallow the
    selection, so pressing enter did nothing."""
    (tmp_path / "Invoices").mkdir()
    (tmp_path / "Receipts").mkdir()
    app = DriveFolderPicker(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.tree.cursor_node.label.plain == "Invoices"
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == tmp_path / "Invoices"


@pytest.mark.asyncio
async def test_drive_picker_arrow_navigation(tmp_path: Path):
    (tmp_path / "Invoices" / "2026").mkdir(parents=True)
    app = DriveFolderPicker(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")  # -> Invoices
        await pilot.press("right")  # open Invoices
        await pilot.pause()
        assert app.tree.cursor_node.is_expanded
        assert [c.label.plain for c in app.tree.cursor_node.children] == ["2026"]
        await pilot.press("right")  # step into 2026
        await pilot.pause()
        assert app.tree.cursor_node.label.plain == "2026"
        await pilot.press("left")  # back to Invoices
        await pilot.pause()
        assert app.tree.cursor_node.label.plain == "Invoices"


@pytest.mark.asyncio
async def test_drive_picker_enter_chooses_child_folder(tmp_path: Path):
    target = tmp_path / "Invoices" / "2026"
    target.mkdir(parents=True)
    app = DriveFolderPicker(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down", "right", "right")  # into 2026
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == target


@pytest.mark.asyncio
async def test_nocfo_picker_enter_selects_via_keypress():
    app = NoCfoFolderPicker(FakeClient())  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")  # -> Receipts (id 1)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.cancelled is False
        assert app.result == 1
        assert app.result_name == "Receipts"


@pytest.mark.asyncio
async def test_nocfo_picker_enter_on_root_selects_root():
    app = NoCfoFolderPicker(FakeClient())  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # cursor on "NoCFO root"
        await pilot.pause()
        assert app.cancelled is False
        assert app.result is None
        assert app.result_name is None


@pytest.mark.asyncio
async def test_nocfo_picker_new_folder_via_screen():
    client = FakeClient()
    app = NoCfoFolderPicker(client)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.tree
        tree.select_node(tree.root.children[0])
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        from textual.widgets import Input

        inp = app.screen.query_one(Input)
        inp.value = "2026"
        await pilot.press("enter")
        await pilot.pause()
        assert ("2026", 1) in client.created
