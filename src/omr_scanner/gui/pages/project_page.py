"""The Project page: a desktop dashboard for opening and reviewing a project.

Purpose:
    Be the application's landing page. With no project open it says so plainly
    and offers the two things worth doing; with one open it shows what that
    project is. Alongside either, it offers the short list of ways in and the
    projects most recently worked on.

Responsibilities:
    * Render :class:`~omr_scanner.services.ProjectSession` state, or the empty
      state.
    * Present Getting Started and Recent Projects.
    * Lay itself out in two columns or one, decided by its own width -
      independently of the workflow navigator above it.

What does NOT belong here:
    * Calling the project service, and reading the application
      configuration. A page that opened projects itself would give the
      application two places a project can change. Everything visible here
      emits a signal; the main window, which owns the project lifecycle and
      the configuration, performs the action and hands back the recent list.

Why its responsiveness is its own:
    The brief is explicit that the navigator and the page content must respond
    independently, and the two genuinely need different thresholds: the
    navigator's depends on nine labels' total width, this page's on whether a
    268-pixel information column still leaves the main content 420. Sharing
    one breakpoint would make one of the two wrong at every width.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.pages.catalog import WorkflowPageSpec
from omr_scanner.gui.theme import Dashboard, IconSize, Spacing
from omr_scanner.gui.widgets.buttons import primary_button, secondary_button
from omr_scanner.gui.widgets.card import ActionRow, Card, EmptyState
from omr_scanner.gui.widgets.page_header import PageHeader
from omr_scanner.services import ProjectSession, project_sets

NO_PROJECT_HEADLINE = "No project is open."
NO_PROJECT_DETAIL = "Create a new project or open an existing one to get started."

NO_SETS_TEXT = "None defined yet - see the application menu, File > Project Configuration..."

NO_RECENT_HEADLINE = "No recent projects"
NO_RECENT_DETAIL = "Your recently opened projects will appear here."

MISSING_PROJECT_DETAIL = "Folder not found - it may have been moved or deleted."
"""Shown instead of the path when a remembered project is no longer there.

A remembered path is a path that *was* valid, so an invalid one is expected
rather than exceptional. The entry is disabled and says why, which is the
graceful handling the brief asks for - never a crash, and never a live-looking
row that fails when clicked.
"""

PROJECT_INFO_UNAVAILABLE = "Open a project first - this manages the open project's details."

MAX_SETS_LISTED = 8
"""How many set codes are named before the summary says "and N more".

