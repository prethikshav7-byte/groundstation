"""
ground_station/widgets/health_strip.py
══════════════════════════════════════
§6.5 — "source state, receiver heartbeat, and per-vehicle link state,
RSSI, and packet-success rate — all three levels from §3.6 visible at
once."

The whole point of this widget is that the three levels are visibly
separate. §3.6: "A healthy USB port proves nothing about either vehicle's
radio link. Source health, receiver health, and vehicle health are three
different facts and are displayed as three different indicators." A
single merged "connection: OK" light would be the failure this strip
exists to prevent.

It also carries the §3.5 rule. When both vehicles go silent at once, the
strip says so as one ground-side event rather than lighting two vehicle
alarms — "the app must never interpret simultaneous loss of both vehicles
as two independent vehicle failures — that reads as a catastrophic flight
event when it is far more likely to be a USB cable."
"""
from __future__ import annotations

from typing import Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from ..models import (
    ReceiverState, SourceState, VehicleID, VehicleLinkState,
)
from ..parser import StreamCounters
from ..theme import ThemeManager


def _dot(colour: str) -> str:
    return f"color: {colour}; font-size: 14px;"


class _Indicator(QWidget):
    """One labelled status light."""

    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        c = ThemeManager.C()
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 8, 2)
        row.setSpacing(6)

        self._dot = QLabel("●")
        self._dot.setStyleSheet(_dot(c.TEXT_MUTED))

        self._caption = QLabel(caption)
        self._caption.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self._caption.setStyleSheet(f"color: {c.TEXT_DIM};")

        self._value = QLabel("—")
        self._value.setFont(QFont("monospace", 8))
        self._value.setStyleSheet(f"color: {c.TEXT};")

        row.addWidget(self._dot)
        row.addWidget(self._caption)
        row.addWidget(self._value)
        row.addStretch()

    def set(self, text: str, colour: str) -> None:
        self._value.setText(text)
        self._dot.setStyleSheet(_dot(colour))


class HealthStrip(QWidget):
    """§6.5 — the three levels, side by side, always visible."""

    def __init__(self, parent=None):
        super().__init__(parent)
        c = ThemeManager.C()
        self.setStyleSheet(
            f"background-color: {c.BG_PANEL}; "
            f"border-bottom: 1px solid {c.BORDER};")
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        # §18 — pin height so the strip never shifts the page below it
        # when font rendering varies between platforms or display scales.
        self.setFixedHeight(36)

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(4)

        # Level 1 — the USB source.
        self.source = _Indicator("SOURCE")
        # Level 2 — the ground receiver, its own status item per §3.5.
        self.receiver = _Indicator("RECEIVER")
        row.addWidget(self.source)
        row.addWidget(self._divider())
        row.addWidget(self.receiver)
        row.addWidget(self._divider())

        # Level 3 — each vehicle, separately.
        self.vehicles: Dict[VehicleID, _Indicator] = {}
        for vid in VehicleID:
            ind = _Indicator(vid.value)
            self.vehicles[vid] = ind
            row.addWidget(ind)

        row.addStretch()

        self._warning = QLabel("")
        self._warning.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self._warning.setStyleSheet(f"color: {c.AMBER};")
        row.addWidget(self._warning)

    def _divider(self) -> QFrame:
        c = ThemeManager.C()
        f = QFrame()
        f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet(f"color: {c.BORDER};")
        return f

    # ── updates ──────────────────────────────────────────────────────────

    def set_source(self, state: SourceState) -> None:
        c = ThemeManager.C()
        colour = {
            SourceState.CONNECTED: c.GREEN,
            SourceState.CONNECTING: c.AMBER,
            SourceState.NO_DATA: c.AMBER,
            SourceState.LINK_ERROR: c.RED,
            SourceState.DISCONNECTED: c.TEXT_MUTED,
        }[state]
        self.source.set(state.value, colour)

    def set_receiver(self, state: ReceiverState) -> None:
        c = ThemeManager.C()
        colour = c.GREEN if state is ReceiverState.ALIVE else c.RED
        self.receiver.set(state.value, colour)

    def set_vehicle(self, vid: VehicleID, state: VehicleLinkState,
                    counters: Optional[StreamCounters] = None,
                    rssi: Optional[float] = None,
                    success: Optional[float] = None) -> None:
        c = ThemeManager.C()
        colour = {
            VehicleLinkState.RECEIVING: c.GREEN,
            VehicleLinkState.STALE: c.AMBER,
            VehicleLinkState.LOST: c.RED,
        }[state]

        # §3.7 — RSSI and rolling packet-success alongside the state.
        # "During a flight this is the single most useful diagnostic
        # available — it is what distinguishes 'the CanSat is failing'
        # from 'the CanSat is behind the rocket body relative to our
        # antenna'."
        bits = [state.value]
        if rssi is not None:
            bits.append(f"{rssi:.0f} dBm")
        if success is not None:
            bits.append(f"{success:.0f}%")
        if counters is not None and counters.rejected_total:
            bits.append(f"rej {counters.rejected_total}")
        self.vehicles[vid].set("  ".join(bits), colour)

    def set_ground_side_warning(self, on: bool) -> None:
        """§3.5 — one ground-side event, not two vehicle failures."""
        self._warning.setText(
            "BOTH VEHICLES SILENT — ground-side fault suspected" if on else ""
        )
