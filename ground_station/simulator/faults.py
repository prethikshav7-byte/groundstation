"""
ground_station/simulator/faults.py
══════════════════════════════════
§13.6 — the failure modes the simulator must be able to produce on
demand.

The spec's own justification is the reason this file is as large as it
is: "Everything in §3.5, §4.5, §5.6, §7.5, §10.3, §11.5, and §11.7 is
otherwise untestable without a real failed flight."

That is not rhetorical. Roughly a third of the application is code that
only executes when something goes wrong on a link that mostly works.
Without deliberate injection, the first time the outage-reconciliation
path runs is the flight where an outage happened — which is exactly when
nobody can afford to discover it was wrong.

ON DEMAND, NOT AT RANDOM
────────────────────────
Every fault here is a scheduled event with an explicit trigger, not a
probability. Two reasons:

  1. A reproducible failure can be debugged. "It sometimes draws the
     wrong arc" is not a bug report anyone can act on.
  2. §13.6 requires specific, individually demonstrable behaviours — "an
     APOGEE lost entirely to packet loss" is a precise scenario. At 1%
     random loss, an eight-packet APOGEE window survives about 92% of the
     time, so a random simulator would almost never produce the case it
     is required to produce.

`FaultSchedule.demo()` builds a run in which every §13.6 item fires
exactly once, in an order that does not overlap.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..models import VehicleID


class FaultKind(Enum):
    """One per §13.6 bullet, plus the loss tiers it names separately."""
    DROPOUT = "dropout"                 # §7.5.2 tier 1: 1–4 packets
    GAP = "gap"                         # §7.5.2 tier 2: 5–19 packets
    OUTAGE = "outage"                   # §7.5.2 tier 3: ≥20 packets
    APOGEE_LOST = "apogee_lost"         # §5.6 inferred state
    MALFORMED = "malformed"             # §4.5
    CHECKSUM = "checksum"               # §4.3
    OUT_OF_ORDER = "out_of_order"       # §4.6
    DUPLICATE = "duplicate"             # §4.6
    HEARTBEAT_LOSS = "heartbeat_loss"   # §3.4 / §3.5
    SOURCE_DROP = "source_drop"         # §3.9
    REFRESH_DROPOUT = "refresh_dropout"  # §10.3 / §7.5.10
    RSSI_FADE = "rssi_fade"             # §3.7
    COMMAND_UNACKED = "command_unacked"  # §10.7
    TRANSFER_FAILURE = "transfer_failure"  # §11.5


@dataclass
class FaultWindow:
    """One scheduled fault.

    Windows are expressed in mission time rather than packet index so a
    schedule stays meaningful if the downlink rate changes (§3.11 option
    3 drops it to 3–5 Hz), and so the same schedule can be described in
    the same terms as the flight events it is meant to coincide with.
    """
    kind: FaultKind
    start_s: float
    end_s: float
    vehicle: Optional[VehicleID] = None   # None = both
    #: Free-form, surfaced in the simulator's event log so a tester can
    #: see what was injected without reading this file.
    note: str = ""

    def active(self, t: float, vehicle: VehicleID) -> bool:
        if self.vehicle is not None and self.vehicle is not vehicle:
            return False
        return self.start_s <= t < self.end_s

    def fires_at(self, t: float, dt: float, vehicle: VehicleID) -> bool:
        """True on the single tick where the window opens."""
        if self.vehicle is not None and self.vehicle is not vehicle:
            return False
        return self.start_s <= t < self.start_s + dt


@dataclass
class FaultSchedule:
    """An ordered set of fault windows for one simulated run."""
    windows: List[FaultWindow] = field(default_factory=list)
    #: Deterministic by default. A tester chasing a rendering bug needs
    #: the same flight twice; §13.6's failure modes are only useful if
    #: they can be reproduced on demand.
    seed: int = 20260808

    def add(self, kind: FaultKind, start_s: float, duration_s: float,
            vehicle: Optional[VehicleID] = None, note: str = "") -> "FaultSchedule":
        self.windows.append(
            FaultWindow(kind, start_s, start_s + duration_s, vehicle, note))
        return self

    def active_kinds(self, t: float, vehicle: VehicleID) -> Set[FaultKind]:
        return {w.kind for w in self.windows if w.active(t, vehicle)}

    def firing(self, t: float, dt: float,
               vehicle: VehicleID) -> List[FaultWindow]:
        return [w for w in self.windows if w.fires_at(t, dt, vehicle)]

    def window_for(self, kind: FaultKind) -> Optional[FaultWindow]:
        for w in self.windows:
            if w.kind is kind:
                return w
        return None

    # ── the §13.6 demonstration run ──────────────────────────────────────

    @classmethod
    def demo(cls, profile_apogee_s: float = 15.7,
             landing_s: float = 100.0) -> "FaultSchedule":
        """Every §13.6 item, once each, non-overlapping.

        The APOGEE window is placed from the profile's own apogee time
        rather than a hardcoded number, so it still lands on apogee if
        the flight profile is retuned. Getting that wrong would produce a
        schedule that claims to test §5.6 while quietly testing nothing —
        the most expensive kind of passing test.
        """
        s = cls()

        # ── §7.5.2, all three tiers, on the rocket during ascent ─────────
        s.add(FaultKind.DROPOUT, 6.0, 0.3, VehicleID.ROCKET,
              "3 packets lost — connects straight through, no annotation")
        s.add(FaultKind.GAP, 9.0, 1.0, VehicleID.ROCKET,
              "10 packets lost — line breaks, no interpolation")

        # ── §5.6, the whole APOGEE window ────────────────────────────────
        # Wide enough to swallow every packet carrying APOGEE, so the
        # observed sequence jumps COAST → DESCENT and the timeline must
        # mark APOGEE "inferred, not received".
        s.add(FaultKind.APOGEE_LOST, profile_apogee_s - 1.2, 2.8,
              VehicleID.ROCKET,
              "every APOGEE packet lost — §5.6 must infer the state")

        # ── §7.5.5, an outage that spans apogee on the CanSat ────────────
        # On the other vehicle and at a different time, so the two do not
        # mask each other and §3.8's independence is visible.
        s.add(FaultKind.OUTAGE, profile_apogee_s - 2.0, 4.0,
              VehicleID.CANSAT,
              "≥20 packets lost across apogee — ballistic reconciliation")

        # ── §4.5 / §4.3 / §4.6 ───────────────────────────────────────────
        s.add(FaultKind.MALFORMED, 24.0, 0.3, VehicleID.ROCKET,
              "truncated and non-numeric rows — logged and dropped")
        s.add(FaultKind.CHECKSUM, 27.0, 0.3, VehicleID.CANSAT,
              "bit flips caught by the vehicle checksum")
        s.add(FaultKind.OUT_OF_ORDER, 31.0, 0.5, VehicleID.ROCKET,
              "packets arrive out of sequence")
        s.add(FaultKind.DUPLICATE, 34.0, 0.4, VehicleID.CANSAT,
              "relay retransmits — counted apart from malformed")

        # ── §3.4 / §3.5 ──────────────────────────────────────────────────
        s.add(FaultKind.HEARTBEAT_LOSS, 38.0, 5.0, None,
              "receiver silent while vehicles keep transmitting — "
              "receiver-level alarm, not a vehicle fault")

        # ── §3.9 ─────────────────────────────────────────────────────────
        # Both vehicles go silent at once, which §3.5 says must NOT read
        # as two independent vehicle failures.
        s.add(FaultKind.SOURCE_DROP, 48.0, 4.0, None,
              "USB source drops — both vehicles silent together")

        # ── §3.7 ─────────────────────────────────────────────────────────
        s.add(FaultKind.RSSI_FADE, 56.0, 10.0, VehicleID.CANSAT,
              "CanSat fades behind the rocket body relative to the antenna")

        # ── §10.3 / §7.5.10 ──────────────────────────────────────────────
        s.add(FaultKind.REFRESH_DROPOUT, 70.0, 1.5, VehicleID.ROCKET,
              "REFRESH_PROCESSOR dropout — annotated, not reconciled blindly")

        # ── §10.7 and §11.5 are triggered by operator action rather than
        #    by mission time, so they are armed here and fire when the
        #    operator sends a command or starts a transfer.
        s.add(FaultKind.COMMAND_UNACKED, 0.0, landing_s, None,
              "the next command after T+80 s is never acknowledged")
        s.add(FaultKind.TRANSFER_FAILURE, 0.0, landing_s, None,
              "the first file transfer fails partway and can be resumed")

        return s


# ─────────────────────────────────────────────────────────────────────────────
#  Line corruption
# ─────────────────────────────────────────────────────────────────────────────

class LineCorrupter:
    """Turns a valid wire line into a specific kind of invalid one.

    Each method produces exactly one failure mode. Deliberately not a
    single "corrupt()" with a random choice: a test that asserts the
    parser reports CHECKSUM needs a line that fails the checksum and
    nothing else, or the assertion passes for the wrong reason.
    """

    def __init__(self, seed: int = 20260808):
        self.rng = random.Random(seed)

    def truncate(self, line: str) -> str:
        """§4.5 — wrong field count."""
        cut = max(12, len(line) // 2)
        return line[:cut]

    def non_numeric(self, line: str) -> str:
        """§4.5 — non-numeric value in a numeric field, checksum still
        valid, so the parser must reach the field converter to catch it."""
        from ..codec import PREFIX_TELEMETRY, xor_checksum
        parts = line.rsplit(",", 1)[0].split(",")
        if len(parts) < 8:
            return self.truncate(line)
        parts[7] = "SENSOR_ERR"          # pressure
        body = ",".join(parts)
        return body + "," + xor_checksum(body)

    def bit_flip(self, line: str) -> str:
        """§4.3 — corruption on the LoRa hop, caught by the checksum."""
        if len(line) < 20:
            return line
        i = self.rng.randrange(10, len(line) - 4)
        replacement = self.rng.choice("0123456789")
        if line[i] == replacement:
            replacement = "7" if replacement != "7" else "3"
        return line[:i] + replacement + line[i + 1:]

    def extra_field(self, line: str) -> str:
        """§4.7 — an unrecognised trailing field, which must NOT reject
        the packet. The one 'corruption' that has to be tolerated."""
        return line + ",EXPERIMENTAL_FIELD=1"
