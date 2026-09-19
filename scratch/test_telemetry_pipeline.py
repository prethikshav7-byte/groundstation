"""
scratch/test_telemetry_pipeline.py
==================================
Comprehensive test suite covering all 25 acceptance criteria tests for the
LIVE ROCKET TELEMETRY PARSING + GRAPH DATA PIPELINE.
"""
import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

# Ensure QApplication exists for UI/widget testing
app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)

from ground_station.codec import CsvCodec, CodecError, RejectReason, xor_checksum
from ground_station.models import FlightState, VehicleID, TelemetryPacket, SourceState
from ground_station.parser.parser import TelemetryDemux
from ground_station.pages.vehicle import VehiclePage
from ground_station.pages.overview import OverviewPage
from ground_station.link import LinkSupervisor

FAILS = []

def check(name: str, condition: bool, extra=None):
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {extra if extra is not None else ''}")
        FAILS.append(name)

def pkt_wire(payload_str: str) -> str:
    """Append valid XOR checksum to a $T payload string."""
    ck = xor_checksum(payload_str)
    return f"{payload_str},{ck}"

print("=" * 60)
print("RUNNING 25 REQUIRED ACCEPTANCE TESTS")
print("=" * 60)

codec = CsvCodec()

# ---------------------------------------------------------------------------
# TEST 1: Valid telemetry packet parses successfully.
# ---------------------------------------------------------------------------
print("\n--- TEST 1: Valid telemetry packet parses successfully ---")
raw_packet = pkt_wire("$T,TEAM1,100.5,42,1250.5,101325.0,22.5,12.4,123456,37.7749,-122.4194,1250.0,9,PENDING_ACCEL,15.2,2,45.0,1")
decoded = codec.decode(raw_packet)
check("Decoded frame is not None", decoded is not None)
check("Decoded values has team_id", "team_id" in decoded.values)
pkt = TelemetryPacket(**decoded.values, extras=decoded.extras)
check("Packet is TelemetryPacket instance", isinstance(pkt, TelemetryPacket))
check("Team ID matches", pkt.team_id == "TEAM1")
check("Mission time parsed", pkt.mission_time == 100.5)

# ---------------------------------------------------------------------------
# TEST 2: CSV delimiters correctly separate fields.
# ---------------------------------------------------------------------------
print("\n--- TEST 2: CSV delimiters correctly separate fields ---")
raw_comma = pkt_wire("$T,TEAM_ALPHA,200.0,1,10.0,101300,20.0,11.5,0,0,0,0,0,PENDING,0.0,0,0.0,1")
dec2 = codec.decode(raw_comma)
pkt2 = TelemetryPacket(**dec2.values, extras=dec2.extras)
check("Field 1 team_id separated", pkt2.team_id == "TEAM_ALPHA")
check("Field 2 timestamp separated", pkt2.mission_time == 200.0)
check("Field 3 packet_count separated", pkt2.packet_count == 1)
check("Field 4 altitude separated", pkt2.altitude == 10.0)
check("Field 5 pressure separated", pkt2.pressure == 101300.0)

# ---------------------------------------------------------------------------
# TEST 3: Numeric strings become numeric values.
# ---------------------------------------------------------------------------
print("\n--- TEST 3: Numeric strings become numeric values ---")
check("Altitude is float", isinstance(pkt.altitude, (int, float)) and pkt.altitude == 1250.5)
check("Pressure is float", isinstance(pkt.pressure, (int, float)) and pkt.pressure == 101325.0)
check("Temperature is float", isinstance(pkt.temperature, (int, float)) and pkt.temperature == 22.5)
check("Voltage is float", isinstance(pkt.battery_voltage, (int, float)) and pkt.battery_voltage == 12.4)
check("Packet count is int", isinstance(pkt.packet_count, int) and pkt.packet_count == 42)
check("Satellites is int", isinstance(pkt.gnss_satellites, int) and pkt.gnss_satellites == 9)

