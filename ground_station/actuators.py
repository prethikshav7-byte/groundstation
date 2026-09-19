"""
ground_station/actuators.py
═══════════════════════════
§10.1 — three positional servos on one PCA9685. §10.1.1 travel limits and
clamping. §10.1.2 shared-bus fairness. §10.6 destructive-command arming.

Qt-free; the page renders what this reports.

THE ±180° PROBLEM (§10.1.1)
───────────────────────────
Range is 0°–360° with home at 180°, so a ±180° move is reachable only
from home and lands exactly on a hard stop with zero margin. §10.1.1's
instruction is to disable the ±180° buttons unless the actuator is at
home, rather than offering them and clamping — "a control that is nearly
always impossible trains operators to ignore clamp warnings."

That last clause is the real requirement. Clamp warnings only work if
they are rare. This module therefore reports reachability *before* the
click (`can_move`) as well as clamping after it, and the page uses the
former to disable rather than the latter to apologise.

NEVER DISPLAY COMMANDED AS ACTUAL (§10.1)
─────────────────────────────────────────
`commanded` and `actual` are separate fields and `actual` stays None
until the vehicle echoes a position back. A UI that falls back to the
commanded value when no echo has arrived is showing the operator their
own intention and calling it telemetry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
#  §10.1 — the hardware, confirmed
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ActuatorSpec:
    id: str
    mechanism: str
    driver: str
    channel: int
    range_lo: float
    range_hi: float
    home: float
    #: §10.6 — irreversible in flight; needs arm → fire.
    destructive: bool = False


#: §10.1, verbatim. All three on one PCA9685 — no stepper, no pyro, no
#: solenoid. (The ULN2003 in the original note is not part of this build.)
ACTUATORS: Tuple[ActuatorSpec, ...] = (
    ActuatorSpec("ACT_DOOR", "Rocket door", "PCA9685", 0, 0.0, 360.0, 180.0),
    ActuatorSpec("ACT_DEPLOY", "Payload deploy", "PCA9685", 1, 0.0, 360.0,
                 180.0, destructive=True),
    ActuatorSpec("ACT_SEPARATE", "Separation", "PCA9685", 2, 0.0,
                 360.0, 180.0, destructive=True),
)

BY_ID: Dict[str, ActuatorSpec] = {a.id: a for a in ACTUATORS}

#: Tolerance for "is at home". A servo that reports 180.2° is at home for
#: any practical purpose, and requiring exactness would make the ±180°
#: buttons permanently dead on real hardware.
HOME_TOLERANCE_DEG = 1.0


@dataclass(frozen=True)
class MoveResult:
    """Outcome of evaluating a requested move."""
    target: float
    clamped: bool
    #: §10.1.1 — "requested +180°, clamped to +115°". Never silent, never
    #: wrapping.
    message: str = ""
    delta_applied: float = 0.0
    delta_requested: float = 0.0


class Actuator:
    """Live state of one servo."""

    def __init__(self, spec: ActuatorSpec):
        self.spec = spec
        #: What the operator last asked for.
        self.commanded: Optional[float] = None
        #: What the vehicle last echoed back. None until it does.
        self.actual: Optional[float] = None
        self.last_command_at: Optional[float] = None
        self.locked: bool = True

    # ── position ─────────────────────────────────────────────────────────

    @property
    def position(self) -> Optional[float]:
        """Best known *actual* position.

        Falls back to nothing. §10.1: "Never display commanded position as
        if it were actual." Callers that want the commanded value must ask
        for it by name.
        """
        return self.actual

    @property
    def at_home(self) -> bool:
        if self.actual is None:
            return False
        return abs(self.actual - self.spec.home) <= HOME_TOLERANCE_DEG

    @property
    def remaining_travel(self) -> Tuple[Optional[float], Optional[float]]:
        """(down, up) degrees available from the current actual position.

        None when the position is unknown — remaining travel computed
        from a commanded-but-unconfirmed position would be a guess
        presented as a measurement.
        """
        if self.actual is None:
            return None, None
        return (self.actual - self.spec.range_lo,
                self.spec.range_hi - self.actual)

    # ── §10.1.1 ──────────────────────────────────────────────────────────

    def can_move(self, delta: float) -> Tuple[bool, str]:
        """Would this relative move fit, without clamping?

        Used to *disable* controls rather than to clamp them after the
        fact — see the module docstring.
        """
        if self.actual is None:
            return False, "Actual position unknown — waiting for echo"
        target = self.actual + delta
        if target < self.spec.range_lo:
            return False, (f"{delta:+.0f}° would pass the {self.spec.range_lo:.0f}° "
                           f"limit from {self.actual:.0f}°")
        if target > self.spec.range_hi:
            return False, (f"{delta:+.0f}° would pass the {self.spec.range_hi:.0f}° "
                           f"limit from {self.actual:.0f}°")
        return True, ""

    def evaluate(self, delta: float) -> MoveResult:
        """Clamp a relative move to the travel limits (§10.1.1).

        Clamped, never wrapped: a 360° range with a wrap would turn a
        +180° request from 200° into a move to 20°, which is a completely
        different mechanical outcome from the one requested.
        """
        base = self.actual if self.actual is not None else self.spec.home
        raw = base + delta
        target = min(self.spec.range_hi, max(self.spec.range_lo, raw))
        clamped = abs(target - raw) > 1e-9
        applied = target - base

        msg = ""
        if clamped:
            msg = (f"requested {delta:+.0f}°, clamped to {applied:+.0f}° "
                   f"(limit {self.spec.range_lo:.0f}–{self.spec.range_hi:.0f}°)")
        return MoveResult(target=target, clamped=clamped, message=msg,
                          delta_applied=applied, delta_requested=delta)

    def note_commanded(self, target: float) -> None:
        self.commanded = target
        self.last_command_at = time.monotonic()

    def note_echo(self, actual: float) -> None:
        """Position echoed back by the vehicle (§10.1)."""
        self.actual = actual

    @property
    def commanded_matches_actual(self) -> Optional[bool]:
        """None while either is unknown. False is a real signal — the
        servo did not reach where it was told to go."""
        if self.commanded is None or self.actual is None:
            return None
        return abs(self.commanded - self.actual) <= HOME_TOLERANCE_DEG


# ─────────────────────────────────────────────────────────────────────────────
#  §10.6 — arm → fire
# ─────────────────────────────────────────────────────────────────────────────

#: §10.6 — armed state auto-disarms after this long.
ARM_TIMEOUT_S = 10.0


class ArmingCentre:
    """Two-step arming for irreversible commands (§10.6).

    "MANUAL_DEPLOY, ACT_DEPLOY, and ACT_SEPARATE are irreversible in
    flight. Two-step arm → fire, armed state visibly indicated,
    auto-disarm after 10 s. No single click fires a separation or
    deployment. Arming one destructive control disarms any other."

    That last sentence is why this is one shared object rather than a
    flag on each control: with per-control flags, two things can be armed
    at once and the operator's next click lands on whichever their cursor
    happens to be over.
    """

    def __init__(self, timeout: float = ARM_TIMEOUT_S):
        self.timeout = timeout
        self._armed: Optional[str] = None
        self._armed_at: float = 0.0

    def arm(self, control_id: str, now: Optional[float] = None) -> None:
        # Arming one disarms any other, implicitly — there is only ever
        # one slot.
        self._armed = control_id
        self._armed_at = now if now is not None else time.monotonic()

    def disarm(self) -> None:
        self._armed = None

    def expire(self, now: Optional[float] = None) -> bool:
        """Apply the auto-disarm timeout.  Returns True if it just fired.

        This is the ONLY place that mutates ``_armed`` on timeout.  Call
        it from every refresh loop so the countdown actually reaches zero.
        Keeping the mutation here instead of inside ``armed_control`` means
        the getter is a pure read — the 500 ms UI refresh can call it any
        number of times without accidentally clearing the armed state.
        """
        if self._armed is None:
            return False
        now = now if now is not None else time.monotonic()
        if now - self._armed_at > self.timeout:
            self._armed = None
            return True
        return False

    def armed_control(self, now: Optional[float] = None) -> Optional[str]:
        """Pure read — returns the currently armed control ID, or None.

        Does NOT apply the timeout; call ``expire()`` from the refresh
        loop for that.  Keeping this pure means multiple callers in one
        tick all see the same answer.
        """
        if self._armed is None:
            return None
        now = now if now is not None else time.monotonic()
        if now - self._armed_at > self.timeout:
            # Already expired but expire() hasn't been called yet.  Report
            # None without clearing — the next expire() call will do that.
            return None
        return self._armed

    def is_armed(self, control_id: str, now: Optional[float] = None) -> bool:
        return self.armed_control(now) == control_id

    def seconds_remaining(self, now: Optional[float] = None) -> float:
        if self._armed is None:
            return 0.0
        now = now if now is not None else time.monotonic()
        return max(0.0, self.timeout - (now - self._armed_at))

    def fire(self, control_id: str, now: Optional[float] = None) -> bool:
        """Consume the arm. Returns False if this control is not armed.

        Disarms on success, so a second click cannot fire again without a
        fresh arm — the auto-disarm timeout protects against walking
        away, and this protects against a double-click.
        """
        if not self.is_armed(control_id, now):
            return False
        self._armed = None
        return True


# ─────────────────────────────────────────────────────────────────────────────
#  §10.1.2 — shared-bus fairness
# ─────────────────────────────────────────────────────────────────────────────

class BusQueue:
    """Serialises actuator traffic in the driver layer, not the UI.

    §10.1.2: "Commanding one must not block or delay status polling of
    the others — serialise bus access in the driver layer, not by
    stalling the UI."

    Three servos share one PCA9685 behind one I²C bus, so the ordering
    has to happen somewhere. Doing it here means a click returns
    immediately and the queue drains on its own; doing it by making the
    UI wait would freeze the whole dashboard behind one servo, at
    precisely the moment the operator most wants to see the other two.

    Ordering: moves before polls. A status poll delayed by 20 ms costs
    nothing; a deployment delayed behind three polls is a command the
    operator watched not happen.
    """

    def __init__(self) -> None:
        self._moves: List[Tuple[str, float]] = []
        self._polls: List[str] = []

    def submit_move(self, actuator_id: str, target: float) -> None:
        self._moves.append((actuator_id, target))

    def submit_poll(self, actuator_id: str) -> None:
        if actuator_id not in self._polls:
            self._polls.append(actuator_id)

    def next(self) -> Optional[Tuple[str, str, Optional[float]]]:
        """Pop the next bus operation: (kind, actuator_id, target)."""
        if self._moves:
            aid, target = self._moves.pop(0)
            return ("move", aid, target)
        if self._polls:
            return ("poll", self._polls.pop(0), None)
        return None

    @property
    def depth(self) -> int:
        return len(self._moves) + len(self._polls)


# ─────────────────────────────────────────────────────────────────────────────
#  §10.2 — calibration
# ─────────────────────────────────────────────────────────────────────────────

class CalibrationOutcome(Enum):
    NOT_RUN = "not run"
    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class SensorCalibration:
    key: str
    label: str
    target: str


#: §10.2, verbatim.
CALIBRATIONS: Tuple[SensorCalibration, ...] = (
    SensorCalibration("PRESSURE", "Pressure",
                      "Actual atmospheric pressure at launch site altitude, "
                      "noise-filtered"),
    SensorCalibration("ALTITUDE", "Altitude",
                      "Zero at ground level — AGL, not MSL"),
    SensorCalibration("IMU", "IMU / gyro", "All axes zeroed"),
    SensorCalibration("GNSS", "GNSS", "Reference fix captured at the pad"),
    SensorCalibration("TEMPERATURE", "Temperature",
                      "Ambient atmospheric temperature"),
)


@dataclass
class CalibrationRun:
    """Result set for one Calibrate All sequence (§10.2).

    Per-sensor pass/fail, "not one aggregate result" — an aggregate hides
    which sensor failed, which is the only part the operator can act on.
    """
    results: Dict[str, CalibrationOutcome] = field(
        default_factory=lambda: {c.key: CalibrationOutcome.NOT_RUN
                                 for c in CALIBRATIONS})
    messages: Dict[str, str] = field(default_factory=dict)

    def set(self, key: str, outcome: CalibrationOutcome, message: str = "") -> None:
        self.results[key] = outcome
        if message:
            self.messages[key] = message

    @property
    def complete(self) -> bool:
        return all(o in (CalibrationOutcome.PASS, CalibrationOutcome.FAIL)
                   for o in self.results.values())

    @property
    def failures(self) -> List[str]:
        return [k for k, v in self.results.items()
                if v is CalibrationOutcome.FAIL]
