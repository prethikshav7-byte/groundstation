"""
scratch/render_com_console.py
═════════════════════════════
Render screenshots of the PHOENIX dB.V1 COM / Telemetry Communication Console.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication
from ground_station.app import GroundStationApp
from ground_station.models import FlightState, TelemetryPacket, VehicleID
from ground_station.parser import IngestResult, LossTier
from ground_station.simulator.source import SimulatedSource


def main():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    win = GroundStationApp(mode_label="LIVE", simulator_mode=False)
    sim = SimulatedSource()
    win.connect_supervisor(sim)
    win.show()

    # Navigate to COM console (index 5)
    win._navigate(5)

    # Ingest 15 packets for Rocket and CanSat
    for i in range(1, 16):
        t = float(i)
        p_rock = TelemetryPacket(
            team_id="1000",
            vehicle_id=VehicleID.ROCKET,
            mission_time=t,
            packet_count=i,
            state=FlightState.BOOST if i < 10 else FlightState.APOGEE,
            altitude=100.0 + i * 25.5,
            pressure=101325.0 - i * 120.0,
            temperature=24.5 - i * 0.2,
            battery_voltage=7.4 - i * 0.01,
            gnss_time="18:42:15",
            gnss_latitude=13.082700,
            gnss_longitude=80.270700,
            gnss_altitude=120.0 + i * 25.0,
            gnss_satellites=8,
            accel_x=0.1,
            accel_y=0.2,
            accel_z=14.5,
            gyro_x=0.01,
            gyro_y=0.02,
            gyro_z=0.5,
            raw=f"$T,1000,1,{t:.2f},{i},2,{100.0 + i*25.5:.2f},98000,22.0,7.4,184215,130827,802707,120,8,14.5,0.5*4F,RSSI=-62.1,SNR=8.5",
        )
        win._on_rocket(p_rock, IngestResult(packet=p_rock, tier=LossTier.NONE))

        p_can = TelemetryPacket(
            team_id="1000",
            vehicle_id=VehicleID.CANSAT,
            mission_time=t,
            packet_count=i,
            state=FlightState.BOOST,
            altitude=95.0 + i * 24.0,
            pressure=101400.0 - i * 115.0,
            temperature=25.0 - i * 0.15,
            battery_voltage=8.1 - i * 0.01,
            gnss_time="18:42:15",
            gnss_latitude=13.082710,
            gnss_longitude=80.270720,
            gnss_altitude=115.0 + i * 24.0,
            gnss_satellites=7,
            accel_x=0.05,
            accel_y=0.1,
            accel_z=12.2,
            gyro_x=0.02,
            gyro_y=0.01,
            gyro_z=0.3,
            raw=f"$T,1000,2,{t:.2f},{i},2,{95.0 + i*24.0:.2f},98100,23.0,8.1,184215,130827,802707,115,7,12.2,0.3*5A,RSSI=-66.0,SNR=7.8",
        )
        win._on_cansat(p_can, IngestResult(packet=p_can, tier=LossTier.NONE))

    app.processEvents()

    out_dir = os.path.join(os.path.dirname(__file__), "renders")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "com_console_screenshot.png")

    pixmap = win.grab()
    pixmap.save(out_path)
    print(f"Screenshot saved to {out_path}")
    win.close()


if __name__ == "__main__":
    main()
