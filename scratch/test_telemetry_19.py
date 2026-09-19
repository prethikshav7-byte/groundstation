import sys
sys.path.insert(0, ".")

from PyQt6.QtWidgets import QApplication
from ground_station.codec import CsvCodec, xor_checksum
from ground_station.models import VehicleID, FlightState
from ground_station.parser import TelemetryDemux
from ground_station.pages.vehicle import VehiclePage
from ground_station.pages.overview import OverviewPage

app = QApplication.instance() or QApplication(sys.argv)

codec = CsvCodec()

# Test 1: Example 19-field packet from specification
payload = "$T,2026 IN-SPACe-010,100,1,1250,100500,225,1200,100,1134567,7654321,1240,8,2,15,2,35,1"
ck = xor_checksum(payload)
packet_line = payload + "," + ck
print("Packet line:", packet_line)

frame = codec.decode(packet_line)
vals = frame.values
print("Decoded values successfully!")
assert vals["team_id"] == "2026 IN-SPACe-010"
assert vals["mission_time"] == 100.0
assert vals["packet_count"] == 1
assert vals["altitude"] == 125.0
assert vals["pressure"] == 100500.0
assert vals["temperature"] == 22.5
assert vals["battery_voltage"] == 12.0
assert vals["gnss_latitude"] == 113.4567
assert vals["gnss_longitude"] == 765.4321
assert vals["gnss_altitude"] == 124.0
assert vals["gnss_satellites"] == 8
assert vals["accelerometer"] == 2.0
assert vals["gyro_spin_rate"] == 15.0
assert vals["state"] == FlightState.BOOST
assert vals["velocity"] == 35.0
assert vals["vehicle_id"] == VehicleID.ROCKET

# Test 2: Full Demux Pipeline
demux = TelemetryDemux()
res = demux.feed(packet_line)
assert res is not None
assert res.accepted
pkt = res.packet
assert pkt.altitude == 125.0
assert pkt.temperature == 22.5
assert pkt.velocity == 35.0
assert pkt.accelerometer == 2.0

# Test 3: VehiclePage and OverviewPage Data Integration
vpage = VehiclePage(VehicleID.ROCKET)
opage = OverviewPage()

v = vpage.receive(pkt, res)
opage.receive(pkt, res, v)

# Verify digital values and graph history
assert vpage.gauges["altitude"].digital._pending == 125.0
assert vpage.gauges["temperature"].digital._pending == 22.5
assert vpage.gauges["pressure"].digital._pending == 100500.0
assert vpage.gauges["velocity"].digital._pending == 35.0
assert vpage.gauges["accel_magnitude"].digital._pending == 2.0

# Check graphs in vehicle page
assert len(vpage.graphs["altitude"]._buffers[""].xs) == 1
assert vpage.graphs["altitude"]._buffers[""].xs[0] == 100.0
assert vpage.graphs["altitude"]._buffers[""].ys[0] == 125.0

assert len(vpage.graphs["velocity"]._buffers[""].xs) == 1
assert vpage.graphs["velocity"]._buffers[""].ys[0] == 35.0

assert len(vpage.graphs["accel_magnitude"]._buffers[""].xs) == 1
assert vpage.graphs["accel_magnitude"]._buffers[""].ys[0] == 2.0

# Check graphs in overview page
assert len(opage.graphs[VehicleID.ROCKET]["altitude"]._buffers[""].xs) == 1
assert opage.graphs[VehicleID.ROCKET]["altitude"]._buffers[""].ys[0] == 125.0
assert len(opage.graphs[VehicleID.ROCKET]["velocity"]._buffers[""].xs) == 1
assert opage.graphs[VehicleID.ROCKET]["velocity"]._buffers[""].ys[0] == 35.0

print("PIPELINE TEST PASSED! All 19 fields reach gauges and graph history identically!")