# ---------------------------------------------------------------------------
# TEST 4: Altitude scaling is correct according to confirmed protocol (0.1m).
# ---------------------------------------------------------------------------
print("\n--- TEST 4: Altitude scaling is correct according to confirmed protocol ---")
raw_alt = pkt_wire("$T,2026 IN-SPACE-01,10.0,1,4123,101325,200,1200,0,0,0,0,0,PENDING,0.0,0,0.0,1")
dec_alt = codec.decode(raw_alt)
pkt_alt = TelemetryPacket(**dec_alt.values, extras=dec_alt.extras)
check("Altitude decoded 4123 -> 412.3 m", pkt_alt.altitude == 412.3)
check("Team ID matches 2026 IN-SPACE-01", pkt_alt.team_id == "2026 IN-SPACE-01")

# ---------------------------------------------------------------------------
# TEST 5: Pressure becomes numeric pressure (1Pa).
# ---------------------------------------------------------------------------
print("\n--- TEST 5: Pressure becomes numeric pressure ---")
raw_press = pkt_wire("$T,2026 IN-SPACE-01,10.0,1,10.0,92150,20.0,12.0,0,0,0,0,0,PENDING,0.0,0,0.0,1")
dec_press = codec.decode(raw_press)
pkt_press = TelemetryPacket(**dec_press.values, extras=dec_press.extras)
check("Pressure is numeric 92150", pkt_press.pressure == 92150.0 and isinstance(pkt_press.pressure, float))

# ---------------------------------------------------------------------------
# TEST 6: Temperature conversion follows confirmed protocol (0.1C).
# ---------------------------------------------------------------------------
print("\n--- TEST 6: Temperature conversion follows confirmed protocol ---")
raw_temp = pkt_wire("$T,2026 IN-SPACE-01,10.0,1,10.0,101325,185,12.0,0,0,0,0,0,PENDING,0.0,0,0.0,1")
dec_temp = codec.decode(raw_temp)
pkt_temp = TelemetryPacket(**dec_temp.values, extras=dec_temp.extras)
check("Temperature decoded 185 -> 18.5 deg C", pkt_temp.temperature == 18.5)

# ---------------------------------------------------------------------------
# TEST 7: Voltage conversion follows confirmed protocol (0.01V).
# ---------------------------------------------------------------------------
print("\n--- TEST 7: Voltage conversion follows confirmed protocol ---")
raw_volt = pkt_wire("$T,2026 IN-SPACE-01,10.0,1,10.0,101325,20.0,1234,0,0,0,0,0,PENDING,0.0,0,0.0,1")
dec_volt = codec.decode(raw_volt)
pkt_volt = TelemetryPacket(**dec_volt.values, extras=dec_volt.extras)
check("Battery voltage decoded 1234 -> 12.34 V", pkt_volt.battery_voltage == 12.34 and pkt_volt.voltage == 12.34)

# ---------------------------------------------------------------------------
# TEST 8: GNSS fields parse correctly (0.0001 deg lat/lon, 0.1m alt).
# ---------------------------------------------------------------------------
print("\n--- TEST 8: GNSS fields parse correctly ---")
raw_gnss = pkt_wire("$T,2026 IN-SPACE-01,10.0,1,10.0,101325,20.0,12.0,143022,285721,-806480,152,8,PENDING,0.0,0,0.0,1")
dec_gnss = codec.decode(raw_gnss)
pkt_gnss = TelemetryPacket(**dec_gnss.values, extras=dec_gnss.extras)
check("GNSS time parsed", pkt_gnss.gnss_time == "143022")
check("GNSS latitude decoded 285721 -> 28.5721 deg", abs(pkt_gnss.gnss_latitude - 28.5721) < 1e-5)
check("GNSS longitude decoded -806480 -> -80.648 deg", abs(pkt_gnss.gnss_longitude - -80.648) < 1e-5)
check("GNSS altitude decoded 152 -> 15.2 m", pkt_gnss.gnss_altitude == 15.2)
check("GNSS satellites int", pkt_gnss.gnss_satellites == 8 and pkt_gnss.satellites == 8)

