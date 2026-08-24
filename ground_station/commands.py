"""
ground_station/commands.py
══════════════════════════
§10.5 command set, §10.4 sampling-vs-transmission state, §10.7
acknowledgment lifecycle. Qt-free so every guard is testable.

THREE RULES THIS MODULE EXISTS TO ENFORCE
─────────────────────────────────────────

1. §1.4 — "No command is ever sent automatically. Every telecommand is
   operator-initiated." There is no timer, no retry loop and no
   watchdog in this file. `CommandCentre.send()` is only ever called
   from a widget's clicked handler.

2. §10.7 — retry is operator-initiated only. "An auto-retried
   ACT_SEPARATE that was in fact received the first time fires the
   mechanism twice." `resend()` exists and is deliberately not wired to
   anything that could call it on its own.

3. §10.4 — both mode indicators show the *confirmed-active* mode from
   the vehicle's acknowledgment, never the last-requested mode. Never
   flip optimistically. This is why `VehicleModes` has three values per
   axis rather than two: the third is "we asked and do not yet know".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from .models import FlightState, GROUND_STATES, VehicleID, VehicleLinkState


# ─────────────────────────────────────────────────────────────────────────────
#  §10.5 — the command set and its guards
# ─────────────────────────────────────────────────────────────────────────────

class Guard(Enum):
    NONE = "none"
    CONFIRM_IN_FLIGHT = "confirm if in flight"
    GROUND_ONLY = "BOOT / PRE_LAUNCH only"
    TWO_STEP = "two-step confirm"
    DESTRUCTIVE = "two-step arm → fire"
    RECOVERY_USB_ONLY = "direct USB only; RECOVERY only"
    NOT_IN_SIMULATOR_MODE = "disabled in Simulator Mode"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    guard: Guard = Guard.NONE
    #: Rocket-only commands (§10.5 marks MANUAL_DEPLOY as such).
    rocket_only: bool = False
    #: §10.6 — irreversible in flight, needs arm → fire rather than a
    #: single click.
    destructive: bool = False


#: §10.5, verbatim. RESET_COUNTERS is absent by design — it was removed,
#: and counters are monotonic for the entire mission (§4.1 field 4).
COMMANDS: Dict[str, CommandSpec] = {
    c.name: c for c in [
        CommandSpec("ENABLE_TELEMETRY", "Start transmission"),
        CommandSpec("DISABLE_TELEMETRY", "Stop transmission"),
        CommandSpec("SET_MODE_LIVE", "Start sampling",
                    Guard.CONFIRM_IN_FLIGHT),
        CommandSpec("SET_MODE_HIBERNATE", "Stop sampling",
                    Guard.CONFIRM_IN_FLIGHT),
        CommandSpec("ENTER_SIMULATION", "Flight software simulation mode",
                    Guard.NOT_IN_SIMULATOR_MODE),
        CommandSpec("REQUEST_STATUS", "System health report"),
        CommandSpec("REFRESH_PROCESSOR", "Reinitialise the processing loop",
                    Guard.TWO_STEP),
        CommandSpec("MANUAL_DEPLOY", "Emergency manual deployment",
                    Guard.DESTRUCTIVE, rocket_only=True, destructive=True),
        CommandSpec("LIST_FILES", "List experiment files",
                    Guard.RECOVERY_USB_ONLY),
        CommandSpec("REQUEST_FILE", "Retrieve an experiment file",
                    Guard.RECOVERY_USB_ONLY),
        CommandSpec("ABORT_TRANSFER", "Abort the current transfer",
                    Guard.RECOVERY_USB_ONLY),
        # §10.2 — calibration commands.  GROUND_ONLY because zeroing the
        # barometer mid-flight corrupts the altitude datum for the rest of
        # the mission.  Each sensor is a separate command so the operator
        # can re-run one without re-running all.
        CommandSpec("CALIBRATE_PRESSURE",
                    "Zero barometer to local atmospheric pressure",
                    Guard.GROUND_ONLY),
        CommandSpec("CALIBRATE_ALTITUDE",
                    "Zero altimeter AGL datum at ground level",
                    Guard.GROUND_ONLY),
        CommandSpec("CALIBRATE_IMU",
                    "Zero all IMU / gyro axes",
                    Guard.GROUND_ONLY),
        CommandSpec("CALIBRATE_GNSS",
                    "Capture GNSS reference fix at the pad",
                    Guard.GROUND_ONLY),
        CommandSpec("CALIBRATE_TEMPERATURE",
                    "Record ambient atmospheric temperature",
                    Guard.GROUND_ONLY),
        CommandSpec("CALIBRATE_ALL",
                    "Run all calibrations in sequence",
                    Guard.GROUND_ONLY),
    ]
}


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str = ""
    #: True when the operator must confirm before this proceeds.
    needs_confirm: bool = False


def check_guard(spec: CommandSpec,
                state: Optional[FlightState],
                link: VehicleLinkState,
                simulator_mode: bool,
                usb_direct: bool = False) -> GuardResult:
    """Evaluate one command against current conditions.

    Returns a reason on refusal rather than a bare False, because a
    disabled control with no explanation is a control the operator will
    eventually work around.
    """
    # §10.8 — controls for a vehicle in LOST state are disabled rather
    # than queued to fire on reconnect. A queued ACT_SEPARATE that fires
    # the instant the link returns is a command nobody is watching for.
    if link is VehicleLinkState.LOST:
        return GuardResult(False, "Vehicle link lost — command not queued")

    if spec.guard is Guard.NOT_IN_SIMULATOR_MODE:
        if simulator_mode:
            # §2.5 — the app-side Simulator Mode and the vehicle-side
            # Flight Software Simulation are different things, and
            # ENTER_SIMULATION is disabled while in the former.
            return GuardResult(
                False,
                "Disabled in Simulator Mode — this command puts the "
                "vehicle's flight software into simulation, which is a "
                "different thing")
        if state is not None and state.ordinal > FlightState.PRE_LAUNCH.ordinal:
            return GuardResult(False, "Disallowed after PRE_LAUNCH")
        return GuardResult(True)

    if spec.guard is Guard.GROUND_ONLY:
        if state is None or state not in GROUND_STATES:
            # §10.2 — "zeroing the barometer mid-flight corrupts the
            # altitude datum for the rest of the mission".
            return GuardResult(
                False,
                f"Only available in BOOT or PRE_LAUNCH "
                f"(vehicle is in {state.value if state else 'unknown'})")
        return GuardResult(True)

    if spec.guard is Guard.RECOVERY_USB_ONLY:
        if not usb_direct:
            # §11.4 — a flight-length file over LoRa is tens of minutes
            # to hours, starving telemetry. Wrong link for the job.
            return GuardResult(
                False,
                "Needs a direct USB connection to the recovered CanSat — "
                "not available over the LoRa relay")
        if state is not FlightState.RECOVERY:
            return GuardResult(
                False, "Only available in RECOVERY — never during flight")
        return GuardResult(True)

    if spec.guard is Guard.CONFIRM_IN_FLIGHT:
        in_flight = state is not None and state not in GROUND_STATES
        return GuardResult(True, needs_confirm=in_flight)

    if spec.guard in (Guard.TWO_STEP, Guard.DESTRUCTIVE):
        return GuardResult(True, needs_confirm=True)

    return GuardResult(True)


# ─────────────────────────────────────────────────────────────────────────────
#  §10.7 — acknowledgment lifecycle
# ─────────────────────────────────────────────────────────────────────────────

class CommandStatus(Enum):
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    EXECUTED = "executed"
    REJECTED = "rejected"
    TIMED_OUT = "timed out"


#: §10.7 — how long before an unacknowledged command is called out.
#: Generous relative to the link: an uplink at LoRa rates plus vehicle
#: turnaround is well under a second, so 4 s means "this did not arrive"
#: rather than "this is slow".
ACK_TIMEOUT_S = 4.0


@dataclass
class CommandRecord:
    """One sent command and everything known about its fate."""
    sequence: int
    target: VehicleID
    command: str
    argument: str = ""
    sent_at: float = field(default_factory=time.monotonic)
    status: CommandStatus = CommandStatus.SENT
    #: The vehicle's own response text, when one arrives.
    response: str = ""
    #: §10.7 — a receiver forwarding confirmation is displayed as a
    #: separate, lesser signal. It is NOT an acknowledgment: "the ground
    #: receiver forwarding a command successfully says nothing about
    #: whether the vehicle heard it."
    receiver_forwarded: bool = False
    #: Set when this record is a resend of an earlier attempt.
    resend_of: Optional[int] = None

    @property
    def wire_text(self) -> str:
        return f"{self.command} {self.argument}".strip()

    @property
    def unacknowledged(self) -> bool:
        return self.status in (CommandStatus.SENT, CommandStatus.TIMED_OUT)

    def age(self, now: Optional[float] = None) -> float:
        return (now or time.monotonic()) - self.sent_at


class CommandCentre:
    """Tracks every command sent this session (§10.7).

    Holds no Qt objects and opens no ports — `_transmit` is injected, so
    the whole lifecycle including timeouts can be driven in a test.
    """

    def __init__(self, transmit: Callable[[VehicleID, str, int], None],
                 ack_timeout: float = ACK_TIMEOUT_S):
        self._transmit = transmit
        self.ack_timeout = ack_timeout
        self.records: List[CommandRecord] = []
        self._by_sequence: Dict[int, CommandRecord] = {}
        self._sequence = 0

        #: §10.4 — per-vehicle confirmed modes.
        self.modes: Dict[VehicleID, "VehicleModes"] = {
            v: VehicleModes() for v in VehicleID
        }

    # ── sending ──────────────────────────────────────────────────────────

    def send(self, target: VehicleID, command: str,
             argument: str = "", resend_of: Optional[int] = None
             ) -> CommandRecord:
        """Transmit one command. Only ever called from an operator action."""
        self._sequence += 1
        rec = CommandRecord(sequence=self._sequence, target=target,
                            command=command, argument=argument,
                            resend_of=resend_of)
        self.records.append(rec)
        self._by_sequence[rec.sequence] = rec

        # §10.4 — record the *request*, but do not touch the displayed
        # mode. That only moves on an acknowledgment.
        modes = self.modes[target]
        if command in ("SET_MODE_LIVE", "SET_MODE_HIBERNATE"):
            modes.request_sampling(command, rec.sequence)
        elif command in ("ENABLE_TELEMETRY", "DISABLE_TELEMETRY"):
            modes.request_telemetry(command, rec.sequence)

        self._transmit(target, rec.wire_text, rec.sequence)
        return rec

    def resend(self, sequence: int) -> Optional[CommandRecord]:
        """§10.7 — operator-initiated resend, never automatic.

        A new sequence number is issued rather than reusing the original.
        Reusing it would make the acknowledgment ambiguous — an ack for
        the first attempt would be indistinguishable from an ack for the
        resend, which is exactly how a mechanism gets fired twice.
        """
        original = self._by_sequence.get(sequence)
        if original is None:
            return None
        return self.send(original.target, original.command,
                         original.argument, resend_of=sequence)

    # ── responses ────────────────────────────────────────────────────────

    def acknowledge(self, sequence: int, response: str = "") -> Optional[CommandRecord]:
        """A vehicle acknowledgment. Only the vehicle may call this path."""
        rec = self._by_sequence.get(sequence)
        if rec is None:
            return None
        rec.status = CommandStatus.ACKNOWLEDGED
        rec.response = response
        self.modes[rec.target].confirm(rec.sequence, rec.command)
        return rec

    def mark_executed(self, sequence: int, response: str = "") -> Optional[CommandRecord]:
        rec = self._by_sequence.get(sequence)
        if rec is None:
            return None
        rec.status = CommandStatus.EXECUTED
        if response:
            rec.response = response
        return rec

    def reject(self, sequence: int, reason: str) -> Optional[CommandRecord]:
        rec = self._by_sequence.get(sequence)
        if rec is None:
            return None
        rec.status = CommandStatus.REJECTED
        rec.response = reason
        self.modes[rec.target].abandon(rec.sequence)
        return rec

    def note_receiver_forwarded(self, sequence: int) -> None:
        """§10.7 — a lesser signal than an acknowledgment, shown
        separately and never mistaken for one."""
        rec = self._by_sequence.get(sequence)
        if rec is not None:
            rec.receiver_forwarded = True

    # ── timeouts ─────────────────────────────────────────────────────────

    def poll_timeouts(self, now: Optional[float] = None) -> List[CommandRecord]:
        """Move stale SENT records to TIMED_OUT and return them.

        Timing out is *not* retrying. The record is flagged so the UI can
        offer an explicit Resend; nothing here re-transmits.
        """
        now = now or time.monotonic()
        out = []
        for rec in self.records:
            if rec.status is CommandStatus.SENT and rec.age(now) > self.ack_timeout:
                rec.status = CommandStatus.TIMED_OUT
                self.modes[rec.target].abandon(rec.sequence)
                out.append(rec)
        return out

    @property
    def unacknowledged(self) -> List[CommandRecord]:
        return [r for r in self.records if r.unacknowledged]


# ─────────────────────────────────────────────────────────────────────────────
#  §10.4 — sampling and transmission are orthogonal
# ─────────────────────────────────────────────────────────────────────────────

class ModeValue(Enum):
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"          # requested, not yet acknowledged
    UNCONFIRMED = "UNCONFIRMED"  # requested, acknowledgment never came
    LIVE = "LIVE"
    HIBERNATE = "HIBERNATE"
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class VehicleModes:
    """Confirmed sampling and telemetry modes for one vehicle (§10.4).

    Both start UNKNOWN rather than defaulting to a plausible value. The
    app has not heard from the vehicle yet, and showing LIVE/ENABLED
    before any acknowledgment is the optimistic flip §10.4 forbids.
    """

    def __init__(self) -> None:
        self.sampling = ModeValue.UNKNOWN
        self.telemetry = ModeValue.UNKNOWN
        self._pending: Dict[int, Tuple[str, str]] = {}   # seq -> (axis, cmd)

    def request_sampling(self, command: str, sequence: int) -> None:
        self.sampling = ModeValue.PENDING
        self._pending[sequence] = ("sampling", command)

    def request_telemetry(self, command: str, sequence: int) -> None:
        self.telemetry = ModeValue.PENDING
        self._pending[sequence] = ("telemetry", command)

    def confirm(self, sequence: int, command: str) -> None:
        axis_cmd = self._pending.pop(sequence, None)
        if axis_cmd is None:
            return
        axis, cmd = axis_cmd
        if axis == "sampling":
            self.sampling = (ModeValue.LIVE if cmd == "SET_MODE_LIVE"
                             else ModeValue.HIBERNATE)
        else:
            self.telemetry = (ModeValue.ENABLED if cmd == "ENABLE_TELEMETRY"
                              else ModeValue.DISABLED)

    def abandon(self, sequence: int) -> None:
        """Request timed out or was rejected — the mode is not what was
        asked for, and it is not what it was before either."""
        axis_cmd = self._pending.pop(sequence, None)
        if axis_cmd is None:
            return
        axis, _ = axis_cmd
        if axis == "sampling":
            self.sampling = ModeValue.UNCONFIRMED
        else:
            self.telemetry = ModeValue.UNCONFIRMED

    @property
    def dangerous(self) -> bool:
        """§10.4 — HIBERNATE + ENABLED.

        "Packets keep arriving so the link looks healthy, but the values
        are not live measurements." The one combination that is actively
        misleading rather than merely idle, and the reason this needs a
        persistent unmissable banner rather than a status line.
        """
        return (self.sampling is ModeValue.HIBERNATE
                and self.telemetry is ModeValue.ENABLED)
