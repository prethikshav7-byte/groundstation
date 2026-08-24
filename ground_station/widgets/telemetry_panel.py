"""
ground_station/widgets/telemetry_panel.py
═════════════════════════════════════════
§9.3 — "exactly three elements": panel title, current status, and a
single Pause/Resume toggle. Nothing else goes in this panel.

§9.4 is the subtle one and the reason this widget owns a buffer:

    "Pause is display-only. It freezes the view. Packets continue to be
    received, parsed, buffered, and logged. On Resume the graphs fill in
    everything that arrived while paused — a pause never creates a gap or
    triggers §7.5."

So pausing must not stop the parser, must not stop logging, and must not
look like packet loss. The samples that arrive while paused are held here
and replayed into the graphs on resume, which is what keeps §7.5's tiers
from firing on an operator action. A pause implemented by dropping
samples would show up as a GAP or OUTAGE and draw a reconciliation
segment across data the app actually received — inventing an estimate to
cover a hole of its own making.

§9.5: pausing one vehicle must not pause the other. One instance per
vehicle, no shared state.

Explicitly NOT HIBERNATE (§10.4) — that stops the vehicle sampling and
lives on the Command dashboard. The label says "Pause Display" so the two
cannot be confused at a glance.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ..models import VehicleLinkState
from ..theme import ThemeManager

#: A replayed sample: (graph_key, trace, x, y, tier, state, v_entry)
Buffered = Tuple[str, str, float, float, object, object, Optional[float]]


class TelemetryPanel(QWidget):
    """Per-vehicle display pause (§9.3–9.5)."""

    paused_changed = pyqtSignal(bool)

    #: Safety valve on the held buffer (§13.2 — no unbounded growth). At
    #: 10 Hz across 6 channels this is roughly 9 minutes of pause, far
    #: beyond any plausible operator pause. If it is ever hit, the oldest
    #: held samples are dropped and the operator is told — silently
    #: discarding them would produce a gap on resume that looks like
    #: packet loss, which is precisely what §9.4 forbids.
    BUFFER_MAX = 36000

    def __init__(self, vehicle_label: str, parent=None):
        super().__init__(parent)
        c = ThemeManager.C()
        self._paused = False
        self._buffer: List[Buffered] = []
        self._overflowed = False
        self._replay: Optional[Callable[[Buffered], None]] = None

        self.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)

        # ── element 1: panel title ───────────────────────────────────────
        title = QLabel("Telemetry")
        title.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {c.CYAN};")
        outer.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(8)

        # ── element 2: current status ────────────────────────────────────
        self._status = QLabel("No Signal")
        self._status.setFont(QFont("monospace", 9))
        self._status.setStyleSheet(f"color: {c.TEXT_DIM};")
        row.addWidget(self._status)
        row.addStretch()

        # ── element 3: the single toggle ─────────────────────────────────
        self._button = QPushButton("Pause Display")
        self._button.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        self._button.setCursor(self.cursor())
        self._button.clicked.connect(self._toggle)
        self._style_button()
        row.addWidget(self._button)

        outer.addLayout(row)

        self._note = QLabel("")
        self._note.setFont(QFont("monospace", 8))
        self._note.setStyleSheet(f"color: {c.AMBER};")
        self._note.setVisible(False)
        outer.addWidget(self._note)

    # ── wiring ───────────────────────────────────────────────────────────

    def set_replay_target(self, fn: Callable[[Buffered], None]) -> None:
        """Where held samples go on resume — the page's graph fan-out."""
        self._replay = fn

    # ── state ────────────────────────────────────────────────────────────

    @property
    def paused(self) -> bool:
        return self._paused

    def _toggle(self) -> None:
        self._paused = not self._paused
        self._button.setText("Resume Display" if self._paused
                             else "Pause Display")
        self._style_button()
        if not self._paused:
            self._flush()
        self._refresh_status()
        self.paused_changed.emit(self._paused)

    def _style_button(self) -> None:
        c = ThemeManager.C()
        accent = c.AMBER if self._paused else c.CYAN
        self._button.setStyleSheet(
            f"QPushButton {{ background-color: {c.BG_INPUT}; "
            f"color: {accent}; border: 1px solid {accent}; "
            f"border-radius: 4px; padding: 4px 12px; }}"
            f"QPushButton:hover {{ background-color: {c.BG_CARD_HOVER}; }}")

    # ── §9.4 buffering ───────────────────────────────────────────────────

    def accept(self, sample: Buffered) -> bool:
        """Offer one sample.

        Returns True if the caller should draw it now, False if it was
        held for replay. Either way the packet has already been parsed,
        counted and logged upstream — pause never reaches back into the
        data path.
        """
        if not self._paused:
            return True
        self._buffer.append(sample)
        if len(self._buffer) > self.BUFFER_MAX:
            del self._buffer[:len(self._buffer) - self.BUFFER_MAX]
            if not self._overflowed:
                self._overflowed = True
                self._note.setText(
                    "Pause buffer full — oldest held samples dropped. "
                    "The log is unaffected.")
                self._note.setVisible(True)
        return False

    def _flush(self) -> None:
        if self._replay is None:
            self._buffer.clear()
            return
        held, self._buffer = self._buffer, []
        for sample in held:
            self._replay(sample)
        self._overflowed = False
        self._note.setVisible(False)

    # ── status text ──────────────────────────────────────────────────────

    def set_link_state(self, state: VehicleLinkState) -> None:
        self._link = state
        self._refresh_status()

    _link: VehicleLinkState = VehicleLinkState.LOST

    def _refresh_status(self) -> None:
        c = ThemeManager.C()
        if self._paused:
            # §9.4 — say plainly that data is still arriving, so a paused
            # display is never mistaken for a stopped link.
            held = len(self._buffer)
            text = f"Paused — {held} samples held" if held else "Paused"
            colour = c.AMBER
        elif self._link is VehicleLinkState.RECEIVING:
            text, colour = "Live", c.GREEN
        elif self._link is VehicleLinkState.STALE:
            text, colour = "Stale", c.AMBER
        else:
            text, colour = "No Signal", c.RED
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {colour};")