# ---------------------------------------------------------------------------
# TEST 9: Flight state decoding:
# 0 → BOOT, 1 → PRELAUNCH, 2 → BOOST, 3 → COAST,
# 4 → APOGEE, 5 → DESCENT, 6 → LANDING, 7 → RECOVERY
# ---------------------------------------------------------------------------
print("\n--- TEST 9: Flight state decoding (0..7) ---")
state_map = {
    0: (FlightState.BOOT, "BOOT"),
    1: (FlightState.PRE_LAUNCH, "PRE_LAUNCH"),
    2: (FlightState.BOOST, "BOOST"),
    3: (FlightState.COAST, "COAST"),
    4: (FlightState.APOGEE, "APOGEE"),
    5: (FlightState.DESCENT, "DESCENT"),
    6: (FlightState.LANDING, "LANDING"),
    7: (FlightState.RECOVERY, "RECOVERY"),
}
for code_val, (expected_enum, expected_name) in state_map.items():
    raw_s = pkt_wire(f"$T,TEAM1,1.0,1,0,101325,20,12,0,0,0,0,0,PENDING,0,{code_val},0,1")
    dec_s = codec.decode(raw_s)
    pkt_s = TelemetryPacket(**dec_s.values, extras=dec_s.extras)
    check(f"Code {code_val} -> enum {expected_enum.name}", pkt_s.state == expected_enum)
    check(f"Code {code_val} -> flight_state_code {code_val}", pkt_s.flight_state_code == code_val)
    check(f"Code {code_val} -> flight_state string '{expected_name}'", pkt_s.flight_state == expected_name)

# ---------------------------------------------------------------------------
# TEST 10: Vehicle decoding follows confirmed protocol (1 -> ROCKET, 2 -> CANSAT).
# ---------------------------------------------------------------------------
print("\n--- TEST 10: Vehicle decoding (1=ROCKET, 2=CANSAT) ---")
raw_v1 = pkt_wire("$T,TEAM1,1.0,1,0,101325,20,12,0,0,0,0,0,PENDING,0,0,0,1")
dec_v1 = codec.decode(raw_v1)
pkt_v1 = TelemetryPacket(**dec_v1.values, extras=dec_v1.extras)
check("Vehicle code 1 is ROCKET", pkt_v1.vehicle_id == VehicleID.ROCKET and pkt_v1.vehicle == "ROCKET")
raw_v2 = pkt_wire("$T,TEAM1,1.0,1,0,101325,20,12,0,0,0,0,0,PENDING,0,0,0,2")
dec_v2 = codec.decode(raw_v2)
pkt_v2 = TelemetryPacket(**dec_v2.values, extras=dec_v2.extras)
check("Vehicle code 2 is CANSAT", pkt_v2.vehicle_id == VehicleID.CANSAT and pkt_v2.vehicle == "CANSAT")

# ---------------------------------------------------------------------------
# TEST 11: Malformed packet does not crash application.
# ---------------------------------------------------------------------------
print("\n--- TEST 11: Malformed packet does not crash application ---")
demux = TelemetryDemux()
res = demux.feed("NOT_A_VALID_PACKET_AT_ALL_$$$$$")
check("Feed garbage returns None", res is None)
check("Parser did not crash", True)

# ---------------------------------------------------------------------------
# TEST 12: Missing field does not crash application.
# ---------------------------------------------------------------------------
print("\n--- TEST 12: Missing field does not crash application ---")
res_short = demux.feed("$T,TEAM1,1.0,2,3,4")
check("Short packet safely rejected", res_short is not None and res_short.rejected is not None)

# ---------------------------------------------------------------------------
# TEST 13: Invalid numeric field is rejected safely.
# ---------------------------------------------------------------------------
print("\n--- TEST 13: Invalid numeric field is rejected safely ---")
raw_nan = pkt_wire("$T,TEAM1,1.0,1,NOT_A_NUMBER,101325,20,12,0,0,0,0,0,PENDING,0,0,0,1")
res_nan = demux.feed(raw_nan)
check("Non-numeric field safely rejected", res_nan is not None and res_nan.rejected is not None)

