"""Sanity checks for the Qt-free layer. Run: python3 test_phase1.py"""
import sys
sys.path.insert(0, ".")

from ground_station.codec import CsvCodec, encode_heartbeat, xor_checksum, frame_command
from ground_station.models import FlightState, VehicleID, RejectReason, StateObservation
from ground_station.parser import TelemetryDemux, LossTier
from ground_station.timeline import StateTimeline
from ground_station.derived import DerivedVelocity

codec = CsvCodec()
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {extra}")
        FAILS.append(name)


def make(pc=1, mt=1.0, state=FlightState.BOOST, alt=100.0,
         vehicle=VehicleID.ROCKET, extras=""):
    vals = dict(team_id=1234, vehicle_id=vehicle, mission_time=mt,
                packet_count=pc, state=state, altitude=alt, pressure=99000.0,
                temperature=15.5, battery_voltage=7.9,
                gnss_time="12:34:56.78", gnss_latitude=10.36,
                gnss_longitude=77.98, gnss_altitude=430.0, gnss_satellites=9,
                accel_x=0.1, accel_y=-0.2, accel_z=9.7,
                gyro_x=1.0, gyro_y=-2.0, gyro_z=0.5)
    line = codec.encode(vals)
    return line + extras


print("\n§4.1/§4.3 codec round trip")
d = TelemetryDemux()
r = d.feed(make(pc=1, mt=1.0))
check("accepted", r.accepted, r.rejected)
check("vehicle routed", r.packet.vehicle_id is VehicleID.ROCKET)
check("state decoded", r.packet.state is FlightState.BOOST)
check("altitude", abs(r.packet.altitude - 100.0) < 1e-6)

print("\n§4.3 checksum rejection")
bad = make(pc=2, mt=2.0)
bad = bad[:-2] + "FF"
r = d.feed(bad)
check("rejected", not r.accepted)
check("reason CHECKSUM", r.rejected.reason is RejectReason.CHECKSUM, r.rejected)
check("attributed to rocket", r.rejected.vehicle_id is VehicleID.ROCKET)
check("counter", d.streams[VehicleID.ROCKET].counters.checksum_failed == 1)

print("\n§4.5 malformed, never zero-filled")
r = d.feed("$T,1234,ROCKET,3.0,3,BOOST,00")
check("field count rejected", r.rejected.reason is RejectReason.FIELD_COUNT)
good = make(pc=4, mt=4.0, state=FlightState.BOOST)
impact = good.rsplit(",", 1)[0].replace(",BOOST,", ",IMPACT,")
impact = impact + "," + xor_checksum(impact)      # valid checksum, bad state
r = d.feed(impact)
check("IMPACT rejected as unknown state",
      r.rejected.reason is RejectReason.UNKNOWN_STATE, r.rejected)
check("IMPACT detail names the firmware mismatch",
      "IMPACT" in r.rejected.detail and "firmware" in r.rejected.detail,
      r.rejected.detail)

parts = good.rsplit(",", 1)[0].split(",")
parts[7] = "abc"                                  # field 7 = PRESSURE
nonnum = ",".join(parts)
nonnum = nonnum + "," + xor_checksum(nonnum)
r = d.feed(nonnum)
check("non-numeric rejected with field name",
      r.rejected.reason is RejectReason.NON_NUMERIC
      and "pressure" in r.rejected.detail, r.rejected.detail)

print("\n§4.6 duplicate + regression, counted separately")
d2 = TelemetryDemux()
d2.feed(make(pc=10, mt=10.0))
r = d2.feed(make(pc=10, mt=10.1))
check("duplicate rejected", r.rejected.reason is RejectReason.DUPLICATE)
r = d2.feed(make(pc=11, mt=9.0))
check("regression rejected", r.rejected.reason is RejectReason.TIME_REGRESSION)
c = d2.streams[VehicleID.ROCKET].counters
check("dupes counted apart from malformed",
      c.duplicates == 1 and c.time_regressions == 1 and c.malformed == 0)

print("\n§4.7 trailing fields accepted, unknown reported once")
d3 = TelemetryDemux()
r = d3.feed(make(pc=1, mt=1.0, extras=",RSSI=-97.5,SNR=8.2"))
check("packet still accepted", r.accepted)
check("rssi parsed", r.packet.rssi == -97.5)
check("snr parsed", r.packet.snr == 8.2)
d3.feed(make(pc=2, mt=2.0, extras=",RSSI=-97,SNR=8,FOO=1"))
d3.feed(make(pc=3, mt=3.0, extras=",RSSI=-97,SNR=8,FOO=2"))
n = d3.drain_notices()
check("unknown extra reported once per session", len(n) == 1, n)
check("positional extras tolerated",
      d3.feed(make(pc=4, mt=4.0, extras=",-91.0,7.5")).packet.rssi == -91.0)

