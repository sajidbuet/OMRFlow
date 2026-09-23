"""The single-line workflow ribbon, and the layout algorithm behind it.

Purpose:
    Present the nine workflow stages as one connected horizontal process
    inside the application chrome row - **always on one line** - choosing
    between showing them all, scrolling them, or collapsing to the current
    one, according to the width actually available.

Responsibilities:
    * :class:`RibbonMode` - the three layouts, in order of decreasing width.
    * :meth:`WorkflowRibbon.plan_for_width` - the decision: given a width,
      which layout, and where does every step go. A query, not a mutation, so
      a test can ask "what would you do at 600 pixels?" without resizing
      anything or waiting for a Qt layout pass.
    * :class:`WorkflowRibbon` - the widget: applies a plan, owns the
      current/enabled state and the density level, keeps the active step in
      view, and owns the narrow layout's stage selector.

What does NOT belong here:
    * How a step draws itself, and how wide it is at a given density - that is
      :mod:`omr_scanner.gui.widgets.workflow_step`.
    * Which stages are available when. The ribbon is *told*; the main window,
      which knows the project state, decides.
    * Page routing, and the previous/next buttons themselves. The ribbon
      emits :attr:`WorkflowRibbon.step_activated` and answers
      :meth:`WorkflowRibbon.adjacent_key`; the chrome row owns the buttons and
      the main window owns what a navigation means.

Why the workflow never wraps:
    A workflow is an ordered sequence, and a second row breaks the one thing
    the chevrons exist to show - that each stage feeds the next. The predecessor
    of this module had a two-row and a two-column layout; both are gone. When
    the nine no longer fit, the strip scrolls, and when scrolling would show
    barely one stage at a time it collapses to the current stage plus a
    selector listing all nine. Neither hides a stage: every one of the nine is
    always present, always enabled or disabled for its own reason, and always
    reachable.

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

Why there is no visible scrollbar in the scrolling layout:
    The whole ribbon is one 34-pixel line inside a 46-pixel chrome row. A
    horizontal scrollbar would take a third of that line's height from the
    chevrons, which is what clipped them in the previous design. The brief
    explicitly allows a cleaner mechanism instead, so scrolling is driven by
    the wheel (vertical and horizontal, with or without Shift), by the
    previous/next buttons beside the strip, and by the automatic scroll that
    brings the active stage into view after every navigation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.theme import Density, Navigator, Stroke
from omr_scanner.gui.widgets.workflow_step import (
    StepGeometry,
    StepShape,
    WorkflowStep,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from PySide6.QtGui import QAction, QKeyEvent, QResizeEvent, QWheelEvent

logger = logging.getLogger(__name__)

MIN_SCROLL_STEPS = 3
"""How many stages a scrolling strip must be able to show at once.

Below this the strip stops being a view of a process and becomes a keyhole:
an operator scrolling nine stages past a window barely one-and-a-bit stages
wide has a worse time than one who sees the current stage and opens a list of
all nine. That is the boundary between
:attr:`RibbonMode.SCROLL` and :attr:`RibbonMode.CURRENT_ONLY`, and it is
measured against the three *widest* labels in the font actually in use - never
against a screen resolution.
"""

FLYOUT_ACCESSIBLE_NAME = "Workflow stages"
"""What a screen reader calls the narrow layout's stage selector."""


