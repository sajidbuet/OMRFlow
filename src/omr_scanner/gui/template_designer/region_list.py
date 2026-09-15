"""The left-hand list of every marker and region in the template.

Purpose:
    Give a text-based way to select, show/hide, lock/unlock and delete
    anything on the canvas - useful in its own right (finding "Question set"
    among forty regions by scrolling a canvas is slower than reading a list),
    and essential once regions overlap enough that clicking the right one on
    the canvas becomes fiddly.

Responsibilities:
    * Render a flat, grouped list (Markers, Orientation, Regions) from plain
      data the page supplies.
    * Emit signals when the user selects, toggles visibility/lock, renames, or
      asks to delete/duplicate an entry - the page performs the action.

What does NOT belong here:
    * Any decision about *what* an id means to the template; this widget only
      ever sees the strings and flags it is given.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.icons import load_icon

ITEM_ID_ROLE = Qt.ItemDataRole.UserRole
ITEM_KIND_ROLE = Qt.ItemDataRole.UserRole + 1


@dataclass(frozen=True, slots=True)
class RegionListEntry:
    """One row of the region list.

    Attributes:
        item_id: Same id the canvas uses for this item.
        kind: ``"marker"``, ``"orientation"`` or ``"zone"``.
        label: Display text.
        visible: Whether the region is currently shown on the canvas.
        locked: Whether the region is currently locked against editing.
    """

    item_id: str
    kind: str
    label: str
    visible: bool = True
    locked: bool = False


class RegionListPanel(QWidget):
    """Grouped, checkable list of everything the template currently defines.

    Signals:
        selection_requested: ``(item_id)`` - the user clicked a row.
        visibility_toggled: ``(item_id, visible)``.
        lock_toggled: ``(item_id, locked)``.
        delete_requested: ``(item_id)`` - from the row's context menu.
        duplicate_requested: ``(item_id)``.
        rename_requested: ``(item_id, new_label)`` - after inline editing.
    """

    selection_requested = Signal(str)
    visibility_toggled = Signal(str, bool)
    lock_toggled = Signal(str, bool)
    delete_requested = Signal(str)
    duplicate_requested = Signal(str)
    rename_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QLabel("Regions")
        header_font = header.font()
        header_font.setBold(True)
        header.setFont(header_font)
        layout.addWidget(header)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.tree, stretch=1)

        buttons = QHBoxLayout()
        self.duplicate_button = QPushButton(load_icon("copy"), "Duplicate")
        self.duplicate_button.setToolTip("Duplicate the selected region (Ctrl+D)")
        self.duplicate_button.clicked.connect(self._emit_duplicate)
        self.delete_button = QPushButton(load_icon("trash"), "Delete")
        self.delete_button.setToolTip("Delete the selected region (Delete)")
        # Icon for recognisability, but no colour/size treatment that would
        # make this destructive action visually dominant over its neighbour.
        self.delete_button.clicked.connect(self._emit_delete)
        buttons.addWidget(self.duplicate_button)
        buttons.addWidget(self.delete_button)
        layout.addLayout(buttons)

        self._groups: dict[str, QTreeWidgetItem] = {}
        self._suppress_signals = False

    def set_entries(self, entries: list[RegionListEntry]) -> None:
        """Replace the whole list from scratch, preserving no selection.

        The caller (the page) re-selects afterwards if it needs to; keeping
        this method a plain rebuild - like the canvas's own region rebuild -
        means the panel can never drift out of sync with the document.
        """
        self._suppress_signals = True
        try:
            self.tree.clear()
            self._groups = {}
            order = [("marker", "Registration markers"), ("orientation", "Orientation"),
                     ("zone", "Regions")]
            for kind, title in order:
                group = QTreeWidgetItem([title])
                group.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.tree.addTopLevelItem(group)
                self._groups[kind] = group

            for entry in entries:
                target_group = self._groups.get(entry.kind)
                if target_group is None:
                    continue
                row = QTreeWidgetItem([entry.label])
                row.setData(0, ITEM_ID_ROLE, entry.item_id)
                row.setData(0, ITEM_KIND_ROLE, entry.kind)
                row.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                row.setCheckState(
                    0, Qt.CheckState.Checked if entry.visible else Qt.CheckState.Unchecked
                )
                if entry.locked:
                    row.setText(0, f"{entry.label}  \U0001f512")
                target_group.addChild(row)

            self.tree.expandAll()
        finally:
            self._suppress_signals = False

    def select(self, item_id: str | None) -> None:
        """Programmatically select the row for ``item_id`` (or clear selection)."""
        self._suppress_signals = True
        try:
            self.tree.clearSelection()
            if item_id is None:
                return
            for group in self._groups.values():
                for index in range(group.childCount()):
                    row = group.child(index)
                    if row is not None and row.data(0, ITEM_ID_ROLE) == item_id:
                        row.setSelected(True)
                        self.tree.scrollToItem(row)
                        return
        finally:
            self._suppress_signals = False

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------
    def _on_selection_changed(self) -> None:
        if self._suppress_signals:
            return
        selected = self.tree.selectedItems()
        if selected:
            item_id = selected[0].data(0, ITEM_ID_ROLE)
            if item_id is not None:
                self.selection_requested.emit(item_id)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._suppress_signals or column != 0:
            return
        item_id = item.data(0, ITEM_ID_ROLE)
        if item_id is None:
            return
        self.visibility_toggled.emit(item_id, item.checkState(0) == Qt.CheckState.Checked)

    def _on_context_menu(self, position: QPoint) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        item_id = item.data(0, ITEM_ID_ROLE)
        kind = item.data(0, ITEM_KIND_ROLE)
        # Only regions (not markers) support these actions: a marker is
        # deleted implicitly by placing another one, never removed outright.
        if item_id is None or kind != "zone":
            return

        menu = QMenu(self)
        rename_action = menu.addAction("Rename...")
        duplicate_action = menu.addAction("Duplicate")
        delete_action = menu.addAction("Delete")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(position))
        if chosen is rename_action:
            self._prompt_rename(item_id, item.text(0))
        elif chosen is duplicate_action:
            self.duplicate_requested.emit(item_id)
        elif chosen is delete_action:
            self.delete_requested.emit(item_id)

    def _prompt_rename(self, item_id: str, current_label: str) -> None:
        new_label, accepted = QInputDialog.getText(
            self, "Rename region", "Name:", text=current_label
        )
        if accepted and new_label.strip():
            self.rename_requested.emit(item_id, new_label.strip())

    def _current_zone_id(self) -> str | None:
        item = self.tree.currentItem()
        if item is None or item.data(0, ITEM_KIND_ROLE) != "zone":
            return None
        item_id = item.data(0, ITEM_ID_ROLE)
        return item_id if isinstance(item_id, str) else None

    def _emit_duplicate(self) -> None:
        item_id = self._current_zone_id()
        if item_id is not None:
            self.duplicate_requested.emit(item_id)

    def _emit_delete(self) -> None:
        item_id = self._current_zone_id()
        if item_id is not None:
            self.delete_requested.emit(item_id)


__all__ = ["RegionListEntry", "RegionListPanel"]