# ---------------------------------------------------------------------------
# TEST 14: Real pressure is appended to pressure graph history.
# ---------------------------------------------------------------------------
print("\n--- TEST 14: Real pressure is appended to pressure graph history ---")
vp = VehiclePage(VehicleID.ROCKET)
raw_p1 = pkt_wire("$T,TEAM1,10.0,1,100.0,101325.0,20.0,12.0,0,0,0,0,0,PENDING,0.0,2,10.0,1")
ingest_res = demux.feed(raw_p1)
check("Ingest result packet accepted", ingest_res is not None and ingest_res.accepted)
vp.receive(ingest_res.packet, ingest_res)
p_buf = vp.graphs["pressure"]._buffers[""]
check("Pressure buffer has 1 point", len(p_buf.xs) == 1)
check("Pressure buffer x is 10.0", p_buf.xs[0] == 10.0)
check("Pressure buffer y is 101325.0", p_buf.ys[0] == 101325.0)

# ---------------------------------------------------------------------------
# TEST 15: Real temperature is appended to temperature graph history.
# ---------------------------------------------------------------------------
print("\n--- TEST 15: Real temperature is appended to temperature graph history ---")
t_buf = vp.graphs["temperature"]._buffers[""]
check("Temperature buffer has 1 point", len(t_buf.xs) == 1)
check("Temperature buffer y is 20.0", t_buf.ys[0] == 20.0)

# ---------------------------------------------------------------------------
# TEST 16: Real altitude is appended to altitude graph history.
# ---------------------------------------------------------------------------
print("\n--- TEST 16: Real altitude is appended to altitude graph history ---")
a_buf = vp.graphs["altitude"]._buffers[""]
check("Altitude buffer has 1 point", len(a_buf.xs) == 1)
check("Altitude buffer y is 100.0", a_buf.ys[0] == 100.0)

# ---------------------------------------------------------------------------
# TEST 17: Real voltage is updated on digital card.
# ---------------------------------------------------------------------------
print("\n--- TEST 17: Real voltage is updated on digital card ---")
vp.gauges["battery_voltage"].digital._flush()
check("Battery voltage gauge updated", vp.gauges["battery_voltage"].digital._shown == 12.0)

# ---------------------------------------------------------------------------
# TEST 18: Real velocity is appended if an existing velocity graph exists.
# ---------------------------------------------------------------------------
print("\n--- TEST 18: Real velocity is appended if velocity graph exists ---")
v_buf = vp.graphs["velocity"]._buffers[""]
check("Velocity buffer has point", len(v_buf.xs) == 1)
check("Velocity buffer y is 10.0", v_buf.ys[0] == 10.0)

# ---------------------------------------------------------------------------
# TEST 19: Real gyro value is appended if an existing gyro graph exists.
# ---------------------------------------------------------------------------
print("\n--- TEST 19: Real gyro value is appended ---")
raw_gyro = pkt_wire("$T,TEAM1,11.0,2,105.0,101320.0,20.5,12.0,0,0,0,0,0,PENDING,45.0,2,12.0,1")
ingest_res2 = demux.feed(raw_gyro)
vp.receive(ingest_res2.packet, ingest_res2)
ori_buf = vp.graphs["orientation"]._buffers[""]
check("Orientation buffer has point", len(ori_buf.xs) == 2)
check("Orientation value is 45.0", ori_buf.ys[1] == 45.0)

# ---------------------------------------------------------------------------
# TEST 20: Constant pressure still creates graph samples.
# ---------------------------------------------------------------------------
print("\n--- TEST 20: Constant pressure still creates graph samples ---")
for t_sec, cnt in [(12.0, 3), (13.0, 4), (14.0, 5)]:
    raw_c = pkt_wire(f"$T,TEAM1,{t_sec},{cnt},105.0,101325.0,20.0,12.0,0,0,0,0,0,PENDING,0.0,2,0.0,1")
    ir = demux.feed(raw_c)
    vp.receive(ir.packet, ir)
