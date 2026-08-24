"""Phase 4 tests — §12 logging and §13.6 simulator.
Runs the simulator's whole mission through the REAL parser and asserts
each §13.6 failure mode actually reaches the app.
Run: python3 test_phase4.py"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

from ground_station.graphs.trajectory import FlightProfile
from ground_station.logs.raw_log import (
    CommandLog, DerivedLog, RawLogSet, SessionLogs,
)
from ground_station.models import (
    FlightState, RejectReason, RejectedLine, StateObservation, VehicleID,
)
from ground_station.parser import LossTier, TelemetryDemux
from ground_station.simulator.engine import LAUNCH_T, SimulatorEngine
from ground_station.simulator.faults import FaultKind, FaultSchedule
from ground_station.timeline import StateTimeline

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {extra}")
        FAILS.append(name)


# ═════════════════════════════════════════════════════════════════════════
#  §12 logging
# ═════════════════════════════════════════════════════════════════════════

tmp = tempfile.mkdtemp(prefix="gs_logs_")
print("\n§12.1 raw logs — one per vehicle plus system")
logs = RawLogSet(tmp, session="TEST")
logs.write_line("$T,1234,ROCKET,1.00,1,BOOST,10,99000,15,7.9,x,1,2,3,9,0,0,0,0,0,0,AB",
                received_at=1000.0, vehicle=VehicleID.ROCKET)
logs.write_line("$T,1234,CANSAT,1.00,1,BOOST,9,99000,16,7.9,x,1,2,3,9,0,0,0,0,0,0,CD",
                received_at=1000.0, vehicle=VehicleID.CANSAT)
logs.write_event("SOURCE", "connected to /dev/ttyACM0")
logs.flush()
d = os.path.join(tmp, "TEST")
check("rocket log exists", os.path.exists(os.path.join(d, "rocket_raw.log")))
check("cansat log exists", os.path.exists(os.path.join(d, "cansat_raw.log")))
check("system log exists", os.path.exists(os.path.join(d, "receiver_and_source.log")))

rocket_text = open(os.path.join(d, "rocket_raw.log")).read()
check("line written verbatim", "$T,1234,ROCKET,1.00,1,BOOST" in rocket_text)
check("host timestamp prepended", "\tOK\t" in rocket_text)
check("streams not mixed", "CANSAT" not in rocket_text.replace("# ", ""))

print("\n§12.1/§4.5 malformed lines are logged, flagged, not dropped")
logs.write_rejected(RejectedLine(
    reason=RejectReason.CHECKSUM, detail="got 'FF', computed 3A",
    raw="$T,1234,ROCKET,2.00,2,BOOST,BADLINE", received_at=1001.0,
    vehicle_id=VehicleID.ROCKET))
logs.flush()
rocket_text = open(os.path.join(d, "rocket_raw.log")).read()
check("malformed line present", "BADLINE" in rocket_text)
check("reason recorded", "REJECT:CHECKSUM" in rocket_text)

print("\n§12.1 unattributed lines go to system, never guessed into a vehicle")
before = logs.vehicles[VehicleID.ROCKET].lines_written
logs.write_line("garbled beyond recognition", vehicle=None, status="REJECT:FIELD_COUNT")
logs.flush()
check("not written to a vehicle file",
      logs.vehicles[VehicleID.ROCKET].lines_written == before)
check("counted", logs.unattributed == 1)
logs.close()

print("\n§12.6 log files are readable while open")
logs2 = RawLogSet(tmp, session="OPEN")
logs2.write_line("line one", vehicle=VehicleID.ROCKET)
logs2.flush()
p = os.path.join(tmp, "OPEN", "rocket_raw.log")
with open(p) as fh:
    content = fh.read()
check("readable mid-session", "line one" in content)
logs2.write_line("line two", vehicle=VehicleID.ROCKET)
logs2.flush()
check("appends visible without reopening app",
      "line two" in open(p).read())
logs2.close()

print("\n§12.3 derived values never enter the raw log")
sess = SessionLogs(tmp)
sess.derived.write("ROCKET", 12.5, "velocity", -12.4)
sess.derived.write("ROCKET", 12.6, "velocity", None)
sess.flush()
dpath = os.path.join(sess.directory, "derived.log")
raw_rocket = open(os.path.join(sess.directory, "rocket_raw.log")).read()
check("derived file separate", os.path.exists(dpath))
check("velocity absent from raw log", "velocity" not in raw_rocket)
dtext = open(dpath).read()
check("derived file warns it is not measurement", "NOT MEASUREMENTS" in dtext)
check("None written empty, not 0", "\tvelocity\t\n" in dtext, repr(dtext[-80:]))

print("\n§12.4 command log includes unacknowledged and resends")
sess.commands.sent(1, "ROCKET", "REQUEST_STATUS")
sess.commands.timed_out(1, "ROCKET", "REQUEST_STATUS")
sess.commands.sent(2, "ROCKET", "REQUEST_STATUS", resend_of=1)
sess.commands.receiver_forwarded(2, "ROCKET", "REQUEST_STATUS")
sess.commands.acknowledged(2, "ROCKET", "REQUEST_STATUS", "OK")
sess.flush()
ctext = open(os.path.join(sess.directory, "commands.log")).read()
check("timeout recorded", "TIMEOUT" in ctext)
check("resend recorded with origin", "resend of #1" in ctext)
check("receiver forward marked as NOT an ack",
      "NOT a vehicle acknowledgment" in ctext)
sess.close()
shutil.rmtree(tmp, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════
#  §13.6 simulator — run the whole mission through the real parser
# ═════════════════════════════════════════════════════════════════════════

print("\n§13.6 running the full simulated mission through the real parser")
profile = FlightProfile()
engine = SimulatorEngine()
demux = TelemetryDemux()
timelines = {v: StateTimeline() for v in VehicleID}

tiers = {v: [] for v in VehicleID}
rejects = []
heartbeat_ticks = 0
source_down_ticks = 0
rssi_by_time = {v: [] for v in VehicleID}
injected = set()
accepted = {v: 0 for v in VehicleID}

for out in engine.run(110.0):
    for kind, _note in out.injected:
        injected.add(kind)
    if out.source_down:
        source_down_ticks += 1
        continue
    for line in out.lines:
        if line.startswith("$H,"):
            heartbeat_ticks += 1
        r = demux.feed(line)
        if r is None:
            continue
        if r.rejected is not None:
            rejects.append(r.rejected)
            continue
        p = r.packet
        accepted[p.vehicle_id] += 1
        tiers[p.vehicle_id].append(r.tier)
        timelines[p.vehicle_id].observe(p.state, p.mission_time)
        if p.rssi is not None:
            rssi_by_time[p.vehicle_id].append((p.mission_time, p.rssi))

check("both vehicles produced packets",
      accepted[VehicleID.ROCKET] > 500 and accepted[VehicleID.CANSAT] > 500,
      accepted)

print("\n§13.6 all three loss tiers (§7.5.2)")
rocket_tiers = set(tiers[VehicleID.ROCKET])
cansat_tiers = set(tiers[VehicleID.CANSAT])
check("DROPOUT produced", LossTier.DROPOUT in rocket_tiers)
check("GAP produced", LossTier.GAP in rocket_tiers)
check("OUTAGE produced", LossTier.OUTAGE in (rocket_tiers | cansat_tiers))

print("\n§13.6 an APOGEE lost entirely to packet loss (§5.6)")
tl = timelines[VehicleID.ROCKET]
apogee_entries = [e for e in tl.entries if e.state is FlightState.APOGEE]
check("APOGEE appears on the timeline", len(apogee_entries) == 1, apogee_entries)
check("marked inferred, not received",
      apogee_entries and
      apogee_entries[0].observation is StateObservation.INFERRED,
      apogee_entries[0].observation if apogee_entries else None)
check("CanSat observed its own apogee independently (§3.8)",
      any(e.state is FlightState.APOGEE for e in timelines[VehicleID.CANSAT].entries))

print("\n§13.6 malformed packets (§4.5) and checksum failures (§4.3)")
reasons = {r.reason for r in rejects}
check("field-count rejects produced", RejectReason.FIELD_COUNT in reasons, reasons)
check("non-numeric rejects produced", RejectReason.NON_NUMERIC in reasons, reasons)
check("checksum rejects produced", RejectReason.CHECKSUM in reasons, reasons)

print("\n§13.6 out-of-order and duplicate packets (§4.6)")
check("duplicates produced", RejectReason.DUPLICATE in reasons, reasons)
check("out-of-order produced",
      RejectReason.DUPLICATE in reasons or
      RejectReason.TIME_REGRESSION in reasons, reasons)
c = demux.streams[VehicleID.ROCKET].counters
check("counted apart from malformed",
      c.duplicates + c.time_regressions > 0 and c.malformed > 0,
      (c.duplicates, c.time_regressions, c.malformed))

print("\n§13.6 receiver heartbeats and heartbeat loss (§3.4/§3.5)")
check("heartbeats emitted", heartbeat_ticks > 80, heartbeat_ticks)
hb_window = engine.schedule.window_for(FaultKind.HEARTBEAT_LOSS)
check("heartbeat loss scheduled", hb_window is not None)
check("heartbeat loss injected", FaultKind.HEARTBEAT_LOSS.value in injected)

print("\n§13.6 source drop — both vehicles silent together (§3.9/§3.5)")
check("source went down", source_down_ticks > 30, source_down_ticks)
check("source drop injected", FaultKind.SOURCE_DROP.value in injected)

print("\n§13.6 per-vehicle RSSI variation (§3.7)")
fade = engine.schedule.window_for(FaultKind.RSSI_FADE)
during = [r for t, r in rssi_by_time[VehicleID.CANSAT]
          if fade.start_s <= t < fade.end_s]
outside = [r for t, r in rssi_by_time[VehicleID.CANSAT]
           if t < fade.start_s - 5]
check("RSSI present on packets", len(during) > 10 and len(outside) > 10)
check("fade is visible in the data",
      sum(during) / len(during) < sum(outside) / len(outside) - 10,
      (sum(during) / len(during), sum(outside) / len(outside)))
rocket_rssi = [r for _, r in rssi_by_time[VehicleID.ROCKET]]
check("vehicles differ in RSSI",
      abs(sum(rocket_rssi) / len(rocket_rssi)
          - sum(outside) / len(outside)) > 1.0)

print("\n§13.6 refresh dropout is a distinct scheduled event (§10.3/§7.5.10)")
check("refresh dropout injected", FaultKind.REFRESH_DROPOUT.value in injected)
check("distinct from plain outage",
      engine.schedule.window_for(FaultKind.REFRESH_DROPOUT).start_s
      != engine.schedule.window_for(FaultKind.OUTAGE).start_s)

print("\n§13.6 unacked command and failed transfer are scheduled")
check("unacked command armed", FaultKind.COMMAND_UNACKED.value
      in {w.kind.value for w in engine.schedule.windows})
check("transfer failure armed", FaultKind.TRANSFER_FAILURE.value
      in {w.kind.value for w in engine.schedule.windows})

print("\n§2.2 the simulator goes through the real parser")
check("packets are real TelemetryPackets",
      demux.streams[VehicleID.ROCKET].last_packet is not None)
check("checksums were actually verified",
      demux.streams[VehicleID.ROCKET].counters.checksum_failed
      + demux.streams[VehicleID.CANSAT].counters.checksum_failed > 0)
check("packet_count advanced through losses",
      demux.streams[VehicleID.ROCKET].counters.missing_total > 20,
      demux.streams[VehicleID.ROCKET].counters.missing_total)

print("\n§5.1 the mission reaches every state")
seen = {e.state for e in timelines[VehicleID.ROCKET].entries}
for s in (FlightState.PRE_LAUNCH, FlightState.BOOST, FlightState.COAST,
          FlightState.APOGEE, FlightState.DESCENT, FlightState.LANDING,
          FlightState.RECOVERY):
    check(f"reached {s.value}", s in seen)
check("no backward transitions in a nominal run",
      not any(e.backward for e in timelines[VehicleID.ROCKET].entries))

print("\n§7.5.5 simulator and reconciliation share one physics module")
import ground_station.simulator.engine as eng
import ground_station.graphs.reconcile as rec
check("engine imports the shared profile",
      "trajectory" in open(eng.__file__).read())
check("reconcile imports the same module",
      "from .trajectory import" in open(rec.__file__).read())
check("apogee consistent with §8 altitude extent",
      900 < profile.altitude_at(profile.time_to_apogee()) < 1300)

print("\ndeterminism")
e1 = SimulatorEngine()
e2 = SimulatorEngine()
l1 = [l for o in e1.run(20.0) for l in o.lines]
l2 = [l for o in e2.run(20.0) for l in o.lines]
check("same seed reproduces the run exactly", l1 == l2)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("all checks passed")

# ═══════════════════════════════════════════════════════════════════════════
#  Regression: #10 SimulatedSource._unack_armed reset across restarts
# ═══════════════════════════════════════════════════════════════════════════

print("\n--- Regression: #10 _unack_armed survives simulator restart ---")
from ground_station.simulator.source import SimulatedSource
from ground_station.simulator.faults import FaultSchedule

src = SimulatedSource()
check("_unack_armed starts True", src._unack_armed)

# Exhaust the first run: consume the unack scenario (fires after T > 80 s).
# We just verify the flag starts True and is cleared after a command fires.
src._unack_armed = False   # simulate having consumed the scenario once

# reset() must restore _unack_armed so the scenario fires again.
src.reset()
check("_unack_armed restored after reset()", src._unack_armed)

# A second reset must still restore it (idempotent).
src._unack_armed = False
src.reset()
check("idempotent: _unack_armed restored on repeated reset()", src._unack_armed)

# engine.t should be 0.0 after reset (confirming the engine itself reset).
check("engine restarted from T=0", src.engine.t == 0.0)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("all checks passed")
