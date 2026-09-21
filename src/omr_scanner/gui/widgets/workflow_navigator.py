"""The horizontal workflow navigator, and the layout algorithm behind it.

Purpose:
    Present the nine workflow stages as a connected process across the top of
    the window, in whichever of four layouts the space actually available can
    show *without* shrinking the text or clipping a label.

Responsibilities:
    * :class:`NavigatorMode` - the four layouts, in order of preference.
    * :meth:`WorkflowNavigator.plan_for_width` - the decision: given a width,
      which layout, and where does every step go. A query, not a mutation, so
      a test can ask "what would you do at 1000 pixels?" without resizing
      anything or waiting for a Qt layout pass.
    * :class:`WorkflowNavigator` - the widget: applies a plan, owns the
      current/enabled state, and handles keyboard navigation.

What does NOT belong here:
    * How a step draws itself, and how wide it is - that is
      :mod:`omr_scanner.gui.widgets.workflow_step`.
    * Which stages are available when. The navigator is *told*; the main
      window, which knows the project state, decides.
    * Page routing. The navigator emits
      :attr:`WorkflowNavigator.step_activated` and nothing more.

Why the layout is computed rather than laid out by Qt:
    Consecutive chevrons overlap by the arrow depth, so the advance from one
    step to the next is *less* than the step's own width. Expressing that to
    `QHBoxLayout` means negative spacing, which then fights every other
    calculation it makes - in particular its distribution of surplus width,
    which has to be proportional to each label's length or "4. Scan" gets the
    same room as "7. Answer Key" and one of the two is wrong. Computing the
    geometry directly is a page of arithmetic and is entirely predictable,
    which matters more here than brevity: the alternative failure mode is a
    resize loop.

Why the decision is driven by measurement and not by screen resolution:
    Every threshold below comes from
    :meth:`~omr_scanner.gui.widgets.workflow_step.WorkflowStep.natural_width`,
    which measures the label in the font the widget is actually using. A
    larger platform font, or a Windows text-scaling change, therefore
    *changes the layout* instead of clipping - which is the whole point. There
    is no resolution constant anywhere in this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.theme import Navigator, Stroke
from omr_scanner.gui.widgets.workflow_step import (
    StepGeometry,
    StepShape,
    StepSize,
    WorkflowStep,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from PySide6.QtGui import QKeyEvent, QResizeEvent

logger = logging.getLogger(__name__)

FIRST_ROW_STEPS = 5
"""How many steps go on the first row of the two-row layout.

Five then four, which is the split the brief names and which also happens to
be the better-balanced one for these nine labels. Chosen rather than computed
because the logical break - "prepare the exam" then "produce the results" -
falls exactly here, and a computed split would move it whenever a label's
length changed.
"""

COLUMNS_NARROW = 2
"""Columns in the two-column layout. Row-major, so the reading order is
1-2 / 3-4 / 5-6 / 7-8 / 9, which keeps the workflow's own order intact."""


class StageSpec(Protocol):
    """What the navigator needs to know about a workflow stage.

    A structural type rather than an import of
    :class:`~omr_scanner.gui.pages.catalog.WorkflowPageSpec`, for two
    reasons. It documents the navigator's whole dependency on the rest of the
    application in four attributes - and it keeps the import graph acyclic:
    ``gui.pages`` imports this package (its base page uses the shared page
    header), so this package importing ``gui.pages`` back would be a cycle
    that fails at interpreter start-up rather than at run time.
    """

    @property
    def key(self) -> str:
        """Stable identifier, used to route to the matching page."""

    @property
    def title(self) -> str:
        """The stage's name, shown on the step."""

    @property
    def summary(self) -> str:
        """One line describing the stage, for the tooltip."""

    @property
    def icon(self) -> str:
        """Bundled Lucide icon name."""


class NavigatorMode(Enum):
    """The four layouts, in order of decreasing available width.

    Attributes:
        WIDE: All nine stages in one chevron row.
        MEDIUM: Two chevron rows, five then four.
        NARROW: A two-column grid of tiles, row-major.
        COMPACT: One chevron row at its natural size, scrolled horizontally.
            The last resort, and still complete: every stage is present and
            reachable, which hiding stages or shrinking the font would not be.
    """

    WIDE = "wide"
    MEDIUM = "medium"
    NARROW = "narrow"
    COMPACT = "compact"


