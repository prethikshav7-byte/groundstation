"""
ground_station/simulator/source.py
══════════════════════════════════
§13.6 — the Qt wrapper around SimulatorEngine.

Thin by design. All behaviour lives in engine.py, which is Qt-free and
therefore actually testable (see test_phase4.py); this file only turns
engine output into the same signals LinkSupervisor emits.

That signal compatibility is §2.2's requirement made structural: "Both
modes share the same dashboard, rendering, parsing, and reconciliation
code. Only the data source differs." GroundStationApp.connect_supervisor
takes either object and contains no branch on which one it got. If the
two interfaces ever drift apart, simulator mode stops exercising the code
that runs in live mode, and the simulator becomes a demo instead of a
test.

Note what is NOT here: no flight model (that is trajectory.py, shared
with §7.5.5's reconciliation) and no packet construction (lines go
through the real CsvCodec and the real TelemetryDemux). A simulator that
handed TelemetryPacket objects straight to the UI could never exercise
§4.3's checksum verification, §4.5's rejection paths or §4.6's duplicate
detection, because those live inside the parser it would have skipped.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from ..graphs.trajectory import FlightProfile
from ..models import (
    ReceiverState, SourceState, TelemetryPacket, VehicleID, VehicleLinkState,
)
from ..parser import IngestResult, LossThresholds, TelemetryDemux
from .engine import SIM_DT, SimulatorEngine
from .faults import FaultKind, FaultSchedule


class SimulatedSource(QObject):
    """Drop-in replacement for LinkSupervisor."""

    rocket_packet = pyqtSignal(object, object)
    cansat_packet = pyqtSignal(object, object)
    line_rejected = pyqtSignal(object)
    raw_line = pyqtSignal(str, float)

    source_state_changed = pyqtSignal(object)
    receiver_state_changed = pyqtSignal(object)
    vehicle_state_changed = pyqtSignal(object, object)
    notice = pyqtSignal(str)

    #: Emitted when a fault is injected, so a tester can see what the
    #: simulator did without reading faults.py.
    fault_injected = pyqtSignal(str, str)

    def __init__(self, schedule: Optional[FaultSchedule] = None,
                 profile: Optional[FlightProfile] = None,
                 thresholds: Optional[LossThresholds] = None,
                 parent=None):
        super().__init__(parent)
        active_schedule = schedule if schedule is not None else FaultSchedule()
        self.engine = SimulatorEngine(active_schedule, profile)
        self.demux = TelemetryDemux(self.engine.codec, thresholds)

        self._receiver_state = ReceiverState.NO_HEARTBEAT
        self._vehicle_states: Dict[VehicleID, VehicleLinkState] = {
            v: VehicleLinkState.LOST for v in VehicleID
        }
        self._source_state = SourceState.DISCONNECTED
        self._command_sequence = 0
        self._unack_armed = True

        self._timer = QTimer(self)
        self._timer.setInterval(int(SIM_DT * 1000))
        self._timer.timeout.connect(self._tick)

    # ── lifecycle, mirroring LinkSupervisor ──────────────────────────────

    def connect_to(self, *_args, **_kwargs) -> None:
        self._set_source(SourceState.CONNECTED)
        self._timer.start()

    def disconnect_source(self) -> None:
        self._timer.stop()
        self._set_source(SourceState.DISCONNECTED)

    def send_command(self, target: VehicleID, command: str,
                     sequence: Optional[int] = None) -> int:
        """§10.7 — acknowledges, unless the unacked fault is armed.

        There is no auto-retry here either. The simulator models the
        vehicle, not an app that resends on its own — that app does not
        exist (§1.4), and simulating one would hide its absence.
        """
        if sequence is None:
            self._command_sequence += 1
            sequence = self._command_sequence

        if self._unack_armed and self.engine.t > 80.0:
            # Fires once. A simulator where every later command silently
            # failed would be useless for testing anything else.
            self._unack_armed = False
            self.fault_injected.emit(
                FaultKind.COMMAND_UNACKED.value,
                f"command #{sequence} ({command}) deliberately unacknowledged")
            self.notice.emit(
                f"[SIM] {command} sent and deliberately not acknowledged "
                f"(§10.7) — the operator must Resend; nothing retries it")
            return sequence

        self.notice.emit(f"[SIM] {target.value} acknowledged {command}")
        return sequence

    # ── tick ─────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        out = self.engine.tick()

        for kind, note in out.injected:
            self.fault_injected.emit(kind, note)
            self.notice.emit(f"[SIM] injecting {kind}: {note}")

        if out.source_down:
            # §3.9 — annotated as a source-level outage, not two
            # coincidental vehicle outages (§3.5).
            self._set_source(SourceState.NO_DATA)
            self._poll_health()
            return

        self._set_source(SourceState.CONNECTED)
        for line in out.lines:
            self._feed(line)
        self._poll_health()

    def _feed(self, line: str) -> None:
        received_at = time.monotonic()
        # §12.1 — every received line reaches the log verbatim, before
        # anything decides whether it is valid.
        self.raw_line.emit(line, received_at)

        result: Optional[IngestResult] = self.demux.feed(line, received_at)
        if result is None:
            return
        if result.rejected is not None:
            self.line_rejected.emit(result.rejected)
            return

        packet: TelemetryPacket = result.packet
        if packet.vehicle_id is VehicleID.ROCKET:
            self.rocket_packet.emit(packet, result)
        else:
            self.cansat_packet.emit(packet, result)

    # ── health ───────────────────────────────────────────────────────────

    def _set_source(self, state: SourceState) -> None:
        if state is not self._source_state:
            self._source_state = state
            self.source_state_changed.emit(state)

    def _poll_health(self) -> None:
        now = time.monotonic()
        age = self.demux.heartbeat_age(now)
        rx = (ReceiverState.ALIVE if age is not None and age <= 3.0
              else ReceiverState.NO_HEARTBEAT)
        if rx is not self._receiver_state:
            self._receiver_state = rx
            self.receiver_state_changed.emit(rx)
            if rx is ReceiverState.NO_HEARTBEAT:
                self.notice.emit(
                    "Receiver heartbeat lost — receiver-level fault, "
                    "not a vehicle fault (§3.5).")

        for vid, stream in self.demux.streams.items():
            s = stream.link_state(now)
            if s is not self._vehicle_states[vid]:
                self._vehicle_states[vid] = s
                self.vehicle_state_changed.emit(vid, s)

        for msg in self.demux.drain_notices():
            self.notice.emit(msg)

    def inject_line(self, line: str, received_at: Optional[float] = None) -> None:
        """Inject one line directly into the demux pipeline."""
        if received_at is None:
            received_at = time.monotonic()
        self._feed(line)

    # ── read-only views, mirroring LinkSupervisor ────────────────────────

    @property
    def receiver_state(self) -> ReceiverState:
        return self._receiver_state

    def vehicle_link_state(self, vid: VehicleID) -> VehicleLinkState:
        return self._vehicle_states[vid]

    @property
    def source_state(self) -> SourceState:
        return self._source_state

    @property
    def port_name(self) -> str:
        return "SIMULATOR"

    @property
    def baudrate(self) -> int:
        return 115200

    def reset(self) -> None:
        """Restart the simulation from T=0.

        Resets the engine AND restores ``_unack_armed`` so the §10.7
        unacknowledged-command scenario fires again.  Without this,
        ``engine.reset()`` would reinitialise the flight clock while
        ``_unack_armed`` stayed ``False``, making the fault permanently
        unavailable after the first restart.
        """
        self.engine.reset()
        self._unack_armed = True
