"""
ground_station/simulator/engine.py
══════════════════════════════════
The simulator's actual behaviour, with no Qt.

Split out from source.py for the same reason reconcile.py and
trajectory.py are separate from plot.py: §13.6's requirements are
assertions about what the simulator emits, and an assertion that needs a
running event loop and a display is one nobody runs.

So this file produces lines and the Qt wrapper produces signals. Every
§13.6 bullet can be checked by calling `run()` and inspecting the output,
which is what test_phase4.py does.

Flight physics come from graphs/trajectory.py (§7.5.5), and lines are
encoded with the real CsvCodec (§2.2) — see source.py's docstring for why
both of those matter more than they look.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from ..codec import CsvCodec, encode_heartbeat
from ..graphs.trajectory import FlightProfile
from ..models import FlightState, VehicleID
from .faults import FaultKind, FaultSchedule, LineCorrupter

SIM_DT = 0.1
TEAM_ID = 1234
LAUNCH_T = 5.0


class SimulatedFlight:
    """Sensor values for one vehicle at a given mission time."""

    def __init__(self, vehicle: VehicleID, profile: FlightProfile,
                 seed: int = 0):
        self.vehicle = vehicle
        self.profile = profile
        self.rng = random.Random(seed)
        self.packet_count = 0

        # The CanSat separates near apogee and descends under its own
        # canopy: it trails the rocket slightly and falls more slowly.
        self.t_offset = 0.0 if vehicle is VehicleID.ROCKET else -0.4
        self.descent_scale = 1.0 if vehicle is VehicleID.ROCKET else 0.72

        self.lat = 10.3624
        self.lon = 77.9803
        self.pad_altitude_msl = 430.0

    # ── §5.1 state machine ───────────────────────────────────────────────

    def landing_time(self) -> float:
        p = self.profile
        peak = p.altitude_at(p.time_to_apogee())
        return p.time_to_apogee() + peak / (p.descent_rate * self.descent_scale)

    def state_at(self, t: float) -> FlightState:
        p = self.profile
        ft = t - LAUNCH_T
        if t < 2.0:
            return FlightState.BOOT
        if ft < 0:
            return FlightState.PRE_LAUNCH
        if ft < p.burn_time:
            return FlightState.BOOST
        t_apogee = p.time_to_apogee()
        if ft < t_apogee - 0.6:
            return FlightState.COAST
        if ft < t_apogee + 0.6:
            # ~1.2 s. Deliberately short: §5.6 exists because "a
            # short-lived state such as APOGEE may be missed entirely if
            # every packet carrying it is lost", and a generous window
            # would make that scenario impossible to demonstrate.
            return FlightState.APOGEE
        if ft < self.landing_time():
            return FlightState.DESCENT
        if ft < self.landing_time() + 6.0:
            return FlightState.LANDING
        return FlightState.RECOVERY

    def altitude_at(self, t: float) -> float:
        p = self.profile
        ft = t - LAUNCH_T + self.t_offset
        if ft <= 0:
            return 0.0
        t_apogee = p.time_to_apogee()
        if ft <= t_apogee:
            return p.altitude_at(ft)
        peak = p.altitude_at(t_apogee)
        return max(0.0, peak - p.descent_rate * self.descent_scale
                   * (ft - t_apogee))

    def velocity_at(self, t: float) -> float:
        ft = t - LAUNCH_T + self.t_offset
        if ft <= 0:
            return 0.0
        if ft <= self.profile.time_to_apogee():
            return self.profile.velocity_at(ft)
        return (0.0 if self.altitude_at(t) <= 0.0
                else -self.profile.descent_rate * self.descent_scale)

    def rssi(self, t: float, penalty: float = 0.0) -> float:
        base = -58.0 - self.altitude_at(t) * 0.021
        if self.vehicle is VehicleID.CANSAT:
            base -= 4.0
        return base - penalty + self.rng.gauss(0, 1.6)

    # ── §4.1 field set ───────────────────────────────────────────────────

    def values(self, t: float) -> Dict[str, object]:
        alt = self.altitude_at(t)
        state = self.state_at(t)
        n = self.rng.gauss

        # Real barometric noise, on purpose. §6.4's rolling-window
        # derivative exists precisely because sample-to-sample
        # differencing of a noisy barometer is dominated by the noise; a
        # noiseless simulator would make the naive derivative look fine
        # and the requirement look like overkill.
        alt_measured = max(0.0, alt + n(0, 0.12))
        pressure = 101325.0 * (1 - 2.25577e-5 * alt) ** 5.25588 + n(0, 8)
        temperature = 22.0 - 0.0065 * alt + n(0, 0.15)
        battery = 8.32 - 0.0022 * t - (0.18 if state is FlightState.BOOST else 0)

        if state is FlightState.BOOST:
            ax, ay, az = n(0, 0.4), n(0, 0.4), -7.3 + n(0, 0.6)
        elif state in (FlightState.COAST, FlightState.APOGEE):
            ax, ay, az = n(0, 0.15), n(0, 0.15), -1.0 + n(0, 0.1)
        elif state is FlightState.DESCENT:
            ax, ay, az = n(0, 0.5), n(0, 0.5), -1.0 + n(0, 0.35)
        else:
            ax, ay, az = n(0, 0.02), n(0, 0.02), -1.0 + n(0, 0.02)

        if state is FlightState.DESCENT and self.vehicle is VehicleID.CANSAT:
            gx, gy, gz = n(0, 8), n(0, 8), 42.0 + n(0, 5)     # spin under canopy
        elif state is FlightState.BOOST:
            gx, gy, gz = n(0, 25), n(0, 25), 180.0 + n(0, 30)
        else:
            gx, gy, gz = n(0, 4), n(0, 4), n(0, 6)

        self.lat += n(0, 8e-6)
        self.lon += n(0, 8e-6)
        self.packet_count += 1

        mm, ss = int((t % 3600) // 60), t % 60
        return {
            "team_id": TEAM_ID, "vehicle_id": self.vehicle,
            "mission_time": t, "packet_count": self.packet_count,
            "state": state, "altitude": alt_measured, "pressure": pressure,
            "temperature": temperature, "battery_voltage": battery,
            "gnss_time": f"12:{mm:02d}:{ss:05.2f}",
            "gnss_latitude": self.lat, "gnss_longitude": self.lon,
            "gnss_altitude": alt + self.pad_altitude_msl,
            "gnss_satellites": max(4, int(9 + n(0, 1.5))),
            "accel_x": ax, "accel_y": ay, "accel_z": az,
            "gyro_x": gx, "gyro_y": gy, "gyro_z": gz,
        }


@dataclass
class TickOutput:
    """What one simulated tick puts on the wire."""
    t: float
    lines: List[str] = field(default_factory=list)
    #: True when the USB source is down — nothing at all arrives, not
    #: even the heartbeat (§3.9).
    source_down: bool = False
    injected: List[Tuple[str, str]] = field(default_factory=list)


class SimulatorEngine:
    """Generates the wire stream for a whole simulated mission."""

    def __init__(self, schedule: Optional[FaultSchedule] = None,
                 profile: Optional[FlightProfile] = None):
        self.profile = profile or FlightProfile()
        self.schedule = schedule or FaultSchedule.demo(
            profile_apogee_s=LAUNCH_T + self.profile.time_to_apogee())
        self.codec = CsvCodec()
        self.corrupter = LineCorrupter(self.schedule.seed)
        self.flights: Dict[VehicleID, SimulatedFlight] = {
            vid: SimulatedFlight(vid, self.profile,
                                 seed=self.schedule.seed + i)
            for i, vid in enumerate(VehicleID)
        }
        self.t = 0.0
        self.receiver_uptime = 0.0
        self._held: List[Tuple[float, str]] = []

    # ── one tick ─────────────────────────────────────────────────────────

    def tick(self) -> TickOutput:
        self.t += SIM_DT
        self.receiver_uptime += SIM_DT
        out = TickOutput(t=self.t)

        # Vehicle-agnostic windows (vehicle=None) fire for every vehicle,
        # so report each window once rather than once per vehicle — a
        # duplicated event line in the flight record is a small thing that
        # costs someone real time when they are reading the log to work
        # out what happened.
        seen: set = set()
        for vid in VehicleID:
            for w in self.schedule.firing(self.t, SIM_DT, vid):
                key = (w.kind, w.start_s, w.vehicle)
                if key in seen:
                    continue
                seen.add(key)
                out.injected.append((w.kind.value, w.note))

        if FaultKind.SOURCE_DROP in self.schedule.active_kinds(
                self.t, VehicleID.ROCKET):
            # §3.9 — the whole source is gone, so the vehicles' packets
            # are not merely lost, they never reach the app at all. The
            # packet counters still advance because the vehicles are
            # still transmitting; only the ground stopped listening.
            for f in self.flights.values():
                f.packet_count += 1
            out.source_down = True
            return out

        # Out-of-order lines rejoin here (§4.6).
        due = [l for at, l in self._held if at <= self.t]
        self._held = [(at, l) for at, l in self._held if at > self.t]
        out.lines.extend(due)

        for vid, flight in self.flights.items():
            out.lines.extend(self._lines_for(vid, flight))

        # §3.4 — heartbeat at 1 Hz, absent while the loss window is open.
        hb_lost = FaultKind.HEARTBEAT_LOSS in self.schedule.active_kinds(
            self.t, VehicleID.ROCKET)
        if not hb_lost and round(self.t * 10) % 10 == 0:
            out.lines.append(encode_heartbeat(
                self.receiver_uptime,
                self.flights[VehicleID.ROCKET].packet_count,
                self.flights[VehicleID.CANSAT].packet_count))

        return out

    def _lines_for(self, vid: VehicleID,
                   flight: SimulatedFlight) -> List[str]:
        active = self.schedule.active_kinds(self.t, vid)

        # ── loss (§7.5.2, §5.6, §7.5.10) ─────────────────────────────────
        # PACKET_COUNT still advances: the vehicle really did send these,
        # and that advance is exactly what continuity checking uses to
        # detect the loss (§3.7, §7.5.2). A simulator that froze the
        # counter would produce loss the app cannot see.
        if active & {FaultKind.DROPOUT, FaultKind.GAP, FaultKind.OUTAGE,
                     FaultKind.APOGEE_LOST, FaultKind.REFRESH_DROPOUT}:
            flight.packet_count += 1
            return []

        penalty = 22.0 if FaultKind.RSSI_FADE in active else 0.0
        base = self.codec.encode(flight.values(self.t))

        # Corruption is applied to the VEHICLE's line, before the receiver
        # appends RSSI/SNR — which is the real order of events (§3.4: the
        # receiver appends after field 21, §4.3: the checksum is the
        # vehicle's and covers only its own fields).
        #
        # Getting this backwards is not cosmetic. Corrupting a line that
        # already carried trailing extras made non_numeric() recompute the
        # checksum across the appended fields, so the packet failed as
        # CHECKSUM instead of NON_NUMERIC — the non-numeric path was never
        # once exercised while the test appeared to cover it.
        if FaultKind.MALFORMED in active:
            base = (self.corrupter.truncate(base) if round(self.t * 10) % 2
                    else self.corrupter.non_numeric(base))
        elif FaultKind.CHECKSUM in active:
            base = self.corrupter.bit_flip(base)

        line = base + (f",RSSI={flight.rssi(self.t, penalty):.1f}"
                       f",SNR={max(0.5, 9.0 - penalty * 0.2):.1f}")

        if FaultKind.DUPLICATE in active:
            return [line, line]                      # relay retransmit
        if FaultKind.OUT_OF_ORDER in active:
            # Held back two ticks, so it arrives after packets carrying
            # higher counts and must be dropped as stale (§4.6).
            self._held.append((self.t + 0.2, line))
            return []
        return [line]

    # ── whole run ────────────────────────────────────────────────────────

    def run(self, duration_s: float) -> Iterator[TickOutput]:
        while self.t < duration_s:
            yield self.tick()

    def reset(self) -> None:
        self.__init__(self.schedule, self.profile)
