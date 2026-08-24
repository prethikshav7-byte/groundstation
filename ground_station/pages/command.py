"""
ground_station/pages/command.py
═══════════════════════════════
§10 — the Command dashboard. All actuation lives here and nowhere else
(§9.7 removed it from the vehicle dashboards entirely, including the
layout space it occupied).

This file is UI only. Every rule it enforces lives in commands.py and
actuators.py, which are Qt-free and unit-tested — the widgets ask those
modules whether something is allowed and render the answer. Nothing here
decides anything on its own, which is what makes §1.4 ("no command is
ever sent automatically") checkable: there is no timer in this file that
calls send().

Four §10 requirements are visible in the layout rather than buried:

  §10.4  sampling and transmission are two independent indicators, and
         HIBERNATE + ENABLED raises a persistent banner across the top.
  §10.1  every actuator shows commanded AND actual as separate values.
  §10.6  destructive controls are arm → fire, with the armed state and
         its countdown shown.
  §10.7  the command log shows sent → acknowledged → executed, with
         unacknowledged entries visibly different and a Resend button.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ..actuators import (
    ACTUATORS, ARM_TIMEOUT_S, Actuator, ActuatorSpec, ArmingCentre,
    CALIBRATIONS, CalibrationOutcome, CalibrationRun,
)
from ..commands import (
    COMMANDS, CommandCentre, CommandRecord, CommandStatus, Guard,
    ModeValue, VehicleModes, check_guard,
)
from ..models import FlightState, GROUND_STATES, VehicleID, VehicleLinkState
from ..theme import ThemeManager


def _btn_css(accent: str) -> str:
    c = ThemeManager.C()
    return (f"QPushButton {{ background-color: {c.BG_INPUT}; color: {accent}; "
            f"border: 1px solid {accent}; border-radius: 4px; "
            f"padding: 6px 12px; }}"
            f"QPushButton:hover {{ background-color: {c.BG_CARD_HOVER}; }}"
            f"QPushButton:disabled {{ color: {c.TEXT_MUTED}; "
            f"border-color: {c.BORDER}; }}")


def _armed_btn_css() -> str:
    """§10.6 — visibly indicates the armed state with a filled red background.

    A red border alone reads almost identically to the normal border-accent
    style used by every safe button.  A filled background makes armed vs
    unarmed unmistakable at a glance, which matters for ACT_SEPARATE.
    """
    c = ThemeManager.C()
    return (f"QPushButton {{ background-color: {c.RED}; color: #ffffff; "
            f"border: 2px solid {c.RED}; border-radius: 4px; "
            f"padding: 6px 12px; font-weight: bold; }}"
            f"QPushButton:hover {{ background-color: {c.RED}; }}"
            f"QPushButton:disabled {{ background-color: {c.BG_INPUT}; "
            f"color: {c.TEXT_MUTED}; border-color: {c.BORDER}; }}")


def _card() -> QWidget:
    c = ThemeManager.C()
    w = QWidget()
    w.setStyleSheet(
        f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
        f"border-radius: 6px;")
    return w


def _heading(text: str) -> QLabel:
    c = ThemeManager.C()
    lbl = QLabel(text)
    lbl.setFont(QFont("monospace", 9, QFont.Weight.Bold))
    lbl.setStyleSheet(f"color: {c.CYAN};")
    return lbl


# ─────────────────────────────────────────────────────────────────────────────
#  §10.1 — one actuator
# ─────────────────────────────────────────────────────────────────────────────

class ActuatorPanel(QWidget):
    """Commanded vs actual, travel, and the move controls."""

    move_requested = pyqtSignal(str, float)      # actuator id, delta

    #: §10.1.1 — ±180° is reachable only from home, so it is offered only
    #: from home. The smaller steps are always available and are clamped
    #: with a visible report if they run into a stop.
    STEPS = (-180.0, -90.0, -45.0, 45.0, 90.0, 180.0)

    def __init__(self, spec: ActuatorSpec, actuator: Actuator,
                 arming: ArmingCentre, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.actuator = actuator
        self.arming = arming
        self._enabled = False
        self._buttons: Dict[float, QPushButton] = {}
        self._build()

    def _build(self) -> None:
        c = ThemeManager.C()
        self.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        top = QHBoxLayout()
        name = QLabel(self.spec.mechanism)
        name.setFont(QFont("monospace", 10, QFont.Weight.Bold))
        name.setStyleSheet(f"color: {c.TEXT};")
        top.addWidget(name)
        top.addStretch()
        chan = QLabel(f"{self.spec.driver} ch {self.spec.channel}")
        chan.setFont(QFont("monospace", 8))
        chan.setStyleSheet(f"color: {c.TEXT_DIM};")
        top.addWidget(chan)
        lay.addLayout(top)

        # §10.1 — commanded and actual are separate rows. Never one row
        # showing "position", which is how commanded ends up passing for
        # actual the moment a servo stalls.
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(2)
        self._commanded = self._value_row(grid, 0, "Commanded")
        self._actual = self._value_row(grid, 1, "Actual (from vehicle)")
        self._travel = self._value_row(grid, 2, "Remaining travel")
        self._last = self._value_row(grid, 3, "Last command")
        lay.addLayout(grid)

        self._mismatch = QLabel("")
        self._mismatch.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self._mismatch.setStyleSheet(f"color: {c.RED};")
        self._mismatch.setVisible(False)
        lay.addWidget(self._mismatch)

        row = QHBoxLayout()
        row.setSpacing(4)
        for delta in self.STEPS:
            b = QPushButton(f"{delta:+.0f}°")
            b.setFont(QFont("monospace", 8))
            b.setStyleSheet(_btn_css(c.TEXT))
            b.clicked.connect(lambda _c, d=delta: self._request(d))
            self._buttons[delta] = b
            row.addWidget(b)
        lay.addLayout(row)

        self._note = QLabel("")
        self._note.setFont(QFont("monospace", 8))
        self._note.setStyleSheet(f"color: {c.AMBER};")
        self._note.setWordWrap(True)
        lay.addWidget(self._note)

        if self.spec.destructive:
            self._arm = QPushButton("Arm")
            self._arm.setFont(QFont("monospace", 9, QFont.Weight.Bold))
            self._arm.setStyleSheet(_btn_css(c.RED))
            self._arm.clicked.connect(self._toggle_arm)
            lay.addWidget(self._arm)
        else:
            self._arm = None

    def _value_row(self, grid: QGridLayout, row: int, label: str) -> QLabel:
        c = ThemeManager.C()
        cap = QLabel(label)
        cap.setFont(QFont("monospace", 8))
        cap.setStyleSheet(f"color: {c.TEXT_DIM};")
        val = QLabel("—")
        val.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        val.setStyleSheet(f"color: {c.TEXT};")
        grid.addWidget(cap, row, 0)
        grid.addWidget(val, row, 1)
        return val

    # ── actions ──────────────────────────────────────────────────────────

    def _request(self, delta: float) -> None:
        # §10.6 — arm check comes FIRST for destructive controls.  Running
        # evaluate() before the gate means an unarmed control with no travel
        # left shows a clamp message instead of "arm first", which hides the
        # safety requirement behind an unrelated constraint.
        if self.spec.destructive:
            if not self.arming.is_armed(self.spec.id):
                self._note.setText("Arm this control before commanding a move.")
                return

        result = self.actuator.evaluate(delta)
        # §10.1.1 — a clamped move is reported, never silent, never
        # wrapping.  Shown here whether or not the move then proceeds.
        self._note.setText(result.message)

        if result.delta_applied == 0.0:
            return

        if self.spec.destructive:
            if not self.arming.fire(self.spec.id):
                return
        self.move_requested.emit(self.spec.id, result.delta_applied)

    def _toggle_arm(self) -> None:
        if self.arming.is_armed(self.spec.id):
            self.arming.disarm()
        else:
            # §10.6 — arming one destructive control disarms any other.
            # ArmingCentre holds a single slot, so this is automatic.
            self.arming.arm(self.spec.id)
        self.refresh()

    # ── refresh ──────────────────────────────────────────────────────────

    def set_enabled_by_guard(self, enabled: bool, reason: str = "") -> None:
        self._enabled = enabled
        if not enabled and reason:
            self._note.setText(reason)
        self.refresh()

    def refresh(self) -> None:
        c = ThemeManager.C()
        a = self.actuator

        self._commanded.setText(
            "—" if a.commanded is None else f"{a.commanded:.1f}°")
        # §10.1 — "Never display commanded position as if it were actual."
        # An em dash here means the vehicle has not reported a position,
        # which is a different fact from being at 180°.
        self._actual.setText(
            "not reported" if a.position is None else f"{a.position:.1f}°")

        down, up = a.remaining_travel()
        self._travel.setText(
            "—" if down is None else f"{down:.0f}° down   {up:.0f}° up")
        self._last.setText(
            "—" if a.last_command_at is None
            else time.strftime("%H:%M:%S", time.localtime(a.last_command_at)))

        agree = a.commanded_matches_actual()
        self._mismatch.setVisible(agree is False)
        if agree is False:
            self._mismatch.setText(
                "COMMANDED AND ACTUAL DISAGREE — mechanism may be jammed")

        for delta, b in self._buttons.items():
            allowed = self._enabled
            if abs(delta) >= 180.0:
                # §10.1.1 — disabled unless at home, rather than offered
                # and then clamped. "A control that is nearly always
                # impossible trains operators to ignore clamp warnings."
                allowed = allowed and a.at_home
                b.setToolTip("" if a.at_home else
                             "±180° is reachable only from the home position")
            b.setEnabled(allowed)

        if self._arm is not None:
            armed = self.arming.is_armed(self.spec.id)
            remaining = self.arming.seconds_remaining()
            self._arm.setEnabled(self._enabled)
            self._arm.setText(
                f"ARMED — fire within {remaining:.0f} s   (click to disarm)"
                if armed else "Arm")
            # §10.6 — filled red background when armed so the state is
            # unmistakable at a glance; reverts to dim style on disarm.
            self._arm.setStyleSheet(_armed_btn_css() if armed
                                    else _btn_css(c.TEXT_DIM))


# ─────────────────────────────────────────────────────────────────────────────
#  §10.4 — sampling vs transmission
# ─────────────────────────────────────────────────────────────────────────────

class ModePanel(QWidget):
    """Two orthogonal indicators, never merged into one 'status'."""

    command_requested = pyqtSignal(str)

    def __init__(self, modes: VehicleModes, parent=None):
        super().__init__(parent)
        self.modes = modes
        c = ThemeManager.C()
        self.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)
        lay.addWidget(_heading("Sampling and Transmission"))

        note = QLabel("These are independent. Sampling is what the vehicle "
                      "measures; transmission is what it sends.")
        note.setFont(QFont("monospace", 8))
        note.setStyleSheet(f"color: {c.TEXT_DIM};")
        note.setWordWrap(True)
        lay.addWidget(note)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)

        grid.addWidget(self._cap("Sampling"), 0, 0)
        self._sampling = self._val()
        grid.addWidget(self._sampling, 0, 1)
        for i, cmd in enumerate(("SET_MODE_LIVE", "SET_MODE_HIBERNATE")):
            b = QPushButton(cmd.replace("SET_MODE_", "").title())
            b.setFont(QFont("monospace", 8))
            b.setStyleSheet(_btn_css(ThemeManager.C().TEXT))
            b.clicked.connect(lambda _c, x=cmd: self.command_requested.emit(x))
            grid.addWidget(b, 0, 2 + i)

        grid.addWidget(self._cap("Transmission"), 1, 0)
        self._telemetry = self._val()
        grid.addWidget(self._telemetry, 1, 1)
        for i, cmd in enumerate(("ENABLE_TELEMETRY", "DISABLE_TELEMETRY")):
            b = QPushButton(cmd.split("_")[0].title())
            b.setFont(QFont("monospace", 8))
            b.setStyleSheet(_btn_css(ThemeManager.C().TEXT))
            b.clicked.connect(lambda _c, x=cmd: self.command_requested.emit(x))
            grid.addWidget(b, 1, 2 + i)

        lay.addLayout(grid)

    def _cap(self, text: str) -> QLabel:
        c = ThemeManager.C()
        l = QLabel(text)
        l.setFont(QFont("monospace", 8))
        l.setStyleSheet(f"color: {c.TEXT_DIM};")
        return l

    def _val(self) -> QLabel:
        c = ThemeManager.C()
        l = QLabel("UNKNOWN")
        l.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        l.setStyleSheet(f"color: {c.TEXT_MUTED};")
        return l

    def refresh(self) -> None:
        c = ThemeManager.C()
        for label, value in ((self._sampling, self.modes.sampling),
                             (self._telemetry, self.modes.telemetry)):
            # §10.4 — the confirmed-active mode from the vehicle's
            # acknowledgment, never the last-requested mode. PENDING and
            # UNCONFIRMED are shown as themselves rather than optimistically
            # resolved to the requested value.
            colour = {
                ModeValue.PENDING: c.AMBER,
                ModeValue.UNCONFIRMED: c.RED,
                ModeValue.UNKNOWN: c.TEXT_MUTED,
            }.get(value, c.GREEN)
            label.setText(value.value if hasattr(value, "value") else str(value))
            label.setStyleSheet(f"color: {colour};")


# ─────────────────────────────────────────────────────────────────────────────
#  §10.2 — calibration
# ─────────────────────────────────────────────────────────────────────────────

class CalibrationPanel(QWidget):
    command_requested = pyqtSignal(str)

    def __init__(self, run: CalibrationRun, parent=None):
        super().__init__(parent)
        self.run = run
        self._rows: Dict[str, QLabel] = {}
        c = ThemeManager.C()
        self.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        lay.addWidget(_heading("Calibration"))

        note = QLabel("Runs on the vehicle. Available in BOOT and PRE-LAUNCH "
                      "only — zeroing the barometer in flight corrupts the "
                      "altitude datum for the rest of the mission.")
        note.setFont(QFont("monospace", 8))
        note.setStyleSheet(f"color: {c.TEXT_DIM};")
        note.setWordWrap(True)
        lay.addWidget(note)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        self._buttons: List[QPushButton] = []
        for i, sensor in enumerate(CALIBRATIONS):
            name = QLabel(sensor.label)
            name.setFont(QFont("monospace", 9))
            name.setStyleSheet(f"color: {c.TEXT};")
            name.setToolTip(sensor.target)
            grid.addWidget(name, i, 0)

            result = QLabel("not run")
            result.setFont(QFont("monospace", 8, QFont.Weight.Bold))
            result.setStyleSheet(f"color: {c.TEXT_MUTED};")
            self._rows[sensor.key] = result
            grid.addWidget(result, i, 1)

            b = QPushButton("Calibrate")
            b.setFont(QFont("monospace", 8))
            b.setStyleSheet(_btn_css(c.TEXT))
            b.clicked.connect(
                lambda _c, k=sensor.key:
                self.command_requested.emit(f"CALIBRATE_{k}"))
            self._buttons.append(b)
            grid.addWidget(b, i, 2)
        lay.addLayout(grid)

        self._all = QPushButton("Calibrate All")
        self._all.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        self._all.setStyleSheet(_btn_css(c.CYAN))
        self._all.clicked.connect(
            lambda: self.command_requested.emit("CALIBRATE_ALL"))
        self._buttons.append(self._all)
        lay.addWidget(self._all)

    def set_enabled_by_guard(self, enabled: bool) -> None:
        for b in self._buttons:
            b.setEnabled(enabled)

    def refresh(self) -> None:
        c = ThemeManager.C()
        # §10.2 — per-sensor pass/fail, "not one aggregate result". An
        # aggregate hides which sensor failed, which is the only part the
        # operator can act on.
        for key, label in self._rows.items():
            outcome = self.run.results.get(key, CalibrationOutcome.NOT_RUN)
            colour = {
                CalibrationOutcome.PASS: c.GREEN,
                CalibrationOutcome.FAIL: c.RED,
                CalibrationOutcome.RUNNING: c.AMBER,
            }.get(outcome, c.TEXT_MUTED)
            label.setText(outcome.value)
            label.setStyleSheet(f"color: {colour};")
            label.setToolTip(self.run.messages.get(key, ""))


# ─────────────────────────────────────────────────────────────────────────────
#  §10.7 — command log
# ─────────────────────────────────────────────────────────────────────────────

class CommandLogPanel(QWidget):
    resend_requested = pyqtSignal(int)

    def __init__(self, centre: CommandCentre, parent=None):
        super().__init__(parent)
        self.centre = centre
        c = ThemeManager.C()
        self.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        lay.addWidget(_heading("Command Log"))

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setMinimumHeight(220)
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(3)
        self._body_layout.addStretch()
        self._scroll.setWidget(self._body)
        lay.addWidget(self._scroll)

        self._rendered: Dict[int, QWidget] = {}

    def refresh(self) -> None:
        for record in self.centre.records:
            if record.sequence not in self._rendered:
                w = self._row(record)
                self._rendered[record.sequence] = w
                self._body_layout.insertWidget(0, w)
            self._update_row(self._rendered[record.sequence], record)

    def _row(self, record: CommandRecord) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(8)

        w._time = QLabel("")
        w._time.setFont(QFont("monospace", 8))
        w._text = QLabel("")
        w._text.setFont(QFont("monospace", 8))
        w._status = QLabel("")
        w._status.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        w._resend = QPushButton("Resend")
        w._resend.setFont(QFont("monospace", 8))
        w._resend.setStyleSheet(_btn_css(ThemeManager.C().AMBER))
        w._resend.clicked.connect(
            lambda _c, s=record.sequence: self.resend_requested.emit(s))

        row.addWidget(w._time)
        row.addWidget(w._text, stretch=1)
        row.addWidget(w._status)
        row.addWidget(w._resend)
        return w

    def _update_row(self, w: QWidget, record: CommandRecord) -> None:
        c = ThemeManager.C()
        w._time.setText(time.strftime("%H:%M:%S",
                                      time.localtime(record.sent_at)))
        w._time.setStyleSheet(f"color: {c.TEXT_MUTED};")
        w._text.setText(f"{record.target.value}  {record.command}")
        w._text.setStyleSheet(f"color: {c.TEXT};")

        # §10.7 — "An unacknowledged command must look visibly different
        # from a successful one." Colour plus the persistent Resend
        # button, not colour alone.
        colour = {
            CommandStatus.EXECUTED: c.GREEN,
            CommandStatus.ACKNOWLEDGED: c.CYAN,
            CommandStatus.SENT: c.AMBER,
            CommandStatus.TIMED_OUT: c.RED,
            CommandStatus.REJECTED: c.RED,
        }.get(record.status, c.TEXT_DIM)
        w._status.setText(record.status.value)
        w._status.setStyleSheet(f"color: {colour};")
        # §10.7 — retry is operator-initiated only. The button is the only
        # path to a resend; nothing in this file calls resend() on a timer.
        w._resend.setVisible(record.unacknowledged)


# ─────────────────────────────────────────────────────────────────────────────
#  The page
# ─────────────────────────────────────────────────────────────────────────────

class CommandPage(QWidget):
    """§10 — the whole dashboard for one vehicle at a time."""

    def __init__(self, centre: CommandCentre, simulator_mode: bool = False,
                 parent=None):
        super().__init__(parent)
        self.centre = centre
        self.simulator_mode = simulator_mode
        self.vehicle = VehicleID.ROCKET

        self.arming = ArmingCentre()
        self.actuators: Dict[str, Actuator] = {
            spec.id: Actuator(spec) for spec in ACTUATORS
        }
        # §10.4 modes live on the CommandCentre, which is what receives
        # the acknowledgments. A second copy here would show a mode the
        # vehicle never confirmed — exactly the optimistic flip §10.4
        # forbids.
        self.calibration: Dict[VehicleID, CalibrationRun] = {
            v: CalibrationRun() for v in VehicleID
        }
        self._states: Dict[VehicleID, Optional[FlightState]] = {
            v: None for v in VehicleID
        }
        self._links: Dict[VehicleID, VehicleLinkState] = {
            v: VehicleLinkState.LOST for v in VehicleID
        }
        self._usb_direct = False

        self._build()

        # Refresh only — this timer never sends anything (§1.4). It exists
        # so the arming countdown and ack ages stay current on screen.
        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self.refresh)
        self._tick.start()

    def _build(self) -> None:
        c = ThemeManager.C()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # §10.4 — persistent, unmissable banner whenever HIBERNATE +
        # ENABLED holds: packets keep arriving so the link looks healthy,
        # but the values are not live measurements.
        self._banner = QLabel("")
        self._banner.setFont(QFont("monospace", 10, QFont.Weight.Bold))
        self._banner.setStyleSheet(
            f"background-color: {c.RED}; color: #FFFFFF; padding: 8px;")
        self._banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._banner.setVisible(False)
        outer.addWidget(self._banner)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        lay.addLayout(self._vehicle_selector())

        self.mode_panel = ModePanel(self.centre.modes[self.vehicle])
        self.mode_panel.command_requested.connect(self._send_mode_command)
        lay.addWidget(self.mode_panel)

        lay.addWidget(self._general_commands())

        act_head = _heading("Actuators")
        lay.addWidget(act_head)
        self._actuator_note = QLabel(
            "Actuators are on the Rocket. All three share one PCA9685 on one "
            "I²C bus; commanding one does not delay status polling of the "
            "others.")
        self._actuator_note.setFont(QFont("monospace", 8))
        self._actuator_note.setStyleSheet(f"color: {c.TEXT_DIM};")
        self._actuator_note.setWordWrap(True)
        lay.addWidget(self._actuator_note)

        self.actuator_panels: Dict[str, ActuatorPanel] = {}
        for spec in ACTUATORS:
            p = ActuatorPanel(spec, self.actuators[spec.id], self.arming)
            p.move_requested.connect(self._send_move)
            self.actuator_panels[spec.id] = p
            lay.addWidget(p)

        self.calibration_panel = CalibrationPanel(self.calibration[self.vehicle])
        self.calibration_panel.command_requested.connect(self._send_guarded)
        lay.addWidget(self.calibration_panel)

        self.log_panel = CommandLogPanel(self.centre)
        self.log_panel.resend_requested.connect(self.centre.resend)
        lay.addWidget(self.log_panel)

        lay.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll)

    def _vehicle_selector(self) -> QHBoxLayout:
        c = ThemeManager.C()
        row = QHBoxLayout()
        row.addWidget(_heading("Target vehicle"))
        self._vehicle_buttons: Dict[VehicleID, QPushButton] = {}
        for vid in VehicleID:
            b = QPushButton(vid.value)
            b.setCheckable(True)
            b.setFont(QFont("monospace", 9, QFont.Weight.Bold))
            b.setStyleSheet(_btn_css(c.CYAN))
            b.clicked.connect(lambda _c, v=vid: self._select_vehicle(v))
            self._vehicle_buttons[vid] = b
            row.addWidget(b)
        self._vehicle_buttons[VehicleID.ROCKET].setChecked(True)
        row.addStretch()

        self._guard_note = QLabel("")
        self._guard_note.setFont(QFont("monospace", 8))
        self._guard_note.setStyleSheet(f"color: {c.AMBER};")
        row.addWidget(self._guard_note)
        return row

    def _general_commands(self) -> QWidget:
        c = ThemeManager.C()
        w = _card()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(_heading("Commands"))

        grid = QGridLayout()
        self._command_buttons: Dict[str, QPushButton] = {}
        names = ["REQUEST_STATUS", "ENTER_SIMULATION", "REFRESH_PROCESSOR",
                 "MANUAL_DEPLOY"]
        for i, name in enumerate(names):
            spec = COMMANDS[name]
            b = QPushButton(name.replace("_", " ").title())
            b.setFont(QFont("monospace", 9))
            accent = c.RED if spec.destructive else c.TEXT
            b.setStyleSheet(_btn_css(accent))
            b.setToolTip(f"{spec.description}  ({spec.guard.value})")
            b.clicked.connect(lambda _c, n=name: self._send_guarded(n))
            self._command_buttons[name] = b
            grid.addWidget(b, i // 2, i % 2)
        lay.addLayout(grid)
        return w

    # ── sending ──────────────────────────────────────────────────────────

    def _select_vehicle(self, vid: VehicleID) -> None:
        self.vehicle = vid
        for v, b in self._vehicle_buttons.items():
            b.setChecked(v is vid)
        self.mode_panel.modes = self.centre.modes[vid]
        self.calibration_panel.run = self.calibration[vid]
        self.refresh()

    def _send_guarded(self, command: str) -> None:
        base = command.split()[0]
        spec = COMMANDS.get(base)
        if spec is None:
            # Unknown command — refuse rather than borrowing another
            # command's guard.  Every command that should be sendable must
            # be registered in commands.COMMANDS with the correct Guard.
            self._guard_note.setText(
                f"Unknown command {base!r} — not in the command set")
            return
        result = check_guard(spec, self._states[self.vehicle],
                             self._links[self.vehicle], self.simulator_mode,
                             self._usb_direct)
        if not result.allowed:
            self._guard_note.setText(result.reason)
            return
        self._guard_note.setText("")
        self.centre.send(self.vehicle, command)
        self.refresh()

    def _send_mode_command(self, command: str) -> None:
        record = self.centre.send(self.vehicle, command)
        if record is None:
            return
        # §10.4 — the mode goes to PENDING here and only becomes the
        # requested value when the vehicle acknowledges this exact
        # sequence. Never flip optimistically.
        modes = self.centre.modes[self.vehicle]
        if command.startswith("SET_MODE"):
            modes.request_sampling(command, record.sequence)
        else:
            modes.request_telemetry(command, record.sequence)
        self.refresh()

    def _send_move(self, actuator_id: str, delta: float) -> None:
        actuator = self.actuators[actuator_id]
        if actuator.position is None:
            # §10.1 — "Never display commanded position as if it were actual."
            # If we don't know where the servo is, substituting home (180°)
            # would silently send an absolute target based on a position the
            # vehicle never confirmed.  A 0° servo (falsy) + home fallback
            # = 180° error on an irreversible mechanism.
            # Refuse the move until the vehicle has echoed a position back.
            self.refresh()
            return
        target = actuator.position + delta
        actuator.note_commanded(target)
        self.centre.send(self.vehicle, f"{actuator_id}_MOVE {target:.1f}")
        self.refresh()

    # ── inbound ──────────────────────────────────────────────────────────

    def set_vehicle_state(self, vid: VehicleID,
                          state: Optional[FlightState]) -> None:
        self._states[vid] = state

    def set_link_state(self, vid: VehicleID, link: VehicleLinkState) -> None:
        self._links[vid] = link

    def set_usb_direct(self, on: bool) -> None:
        self._usb_direct = on

    def note_actuator_echo(self, actuator_id: str, actual: float) -> None:
        a = self.actuators.get(actuator_id)
        if a is not None:
            a.note_echo(actual)

    # ── refresh ──────────────────────────────────────────────────────────

    def refresh(self) -> None:
        # §10.6 — apply the auto-disarm timeout before reading any armed
        # state.  armed_control() is now a pure read; expire() is the only
        # place that clears _armed on timeout, so it must be called here.
        self.arming.expire()

        state = self._states[self.vehicle]
        link = self._links[self.vehicle]

        for name, button in self._command_buttons.items():
            result = check_guard(COMMANDS[name], state, link,
                                 self.simulator_mode, self._usb_direct)
            button.setEnabled(result.allowed)
            button.setToolTip(result.reason or COMMANDS[name].description)

        # §10.2 — blocked outside BOOT and PRE_LAUNCH.
        on_ground = state in GROUND_STATES if state is not None else True
        self.calibration_panel.set_enabled_by_guard(
            on_ground and link is not VehicleLinkState.LOST)

        # §9.7 / §10.1 — actuators are rocket hardware; the panels are
        # disabled rather than hidden when CanSat is selected, so the
        # operator can see they exist and why they are unavailable.
        rocket_selected = self.vehicle is VehicleID.ROCKET
        for p in self.actuator_panels.values():
            p.set_enabled_by_guard(
                rocket_selected and link is not VehicleLinkState.LOST,
                "" if rocket_selected else "Actuators are on the Rocket.")

        self.mode_panel.refresh()
        self.calibration_panel.refresh()
        self.log_panel.refresh()

        # §10.4 — the dangerous combination.
        modes = self.centre.modes[self.vehicle]
        self._banner.setVisible(modes.dangerous)
        if modes.dangerous:
            self._banner.setText(
                "SAMPLING HIBERNATED WHILE TRANSMITTING — packets are "
                "arriving but the values are not live measurements")

        # §10.7 — surface unacknowledged commands that have aged out
        # without ever auto-retrying them.
        self.centre.poll_timeouts()
