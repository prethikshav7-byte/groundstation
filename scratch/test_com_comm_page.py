"""
scratch/test_com_comm_page.py
═════════════════════════════
Unit and integration tests for the PHOENIX dB.V1 COM / Telemetry Communication Console.
"""
from __future__ import annotations

import time
import pytest
from PyQt6.QtWidgets import QApplication

from ground_station.app import GroundStationApp
from ground_station.models import FlightState, SourceState, TelemetryPacket, VehicleID
from ground_station.pages.com import ComPage, PacketRecord
from ground_station.parser import IngestResult, LossTier
from ground_station.simulator.source import SimulatedSource


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_packet(vehicle_id: VehicleID, count: int, mission_time: float,
                 altitude: float, state: FlightState = FlightState.BOOST,
                 is_sim: bool = False) -> TelemetryPacket:
    extras = {"SIM": "1"} if is_sim else {}
    return TelemetryPacket(
        team_id=1234,
        vehicle_id=vehicle_id,
        mission_time=mission_time,
        packet_count=count,
        state=state,
        altitude=altitude,
        pressure=101325.0 - altitude * 10,
        temperature=22.5,
        battery_voltage=7.4,
        gnss_time="18:30:00",
        gnss_latitude=13.0827,
        gnss_longitude=80.2707,
        gnss_altitude=50.0 + altitude,
        gnss_satellites=8,
        accel_x=0.1,
        accel_y=0.2,
        accel_z=14.5,
        gyro_x=0.01,
        gyro_y=0.02,
        gyro_z=0.5,
        extras=extras,
        raw=f"$T,1234,{vehicle_id.value},{mission_time:.2f},{count},{state.value},{altitude:.2f}*4A",
    )


def test_com_console_initialization(qapp):
    """Test COM console sub-components and initial states."""
    page = ComPage()
    assert page.rocket_stream.model.rowCount() == 0
    assert page.cansat_stream.model.rowCount() == 0
    assert page.rocket_stream.stack.currentIndex() == 0  # Empty state
    assert page.cansat_stream.stack.currentIndex() == 0  # Empty state

    assert page.port_val.text() == "—"
    assert page.baud_val.text() == "115200"
    assert page.total_pkts_val.text() == "0000"
    assert "DISCONNECTED" in page.status_lbl.text()


def test_packet_stream_newest_at_top(qapp):
    """Test that incoming packets appear at index 0 (top) in the appropriate stream."""
    page = ComPage()
    res = IngestResult(packet=None, tier=LossTier.NONE)

    p1 = _make_packet(VehicleID.ROCKET, 1, 10.0, 100.0)
    p2 = _make_packet(VehicleID.ROCKET, 2, 11.0, 150.0)

    page.receive(p1, res, derived_v=10.0)
    assert page.rocket_stream.model.rowCount() == 1
    assert page.rocket_stream.stack.currentIndex() == 1  # Switched to list view
    r0 = page.rocket_stream.model.get_packet(0)
    assert r0.packet_count == 1
    assert r0.altitude == 100.0

    page.receive(p2, res, derived_v=15.0)
    assert page.rocket_stream.model.rowCount() == 2
    # Row 0 must be p2 (newest)
    r_top = page.rocket_stream.model.get_packet(0)
    assert r_top.packet_count == 2
    assert r_top.altitude == 150.0
    # Row 1 must be p1 (older)
    r_prev = page.rocket_stream.model.get_packet(1)
    assert r_prev.packet_count == 1
    assert r_prev.altitude == 100.0


def test_dual_stream_separation(qapp):
    """Test Rocket vs CanSat packet segregation."""
    page = ComPage()
    res = IngestResult(packet=None, tier=LossTier.NONE)

    rp = _make_packet(VehicleID.ROCKET, 10, 5.0, 50.0)
    cp = _make_packet(VehicleID.CANSAT, 20, 6.0, 30.0)

    page.receive(rp, res)
    assert page.rocket_stream.model.rowCount() == 1
    assert page.cansat_stream.model.rowCount() == 0

    page.receive(cp, res)
    assert page.rocket_stream.model.rowCount() == 1
    assert page.cansat_stream.model.rowCount() == 1

    assert page.rocket_stream.model.get_packet(0).packet_count == 10
    assert page.cansat_stream.model.get_packet(0).packet_count == 20


