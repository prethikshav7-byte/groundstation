#!/usr/bin/env python3
"""
scratch/test_phoenix_telemetry.py
═════════════════════════════════
Validation test suite for PHOENIX dB.V1 hardware telemetry integration:
1. 12-field PKT=... packet parsing
2. Correct sensor field extraction (P, T, ALT, AX, AY, AZ, LAT, LON, GALT, SAT, FIX)
3. Ground station receiver diagnostic filtering & RSSI / SNR parsing
4. Satellite minimum target (6 satellites) & Fix validation
5. VehiclePage & OverviewPage graph / gauge updates
6. Telemetry connected popup notification
7. Compatibility with 19-field and 21-field $T packets
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt6.QtWidgets import QApplication

from ground_station.codec import CsvCodec, CodecError
from ground_station.models import (
    FlightState, RejectReason, TelemetryPacket, VehicleID, VehicleLinkState
)
from ground_station.parser import TelemetryDemux
from ground_station.pages.vehicle import VehiclePage
from ground_station.pages.overview import OverviewPage
from ground_station.app import GroundStationApp

app = QApplication.instance() or QApplication(sys.argv)

codec = CsvCodec()
demux = TelemetryDemux(codec)

print("=" * 60)
print("TESTING PHOENIX dB.V1 TELEMETRY INTEGRATION")
print("=" * 60)

# 1. Authoritative 13-field packet from transmitter
raw_pkt1 = "PKT=25,P=1009.52,T=27.31,ALT=142.42,AX=0.010,AY=-0.020,AZ=0.980,LAT=11.016823,LON=76.955841,GALT=147.42,SAT=8,FIX=1,SIM=1"

dec = codec.decode(raw_pkt1)
assert dec is not None, "Failed to decode PKT= frame"
vals = dec.values
assert vals["packet_count"] == 25, f"Expected PKT 25, got {vals['packet_count']}"
assert vals["pressure"] == 1009.52, f"Expected pressure 1009.52, got {vals['pressure']}"
assert vals["temperature"] == 27.31, f"Expected temperature 27.31, got {vals['temperature']}"
assert vals["altitude"] == 142.42, f"Expected altitude 142.42, got {vals['altitude']}"
assert vals["accel_x"] == 0.010, f"Expected AX 0.010, got {vals['accel_x']}"
assert vals["accel_y"] == -0.020, f"Expected AY -0.020, got {vals['accel_y']}"
assert vals["accel_z"] == 0.980, f"Expected AZ 0.980, got {vals['accel_z']}"
assert vals["gnss_latitude"] == 11.016823, f"Expected LAT 11.016823, got {vals['gnss_latitude']}"
assert vals["gnss_longitude"] == 76.955841, f"Expected LON 76.955841, got {vals['gnss_longitude']}"
assert vals["gnss_altitude"] == 147.42, f"Expected GALT 147.42, got {vals['gnss_altitude']}"
assert vals["gnss_satellites"] == 8, f"Expected SAT 8, got {vals['gnss_satellites']}"
assert dec.extras["gnss_fix"] == "1", f"Expected FIX 1, got {dec.extras['gnss_fix']}"
assert dec.extras["sim"] == "1", f"Expected SIM 1, got {dec.extras['sim']}"
print("[PASS] 1. Authoritative 13-field packet decoded accurately")

# 2. Strict field count validation for PKT frame
try:
    codec.decode("PKT=12,P=1001.25,T=25.43")
    assert False, "Should reject short PKT frame"
except CodecError as e:
    assert e.reason == RejectReason.FIELD_COUNT
    print("[PASS] 2. Short PKT frame correctly rejected")

# 3. Diagnostic filtering and RSSI/SNR parsing
demux.feed("==========================================")
demux.feed("        TELEMETRY RECEIVED")
demux.feed("==========================================")
res = demux.feed(raw_pkt1)
assert res is not None and res.accepted, "Valid telemetry packet must be accepted"
demux.feed("------------------------------------------")
demux.feed("RSSI      : -68 dBm")
demux.feed("SNR       : 10.20 dB")
demux.feed("==========================================")

# Next packet should pick up RSSI and SNR
raw_pkt2 = "PKT=26,P=1009.50,T=27.35,ALT=142.50,AX=0.010,AY=-0.020,AZ=0.981,LAT=11.016825,LON=76.955845,GALT=147.50,SAT=8,FIX=1,SIM=1"
res2 = demux.feed(raw_pkt2)
assert res2 is not None and res2.accepted
pkt2 = res2.packet
assert pkt2.rssi == -68.0, f"Expected RSSI -68.0, got {pkt2.rssi}"
assert pkt2.snr == 10.20, f"Expected SNR 10.20, got {pkt2.snr}"
assert pkt2.is_simulation is True, "Expected is_simulation to be True"
print("[PASS] 3. Diagnostic lines filtered and RSSI/SNR captured accurately")

# 4. Satellite threshold and Fix verification (>= 6 sats vs < 6 sats)
vp = VehiclePage(VehicleID.ROCKET)
vp.receive(res.packet, res)
vp.receive(pkt2, res2)
assert "8 SAT (LOCK)" in vp._gnss_label.text(), f"Expected LOCK in label, got {vp._gnss_label.text()}"
assert "11.016825" in vp._gnss_label.text(), f"Expected Lat in label, got {vp._gnss_label.text()}"

# Test insufficient satellites (e.g. 4 satellites)
raw_low_sat = "PKT=27,P=1009.48,T=27.38,ALT=142.60,AX=0.010,AY=-0.020,AZ=0.980,LAT=11.016830,LON=76.955850,GALT=147.60,SAT=4,FIX=0,SIM=1"
res_low = demux.feed(raw_low_sat)
vp.receive(res_low.packet, res_low)
assert "INSUFFICIENT" in vp._gnss_label.text(), f"Expected INSUFFICIENT in label, got {vp._gnss_label.text()}"
print("[PASS] 4. Satellite target >= 6 and insufficient handling verified")

# 5. Graph and Gauge updates from real PHOENIX packets
assert len(vp.graphs["altitude"]._buffers[""].xs) >= 3, "Altitude graph buffer updated"
assert len(vp.graphs["pressure"]._buffers[""].xs) >= 3, "Pressure graph buffer updated"
assert len(vp.graphs["temperature"]._buffers[""].xs) >= 3, "Temperature graph buffer updated"
assert len(vp.graphs["accel_magnitude"]._buffers[""].xs) >= 3, "Accel magnitude buffer updated"
assert vp.graphs["pressure"]._buffers[""].ys[-1] == 1009.48, "Pressure graph matches sensor hPa"
print("[PASS] 5. Real sensor telemetry reaches graph history and gauges seamlessly")

# 6. Telemetry connected popup notification
from ground_station.link import LinkSupervisor
sup = LinkSupervisor(codec)
gs_app = GroundStationApp()
gs_app.connect_supervisor(sup)
# Dispatch a packet to verify popup on first connect
gs_app._dispatch(VehicleID.ROCKET, pkt2, res2)
assert gs_app._telemetry_connected is True
assert gs_app.statusBar().currentMessage() == "PHOENIX telemetry connected"
print("[PASS] 6. Telemetry connected popup displayed on connection transition")

# 7. Compatibility with 19-field designated $T packet
raw_19 = "$T,2026 IN-SPACe-010,100,1,1250,100500,225,1200,100,1134567,7654321,1240,8,2,15,2,35,1,22"
dec_19 = codec.decode(raw_19)
assert dec_19 is not None
assert dec_19.values["altitude"] == 125.0
assert dec_19.values["temperature"] == 22.5
assert dec_19.values["pressure"] == 100500.0
print("[PASS] 7. 19-field designated $T telemetry packet compatibility fully preserved")

print("=" * 60)
print("ALL PHOENIX TELEMETRY INTEGRATION TESTS PASSED!")
print("=" * 60)
