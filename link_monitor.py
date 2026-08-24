#!/usr/bin/env python3
"""
link_monitor.py — headless telemetry monitor
════════════════════════════════════════════
Runs the real §4 parser with no GUI, so the wire format can be validated
against firmware long before the dashboards are rebuilt. This is the
thing to run while the receiver firmware is being written: it reports
exactly which field failed and why, which is far easier to work against
than a graph that silently shows nothing.

Deliberately does NOT import ground_station.link — that module needs Qt.
Everything here runs on the Qt-free layer, which is the point of keeping
that boundary.

USAGE

    # no hardware — synthetic stream through the real codec and parser
    python3 link_monitor.py --demo

    # same, but with the failure modes §13.6 will need to inject
    python3 link_monitor.py --demo --faults

    # list attached serial devices with their stable identifiers (§3.3)
    python3 link_monitor.py --list

    # live, against the ground receiver
    python3 link_monitor.py --port /dev/ttyACM0
    python3 link_monitor.py --port COM5 --baud 115200

    # print every accepted packet rather than a periodic summary
    python3 link_monitor.py --port /dev/ttyACM0 --verbose

Requires pyserial only for --list and --port. --demo needs nothing.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import time
from typing import Optional

from ground_station.codec import CsvCodec, encode_heartbeat, xor_checksum
from ground_station.derived import DerivedVelocity
from ground_station.models import (
    FlightState, RejectReason, VehicleID, VehicleLinkState,
)
from ground_station.parser import LossTier, TelemetryDemux
from ground_station.timeline import StateTimeline

try:
    import serial
    from serial.tools import list_ports
    HAVE_SERIAL = True
except ImportError:
    HAVE_SERIAL = False


# ─────────────────────────────────────────────────────────────────────────────
#  Monitor
# ─────────────────────────────────────────────────────────────────────────────

class Monitor:
    def __init__(self, verbose: bool = False):
        self.demux = TelemetryDemux()
        self.timelines = {v: StateTimeline() for v in VehicleID}
        self.velocity = {v: DerivedVelocity() for v in VehicleID}
        self.verbose = verbose
        self.started = time.monotonic()
        self._last_summary = 0.0

    def feed(self, line: str) -> None:
        result = self.demux.feed(line)
        if result is None:
            return

        if result.rejected is not None:
            r = result.rejected
            who = r.vehicle_id.value if r.vehicle_id else "?"
            print(f"  REJECT [{who}] {r.reason.name}: {r.detail}")
            print(f"         {r.raw[:110]}")
            return

        p = result.packet
        vid = p.vehicle_id

        # §7.5.2 — the derived-velocity window must not span a gap.
        if result.tier in (LossTier.GAP, LossTier.OUTAGE):
            self.velocity[vid].on_gap()
            print(f"  {result.tier.name} [{vid.value}] "
                  f"{result.missing} packets missing")
        elif result.tier is LossTier.DROPOUT:
            pass   # §7.5.2: no annotation, counted only

        v = self.velocity[vid].add(p.mission_time, p.altitude)

        for entry in self.timelines[vid].observe(p.state, p.mission_time):
            mark = ""
            if entry.observation.name != "OBSERVED":
                mark = f"  <{entry.observation.value}>"
            if entry.backward:
                mark += "  <<BACKWARD TRANSITION — §5.2 anomaly>>"
            print(f"  STATE  [{vid.value}] {entry.state.value} "
                  f"@ T+{entry.mission_time:.2f}s{mark}")

        if self.verbose:
            vtxt = "  —  " if v is None else f"{v:+7.2f}"
            print(f"  [{vid.value:6}] #{p.packet_count:<5} T+{p.mission_time:7.2f}  "
                  f"{p.state.value:<10} alt {p.altitude:8.2f} m  "
                  f"vel {vtxt} m/s (derived)  "
                  f"{p.pressure:9.1f} Pa  {p.temperature:6.2f} °C  "
                  f"{p.battery_voltage:.2f} V  sats {p.gnss_satellites}"
                  + (f"  RSSI {p.rssi}" if p.rssi is not None else ""))

    def maybe_summary(self, every: float = 2.0) -> None:
        now = time.monotonic()
        if now - self._last_summary < every:
            return
        self._last_summary = now
        self.summary()

    def summary(self) -> None:
        age = self.demux.heartbeat_age()
        rx = "NO HEARTBEAT" if age is None or age > 3.0 else f"ALIVE ({age:.1f}s)"
        print(f"\n── T+{time.monotonic() - self.started:6.1f}s   receiver: {rx}"
              f"   ignored lines: {self.demux.ignored_lines}")
        for vid, s in self.demux.streams.items():
            c = s.counters
            pct = s.packet_success
            pct_txt = "  —  " if pct is None else f"{pct:5.1f}%"
            print(f"   {vid.value:6}  {s.link_state().value:<10} "
                  f"ok {c.accepted:<6} success {pct_txt}  "
                  f"rej {c.rejected_total:<4} "
                  f"(malformed {c.malformed} cksum {c.checksum_failed} "
                  f"dup {c.duplicates} time {c.time_regressions})  "
                  f"tiers D{c.dropouts}/G{c.gaps}/O{c.outages}")
        for n in self.demux.drain_notices():
            print(f"   NOTE: {n}")
        print()


# ─────────────────────────────────────────────────────────────────────────────
#  Synthetic stream (a stand-in until the §13.6 simulator is built)
# ─────────────────────────────────────────────────────────────────────────────

class DemoStream:
    """Minimal two-vehicle flight through the real codec.

    NOT the §13.6 simulator — that is a Phase 4 deliverable and has to
    cover far more. This exists so the parser can be exercised today, and
    so `--faults` can demonstrate that the rejection paths actually fire.
    """

    def __init__(self, faults: bool = False):
        self.faults = faults
        self.codec = CsvCodec()
        self.t = 0.0
        self.count = {VehicleID.ROCKET: 0, VehicleID.CANSAT: 0}
        self.uptime = 0.0

    def _state(self, t: float) -> FlightState:
        if t < 3:    return FlightState.PRE_LAUNCH
        if t < 10:   return FlightState.BOOST
        if t < 27:   return FlightState.COAST
        if t < 29:   return FlightState.APOGEE
        if t < 70:   return FlightState.DESCENT
        if t < 75:   return FlightState.LANDING
        return FlightState.RECOVERY

    def _altitude(self, t: float) -> float:
        if t < 3:    return 0.0
        if t < 10:   return 180.0 * (t - 3) ** 1.6 / 7 ** 0.6
        if t < 29:   return 1000.0 - 4.9 * (t - 24.6) ** 2 + 500
        if t < 70:   return max(0.0, 1043.0 - 25.0 * (t - 29))
        return 0.0

    def lines(self, dt: float = 0.1):
        """Yield the lines that would arrive in one dt of wall time."""
        self.t += dt
        self.uptime += dt
        out = []

        for vid in VehicleID:
            self.count[vid] += 1
            t = self.t if vid is VehicleID.ROCKET else max(0.0, self.t - 0.4)
            alt = self._altitude(t)
            if vid is VehicleID.CANSAT:
                alt *= 0.97

            line = self.codec.encode(dict(
                team_id=1234, vehicle_id=vid, mission_time=self.t,
                packet_count=self.count[vid], state=self._state(t),
                altitude=alt + random.gauss(0, 0.1),
                pressure=101325.0 * (1 - 2.25577e-5 * max(alt, 0)) ** 5.25588,
                temperature=22.0 - 0.0065 * alt + random.gauss(0, 0.2),
                battery_voltage=8.4 - 0.004 * self.t,
                gnss_time=f"12:{int(self.t // 60):02d}:{self.t % 60:05.2f}",
                gnss_latitude=10.3624 + random.gauss(0, 2e-5),
                gnss_longitude=77.9803 + random.gauss(0, 2e-5),
                gnss_altitude=alt + 430.0, gnss_satellites=random.randint(6, 12),
                accel_x=random.gauss(0, 0.2), accel_y=random.gauss(0, 0.2),
                accel_z=-9.71 + random.gauss(0, 0.3),
                gyro_x=random.gauss(0, 5), gyro_y=random.gauss(0, 5),
                gyro_z=random.gauss(0, 5),
            ))
            line += f",RSSI={-60 - alt * 0.03:.1f},SNR={random.uniform(6, 11):.1f}"

            if self.faults:
                out.extend(self._inject(line))
            else:
                out.append(line)

        if int(self.uptime * 10) % 10 == 0:
            out.append(encode_heartbeat(self.uptime,
                                        self.count[VehicleID.ROCKET],
                                        self.count[VehicleID.CANSAT]))
        return out

    def _inject(self, line: str) -> list:
        """Return the lines actually put on the wire for one packet.

        A list rather than an Optional[str], because the failure modes are
        not all one-in-one-out: loss yields zero lines and a relay
        retransmit yields two. An earlier cut returned a single string and
        so could not express the duplicate case at all — it returned the
        line unchanged, which is indistinguishable from no fault, and the
        §4.6 path was never once exercised.
        """
        roll = random.random()
        if roll < 0.010:                      # burst loss -> DROPOUT/GAP/OUTAGE
            return []
        if roll < 0.014:                      # bit flip caught by checksum
            i = random.randrange(10, len(line) - 4)
            return [line[:i] + random.choice("0123456789") + line[i + 1:]]
        if roll < 0.017:                      # truncated frame (§4.5)
            return [line[:len(line) // 2]]
        if roll < 0.022:                      # relay retransmit (§4.6)
            return [line, line]
        return [line]


# ─────────────────────────────────────────────────────────────────────────────
#  Entry points
# ─────────────────────────────────────────────────────────────────────────────

def cmd_list() -> int:
    if not HAVE_SERIAL:
        print("pyserial not installed:  pip install pyserial")
        return 1
    ports = list(list_ports.comports())
    if not ports:
        print("No serial devices found.")
        return 0
    print(f"{'DEVICE':<20} {'STABLE ID (§3.3)':<28} DESCRIPTION")
    for p in ports:
        sn = getattr(p, "serial_number", None)
        vid, pid = getattr(p, "vid", None), getattr(p, "pid", None)
        stable = (f"SNR:{sn}" if sn else
                  f"USB:{vid:04X}:{pid:04X}" if vid and pid else
                  f"DEV:{p.device}")
        print(f"{p.device:<20} {stable:<28} {p.description}")
    return 0


def cmd_demo(faults: bool, verbose: bool, duration: float) -> int:
    print(f"Synthetic stream through the real codec and parser"
          f"{' — with injected faults' if faults else ''}.")
    print("Every line below went out as CSV and came back through "
          "TelemetryDemux.feed().\n")
    mon = Monitor(verbose=verbose)
    stream = DemoStream(faults=faults)
    end = time.monotonic() + duration
    try:
        while time.monotonic() < end:
            for line in stream.lines(0.1):
                mon.feed(line)
            mon.maybe_summary()
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\ninterrupted")
    mon.summary()
    return 0


def cmd_live(port: str, baud: int, verbose: bool) -> int:
    if not HAVE_SERIAL:
        print("pyserial not installed:  pip install pyserial")
        return 1
    try:
        ser = serial.Serial(port, baud, timeout=0.2)
    except Exception as e:
        print(f"Could not open {port}: {e}")
        return 1

    print(f"Reading {port} at {baud}. Ctrl-C to stop.\n")
    # §3.12 — discard whatever fragment was mid-flight at connect time.
    try:
        ser.reset_input_buffer()
    except Exception:
        pass

    mon = Monitor(verbose=verbose)
    try:
        while True:
            raw = ser.readline()
            if raw:
                text = raw.decode("utf-8", errors="ignore").strip()
                if text:
                    mon.feed(text)
            mon.maybe_summary()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        ser.close()
    mon.summary()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list serial devices")
    ap.add_argument("--demo", action="store_true", help="synthetic stream, no hardware")
    ap.add_argument("--faults", action="store_true", help="inject loss/corruption in --demo")
    ap.add_argument("--port", help="serial device, e.g. /dev/ttyACM0 or COM5")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--verbose", "-v", action="store_true", help="print every packet")
    ap.add_argument("--seconds", type=float, default=80.0, help="--demo duration")
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    if args.demo:
        return cmd_demo(args.faults, args.verbose, args.seconds)
    if args.port:
        return cmd_live(args.port, args.baud, args.verbose)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