def test_packet_inspector_display(qapp):
    """Test inspecting selected packet for all 19 specification fields."""
    page = ComPage()
    p = _make_packet(VehicleID.ROCKET, 42, 14.5, 412.3, state=FlightState.APOGEE)
    rec = PacketRecord(p, derived_v=25.0)

    page.inspector.display_packet(rec)
    assert "412.30" in page.inspector.raw_box.toPlainText()
    assert page.inspector.table.rowCount() >= 19

    # Build field mapping from table
    field_map = {}
    for r in range(page.inspector.table.rowCount()):
        name = page.inspector.table.item(r, 0).text()
        val = page.inspector.table.item(r, 1).text()
        unit = page.inspector.table.item(r, 2).text()
        field_map[name] = (val, unit)

    # Check required specification fields
    assert "Prefix" in field_map
    assert field_map["Prefix"][0] == "$T"

    assert "Team ID" in field_map
    assert field_map["Team ID"][0] == "1234"

    assert "Time Stamp" in field_map
    assert field_map["Time Stamp"][0] == "14.50 s"

    assert "Packet Count" in field_map
    assert field_map["Packet Count"][0] == "42"

    assert "Altitude" in field_map
    assert field_map["Altitude"][0] == "412.30 m"

    assert "Flight Software State" in field_map
    assert field_map["Flight Software State"][0] == "4 — APOGEE"

    assert "Vehicle" in field_map
    assert field_map["Vehicle"][0] == "1 — ROCKET"

    assert "Checksum" in field_map
    assert field_map["Checksum"][0] == "4A"


def test_com_metrics_and_dev_badge(qapp):
    """Test metrics bar (LIFT, RATE, VALID, REJECTED, SUCCESS) and DEV badge."""
    page = ComPage()
    assert page.dev_badge.text() == "DEV"
    assert page.lift_val.text() == "STANDBY"

    res = IngestResult(packet=None, tier=LossTier.NONE)
    p = _make_packet(VehicleID.ROCKET, 1, 1.0, 10.0, state=FlightState.BOOST)

    page.receive(p, res)
    assert page.lift_val.text() == "BOOST"
    assert page.valid_val.text() == "1"
    assert page.total_pkts_val.text() == "0001"


def test_simulation_controls(qapp):
    """Test Rocket and CanSat simulation start, pause, and reset."""
    lines_received = []
    sim_ctrl = page_sim = None

    def on_line(l):
        lines_received.append(l)

    page = ComPage()
    page.sim_rocket.on_line_ready = on_line

    # Start simulation
    page.sim_rocket.start()
    assert page.sim_rocket.running is True
    assert page.sim_rocket.status_lbl.text() == "RUNNING"

    # Step simulation manually
    page.sim_rocket._step()
    assert len(lines_received) == 1
    assert "$T," in lines_received[0]
    assert ",SIM=1" in lines_received[0]

    # Pause
    page.sim_rocket.pause()
    assert page.sim_rocket.running is False
    assert page.sim_rocket.status_lbl.text() == "PAUSED"

    # Reset
    page.sim_rocket.reset()
    assert page.sim_rocket.t == 0.0
    assert page.sim_rocket.status_lbl.text() == "IDLE"


def test_app_integration_and_navigation(qapp):
    """Test navigation to COM in GroundStationApp and supervisor connectivity."""
    app = GroundStationApp(mode_label="SIMULATOR MODE", simulator_mode=True)
    sim = SimulatedSource()
    app.connect_supervisor(sim)

    # Check sidebar COM navigation button
    com_btn = app._nav[5]
    assert com_btn.text() == "COM"
    assert app.stack.widget(5) == app.com_page

    com_btn.click()
    assert app.stack.currentIndex() == 5
    assert com_btn.isChecked()

    # Check connection reflection
    assert app.com_page.port_val.text() == "SIMULATOR"
    assert app.com_page.baud_val.text() == "115200"

    # Feed packet through dispatch
    p = _make_packet(VehicleID.ROCKET, 99, 12.0, 300.0)
    res = IngestResult(packet=p, tier=LossTier.NONE)
    app._on_rocket(p, res)

    assert app.com_page.rocket_stream.model.rowCount() == 1
    assert app.com_page.total_pkts_val.text() == "0001"
