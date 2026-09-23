"""
ground_station/link.py
══════════════════════
§3 — the link layer. One USB source carrying both vehicles (§3.1), routed
by VEHICLE_ID (§3.2), with source / receiver / vehicle health tracked as
three separate facts (§3.6).

This is the only module in the §1–5 layer that imports Qt or pyserial.
Everything it depends on (codec, parser, timeline, derived) is importable
and testable without either, which is what let the whole packet layer be
verified with no hardware and no display attached.

⚠️ DELIBERATE DEVIATION FROM §3.10 — please read before "fixing" it.

    §3.10 says "parsed packets enter a bounded, drop-oldest queue
    consumed by the UI", which reads as: parse on the reader thread,
    queue the results.

    What is implemented instead: the reader thread does serial I/O and
    nothing else, and queues raw lines with their receive timestamps; the
    demultiplexer runs on the UI thread as the queue is drained.

    Reason: TelemetryDemux owns per-vehicle counters, packet-count
    continuity, loss tiering, and the state timeline — all mutable, all
    read by the dashboards. Parsing on the reader thread puts that state
    on one thread and its readers on another, which is precisely the
    shared mutable state §13.5 rules out, and would need a lock on every
    counter read.

    The requirement §3.10 exists to protect is the one in its own last
    line: "Serial I/O never blocks the render thread." That is fully
    satisfied here — the blocking readline() is on the reader thread.
    Decoding a 144-byte CSV line costs a few microseconds; at the
    nominal 20 lines/s that is well under 0.1% of the UI thread, and the
    §3.10 requirement to sustain ≥40 lines/s has roughly three orders of
    magnitude of headroom.

    If the wire format later becomes binary with an expensive decode, or
    the rate rises sharply, revisit this — the split point is the only
    thing that would need to move.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from .codec import CsvCodec, PacketCodec, frame_command
from .commands import BENCH_COMMANDS
from .models import (
    HEARTBEAT_TIMEOUT_S, NO_DATA_AFTER_S, ReceiverState, SourceState,
    TelemetryPacket, VehicleID, VehicleLinkState,
)
from .parser import IngestResult, LossThresholds, TelemetryDemux

try:
    import serial
    from serial.tools import list_ports
    SERIAL_AVAILABLE = True
except ImportError:                                  # pragma: no cover
    serial = None                                    # type: ignore
    list_ports = None                                # type: ignore
    SERIAL_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
#  §3.3 — port enumeration bound to a stable USB identifier
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PortInfo:
    device: str                  # /dev/ttyACM0, COM5 — NOT stable
    description: str
    serial_number: Optional[str]
    vid: Optional[int]
    pid: Optional[int]

    @property
    def stable_id(self) -> str:
        """Identifier that survives a replug (§3.3).

        The device path does not: unplugging the receiver mid-session and
        plugging it back in can move /dev/ttyACM0 to /dev/ttyACM1, and on
        Windows COM numbering is worse. Reconnecting to a remembered
        device path can therefore attach to a completely different piece
        of hardware. Serial number is preferred; VID:PID is the fallback
        and is only unique if one such device is attached.
        """
        if self.serial_number:
            return f"SNR:{self.serial_number}"
        if self.vid is not None and self.pid is not None:
            return f"USB:{self.vid:04X}:{self.pid:04X}"
        return f"DEV:{self.device}"

    @property
    def label(self) -> str:
        bits = [self.device]
        if self.description and self.description != "n/a":
            bits.append(self.description)
        if self.serial_number:
            bits.append(f"[{self.serial_number}]")
        return "  ".join(bits)


def enumerate_ports() -> List[PortInfo]:
    """Refreshable device list for the connection panel (§3.3)."""
    if not SERIAL_AVAILABLE:
        return []
    out = []
    for p in list_ports.comports():
        out.append(PortInfo(
            device=p.device,
            description=p.description or "",
            serial_number=getattr(p, "serial_number", None),
            vid=getattr(p, "vid", None),
            pid=getattr(p, "pid", None),
        ))
    return out


def resolve_stable_id(stable_id: str) -> Optional[str]:
    """Find the device path currently backing a remembered stable id."""
    for p in enumerate_ports():
        if p.stable_id == stable_id:
            return p.device
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Reader thread
# ─────────────────────────────────────────────────────────────────────────────

class SerialSource(QThread):
    """One reader thread for the single USB source (§3.10).

    Owns the port exclusively. Reconnects on its own every 2 s while the
    supervisor wants it connected (§3.9).
    """

    #: Emitted when lines are waiting. Carries no payload — the supervisor
    #: drains the queue, so a burst of packets is one signal rather than
    #: one signal each, and the UI cannot be flooded with queued slots.
    lines_available = pyqtSignal()
    state_changed = pyqtSignal(object)          # SourceState
    #: Source-level events for the log and the event strip (§3.9): the
    #: outage is annotated as a source event, not two vehicle outages.
    source_event = pyqtSignal(str)

    #: §3.10 — bounded, drop-oldest. Sized at 10 s of nominal traffic
    #: (20 lines/s + heartbeat) so a brief UI stall loses nothing, while
    #: a genuine stall drops the oldest lines rather than growing without
    #: bound (§13.2). Dropped lines are counted and surfaced, never
    #: silently discarded.
    QUEUE_MAX = 250

    RECONNECT_INTERVAL_S = 2.0                  # §3.9

    def __init__(self, stable_id: str, baudrate: int = 115200,
                 device_hint: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.stable_id = stable_id
        self.baudrate = baudrate
        self.device_hint = device_hint

        self._running = False
        self._ser = None
        self._state = SourceState.DISCONNECTED

        # deque(maxlen=…) makes the bounded-drop-oldest guarantee
        # structurally atomic rather than accidentally safe (§13.2, §5 review).
        self._queue: Deque[Tuple[str, float]] = deque(maxlen=self.QUEUE_MAX)
        self._dropped = 0
        self._last_line_at = 0.0

        #: Outgoing lines, written from the reader thread so the UI never
        #: touches the port. Small — commands are operator-initiated and
        #: therefore rare (§1.4).
        self._tx: Deque[str] = deque()

    # ── public API (UI thread) ───────────────────────────────────────────

    def drain(self, limit: int = 500) -> List[Tuple[str, float]]:
        """Take up to `limit` queued (line, received_at) pairs."""
        out: List[Tuple[str, float]] = []
        q = self._queue
        while q and len(out) < limit:
            try:
                out.append(q.popleft())
            except IndexError:
                break
        return out

    @property
    def dropped_lines(self) -> int:
        return self._dropped

    @property
    def state(self) -> SourceState:
        return self._state

    @property
    def port_name(self) -> str:
        if self._ser is not None and getattr(self._ser, "port", None):
            return str(self._ser.port)
        resolved = resolve_stable_id(self.stable_id)
        if resolved:
            return resolved
        if self.device_hint:
            return self.device_hint
        if self.stable_id:
            if self.stable_id.startswith("DEV:"):
                return self.stable_id[4:]
            return self.stable_id
        return "—"

    def send_line(self, line: str) -> None:
        """Queue one already-framed line for transmission (§10.5)."""
        self._tx.append(line)

    def stop(self) -> None:
        self._running = False
        self.wait(3000)

    # ── thread body ──────────────────────────────────────────────────────

    def run(self) -> None:                                # pragma: no cover
        self._running = True
        while self._running:
            if self._ser is None:
                if not self._try_open():
                    # §3.9 — retry the same stable identifier every 2 s.
                    # Sleep in slices so stop() stays responsive.
                    self._sleep_interruptible(self.RECONNECT_INTERVAL_S)
                    continue
            self._pump()
        self._close()

    def _try_open(self) -> bool:
        self._set_state(SourceState.CONNECTING)
        device = resolve_stable_id(self.stable_id) or self.device_hint
        if device is None:
            self._set_state(SourceState.DISCONNECTED)
            return False
        try:
            # timeout=0.1 rather than blocking: the loop must stay
            # responsive to stop() and to queued commands even when the
            # receiver has gone quiet.
            self._ser = serial.Serial(device, self.baudrate, timeout=0.1)
        except Exception as e:
            self._set_state(SourceState.LINK_ERROR)
            self.source_event.emit(f"Could not open {device}: {e}")
            return False

        # §3.12 — discard any partial line sitting in the driver buffer at
        # connect time. Without this the first "line" read is whatever
        # fragment was mid-flight, which the checksum would reject anyway
        # but which shows up as a spurious malformed count at every
        # connect.
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass

        self._last_line_at = time.monotonic()
        self._set_state(SourceState.CONNECTED)
        self.source_event.emit(f"Connected to {device} at {self.baudrate} baud")
        return True

    def _pump(self) -> None:
        try:
            for line in self._tx_drain():
                self._ser.write(line.encode("utf-8"))
            if self._tx_wrote:
                self._ser.flush()

            raw = self._ser.readline()
            now = time.monotonic()

            if raw:
                text = raw.decode("utf-8", errors="ignore").strip()
                if text:
                    self._last_line_at = now
                    if self._state is not SourceState.CONNECTED:
                        self._set_state(SourceState.CONNECTED)
                    self._enqueue(text, now)
            else:
                # §3.6 — port open but silent. Distinct from a vehicle
                # being silent: this means nothing at all is arriving,
                # including the receiver's own heartbeat.
                if (self._state is SourceState.CONNECTED
                        and now - self._last_line_at > NO_DATA_AFTER_S):
                    self._set_state(SourceState.NO_DATA)

        except Exception as e:
            self.source_event.emit(f"Source dropped: {e}")
            self._close()
            self._set_state(SourceState.LINK_ERROR)

    _tx_wrote = False

    def _tx_drain(self) -> List[str]:
        out = []
        while self._tx:
            out.append(self._tx.popleft())
        self._tx_wrote = bool(out)
        return out

    def _enqueue(self, text: str, now: float) -> None:
        # With maxlen set, deque.append() atomically evicts from the left
        # when full — no separate check-then-popleft pair that could race
        # with drain() on the UI thread.
        was_full = len(self._queue) == self.QUEUE_MAX
        self._queue.append((text, now))
        if was_full:
            # The oldest entry was silently evicted by maxlen; count it.
            self._dropped += 1
        if len(self._queue) == 1:
            self.lines_available.emit()

    def _sleep_interruptible(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            self.msleep(50)

    def _close(self) -> None:
        if self._ser is not None:
            try:
                if self._ser.is_open:
                    self._ser.close()
            except Exception:
                pass
            self._ser = None

    def _set_state(self, s: SourceState) -> None:
        if s is not self._state:
            self._state = s
            self.state_changed.emit(s)


# ─────────────────────────────────────────────────────────────────────────────
#  Supervisor — the three health levels of §3.5 / §3.6
# ─────────────────────────────────────────────────────────────────────────────

class LinkSupervisor(QObject):
    """Owns the source and the demultiplexer, and turns raw lines into
    per-vehicle telemetry on the UI thread.

    Also the single place that knows the §3.5 rule: simultaneous loss of
    both vehicles is reported as one receiver-or-source problem, never as
    two independent vehicle failures.
    """

    rocket_packet = pyqtSignal(object, object)   # TelemetryPacket, IngestResult
    cansat_packet = pyqtSignal(object, object)
    line_rejected = pyqtSignal(object)           # RejectedLine
    raw_line = pyqtSignal(str, float)            # for the raw log (§12.1)

    source_state_changed = pyqtSignal(object)    # SourceState
    receiver_state_changed = pyqtSignal(object)  # ReceiverState
    vehicle_state_changed = pyqtSignal(object, object)  # VehicleID, VehicleLinkState
    notice = pyqtSignal(str)

    #: §13.3 — drain and render at a fixed rate rather than per packet.
    DRAIN_INTERVAL_MS = 33                       # ~30 fps ceiling

    def __init__(self, codec: Optional[PacketCodec] = None,
                 thresholds: Optional[LossThresholds] = None, parent=None):
        super().__init__(parent)
        self.demux = TelemetryDemux(codec or CsvCodec(), thresholds)
        self.source: Optional[SerialSource] = None

        self._receiver_state = ReceiverState.NO_HEARTBEAT
        self._vehicle_states: Dict[VehicleID, VehicleLinkState] = {
            v: VehicleLinkState.LOST for v in VehicleID
        }
        self._command_sequence = 0

        self._timer = QTimer(self)
        self._timer.setInterval(self.DRAIN_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    # ── lifecycle ────────────────────────────────────────────────────────

    def connect_to(self, stable_id: str, baudrate: int = 115200,
                   device_hint: Optional[str] = None) -> None:
        self.disconnect_source()
        self.source = SerialSource(stable_id, baudrate, device_hint)
        self.source.state_changed.connect(self.source_state_changed)
        self.source.source_event.connect(self.notice)
        self.source.start()
        self._timer.start()

    def disconnect_source(self) -> None:
        self._timer.stop()
        if self.source is not None:
            self.source.stop()
            self.source = None

    # ── §10.5 uplink ─────────────────────────────────────────────────────

    def send_command(self, target: VehicleID, command: str,
                     sequence: Optional[int] = None) -> int:
        """Frame and queue one operator-initiated command (§1.4).

        `sequence` is supplied by CommandCentre, which owns the §10.7
        acknowledgment lifecycle. It must be the same number that appears
        on the wire — a second counter here would produce sequence
        numbers the ack matcher has never heard of, and every command
        would sit unacknowledged forever while the vehicle was in fact
        replying correctly. The local counter is only a fallback for
        callers that do not track acks.

        Never called automatically by anything in this module.
        """
        if sequence is None:
            self._command_sequence += 1
            sequence = self._command_sequence
        if self.source is not None:
            if command in BENCH_COMMANDS or command in (
                "TEST1", "TEST2", "TEST3", "TEST4", "TEST5", "NEXT", "TEST6", "RESET", "START", "CONFIRM"
            ):
                self.source.send_line(command + "\n")
            else:
                self.source.send_line(
                    frame_command(target, command, sequence) + "\n")
        return sequence

    # ── drain loop ───────────────────────────────────────────────────────

    def _tick(self) -> None:
        if self.source is None:
            return

        for line, received_at in self.source.drain():
            # §12.1 — every received line goes to the raw log verbatim,
            # including ones about to be rejected, before any judgement is
            # made about it.
            self.raw_line.emit(line, received_at)

            result: Optional[IngestResult] = self.demux.feed(line, received_at)
            if result is None:
                continue
            if result.rejected is not None:
                self.line_rejected.emit(result.rejected)
                continue

            packet: TelemetryPacket = result.packet
            if packet.vehicle_id is VehicleID.ROCKET:
                self.rocket_packet.emit(packet, result)
            else:
                self.cansat_packet.emit(packet, result)

        for msg in self.demux.drain_notices():
            self.notice.emit(msg)

        self._poll_health()

    def _poll_health(self) -> None:
        now = time.monotonic()

        age = self.demux.heartbeat_age(now)
        rx = (ReceiverState.ALIVE
              if age is not None and age <= HEARTBEAT_TIMEOUT_S
              else ReceiverState.NO_HEARTBEAT)
        if rx is not self._receiver_state:
            self._receiver_state = rx
            self.receiver_state_changed.emit(rx)
            if rx is ReceiverState.NO_HEARTBEAT:
                self.notice.emit(
                    "Receiver heartbeat lost — this is a receiver-level fault, "
                    "not a vehicle fault (§3.5)."
                )

        for vid, stream in self.demux.streams.items():
            s = stream.link_state(now)
            if s is not self._vehicle_states[vid]:
                self._vehicle_states[vid] = s
                self.vehicle_state_changed.emit(vid, s)

        # §3.5 — never present a simultaneous double loss as two
        # independent vehicle failures. It reads as a catastrophic flight
        # event when it is far more likely to be a USB cable.
        if self.demux.all_vehicles_silent(now):
            if (self._receiver_state is ReceiverState.NO_HEARTBEAT
                    or (self.source is not None
                        and self.source.state is not SourceState.CONNECTED)):
                self.notice.emit(
                    "Both vehicles silent AND receiver/source unhealthy — "
                    "treat as a ground-side outage until proven otherwise."
                )

    def inject_line(self, line: str, received_at: Optional[float] = None) -> None:
        """Inject one raw line directly into the demux pipeline."""
        if received_at is None:
            received_at = time.monotonic()
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
        for msg in self.demux.drain_notices():
            self.notice.emit(msg)

    # ── read-only views for the health strip (§6.5) ──────────────────────

    @property
    def receiver_state(self) -> ReceiverState:
        return self._receiver_state

    def vehicle_link_state(self, vid: VehicleID) -> VehicleLinkState:
        return self._vehicle_states[vid]

    @property
    def source_state(self) -> SourceState:
        return self.source.state if self.source else SourceState.DISCONNECTED

    @property
    def port_name(self) -> str:
        return self.source.port_name if self.source else "—"

    @property
    def baudrate(self) -> int:
        return self.source.baudrate if self.source else 115200