class StageSpec(Protocol):
    """What the ribbon needs to know about a workflow stage.

    A structural type rather than an import of
    :class:`~omr_scanner.gui.pages.catalog.WorkflowPageSpec`, for two
    reasons. It documents the ribbon's whole dependency on the rest of the
    application in four attributes - and it keeps the import graph acyclic:
    ``gui.pages`` imports this package, so this package importing
    ``gui.pages`` back would be a cycle that fails at interpreter start-up
    rather than at run time.
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


class RibbonMode(Enum):
    """The three layouts, in order of decreasing available width.

    Attributes:
        FULL: All nine stages in one chevron row, stretched to the margin.
        SCROLL: All nine at their natural width in one chevron row, wider
            than the viewport, scrolled horizontally.
        CURRENT_ONLY: The active stage alone, as a tile carrying a caret; the
            other eight are one hover, click or keypress away in the flyout.

    There is deliberately no wrapping mode and no grid mode. See the module
    docstring.
    """

    FULL = "full"
    SCROLL = "scroll"
    CURRENT_ONLY = "current_only"


@dataclass(frozen=True)
class StepPlacement:
    """Where one step goes, and in what geometry.

    Attributes:
        key: The stage's identifier.
        rect: Position and size within the ribbon's strip.
        geometry: The shape the step should draw itself in there.
    """

    key: str
    rect: QRect
    geometry: StepGeometry


@dataclass(frozen=True)
class LayoutPlan:
    """A complete answer to "how should the ribbon look at this width?".

    Attributes:
        mode: Which layout was chosen.
        placements: One per *visible* step. Nine in
            :attr:`RibbonMode.FULL` and :attr:`RibbonMode.SCROLL`, and exactly
            one - the active stage - in :attr:`RibbonMode.CURRENT_ONLY`.
        strip_size: The size the scrolled strip needs. Wider than the
            viewport only in :attr:`RibbonMode.SCROLL`.
    """

    mode: RibbonMode
    placements: tuple[StepPlacement, ...]
    strip_size: QSize

    @property
    def rows(self) -> int:
        """How many distinct vertical positions the placements occupy.

        Always 1. Asserted by the tests rather than trusted, because "the
        workflow never wraps" is the single hardest line in the brief this
        module answers to.
        """
        return len({placement.rect.top() for placement in self.placements}) or 1


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


class WorkflowRibbon(QWidget):
    """The workflow stages, as one responsive line of chevrons.

    Args:
        specs: The stages, in workflow order. Each needs ``key``, ``title``,
            ``summary`` and an ``icon`` name; the page catalog's
            :class:`~omr_scanner.gui.pages.catalog.WorkflowPageSpec` provides
            all four.
        density: Initial density level - see
            :class:`~omr_scanner.gui.theme.Density`. Clamped.
        parent: Optional Qt parent.

    Signals:
        step_activated: A stage was clicked, activated from the keyboard, or
            chosen from the flyout. Carries the stage's key. Emitted only for
            enabled stages, and *not* emitted by :meth:`set_current_key`, so a
            programmatic page change cannot loop back into a navigation
            request.
        density_changed: The density level changed. Carries the new level, so
            the window can persist it.

    Testability:
        :meth:`plan_for_width` is the layout decision, separated from applying
        it. A test asserts on the returned :class:`LayoutPlan` - mode, row
        count, geometry, whether anything is clipped - for any width, with no
        window, no show, and no waiting for Qt to lay anything out.
    """

    step_activated = Signal(str)
    density_changed = Signal(int)

    def __init__(
        self,
        specs: Iterable[StageSpec],
        density: int = Density.DEFAULT,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("workflowRibbon")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(Navigator.STEP_HEIGHT + 2 * Navigator.STRIP_V_PADDING)

        self._steps: list[WorkflowStep] = []
        self._current_key: str | None = None
        self._mode = RibbonMode.FULL
        self._density = Density.clamp(density)
        self._relaying_out = False

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("workflowRibbonScroll")
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidgetResizable(False)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Never visible - see the module docstring. The bar still exists and
        # still carries the scroll position; only its widget is hidden, which
        # is what keeps the whole ribbon one 34-pixel line.
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)

        self._strip = QWidget()
        self._strip.setObjectName("workflowRibbonStrip")
        self._strip.setAutoFillBackground(False)
        self._scroll.setWidget(self._strip)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Navigator.STRIP_H_PADDING,
            Navigator.STRIP_V_PADDING,
            Navigator.STRIP_H_PADDING,
            Navigator.STRIP_V_PADDING,
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
            # The hover reveal in the narrow layout. An event filter rather
            # than a subclass hook, because hovering a step means nothing in
            # the other two layouts and a step should not have to know which
            # one is on screen.
            step.installEventFilter(self)
            self._steps.append(step)

        self.flyout = self._build_flyout()

        if self._steps:
            self.set_current_key(self._steps[0].key)
        self._relayout()

    # ------------------------------------------------------------------
    # The stages
    # ------------------------------------------------------------------
    @property
    def steps(self) -> tuple[WorkflowStep, ...]:
        """The stage buttons, in workflow order. All nine, in every layout."""
        return tuple(self._steps)

    @property
    def keys(self) -> tuple[str, ...]:
        """The stage keys, in workflow order."""
        return tuple(step.key for step in self._steps)

    def step(self, key: str) -> WorkflowStep | None:
        """The stage button for ``key``, or ``None``."""
        return next((step for step in self._steps if step.key == key), None)

    def index_of(self, key: str) -> int:
        """``key``'s 0-based position in the workflow, or ``-1``."""
        return next(
            (index for index, step in enumerate(self._steps) if step.key == key), -1
        )

    def adjacent_key(self, delta: int) -> str | None:
        """The stage ``delta`` places from the current one, if it is available.

        Args:
            delta: ``-1`` for the previous stage, ``+1`` for the next.

        Returns:
            The neighbouring stage's key, or ``None`` when there is no such
            neighbour (the ends of the workflow) or when it is disabled.

        Deliberately the *immediate* neighbour and not "the next enabled one":
        the brief asks the arrows to move one stage at a time, and skipping a
        locked stage would quietly take an operator somewhere they did not
        ask to go. A disabled neighbour therefore disables the button, which
        is visible, rather than redirecting it, which is not.
        """
        index = self.index_of(self._current_key or "")
        if index < 0:
            return None
        target = index + delta
        if not 0 <= target < len(self._steps):
            return None
        step = self._steps[target]
        return step.key if step.isEnabled() else None

    # ------------------------------------------------------------------
    # Current stage
    # ------------------------------------------------------------------
    def current_key(self) -> str | None:
        """The active stage's key, or ``None`` before one is set."""
        return self._current_key

    def set_current_key(self, key: str) -> bool:
        """Mark ``key`` as the active stage, and make sure it can be seen.

        Args:
            key: The stage to activate.

        Returns:
            ``True`` when ``key`` is one of this ribbon's stages.

        Does not emit :attr:`step_activated`: this is how the *window* tells
        the ribbon which page is showing, and a signal here would turn every
        programmatic page change into a navigation request and back again.

        Whatever started the navigation - a click, an arrow button, a menu
        command, a keyboard shortcut or application logic - it ends here, so
        this is the one place that has to make the active stage visible. In
        the scrolling layout that means scrolling to it; in the narrow layout
        it means the strip now shows a different stage altogether.
        """
        target = self.step(key)
        if target is None:
            return False
        changed = key != self._current_key
        self._current_key = key
        for step in self._steps:
            step.setChecked(step is target)
        if changed and self._mode is RibbonMode.CURRENT_ONLY:
            # A different stage is the only visible one now, so the strip has
            # to be rebuilt rather than merely scrolled.
            self._relayout()
        self.ensure_current_visible()
        return True

    def ensure_current_visible(self) -> None:
        """Bring the active stage into the viewport.

        A no-op outside the scrolling layout: the full layout shows every
        stage already, and the narrow layout shows the active one and nothing
        else.
        """
        if self._mode is not RibbonMode.SCROLL or self._current_key is None:
            return
        step = self.step(self._current_key)
        if step is None:  # pragma: no cover - defensive
            return
        self._scroll.ensureWidgetVisible(step, xmargin=Navigator.SCROLL_MARGIN)

    def _on_step_clicked(self) -> None:
        step = self.sender()
        if not isinstance(step, WorkflowStep):  # pragma: no cover - defensive
            return
        # A checkable button toggles itself on click. The ribbon, not the
        # click, decides what is checked: if the window refuses the
        # navigation, the check must not stick.
        step.setChecked(step.key == self._current_key)
        if self._mode is RibbonMode.CURRENT_ONLY:
            # The only step on screen *is* the current page, so activating it
            # would navigate nowhere. Clicking it opens the selector instead,
            # which is what the caret on it promises - and it is the
            # non-hover, non-keyboard route the brief requires.
            self.open_flyout()
            return
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
    # Density
    # ------------------------------------------------------------------
    @property
    def density(self) -> int:
        """The density level currently applied."""
        return self._density

    def set_density(self, level: int) -> bool:
        """Adopt density ``level``, clamped to the supported range.

        Args:
            level: The requested level.

        Returns:
            Whether the level actually changed. ``False`` at either end of the
            range, which is how the chrome's ``-``/``+`` buttons know to
            disable themselves rather than silently doing nothing.

        Changes how much room a step takes, never what a step *is*: the same
        nine stages, in the same order, with the same enabled state and the
        same font. Only the layout decision downstream of it can differ, and
        that is the point - tightening the ribbon is how an operator fits all
        nine on a 1366-pixel display.
        """
        clamped = Density.clamp(level)
        if clamped == self._density:
            return False
        self._density = clamped
        self._relayout()
        self.density_changed.emit(clamped)
        return True

    def can_increase_density(self) -> bool:
        """Whether ``+`` would change anything."""
        return self._density < Density.MAXIMUM

    def can_decrease_density(self) -> bool:
        """Whether ``-`` would change anything."""
        return self._density > Density.MINIMUM

    # ------------------------------------------------------------------
    # The layout decision
    # ------------------------------------------------------------------
    @property
    def mode(self) -> RibbonMode:
        """The layout currently applied."""
        return self._mode

    def _chevron(self, *, first_in_row: bool) -> StepGeometry:
        return StepGeometry(
            shape=StepShape.CHEVRON,
            density=self._density,
            has_left_notch=not first_in_row,
            has_right_point=True,
        )

    def _current_only_geometry(self) -> StepGeometry:
        """The lone tile the narrow layout shows, caret and all."""
        return StepGeometry(
            shape=StepShape.TILE,
            density=self._density,
            has_left_notch=False,
            has_right_point=False,
            has_menu_indicator=True,
        )

    def _chevron_row_width(
        self, steps: Sequence[WorkflowStep]
    ) -> tuple[int, list[int], list[StepGeometry]]:
        """Natural width of one chevron row, plus its per-step parts.

        Returns:
            ``(total, widths, geometries)``. ``total`` accounts for the
            overlap: each step after the first advances by its width less the
            arrow depth, plus the visible seam.
        """
        geometries = [self._chevron(first_in_row=index == 0) for index in range(len(steps))]
        widths = [
            step.natural_width(geom) for step, geom in zip(steps, geometries, strict=True)
        ]
        if not widths:
            return 0, widths, geometries
        overlap = sum(geom.arrow_depth - Navigator.STEP_GAP for geom in geometries[1:])
        return sum(widths) - overlap, widths, geometries

    def _place_chevron_row(
        self, steps: Sequence[WorkflowStep], available: int
    ) -> list[StepPlacement]:
        """Lay one chevron row out across ``available`` pixels.

        The row is stretched to fill ``available`` when there is surplus, so
        it reaches the margin rather than trailing off; it is never compressed
        below its natural width, because the caller only passes an
        ``available`` at least as large as that.
        """
        total, widths, geometries = self._chevron_row_width(steps)
        if not widths:
            return []
        extra = _distribute(available - total, widths)
        placements: list[StepPlacement] = []
        x = 0
        for step, width, bonus, geom in zip(steps, widths, extra, geometries, strict=True):
            final_width = width + bonus
            placements.append(
                StepPlacement(
                    key=step.key,
                    rect=QRect(x, 0, final_width, geom.height),
                    geometry=geom,
                )
            )
            x += geom.advance_for(final_width)
        return placements

    def scroll_floor(self) -> int:
        """The narrowest viewport at which scrolling is still worth offering.

        Room for :data:`MIN_SCROLL_STEPS` of the widest stages at the current
        density. Measured, never a resolution: a larger Windows text size
        moves this boundary outward, which is correct, because that is when a
        scrolling strip genuinely stops showing enough of the workflow.
        """
        if len(self._steps) <= MIN_SCROLL_STEPS:
            return 0
        _, widths, geometries = self._chevron_row_width(self._steps)
        widest = sorted(widths, reverse=True)[:MIN_SCROLL_STEPS]
        overlap = (MIN_SCROLL_STEPS - 1) * (geometries[-1].arrow_depth - Navigator.STEP_GAP)
        return sum(widest) - overlap

    def plan_for_width(self, available_width: int) -> LayoutPlan:
        """Decide the layout for ``available_width`` pixels of strip.

        Args:
            available_width: Width the steps may occupy - the scroll area's
                viewport, not the window.

        Returns:
            The chosen :class:`LayoutPlan`. Its ``rows`` is always 1.

        Three layouts, tried widest first, and the font is never reduced to
        make one of them work. Reaching :attr:`RibbonMode.CURRENT_ONLY` does
        not hide a stage in any sense that matters: the other eight are in the
        flyout, still enabled or disabled for their own reasons, still
        reachable by hover, click and keyboard.
        """
        available = max(available_width, 1)

        if not self._steps:  # pragma: no cover - the catalog is never empty
            return LayoutPlan(RibbonMode.FULL, (), QSize(available, Navigator.STEP_HEIGHT))

        full_total, _, _ = self._chevron_row_width(self._steps)
        if full_total <= available:
            placements = self._place_chevron_row(self._steps, available)
            return LayoutPlan(
                mode=RibbonMode.FULL,
                placements=tuple(placements),
                strip_size=QSize(available, Navigator.STEP_HEIGHT),
            )

        if available >= self.scroll_floor():
            placements = self._place_chevron_row(self._steps, full_total)
            return LayoutPlan(
                mode=RibbonMode.SCROLL,
                placements=tuple(placements),
                strip_size=QSize(full_total, Navigator.STEP_HEIGHT),
            )

        return self._plan_current_only(available)

    def _plan_current_only(self, available: int) -> LayoutPlan:
        """The active stage alone, filling the viewport, with its caret."""
        key = self._current_key or self._steps[0].key
        step = self.step(key) or self._steps[0]
        geometry = self._current_only_geometry()
        width = max(available, step.natural_width(geometry))
        return LayoutPlan(
            mode=RibbonMode.CURRENT_ONLY,
            placements=(
                StepPlacement(
                    key=step.key,
                    rect=QRect(0, 0, width, geometry.height),
                    geometry=geometry,
                ),
            ),
            strip_size=QSize(width, Navigator.STEP_HEIGHT),
        )

    # ------------------------------------------------------------------
    # Applying it
    # ------------------------------------------------------------------
    def _viewport_width(self) -> int:
        """Width the strip may occupy without scrolling.

        Read from the scroll area's viewport, so the ribbon's padding is
        already accounted for - but only once the viewport's width agrees with
        this widget's own. Until Qt has run a layout pass the viewport still
        reports whatever size it was constructed at, and planning against that
        produced the wrong layout for one pass after every programmatic
        resize. Falling back to this widget's width less its padding is the
        same number the viewport will report once it catches up.
        """
        own = max(self.width() - 2 * Navigator.STRIP_H_PADDING, 1)
        viewport_width = self._scroll.viewport().width()
        if abs(viewport_width - own) <= Stroke.HAIRLINE:
            return viewport_width
        return own

    def _activate_layout(self) -> None:
        """Lay the scroll area out against this widget's *current* size, now.

        ``invalidate()`` before ``activate()``, and the first of those is the
        load-bearing one: `QLayout.activate` returns immediately when the
        layout is not marked dirty, and nothing here marks it dirty on its
        own.
        """
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()

    def _relayout(self) -> None:
        """Recompute and apply the layout.

        Guarded against re-entry: applying a plan resizes the strip, which can
        arrive back here, and the guard is what makes the sequence terminate.
        Cheap enough to run on every resize event - it repositions nine
        existing widgets and creates none, which is what keeps a window drag
        from rebuilding anything.
        """
        if self._relaying_out or not self._steps:
            return
        self._relaying_out = True
        try:
            self._activate_layout()
            self._apply(self.plan_for_width(self._viewport_width()))
        finally:
            self._relaying_out = False

    def _apply(self, plan: LayoutPlan) -> None:
        """Position the steps, and hide the ones this layout does not show.

        "Hidden" here is a statement about *this layout's strip* and nothing
        else. A stage hidden in the narrow layout keeps its enabled state, its
        tooltip and its place in the workflow, and is listed in the flyout
        immediately below. Nothing is destroyed and nothing is disabled.
        """
        self._activate_layout()

        by_key = {placement.key: placement for placement in plan.placements}
        for step in self._steps:
            placement = by_key.get(step.key)
            if placement is None:
                step.setVisible(False)
                continue
            step.apply_geometry(placement.geometry)
            step.setGeometry(placement.rect)
            step.setVisible(True)

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

        mode_changed = plan.mode is not self._mode
        self._mode = plan.mode
        if mode_changed:
            logger.debug("Workflow ribbon layout: %s", plan.mode.value)
            if plan.mode is not RibbonMode.CURRENT_ONLY and self.flyout.isVisible():
                # The layout that needed a selector is gone; leaving its menu
                # open over a ribbon that now shows every stage is confusing.
                self.flyout.close()
        self.ensure_current_visible()

    def sizeHint(self) -> QSize:
        """Preferred size: the whole row at the current density."""
        total, _, _ = self._chevron_row_width(self._steps)
        return QSize(total + 2 * Navigator.STRIP_H_PADDING, self.height())

    def minimumSizeHint(self) -> QSize:
        """The ribbon never demands width: it changes layout instead.

        A minimum width here would become the window's minimum width, which
        would make the narrow layout unreachable - the opposite of the point
        of having it.
        """
        return QSize(2 * Navigator.STRIP_H_PADDING, self.height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-decide the layout for the new width."""
        super().resizeEvent(event)
        self._relayout()

    def changeEvent(self, event: QEvent) -> None:
        """Re-decide the layout when the font changes.

        This is how a Windows text-scaling change becomes a layout change:
        the steps re-measure themselves, the width that used to fit nine
        chevrons no longer does, and the ribbon scrolls instead of clipping.
        """
        super().changeEvent(event)
        self._relayout()

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Scroll the strip horizontally, on either wheel axis.

        The vertical axis is accepted as well as the horizontal because the
        strip only ever scrolls sideways, and a mouse with one wheel is the
        common case. Shift+wheel arrives as a horizontal delta from Qt, and a
        touchpad's two-finger swipe as a horizontal one, so all three routes
        the brief names land here. Ignored outside the scrolling layout, so
        the event falls through to whatever is behind it rather than being
        silently eaten.
        """
        if self._mode is not RibbonMode.SCROLL:
            super().wheelEvent(event)
            return
        bar = self._scroll.horizontalScrollBar()
        delta = event.angleDelta().x() or event.angleDelta().y()
        if delta == 0 or bar is None:  # pragma: no cover - defensive
            super().wheelEvent(event)
            return
        notches = delta / 120.0
        bar.setValue(bar.value() - round(notches * Navigator.WHEEL_STEP))
        event.accept()

    # ------------------------------------------------------------------
    # The narrow layout's stage selector
    # ------------------------------------------------------------------
    def _build_flyout(self) -> QMenu:
        """One menu entry per stage, in workflow order.

        A `QMenu` rather than a hand-built popover, and not for brevity: it
        already closes on Escape, closes when clicked outside, is navigable
        with the arrow keys, announces itself to a screen reader, and floats
        above the page instead of pushing it down. Every one of those is a
        requirement here, and each would be a defect waiting to happen in a
        bespoke widget.
        """
        menu = QMenu(self)
        menu.setObjectName("workflowFlyout")
        menu.setAccessibleName(FLYOUT_ACCESSIBLE_NAME)
        self._flyout_group = QActionGroup(menu)
        self._flyout_group.setExclusive(True)
        self._flyout_actions: dict[str, QAction] = {}
        for step in self._steps:
            action = menu.addAction(step.display_text)
            action.setObjectName(f"workflowFlyout_{step.key}")
            action.setIcon(step.stage_icon)
            action.setCheckable(True)
            action.setActionGroup(self._flyout_group)
            action.triggered.connect(
                lambda _checked=False, key=step.key: self._on_flyout_chosen(key)
            )
            self._flyout_actions[step.key] = action
        return menu

    def _on_flyout_chosen(self, key: str) -> None:
        step = self.step(key)
        if step is None or not step.isEnabled():  # pragma: no cover - disabled actions
            return
        self.step_activated.emit(key)

    def refresh_flyout(self) -> None:
        """Bring the selector's entries in step with the ribbon.

        Called immediately before it is shown rather than on every state
        change, because nothing can be looking at it in between.
        """
        for step in self._steps:
            action = self._flyout_actions[step.key]
            action.setEnabled(step.isEnabled())
            action.setChecked(step.key == self._current_key)
            action.setToolTip(step.toolTip())
            action.setStatusTip(step.statusTip())

    def open_flyout(self, at: QPoint | None = None) -> None:
        """Show the stage selector.

        Args:
            at: Where to place it, in global coordinates. Defaults to
                directly beneath the ribbon's left edge, so it reads as
                belonging to the control it came from.

        ``popup()`` and deliberately not ``exec()``: ``exec()`` spins a nested
        modal event loop that only returns when the menu is dismissed, which
        hangs a headless run outright. This project has met that defect four
        times; this is not the fifth.
        """
        self.refresh_flyout()
        anchor = at if at is not None else self.mapToGlobal(QPoint(0, self.height()))
        self.flyout.popup(anchor)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Open the selector when the narrow layout's lone step is hovered.

        The hover is an *enhancement*, never the only route: the same selector
        opens on a click (:meth:`_on_step_clicked`) and from the keyboard
        (:meth:`keyPressEvent`), and the caret painted on the step says so
        without relying on the pointer ever being there.
        """
        if (
            event.type() is QEvent.Type.Enter
            and self._mode is RibbonMode.CURRENT_ONLY
            and isinstance(watched, WorkflowStep)
            and not self.flyout.isVisible()
        ):
            self.open_flyout()
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------
    def _movable_steps(self) -> list[WorkflowStep]:
        """The stages arrow-key focus may land on, in workflow order.

        Enabled *and* on screen: in the narrow layout there is exactly one
        visible step, and moving focus to a stage nobody can see would be a
        focus ring with no widget under it.
        """
        return [
            step for step in self._steps if step.isEnabled() and step.isVisibleTo(self)
        ]

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Move focus between stages, and open the selector in narrow mode.

        Left/Right move along the workflow, Home and End jump to its ends -
        the same keys in every layout, so an operator never has to know which
        one is on screen. Down and Alt+Down open the stage selector, which is
        the platform convention for a control that drops a list, and is the
        keyboard route the brief requires beside the hover one.
        """
        key = event.key()
        if (
            self._mode is RibbonMode.CURRENT_ONLY
            and key in {Qt.Key.Key_Down, Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter}
        ):
            self.open_flyout()
            event.accept()
            return

        movable = self._movable_steps()
        if not movable:
            super().keyPressEvent(event)
            return

        focused = next((step for step in movable if step.hasFocus()), None)
        index = movable.index(focused) if focused is not None else -1

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
        if self._mode is RibbonMode.SCROLL:
            self._scroll.ensureWidgetVisible(target, xmargin=Navigator.SCROLL_MARGIN)
        event.accept()


__all__ = [
    "FLYOUT_ACCESSIBLE_NAME",
    "MIN_SCROLL_STEPS",
    "LayoutPlan",
    "RibbonMode",
    "StepPlacement",
    "WorkflowRibbon",
]
