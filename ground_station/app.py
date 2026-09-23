"""
ground_station/app.py
═════════════════════
The application shell. Replaces the previous app.py entirely — that one
was built around RocketTelemetry / CanSatTelemetry and two unrelated
state enums, which §4.1 and §5.1 collapsed into one packet type and one
8-state machine.

Responsibilities, and nothing beyond them (§1.2): navigation, wiring the
link layer to the dashboards, and the §6.5 health strip. It computes no
telemetry values and makes no decisions — §1.4, "the app has no autonomy
and no watchdog that actuates anything", is a property of there being no
code here that could.

Five dashboards are specified (§6). Three exist after Phase 2: Overview,
Rocket and CanSat. Command (§10) and CanSat Experiment (§11) arrive in
Phase 3; their navigation entries are present and disabled, so the shape
of the app is visible and the buttons do not silently do nothing.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .models import (
    ReceiverState, RejectedLine, SourceState, TelemetryPacket, VehicleID,
    VehicleLinkState,
)
from .parser import IngestResult
from .commands import CommandCentre
from .pages.command import CommandPage
from .pages.com import ComPage
from .pages.experiment import ExperimentPage
from .pages.overview import OverviewPage
from .pages.vehicle import VehiclePage
from .theme import ThemeManager


class NavButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._restyle()

    def _restyle(self) -> None:
        c = ThemeManager.C()
        self.setStyleSheet(
            f"QPushButton {{ background-color: transparent; color: {c.TEXT_DIM}; "
            f"border: none; border-left: 3px solid transparent; "
            f"padding: 11px 16px; text-align: left; }}"
            f"QPushButton:hover {{ background-color: {c.BG_CARD_HOVER}; "
            f"color: {c.TEXT}; }}"
            f"QPushButton:checked {{ color: {c.CYAN}; "
            f"border-left: 3px solid {c.CYAN}; "
            f"background-color: {c.BG_CARD}; }}"
            f"QPushButton:disabled {{ color: {c.TEXT_MUTED}; }}")


class GroundStationApp(QMainWindow):
    """Main window."""

    def __init__(self, mode_label: str = "", simulator_mode: bool = False,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(
            f"Ground Station{f' — {mode_label}' if mode_label else ''}")
        self.resize(1560, 960)
        self.simulator_mode = simulator_mode

        self.overview_page = OverviewPage()
        self.pages: Dict[VehicleID, VehiclePage] = {
            v: VehiclePage(v) for v in VehicleID
        }
        self.rocket_page = self.pages[VehicleID.ROCKET]
        self.cansat_page = self.pages[VehicleID.CANSAT]

        # Fix 1 / §5.2 — give the backward_transition signal a receiver so
        # anomalies reach the status bar.  Connected here (once, at
        # construction) rather than per-packet so there is never a window
        # where the signal exists but has no handler attached.
        for page in self.pages.values():
            page.backward_transition.connect(self._on_notice)

        # §1.4 — the CommandCentre's transmit callback is set in
        # connect_supervisor. Until then it is a no-op, so a command
        # cannot escape before there is a link to carry it.
        self.command_centre = CommandCentre(
            transmit=lambda target, command, seq: None)
        # §2.5 — ENTER_SIMULATION is disabled while in Simulator Mode; the
        # page needs to know which mode the app launched in.
        self.command_page = CommandPage(self.command_centre, simulator_mode)
        self.command_page.notice.connect(self._on_notice)
        self.experiment_page = ExperimentPage()
        self.com_page = ComPage()

        self._build(mode_label)

    # ── construction ─────────────────────────────────────────────────────

    def _build(self, mode_label: str) -> None:
        c = ThemeManager.C()
        app_style = ThemeManager.main_stylesheet()
        self.setStyleSheet(app_style)

        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        row.addWidget(self._sidebar(mode_label))

        self.stack = QStackedWidget()
        self.stack.addWidget(self.overview_page)
        self.stack.addWidget(self.rocket_page)
        self.stack.addWidget(self.cansat_page)
        self.stack.addWidget(self.command_page)
        self.stack.addWidget(self.experiment_page)
        self.stack.addWidget(self.com_page)
        row.addWidget(self.stack, stretch=1)

        self.setCentralWidget(root)
        self._nav[0].setChecked(True)

    def _sidebar(self, mode_label: str) -> QWidget:
        c = ThemeManager.C()
        bar = QWidget()
        bar.setFixedWidth(210)
        bar.setStyleSheet(ThemeManager.sidebar_stylesheet())

        lay = QVBoxLayout(bar)
        lay.setContentsMargins(0, 16, 0, 16)
        lay.setSpacing(2)

        title = QLabel("  GROUND STATION")
        title.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {c.CYAN}; padding: 8px 12px;")
        lay.addWidget(title)

        if mode_label:
            badge = QLabel(f"  {mode_label}")
            badge.setFont(QFont("monospace", 8, QFont.Weight.Bold))
            # §2.5 — the mode is named unambiguously and stays on screen.
            # "Simulator Mode" is the app-side thing; the vehicle-side
            # telecommand is "Flight Software Simulation" and is never
            # given this label.
            badge.setStyleSheet(f"color: {c.GREEN}; padding: 0 12px 10px;")
            lay.addWidget(badge)

        self._nav: List[NavButton] = []
        entries = [
            ("Overview", 0, True),
            ("Rocket", 1, True),
            ("CanSat", 2, True),
            ("Command", 3, True),
            ("CanSat Experiment", 4, True),
            ("COM", 5, True),
        ]
        for text, index, enabled in entries:
            b = NavButton(text)
            b.setEnabled(enabled)
            b.clicked.connect(lambda _checked, i=index: self._navigate(i))
            self._nav.append(b)
            lay.addWidget(b)

        lay.addStretch()
        return bar

    def _navigate(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for i, b in enumerate(self._nav):
            b.setChecked(i == index)

    # ── link wiring ──────────────────────────────────────────────────────

    def connect_supervisor(self, supervisor) -> None:
        """Wire a LinkSupervisor (live) or a simulator exposing the same
        signals (§2.2 — only the data source differs)."""
        supervisor.rocket_packet.connect(self._on_rocket)
        supervisor.cansat_packet.connect(self._on_cansat)
        supervisor.source_state_changed.connect(self._on_source_state)
        supervisor.receiver_state_changed.connect(self._on_receiver_state)
        supervisor.vehicle_state_changed.connect(self._on_vehicle_state)
        supervisor.line_rejected.connect(self._on_rejected)
        supervisor.notice.connect(self._on_notice)
        self._supervisor = supervisor
        self.com_page.set_supervisor(supervisor)
        # §1.4 — the only place a transmit path is installed, installed
        # once. Nothing in the app calls send() on a timer; every call
        # originates in a widget's clicked handler.
        #
        # The sequence flows OUT of CommandCentre, not into it:
        # CommandCentre owns the §10.7 ack lifecycle, so the number it
        # assigns must be the number on the wire. Letting the link layer
        # assign its own would leave every command permanently
        # unacknowledged while the vehicle replied correctly.
        self.command_centre._transmit = (
            lambda target, command, sequence:
            supervisor.send_command(target, command, sequence))

    _supervisor = None
    _telemetry_connected: bool = False

    def _on_rocket(self, packet: TelemetryPacket, result: IngestResult) -> None:
        self._dispatch(VehicleID.ROCKET, packet, result)

    def _on_cansat(self, packet: TelemetryPacket, result: IngestResult) -> None:
        self._dispatch(VehicleID.CANSAT, packet, result)

    def _dispatch(self, vid: VehicleID, packet: TelemetryPacket,
                  result: IngestResult) -> None:
        if not self._telemetry_connected:
            self._telemetry_connected = True
            self.statusBar().showMessage("PHOENIX telemetry connected", 8000)

        self.command_page.set_vehicle_state(vid, packet.state)
        page = self.pages[vid]
        # The vehicle page owns the one derived-velocity estimator for
        # its vehicle (§13.5 — independent per-vehicle state) and returns
        # the value, so the Overview reuses it rather than running a
        # second estimator that could hold a different window and show a
        # different number for the same instant.
        v = page.receive(packet, result)
        self.overview_page.receive(packet, result, v)
        self.com_page.receive(packet, result, v)

        if self._supervisor is not None:
            stream = self._supervisor.demux.streams[vid]
            self.overview_page.health.set_vehicle(
                vid, self._supervisor.vehicle_link_state(vid),
                counters=stream.counters, rssi=packet.rssi,
                success=stream.packet_success)

    def _on_source_state(self, state: SourceState) -> None:
        if state is SourceState.DISCONNECTED or state is SourceState.LINK_ERROR:
            self._telemetry_connected = False
        self.overview_page.health.set_source(state)
        self.com_page.set_source_state(state)

    def _on_receiver_state(self, state: ReceiverState) -> None:
        self.overview_page.health.set_receiver(state)

    def _on_vehicle_state(self, vid: VehicleID, state: VehicleLinkState) -> None:
        if state is VehicleLinkState.LOST:
            self._telemetry_connected = False
        self.pages[vid].set_link_state(state)
        # §10.8 — controls for a vehicle in LOST state are disabled rather
        # than queued to fire on reconnect.
        self.command_page.set_link_state(vid, state)
        self.overview_page.health.set_vehicle(vid, state)
        if self._supervisor is not None:
            self.overview_page.health.set_ground_side_warning(
                self._supervisor.demux.all_vehicles_silent())

    def _on_rejected(self, rejected: RejectedLine) -> None:
        # §4.5 — rejected packets are counted and visible, never
        # displayed as data. The count reaches the health strip via the
        # per-vehicle counters on the next accepted packet.
        self.com_page.on_rejected_line(rejected.reason.value, rejected.detail, rejected.raw)

    def _on_notice(self, text: str) -> None:
        self.statusBar().showMessage(text, 8000)

    # ── operator actions ─────────────────────────────────────────────────

    def reset_all_graphs(self) -> None:
        self.overview_page.clear_graphs()
        for p in self.pages.values():
            p.clear_graphs()