A project may legitimately define fifty; naming them all would turn a
one-line summary into a paragraph. The full list is one menu item away."""

MAX_RECENT_SHOWN = 4
"""How many recent projects the card lists before "View All" is the way to
the rest. Four fits the card without the dashboard column growing taller than
the main content beside it."""

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M UTC"


class ProjectPage(WorkflowPage):
    """The landing dashboard.

    Signals:
        create_requested: The user asked to create a project.
        open_requested: The user asked to open an existing project.
        project_info_requested: The user asked to manage the open project's
            details. Routed to the existing Project Configuration dialog -
            this page defines no new project behaviour.
        recent_project_requested: A recent entry was chosen. Carries its path.
        view_all_recent_requested: The full recent list was asked for.

    Attributes:
        create_button: The primary action.
        open_button: The secondary action.
        empty_state: The no-project panel.
        getting_started_card: The three ways in.
        recent_card: The recent-projects list.
    """

    create_requested = Signal()
    open_requested = Signal()
    project_info_requested = Signal()
    recent_project_requested = Signal(Path)
    view_all_recent_requested = Signal()

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_header=False, compact=True)

        self._side_by_side: bool | None = None
        self._recent_rows: list[ActionRow] = []

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("projectDashboardScroll")
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._canvas = QWidget()
        self._canvas.setObjectName("projectDashboard")
        self._grid = QGridLayout(self._canvas)
        self._grid.setContentsMargins(
            Spacing.SM, Spacing.SM, Spacing.SM, Spacing.SM
        )
        self._grid.setHorizontalSpacing(Dashboard.COLUMN_GAP)
        self._grid.setVerticalSpacing(Dashboard.COLUMN_GAP)

        self._main_column = self._build_main_column()
        self._side_column = self._build_side_column()
        self._scroll.setWidget(self._canvas)
        self.body.addWidget(self._scroll)

        self._apply_columns(side_by_side=True)
        self.on_project_changed(None)

    # ------------------------------------------------------------------
    # The main column
    # ------------------------------------------------------------------
    def _build_main_column(self) -> Card:
        card = Card(parent=self._canvas)
        card.setObjectName("projectMainCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        card.setMinimumWidth(Dashboard.MAIN_MIN_WIDTH // 2)

        self.header = PageHeader(
            self.spec.title, self.spec.summary, self.spec.icon, card, hero=True
        )
        self.title_widget = self.header.title_label
        self.summary_widget = self.header.summary_label
        card.body.addWidget(self.header)

        self.empty_state = EmptyState(
            "folder", NO_PROJECT_HEADLINE, NO_PROJECT_DETAIL, card
        )
        self.create_button = primary_button("Create Project", "createProjectButton")
        self.create_button.setIcon(load_icon("plus"))
        self.create_button.setIconSize(QSize(IconSize.MD, IconSize.MD))
        self.create_button.clicked.connect(self.create_requested.emit)
        self.create_button.setToolTip("Set up a new examination project")

        self.open_button = secondary_button("Open Project", "openProjectButton")
        self.open_button.setIcon(load_icon("folder-open"))
        self.open_button.setIconSize(QSize(IconSize.MD, IconSize.MD))
        self.open_button.clicked.connect(self.open_requested.emit)
        self.open_button.setToolTip("Open a project saved earlier")

        self.empty_state.add_action(self.create_button)
        self.empty_state.add_action(self.open_button)
        card.body.addWidget(self.empty_state, 1)

        self._details = QWidget(card)
        self._details.setObjectName("projectDetails")
        form = QFormLayout(self._details)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setContentsMargins(0, Spacing.SM, 0, 0)
        form.setHorizontalSpacing(Spacing.LG)
        form.setVerticalSpacing(Spacing.SM)

        self._value_labels: dict[str, QLabel] = {}
        for caption in (
            "Exam name",
            "Sets",
            "Name",
            "Description",
            "Location",
            "Project ID",
            "Created",
            "Modified",
        ):
            value = QLabel("")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            form.addRow(f"{caption}:", value)
            self._value_labels[caption] = value

        card.body.addWidget(self._details)
        card.body.addStretch(1)
        self._details.setVisible(False)
        return card

    # ------------------------------------------------------------------
    # The information column
    # ------------------------------------------------------------------
    def _build_side_column(self) -> QWidget:
        column = QWidget(self._canvas)
        column.setObjectName("projectDashboardSide")
        column.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Dashboard.COLUMN_GAP)

        self.getting_started_card = self._build_getting_started(column)
        layout.addWidget(self.getting_started_card)

        self.recent_card = self._build_recent_card(column)
        layout.addWidget(self.recent_card)

        layout.addStretch(1)
        return column

    def _build_getting_started(self, parent: QWidget) -> Card:
        card = Card("Getting Started", parent)
        card.setObjectName("gettingStartedCard")

        self.create_row = ActionRow(
            "file-plus",
            "Create a new project",
            "Set up a new examination with basic details.",
            card,
        )
        self.create_row.clicked.connect(self.create_requested.emit)
        card.body.addWidget(self.create_row)
        card.add_divider()

        self.open_row = ActionRow(
            "folder-open",
            "Open an existing project",
            "Load a previously saved project.",
            card,
        )
        self.open_row.clicked.connect(self.open_requested.emit)
        card.body.addWidget(self.open_row)
        card.add_divider()

        self.info_row = ActionRow(
            "info",
            "Project information",
            "Manage exam name, description, sets, and settings.",
            card,
        )
        self.info_row.clicked.connect(self.project_info_requested.emit)
        card.body.addWidget(self.info_row)
        return card

    def _build_recent_card(self, parent: QWidget) -> Card:
        self.view_all_button = secondary_button("View All", "viewAllRecentButton")
        self.view_all_button.setIcon(load_icon("chevron-right"))
        self.view_all_button.setIconSize(QSize(IconSize.SM, IconSize.SM))
        self.view_all_button.setToolTip("Show every remembered project")
        self.view_all_button.clicked.connect(self.view_all_recent_requested.emit)

        card = Card("Recent Projects", parent, trailing=self.view_all_button)
        card.setObjectName("recentProjectsCard")
        self._recent_body = QVBoxLayout()
        self._recent_body.setContentsMargins(0, 0, 0, 0)
        self._recent_body.setSpacing(Spacing.XXS)
        card.body.addLayout(self._recent_body)
        self._recent_empty = card.add_empty_note(NO_RECENT_HEADLINE, NO_RECENT_DETAIL)
        return card

    # ------------------------------------------------------------------
    # Recent projects
    # ------------------------------------------------------------------
    def set_recent_projects(self, paths: tuple[Path, ...]) -> None:
        """Show ``paths`` as the recent list.

        Args:
            paths: Remembered project directories, most recent first. The
                main window supplies them from the application configuration;
                this page neither reads nor writes that configuration.

        A path that no longer exists is listed, disabled, and says why - it is
        still useful information that the project *was* there.
        """
        for row in self._recent_rows:
            self._recent_body.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._recent_rows.clear()

        shown = paths[:MAX_RECENT_SHOWN]
        for path in shown:
            self._recent_rows.append(self._build_recent_row(path))

        self._recent_empty.setVisible(not shown)
        self.view_all_button.setVisible(len(paths) > len(shown))

    def _build_recent_row(self, path: Path) -> ActionRow:
        exists = path.is_dir()
        row = ActionRow(
            "folder",
            path.name or str(path),
            str(path) if exists else MISSING_PROJECT_DETAIL,
            self.recent_card,
        )
        row.setObjectName(f"recentProject_{path.name}")
        if exists:
            row.clicked.connect(
                lambda _checked=False, target=path: self.recent_project_requested.emit(
                    target
                )
            )
            row.setToolTip(str(path))
        else:
            row.set_unavailable_reason(MISSING_PROJECT_DETAIL)
            row.setToolTip(f"{path}\n{MISSING_PROJECT_DETAIL}")
        self._recent_body.addWidget(row)
        return row

    @property
    def recent_rows(self) -> tuple[ActionRow, ...]:
        """The recent-project entries currently listed."""
        return tuple(self._recent_rows)

    # ------------------------------------------------------------------
    # Project state
    # ------------------------------------------------------------------
    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Show the open project's metadata, or the empty state."""
        if session is None:
            self._details.setVisible(False)
            self.empty_state.setVisible(True)
            self.info_row.set_unavailable_reason(PROJECT_INFO_UNAVAILABLE)
            return

        metadata = session.project.metadata
        self._value_labels["Exam name"].setText(session.exam_name)
        self._value_labels["Sets"].setText(self._describe_sets(session))
        self._value_labels["Name"].setText(metadata.name)
        self._value_labels["Description"].setText(metadata.description or "-")
        self._value_labels["Location"].setText(str(session.root))
        self._value_labels["Project ID"].setText(metadata.project_id)
        self._value_labels["Created"].setText(metadata.created_at.strftime(TIMESTAMP_FORMAT))
        self._value_labels["Modified"].setText(metadata.modified_at.strftime(TIMESTAMP_FORMAT))

        self.empty_state.setVisible(False)
        self._details.setVisible(True)
        self.info_row.set_unavailable_reason("")

    def _describe_sets(self, session: ProjectSession) -> str:
        """Summarise the sets this project defines, in one line.

        Read here rather than cached because this page is redisplayed whenever
        the project changes, which includes returning from *Project
        Configuration* - the one place that can have just changed the answer.
        """
        sets = project_sets.list_sets(session.database)
        if not sets:
            return NO_SETS_TEXT
        listed = ", ".join(item.display_label for item in sets[:MAX_SETS_LISTED])
        if len(sets) > MAX_SETS_LISTED:
            listed += f", and {len(sets) - MAX_SETS_LISTED} more"
        return f"{len(sets)} defined - {listed}"

    # ------------------------------------------------------------------
    # This page's own responsive behaviour
    # ------------------------------------------------------------------
    @property
    def is_side_by_side(self) -> bool:
        """Whether the dashboard is currently in two columns."""
        return bool(self._side_by_side)

    def fits_side_by_side(self, available: int) -> bool:
        """Whether ``available`` pixels can hold both columns readably.

        The threshold is the sum of the two columns' own minimum readable
        widths plus the gap - a measurement of what the content needs, not a
        screen size. Below it the page stacks rather than squeezing the
        information column into the unusable strip the brief warns about.
        """
        return available >= (
            Dashboard.MAIN_MIN_WIDTH + Dashboard.SIDE_MIN_WIDTH + Dashboard.COLUMN_GAP
        )

    def _apply_columns(self, *, side_by_side: bool) -> None:
        if side_by_side == self._side_by_side:
            return
        self._side_by_side = side_by_side

        self._grid.removeWidget(self._main_column)
        self._grid.removeWidget(self._side_column)

        if side_by_side:
            self._grid.addWidget(self._main_column, 0, 0)
            self._grid.addWidget(self._side_column, 0, 1)
            self._grid.setColumnStretch(0, Dashboard.MAIN_STRETCH)
            self._grid.setColumnStretch(1, Dashboard.SIDE_STRETCH)
            self._grid.setRowStretch(0, 1)
            self._grid.setRowStretch(1, 0)
            self._side_column.setMinimumWidth(Dashboard.SIDE_MIN_WIDTH)
            self._side_column.setMaximumWidth(Dashboard.SIDE_MAX_WIDTH)
        else:
            # Stacked order, from the brief: heading and main content first,
            # then Getting Started, then Recent Projects. The heading lives
            # inside the main column, so stacking the two columns puts all
            # four in exactly that order.
            self._grid.addWidget(self._main_column, 0, 0)
            self._grid.addWidget(self._side_column, 1, 0)
            self._grid.setColumnStretch(0, 1)
            self._grid.setColumnStretch(1, 0)
            self._grid.setRowStretch(0, 1)
            self._grid.setRowStretch(1, 0)
            self._side_column.setMinimumWidth(0)
            self._side_column.setMaximumWidth(16_777_215)

        if self.header is not None:
            # The decorative tagline is the first thing to go: it is beside
            # the heading, and once the page is one column narrow enough to
            # stack, the heading needs that width for its own summary.
            self.header.set_hero_visible(side_by_side)

    def content_width(self) -> int:
        """Width actually available to the two columns.

        Computed from this page's own width less the margins it knows about,
        rather than read from the scroll area's viewport. The viewport reports
        its *previous* size until Qt has run a layout pass, so reading it gave
        the answer for the old width for one pass after every resize - and on
        a page nested inside a `QStackedWidget` that pass may not come at all
        before something asks what layout resulted.

        The vertical scrollbar is deducted when it is showing, which only
        happens in the stacked layout. That biases the decision very slightly
        towards *staying* stacked, which is the safe direction: the
        alternative is flipping back to two columns on the strength of width
        the scrollbar has already taken, and then flipping again.
        """
        page_layout = self.layout()
        page_margin = 0
        if page_layout is not None:
            margins = page_layout.contentsMargins()
            page_margin = margins.left() + margins.right()
        grid_margins = self._grid.contentsMargins()
        available = (
            self.width()
            - page_margin
            - grid_margins.left()
            - grid_margins.right()
        )
        scrollbar = self._scroll.verticalScrollBar()
        if scrollbar is not None and scrollbar.isVisible():
            available -= scrollbar.width()
        return max(available, 1)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-decide between two columns and one."""
        super().resizeEvent(event)
        self._apply_columns(side_by_side=self.fits_side_by_side(self.content_width()))


__all__ = [
    "MAX_RECENT_SHOWN",
    "MISSING_PROJECT_DETAIL",
    "NO_PROJECT_DETAIL",
    "NO_PROJECT_HEADLINE",
    "NO_RECENT_DETAIL",
    "NO_RECENT_HEADLINE",
    "PROJECT_INFO_UNAVAILABLE",
    "ProjectPage",
]