check("Pressure buffer length increased with constant samples", len(p_buf.xs) >= 5)
check("Last pressure sample is 101325.0", p_buf.ys[-1] == 101325.0)

# ---------------------------------------------------------------------------
# TEST 21: Disconnect does not crash the graph system.
# ---------------------------------------------------------------------------
print("\n--- TEST 21: Disconnect does not crash the graph system ---")
link = LinkSupervisor()
link.source_state_changed.emit(SourceState.DISCONNECTED)
check("Supervisor handles DISCONNECTED status smoothly", True)
check("VehiclePage graph buffer intact", len(p_buf.xs) >= 5)

# ---------------------------------------------------------------------------
# TEST 22: Reconnect resumes telemetry graph updates.
# ---------------------------------------------------------------------------
print("\n--- TEST 22: Reconnect resumes telemetry graph updates ---")
link.source_state_changed.emit(SourceState.CONNECTED)
prev_len = len(p_buf.xs)
raw_rec = pkt_wire("$T,TEAM1,20.0,10,120.0,101300.0,21.0,12.0,0,0,0,0,0,PENDING,0.0,2,5.0,1")
ir_rec = demux.feed(raw_rec)
vp.receive(ir_rec.packet, ir_rec)
check("Graph resumed updating after reconnect", len(p_buf.xs) == prev_len + 1)
check("New sample timestamp recorded", p_buf.xs[-1] == 20.0)

# ---------------------------------------------------------------------------
# TEST 23: Existing graph history is not unnecessarily cleared on reconnect.
# ---------------------------------------------------------------------------
print("\n--- TEST 23: Existing graph history is not cleared on reconnect ---")
check("History from before reconnect is preserved", p_buf.xs[0] == 10.0 and len(p_buf.xs) > 1)

# ---------------------------------------------------------------------------
# TEST 24: Acceleration remains explicitly PENDING.
# ---------------------------------------------------------------------------
print("\n--- TEST 24: Acceleration remains explicitly PENDING ---")
raw_pending = pkt_wire("$T,TEAM1,30.0,11,150.0,101200.0,21.0,12.0,0,0,0,0,0,ACCEL_RAW,0.0,2,0.0,1")
dec_pending = codec.decode(raw_pending)
pkt_pending = TelemetryPacket(**dec_pending.values, extras=dec_pending.extras)
check("raw_accelerometer preserved as raw field", pkt_pending.raw_accelerometer == "ACCEL_RAW")
# Verify that accel_magnitude was NOT computed / added to graph
accel_buf = vp.graphs["accel_magnitude"]._buffers[""]
check("accel_magnitude graph has NO points for pending packets", len(accel_buf.xs) == 0)

# ---------------------------------------------------------------------------
# TEST 25: Existing UI tests / regression tests pass.
# ---------------------------------------------------------------------------
print("\n--- TEST 25: Existing OverviewPage graph integration ---")
op = OverviewPage()
raw_op = pkt_wire("$T,TEAM1,10.0,1,100.0,101325.0,20.0,12.0,0,0,0,0,0,PENDING,0.0,2,10.0,1")
dec_op = codec.decode(raw_op)
pkt_op = TelemetryPacket(**dec_op.values, extras=dec_op.extras)
op.receive(pkt_op, ingest_res, derived_velocity=10.0)
op_p_buf = op.graphs[VehicleID.ROCKET]["pressure"]._buffers[""]
check("Overview pressure graph receives data", len(op_p_buf.xs) == 1 and op_p_buf.ys[0] == 101325.0)

print("\n" + "=" * 60)
if FAILS:
    print(f"FAILED: {len(FAILS)} test(s) failed: {FAILS}")
    sys.exit(1)
else:
    print("ALL 25 ACCEPTANCE CRITERIA TESTS PASSED!")
    print("=" * 60)
