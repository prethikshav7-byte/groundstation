"""
ground_station/launcher.py
══════════════════════════
§2.1 — "On startup, a mode-selection screen with exactly two options."

    Run Program    connects to the ground receiver over USB (§3)
    Run Simulator  runs the in-process simulator as the data source.
                   No hardware, no port opened.

§2.3 — the choice is made once per launch; switching requires a restart.
Enforced by not offering a way back: once the main window is up there is
no mode control anywhere in the app. A live-mode toggle would mean the
dashboards had to cope with the data source changing underneath them
mid-flight, which §2.2's "only the data source differs" is specifically
arranged to avoid.

§2.5 — this is "Simulator Mode", the app-side thing. It is NOT the
ENTER_SIMULATION telecommand, which puts the *vehicle's* flight software
into simulation and is called "Flight Software Simulation" everywhere in
the UI. The two are different and are never given the same name. Phase 3
disables ENTER_SIMULATION while in Simulator Mode.

Also hosts the §3.3 connection panel: an enumerated, refreshable device
list bound by stable USB identifier and remembered across sessions.
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from .link import PortInfo, SERIAL_AVAILABLE, enumerate_ports
from .theme import ThemeManager


class LaunchMode(Enum):
    LIVE = "live"
    SIMULATOR = "simulator"


class LaunchChoice:
    """What the operator picked."""

    def __init__(self, mode: LaunchMode, stable_id: Optional[str] = None,
                 device: Optional[str] = None, baud: int = 115200):
        self.mode = mode
        self.stable_id = stable_id
        self.device = device
        self.baud = baud


class LauncherDialog(QDialog):
    """§2.1 — exactly two options, plus the §3.3 source configuration."""

    SETTINGS_ORG = "GroundStation"
    SETTINGS_APP = "Launcher"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ground Station")
        self.setMinimumWidth(520)
        self.choice: Optional[LaunchChoice] = None
        self._ports: List[PortInfo] = []
        self._settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)

        self._build()
        self.refresh_ports()

    def _build(self) -> None:
        c = ThemeManager.C()
        self.setStyleSheet(f"background-color: {c.BG};")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(16)

        title = QLabel("Ground Station")
        # §2 — 13pt is the correct cap for a dialog title in this scale.
        # 20pt was jarring next to the 8pt subtitle directly below it.
        title.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {c.CYAN};")
        lay.addWidget(title)

        sub = QLabel("Choose a data source. This is fixed for the session — "
                     "switching requires restarting the app.")
        sub.setFont(QFont("Segoe UI", 9))
        sub.setStyleSheet(f"color: {c.TEXT_DIM};")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        lay.addWidget(self._divider())

        # ── Run Program ──────────────────────────────────────────────────
        live_head = QLabel("Run Program")
        live_head.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        live_head.setStyleSheet(f"color: {c.TEXT};")
        lay.addWidget(live_head)

        live_note = QLabel("Connects to the ground receiver over USB.")
        live_note.setFont(QFont("monospace", 8))
        live_note.setStyleSheet(f"color: {c.TEXT_DIM};")
        lay.addWidget(live_note)

        port_row = QHBoxLayout()
        self._port_box = QComboBox()
        self._port_box.setFont(QFont("monospace", 9))
        self._port_box.setStyleSheet(
            f"QComboBox {{ background-color: {c.BG_INPUT}; color: {c.TEXT}; "
            f"border: 1px solid {c.BORDER}; border-radius: 4px; "
            f"padding: 5px; }}")
        port_row.addWidget(self._port_box, stretch=1)

        refresh = QPushButton("Refresh")
        refresh.setFont(QFont("monospace", 8))
        refresh.setStyleSheet(self._button_css(c.TEXT))
        refresh.clicked.connect(self.refresh_ports)
        port_row.addWidget(refresh)

        self._baud = QSpinBox()
        self._baud.setRange(9600, 2000000)
        self._baud.setSingleStep(9600)
        self._baud.setValue(int(self._settings.value("baud", 115200)))
        self._baud.setFont(QFont("monospace", 9))
        self._baud.setStyleSheet(
            f"QSpinBox {{ background-color: {c.BG_INPUT}; color: {c.TEXT}; "
            f"border: 1px solid {c.BORDER}; border-radius: 4px; padding: 4px; }}")
        port_row.addWidget(QLabel("baud"))
        port_row.addWidget(self._baud)
        lay.addLayout(port_row)

        self._run_live = QPushButton("Run Program")
        self._run_live.setFont(QFont("monospace", 10, QFont.Weight.Bold))
        self._run_live.setStyleSheet(self._button_css(c.CYAN))
        self._run_live.clicked.connect(self._choose_live)
        lay.addWidget(self._run_live)

        lay.addWidget(self._divider())

        # ── Run Simulator ────────────────────────────────────────────────
        sim_head = QLabel("Run Simulator")
        sim_head.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        sim_head.setStyleSheet(f"color: {c.TEXT};")
        lay.addWidget(sim_head)

        sim_note = QLabel(
            "Runs the in-process simulator as the data source. No hardware, "
            "no port opened. The same dashboards, parser and rendering are "
            "used in both modes.")
        sim_note.setFont(QFont("monospace", 8))
        sim_note.setStyleSheet(f"color: {c.TEXT_DIM};")
        sim_note.setWordWrap(True)
        lay.addWidget(sim_note)

        run_sim = QPushButton("Run Simulator")
        run_sim.setFont(QFont("monospace", 10, QFont.Weight.Bold))
        run_sim.setStyleSheet(self._button_css(c.GREEN))
        run_sim.clicked.connect(self._choose_sim)
        lay.addWidget(run_sim)

        self._status = QLabel("")
        self._status.setFont(QFont("monospace", 8))
        self._status.setStyleSheet(f"color: {c.AMBER};")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

    def _button_css(self, accent: str) -> str:
        c = ThemeManager.C()
        return (f"QPushButton {{ background-color: {c.BG_INPUT}; "
                f"color: {accent}; border: 1px solid {accent}; "
                f"border-radius: 5px; padding: 9px 16px; }}"
                f"QPushButton:hover {{ background-color: {c.BG_CARD_HOVER}; }}"
                f"QPushButton:disabled {{ color: {c.TEXT_MUTED}; "
                f"border-color: {c.BORDER}; }}")

    def _divider(self) -> QFrame:
        c = ThemeManager.C()
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet(f"color: {c.BORDER};")
        return f

    # ── §3.3 device list ─────────────────────────────────────────────────

    def refresh_ports(self) -> None:
        self._port_box.clear()
        if not SERIAL_AVAILABLE:
            self._port_box.addItem("pyserial not installed")
            self._run_live.setEnabled(False)
            self._status.setText(
                "pyserial is not installed, so live mode is unavailable. "
                "Install it with:  pip install pyserial")
            return

        self._ports = enumerate_ports()
        if not self._ports:
            self._port_box.addItem("No serial devices found")
            self._run_live.setEnabled(False)
            self._status.setText(
                "No serial devices found. Connect the ground receiver and "
                "select Refresh.")
            return

        self._run_live.setEnabled(True)
        self._status.setText("")
        remembered = self._settings.value("stable_id", "")
        for i, p in enumerate(self._ports):
            self._port_box.addItem(p.label, userData=p)
            # §3.3 — the remembered device is bound by stable identifier,
            # not by device path: a replug can move /dev/ttyACM0 to
            # ttyACM1, and reconnecting by path could attach to entirely
            # different hardware.
            if p.stable_id == remembered:
                self._port_box.setCurrentIndex(i)

    # ── choices ──────────────────────────────────────────────────────────

    def _choose_live(self) -> None:
        port: Optional[PortInfo] = self._port_box.currentData()
        if port is None:
            return
        self._settings.setValue("stable_id", port.stable_id)
        self._settings.setValue("baud", self._baud.value())
        self.choice = LaunchChoice(LaunchMode.LIVE, port.stable_id,
                                   port.device, self._baud.value())
        self.accept()

    def _choose_sim(self) -> None:
        self.choice = LaunchChoice(LaunchMode.SIMULATOR)
        self.accept()