print("\n§3.2 routing by VEHICLE_ID only")
d4 = TelemetryDemux()
d4.feed(make(pc=1, mt=1.0, vehicle=VehicleID.ROCKET))
d4.feed(make(pc=1, mt=1.0, vehicle=VehicleID.CANSAT))
d4.feed(make(pc=2, mt=1.1, vehicle=VehicleID.CANSAT))
check("rocket stream independent",
      d4.streams[VehicleID.ROCKET].counters.accepted == 1)
check("cansat stream independent",
      d4.streams[VehicleID.CANSAT].counters.accepted == 2)

print("\n§7.5.2 loss tiers")
d5 = TelemetryDemux()
d5.feed(make(pc=1, mt=1.0))
check("contiguous -> NONE", d5.feed(make(pc=2, mt=1.1)).tier is LossTier.NONE)
check("3 missing -> DROPOUT", d5.feed(make(pc=6, mt=1.5)).tier is LossTier.DROPOUT)
check("10 missing -> GAP", d5.feed(make(pc=17, mt=2.6)).tier is LossTier.GAP)
check("40 missing -> OUTAGE", d5.feed(make(pc=58, mt=6.7)).tier is LossTier.OUTAGE)
c = d5.streams[VehicleID.ROCKET].counters
check("tier counters", (c.dropouts, c.gaps, c.outages) == (1, 1, 1),
      (c.dropouts, c.gaps, c.outages))

print("\n§3.7 packet success")
pct = d5.streams[VehicleID.ROCKET].packet_success
check("success < 100 after loss", pct is not None and pct < 20, pct)

print("\n§3.12 other line types ignored, not malformed")
d6 = TelemetryDemux()
d6.feed(encode_heartbeat(12.5, 100, 98))
check("heartbeat decoded", d6.last_heartbeat is not None)
check("heartbeat counts", d6.last_heartbeat.counts[VehicleID.ROCKET] == 100)
seen = []
d6.on_file_frame = seen.append
d6.feed("$F,CHUNK,1,abcd")
check("file frame routed", len(seen) == 1)
d6.feed("some garbage from another device")
check("unknown prefix ignored", d6.ignored_lines == 1)
check("no malformed counted",
      d6.streams[VehicleID.ROCKET].counters.malformed == 0)

print("\n§5.2 backward transition flagged, not corrected")
tl = StateTimeline()
tl.observe(FlightState.BOOST, 5.0)
e = tl.observe(FlightState.PRE_LAUNCH, 6.0)
check("backward flagged", e[0].backward)
check("state not corrected", tl.current is FlightState.PRE_LAUNCH)

print("\n§5.6 inferred APOGEE")
tl2 = StateTimeline()
tl2.observe(FlightState.COAST, 10.0)
e = tl2.observe(FlightState.DESCENT, 12.0)
check("two entries appended", len(e) == 2, e)
check("apogee inferred", e[0].state is FlightState.APOGEE
      and e[0].observation is StateObservation.INFERRED)
check("descent observed", e[1].observation is StateObservation.OBSERVED)
check("anomalies surfaced", len(tl2.anomalies) == 1)

print("\n§5.7/§10.3 restored from flash + continuity")
tl3 = StateTimeline()
tl3.observe(FlightState.COAST, 20.0)
tl3.arm_refresh(0.1)
e = tl3.observe(FlightState.COAST, 20.4)
check("marked restored", e[0].observation is StateObservation.RESTORED)
check("uncertainty carried", e[0].time_uncertainty == 0.1)
check("continuity ok", not tl3.continuity_broken)
tl4 = StateTimeline()
tl4.observe(FlightState.DESCENT, 30.0)
tl4.arm_refresh(0.1)
tl4.observe(FlightState.BOOST, 5.0)
check("continuity break latched", tl4.continuity_broken)

print("\n§6.4 derived velocity")
dv = DerivedVelocity(window_s=0.5, min_samples=5)
out = [dv.add(i * 0.1, 100.0 - 5.0 * (i * 0.1)) for i in range(10)]
check("None while filling", out[0] is None and out[3] is None)
check("slope ~ -5 m/s", abs(out[-1] + 5.0) < 1e-6, out[-1])
dv.on_gap()
check("window cleared on gap", not dv.ready)

print("\n§10.7 command framing")
cmd = frame_command(VehicleID.CANSAT, "ACT_SEPARATE_MOVE 90", 7)
check("checksum valid",
      cmd.rsplit(",", 1)[1] == xor_checksum(cmd.rsplit(",", 1)[0]))
check("targets vehicle not channel", ",CANSAT," in cmd)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("all checks passed")