@dataclass(frozen=True)
class StepPlacement:
    """Where one step goes, and in what geometry.

    Attributes:
        key: The stage's identifier.
        rect: Position and size within the navigator's strip.
        geometry: The shape the step should draw itself in there.
    """

    key: str
    rect: QRect
    geometry: StepGeometry


@dataclass(frozen=True)
class LayoutPlan:
    """A complete answer to "how should the navigator look at this width?".

    Attributes:
        mode: Which layout was chosen.
        placements: One per step, in workflow order.
        strip_size: The size the scrolled strip needs. Wider than the
            viewport only in :attr:`NavigatorMode.COMPACT`, which is exactly
            when the horizontal scrollbar should appear.
    """

    mode: NavigatorMode
    placements: tuple[StepPlacement, ...]
    strip_size: QSize


def _distribute(surplus: int, weights: Sequence[int]) -> list[int]:
    """Split ``surplus`` pixels across ``weights``, proportionally, exactly.

    Args:
        surplus: Pixels to hand out. Zero or negative gives all zeros.
        weights: Each recipient's share, typically its natural width.

    Returns:
        One integer per weight, summing to exactly ``surplus``.

    Proportional rather than equal because the labels differ in length by
    almost a factor of two: splitting the extra room equally would leave
    "7. Answer Key" tight while "4. Scan" floated in whitespace. The running
    remainder is what makes the parts sum to the whole - rounding each share
    independently loses up to one pixel per step, which shows as a ragged
    right edge on a row that is supposed to reach the margin.
    """
    total_weight = sum(weights)
    if surplus <= 0 or total_weight <= 0:
        return [0] * len(weights)
    shares: list[int] = []
    handed_out = 0
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            shares.append(surplus - handed_out)
            break
        share = surplus * weight // total_weight
        shares.append(share)
        handed_out += share
    return shares


class WorkflowNavigator(QWidget):
    """The workflow stages, as a responsive row of chevrons.

    Args:
        specs: The stages, in workflow order. Each needs ``key``, ``title``,
            ``summary`` and an ``icon`` name; the page catalog's
            :class:`~omr_scanner.gui.pages.catalog.WorkflowPageSpec` provides
            all four.
        parent: Optional Qt parent.

    Signals:
        step_activated: A stage was clicked or activated from the keyboard.
            Carries the stage's key. Emitted only for enabled stages, and
            *not* emitted by :meth:`set_current_key`, so a programmatic page
            change cannot loop back into a navigation request.

    Testability:
        :meth:`plan_for_width` is the layout decision, separated from applying
        it. A test asserts on the returned :class:`LayoutPlan` - mode, row
        count, geometry, whether anything is clipped - for any width, with no
        window, no show, and no waiting for Qt to lay anything out.
    """

    step_activated = Signal(str)

    def __init__(
        self, specs: Iterable[StageSpec], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("workflowNavigatorBand")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self._steps: list[WorkflowStep] = []
        self._current_key: str | None = None
        self._mode = NavigatorMode.WIDE
        self._relaying_out = False
        self._applied_strip_height = 0

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("workflowNavigatorScroll")
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidgetResizable(False)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Switched on per layout in `_apply`; the strip's height always fits,
        # so vertical scrolling is never wanted at all.
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)

        self._strip = QWidget()
        self._strip.setObjectName("workflowNavigatorStrip")
        self._strip.setAutoFillBackground(False)
        self._scroll.setWidget(self._strip)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Navigator.BAND_H_PADDING,
            Navigator.BAND_V_PADDING,
            Navigator.BAND_H_PADDING,
            Navigator.BAND_V_PADDING,
        )
        outer.setSpacing(0)
        outer.addWidget(self._scroll)

        for index, spec in enumerate(specs):
            step = WorkflowStep(
                key=spec.key,
                number=index + 1,
                label=spec.title,
                icon_name=spec.icon,
                summary=spec.summary,
                parent=self._strip,
            )
            step.clicked.connect(self._on_step_clicked)
            self._steps.append(step)

        if self._steps:
            self.set_current_key(self._steps[0].key)
        self._relayout()

    # ------------------------------------------------------------------
    # The stages
    # ------------------------------------------------------------------
    @property
    def steps(self) -> tuple[WorkflowStep, ...]:
        """The stage buttons, in workflow order."""
        return tuple(self._steps)

    @property
    def keys(self) -> tuple[str, ...]:
        """The stage keys, in workflow order."""
        return tuple(step.key for step in self._steps)

    def step(self, key: str) -> WorkflowStep | None:
        """The stage button for ``key``, or ``None``."""
        return next((step for step in self._steps if step.key == key), None)

    # ------------------------------------------------------------------
    # Current stage
    # ------------------------------------------------------------------
    def current_key(self) -> str | None:
        """The active stage's key, or ``None`` before one is set."""
        return self._current_key

    def set_current_key(self, key: str) -> bool:
        """Mark ``key`` as the active stage.

        Args:
            key: The stage to activate.

        Returns:
            ``True`` when ``key`` is one of this navigator's stages.

        Does not emit :attr:`step_activated`: this is how the *window* tells
        the navigator which page is showing, and a signal here would turn
        every programmatic page change into a navigation request and back
        again.
        """
        target = self.step(key)
        if target is None:
            return False
        self._current_key = key
        for step in self._steps:
            step.setChecked(step is target)
        return True

    def _on_step_clicked(self) -> None:
        step = self.sender()
        if not isinstance(step, WorkflowStep):  # pragma: no cover - defensive
            return
        # A checkable button toggles itself on click. The navigator, not the
        # click, decides what is checked: if the window refuses the
        # navigation, the check must not stick.
        step.setChecked(step.key == self._current_key)
        self.step_activated.emit(step.key)

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------
    def set_step_enabled(self, key: str, *, enabled: bool, reason: str = "") -> None:
        """Enable or disable one stage.

        Args:
            key: The stage.
            enabled: Whether it can be opened.
            reason: Shown in the tooltip when ``enabled`` is false, so a
                disabled stage is never a dead end.
        """
        step = self.step(key)
        if step is None:
            return
        step.setEnabled(enabled)
        step.set_disabled_reason("" if enabled else reason)

    def enabled_keys(self) -> tuple[str, ...]:
        """The keys of the stages that can currently be opened."""
        return tuple(step.key for step in self._steps if step.isEnabled())

    # ------------------------------------------------------------------
    # The layout decision
    # ------------------------------------------------------------------
    @property
    def mode(self) -> NavigatorMode:
        """The layout currently applied."""
        return self._mode

    def _geometry_for(
        self, shape: StepShape, size: StepSize, *, first_in_row: bool
    ) -> StepGeometry:
        return StepGeometry(
            shape=shape,
            size=size,
            has_left_notch=not first_in_row,
            has_right_point=True,
        )

    def _chevron_row_width(
        self, steps: Sequence[WorkflowStep], size: StepSize
    ) -> tuple[int, list[int], list[StepGeometry]]:
        """Natural width of one chevron row, plus its per-step parts.

        Returns:
            ``(total, widths, geometries)``. ``total`` accounts for the
            overlap: each step after the first advances by its width less the
            arrow depth, plus the visible seam.
        """
        geometries = [
            self._geometry_for(StepShape.CHEVRON, size, first_in_row=index == 0)
            for index in range(len(steps))
        ]
        widths = [step.natural_width(geom) for step, geom in zip(steps, geometries, strict=True)]
        if not widths:
            return 0, widths, geometries
        overlap = sum(
            geom.arrow_depth - Navigator.STEP_GAP for geom in geometries[1:]
        )
        return sum(widths) - overlap, widths, geometries

    def _place_chevron_row(
        self,
        steps: Sequence[WorkflowStep],
        size: StepSize,
        available: int,
        top: int,
    ) -> tuple[list[StepPlacement], int]:
        """Lay one chevron row out across ``available`` pixels.

        Returns:
            ``(placements, row_width)``. The row is stretched to fill
            ``available`` when there is surplus, so it reaches the margin
            rather than trailing off; it is never compressed below its natural
            width, because the caller only chooses this layout when the
            natural width fits.
        """
        total, widths, geometries = self._chevron_row_width(steps, size)
        if not widths:
            return [], 0
        extra = _distribute(available - total, widths)
        placements: list[StepPlacement] = []
        x = 0
        for step, width, bonus, geom in zip(steps, widths, extra, geometries, strict=True):
            final_width = width + bonus
            placements.append(
                StepPlacement(
                    key=step.key,
                    rect=QRect(x, top, final_width, geom.height),
                    geometry=geom,
                )
            )
            x += geom.advance_for(final_width)
        row_width = placements[-1].rect.right() + 1
        return placements, row_width

    def plan_for_width(self, available_width: int) -> LayoutPlan:
        """Decide the layout for ``available_width`` pixels of strip.

        Args:
            available_width: Width the steps may occupy - the scroll area's
                viewport, not the window.

        Returns:
            The chosen :class:`LayoutPlan`.

        The four layouts are tried in order and the first one whose *natural*
        width fits is used, so the font is never reduced and no label is ever
        clipped to make a layout work. Reaching
        :attr:`NavigatorMode.COMPACT` means even the two-column grid could not
        show full labels, and the strip becomes scrollable instead of any
        stage becoming unreadable or hidden.
        """
        available = max(available_width, 1)

        wide_total, _, _ = self._chevron_row_width(self._steps, StepSize.REGULAR)
        if wide_total <= available:
            placements, _ = self._place_chevron_row(
                self._steps, StepSize.REGULAR, available, top=0
            )
            height = placements[0].rect.height() if placements else 0
            return LayoutPlan(
                mode=NavigatorMode.WIDE,
                placements=tuple(placements),
                strip_size=QSize(available, height),
            )

        first, second = self._steps[:FIRST_ROW_STEPS], self._steps[FIRST_ROW_STEPS:]
        first_total, _, _ = self._chevron_row_width(first, StepSize.REGULAR)
        second_total, _, _ = self._chevron_row_width(second, StepSize.REGULAR)
        if max(first_total, second_total) <= available:
            top_row, _ = self._place_chevron_row(first, StepSize.REGULAR, available, top=0)
            row_height = top_row[0].rect.height() if top_row else Navigator.STEP_HEIGHT
            bottom_row, _ = self._place_chevron_row(
                second, StepSize.REGULAR, available, top=row_height + Navigator.ROW_GAP
            )
            return LayoutPlan(
                mode=NavigatorMode.MEDIUM,
                placements=(*top_row, *bottom_row),
                strip_size=QSize(available, row_height * 2 + Navigator.ROW_GAP),
            )

        narrow = self._plan_two_columns(available)
        if narrow is not None:
            return narrow

        return self._plan_scrolling(available)

    def _plan_two_columns(self, available: int) -> LayoutPlan | None:
        """A row-major two-column grid of tiles, or ``None`` if labels would clip.

        Tiles rather than chevrons: an arrow at the end of the left column
        would point at the column break, which is the opposite of what the
        shape means. The brief allows exactly this substitution for the
        compact layout, and the numbers, icons, labels and states all survive
        it.
        """
        tile = StepGeometry(
            shape=StepShape.TILE,
            size=StepSize.COMPACT,
            has_left_notch=False,
            has_right_point=False,
        )
        widest = max((step.natural_width(tile) for step in self._steps), default=0)
        column_width = (available - Navigator.STEP_GAP * (COLUMNS_NARROW - 1)) // COLUMNS_NARROW
        if column_width < widest:
            return None

        placements: list[StepPlacement] = []
        row_height = tile.height
        for index, step in enumerate(self._steps):
            row, column = divmod(index, COLUMNS_NARROW)
            placements.append(
                StepPlacement(
                    key=step.key,
                    rect=QRect(
                        column * (column_width + Navigator.STEP_GAP),
                        row * (row_height + Navigator.STEP_GAP),
                        column_width,
                        row_height,
                    ),
                    geometry=tile,
                )
            )
        rows = -(-len(self._steps) // COLUMNS_NARROW)
        return LayoutPlan(
            mode=NavigatorMode.NARROW,
            placements=tuple(placements),
            strip_size=QSize(
                available, rows * row_height + (rows - 1) * Navigator.STEP_GAP
            ),
        )

    def _plan_scrolling(self, available: int) -> LayoutPlan:
        """One chevron row at natural size, wider than the viewport.

        The last resort, and deliberately not a compromise on completeness:
        every stage keeps its number, its icon and its full label, and the
        strip scrolls. Hiding stages, or reducing the font until they fit, are
        the two things the brief rules out, and this is what replaces them.
        """
        total, _, _ = self._chevron_row_width(self._steps, StepSize.COMPACT)
        width = max(total, available)
        placements, _ = self._place_chevron_row(
            self._steps, StepSize.COMPACT, width, top=0
        )
        height = placements[0].rect.height() if placements else Navigator.STEP_HEIGHT_COMPACT
        return LayoutPlan(
            mode=NavigatorMode.COMPACT,
            placements=tuple(placements),
            strip_size=QSize(width, height),
        )

    # ------------------------------------------------------------------
    # Applying it
    # ------------------------------------------------------------------
    def _viewport_width(self) -> int:
        """Width the strip may occupy without scrolling.

        Read from the scroll area's viewport, so the band's padding and any
        visible scrollbar are already accounted for - but only once the
        viewport's width agrees with this widget's own. Until Qt has run a
        layout pass the viewport still reports whatever size it was
        constructed at, and planning against that produced the wrong layout
        for one pass after every programmatic resize. Falling back to this
        widget's width less its padding is the same number the viewport will
        report once it catches up.
        """
        own = max(self.width() - 2 * Navigator.BAND_H_PADDING, 1)
        # The only legitimate difference between the two is a vertical
        # scrollbar, which is switched off, so anything wider than a hairline
        # means the layout has not caught up yet.
        viewport_width = self._scroll.viewport().width()
        if abs(viewport_width - own) <= Stroke.HAIRLINE:
            return viewport_width
        return own

    def _activate_layout(self) -> None:
        """Lay the scroll area out against this widget's *current* size, now.

        ``invalidate()`` before ``activate()``, and the first of those is the
        load-bearing one. `QLayout.activate` returns immediately when the
        layout is not marked dirty, and changing this widget's height does not
        mark it dirty - so the scroll area kept the geometry it was given when
        the band was one row tall, and the second row of chevrons was clipped
        to a sliver inside a 50-pixel viewport in a 104-pixel band. No number
        of extra event-loop passes fixed it, because nothing ever invalidated
        the layout.
        """
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()

    def _relayout(self) -> None:
        """Recompute and apply the layout.

        Guarded against re-entry. Applying a plan changes this widget's
        height, which resizes it, which arrives back here - the classic
        resize loop. The guard, plus touching the height only when it
        actually changed, is what makes the sequence terminate.

        The guard is also why :meth:`_apply` cannot rely on a later corrective
        pass: the re-entrant call it provokes is exactly the one the guard
        drops. Everything that pass would have done is therefore done inline,
        in the right order.
        """
        if self._relaying_out or not self._steps:
            return
        self._relaying_out = True
        try:
            self._activate_layout()
            plan = self.plan_for_width(self._viewport_width())
            self._apply(plan)
        finally:
            self._relaying_out = False

    def _apply(self, plan: LayoutPlan) -> None:
        """Give the band its height, then position the steps inside it.

        The order matters. The height is applied first and the layout run
        immediately after, so the scroll area is already its new size when
        the steps land inside it - otherwise a switch to two rows places them
        in a viewport still sized for one. The actual repair for that is the
        ``invalidate()`` in :meth:`_activate_layout`; this ordering is what
        makes one pass sufficient rather than leaving the fix to depend on
        whatever event happens next.
        """
        height = self._band_height(plan)
        if height != self._applied_strip_height:
            self._applied_strip_height = height
            self.setFixedHeight(height)
            self.updateGeometry()
        self._activate_layout()

        by_key = {placement.key: placement for placement in plan.placements}
        for step in self._steps:
            placement = by_key.get(step.key)
            if placement is None:  # pragma: no cover - every step is placed
                continue
            step.apply_geometry(placement.geometry)
            step.setGeometry(placement.rect)

        # Earlier steps sit *above* later ones, and this is about clicks, not
        # appearance. Step N's arrowhead lies inside step N+1's rectangle. A
        # button whose `hitButton` rejects a press ignores the event, and an
        # ignored press goes to the parent - never to the sibling underneath -
        # so if step N+1 were on top, every click on step N's visible point
        # would be swallowed and do nothing. Raising in reverse order leaves
        # step 1 topmost, so the widget on top at any pixel is always the one
        # whose shape is painted there.
        for step in reversed(self._steps):
            step.raise_()

        self._strip.resize(plan.strip_size)
        self._strip.setMinimumSize(plan.strip_size)

        # Only the compact layout is ever allowed to scroll. The other three
        # are *chosen* because everything fits, so a scrollbar there can only
        # be spurious - and it is not harmless: it appears inside a band whose
        # height was computed without it, steals that height from the
        # viewport, and clips the bottom of every chevron. Leaving the policy
        # on "as needed" let exactly that happen after a resize that passed
        # through a narrower width.
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if plan.mode is NavigatorMode.COMPACT
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        mode_changed = plan.mode is not self._mode
        self._mode = plan.mode
        if mode_changed:
            logger.debug("Workflow navigator layout: %s", plan.mode.value)

    def _band_height(self, plan: LayoutPlan) -> int:
        """Total height of the band, including padding and any scrollbar."""
        height = plan.strip_size.height() + 2 * Navigator.BAND_V_PADDING
        if plan.mode is NavigatorMode.COMPACT:
            scrollbar = self._scroll.horizontalScrollBar()
            if scrollbar is not None:
                height += scrollbar.sizeHint().height()
        return height

    def sizeHint(self) -> QSize:
        """Preferred size: the width of the widest layout, at the current height."""
        total, _, _ = self._chevron_row_width(self._steps, StepSize.REGULAR)
        return QSize(
            total + 2 * Navigator.BAND_H_PADDING,
            self._applied_strip_height or Navigator.STEP_HEIGHT + 2 * Navigator.BAND_V_PADDING,
        )

    def minimumSizeHint(self) -> QSize:
        """The navigator never demands width: it changes layout instead.

        A minimum width here would become the window's minimum width, which
        would make the compact layouts unreachable - the opposite of the point
        of having them.
        """
        return QSize(
            2 * Navigator.BAND_H_PADDING,
            self._applied_strip_height or Navigator.STEP_HEIGHT + 2 * Navigator.BAND_V_PADDING,
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-decide the layout for the new width."""
        super().resizeEvent(event)
        self._relayout()

    def changeEvent(self, event: QEvent) -> None:
        """Re-decide the layout when the font changes.

        This is how a Windows text-scaling change becomes a layout change:
        the steps re-measure themselves, and the width that used to fit nine
        chevrons no longer does, so the navigator drops to two rows instead of
        clipping.
        """
        super().changeEvent(event)
        self._relayout()

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------
    def _movable_steps(self) -> list[WorkflowStep]:
        return [step for step in self._steps if step.isEnabled()]

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Move focus between stages with the arrow keys, Home and End.

        In addition to Tab, never instead of it: Tab still enters and leaves
        the navigator normally. Both axes are accepted because the same nine
        stages are a row in one layout, two rows in another and a grid in a
        third, and an operator should not have to know which one is on screen
        to use the keyboard.
        """
        movable = self._movable_steps()
        if not movable:
            super().keyPressEvent(event)
            return

        focused = next((step for step in movable if step.hasFocus()), None)
        index = movable.index(focused) if focused is not None else -1
        key = event.key()

        if key in {Qt.Key.Key_Right, Qt.Key.Key_Down}:
            target = movable[(index + 1) % len(movable)] if index >= 0 else movable[0]
        elif key in {Qt.Key.Key_Left, Qt.Key.Key_Up}:
            target = movable[(index - 1) % len(movable)] if index >= 0 else movable[-1]
        elif key == Qt.Key.Key_Home:
            target = movable[0]
        elif key == Qt.Key.Key_End:
            target = movable[-1]
        else:
            super().keyPressEvent(event)
            return

        target.setFocus(Qt.FocusReason.TabFocusReason)
        self._ensure_visible(target)
        event.accept()

    def _ensure_visible(self, step: WorkflowStep) -> None:
        """Scroll ``step`` into view, which only matters in the compact layout."""
        if self._mode is not NavigatorMode.COMPACT:
            return
        self._scroll.ensureWidgetVisible(step, xmargin=Navigator.ARROW_DEPTH)


__all__ = [
    "COLUMNS_NARROW",
    "FIRST_ROW_STEPS",
    "LayoutPlan",
    "NavigatorMode",
    "StepPlacement",
    "WorkflowNavigator",
]
