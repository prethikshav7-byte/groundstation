import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication
from ground_station.actuators import BY_ID, Actuator, ArmingCentre, ACTUATORS
from ground_station.models import VehicleID, RocketSafetyState
from ground_station.commands import COMMANDS, BENCH_COMMANDS, CommandCentre, CommandStatus
from ground_station.link import LinkSupervisor, SourceState
from ground_station.pages.command import CommandPage, ActuatorPanel

def test_hardware_protocol_integration():
    app = QApplication.instance() or QApplication(sys.argv)

    class MockSerialSource:
        def __init__(self):
            self.state = SourceState.CONNECTED
            self.lines_sent = []
        def send_line(self, line: str):
            self.lines_sent.append(line)

    mock_source = MockSerialSource()
    sup = LinkSupervisor()
    sup.source = mock_source
    centre = CommandCentre(sup.send_command)

    # 1. Verify Startup Safety: Zero commands sent upon initialization
    assert len(mock_source.lines_sent) == 0, "No command should be sent on init"
    assert len(centre.records) == 0, "Command centre must be empty on init"

    page = CommandPage(centre)
    notices = []
    page.notice.connect(lambda msg: notices.append(msg))
    page.refresh()

    assert len(mock_source.lines_sent) == 0, "No command should be sent on page load or refresh"
    assert len(centre.records) == 0

    # Verify command registration in COMMANDS and BENCH_COMMANDS
    expected_cmds = [
        "DOOR:30", "DOOR:60", "DOOR:90", "LOCK:DOOR",
        "CANSAT:30", "CANSAT:60", "CANSAT:90", "LOCK:CANSAT",
        "SEPARATION:30", "SEPARATION:60", "SEPARATION:90", "LOCK:SEPARATION",
        "LOCK:ALL", "TEST1", "TEST2", "TEST3", "TEST4", "TEST5", "NEXT", "TEST6", "RESET"
    ]
    for cmd in expected_cmds:
        assert cmd in COMMANDS, f"{cmd} must be registered in COMMANDS"
        assert cmd in BENCH_COMMANDS, f"{cmd} must be registered in BENCH_COMMANDS"

    door_p = page.actuator_panels["ACT_DOOR"]
    deploy_p = page.actuator_panels["ACT_DEPLOY"]
    separate_p = page.actuator_panels["ACT_SEPARATE"]

    # 2. Test Door Controls & Repeatability
    # Door 30°
    door_p._angle_buttons[30].click()
    assert mock_source.lines_sent[-1] == "DOOR:30\n"
    assert door_p.actuator.commanded == 30.0
    assert door_p._commanded.text() == "30°"
    assert door_p._last.text() == "DOOR:30"
    assert door_p._angle_buttons[30].isEnabled(), "Button must remain enabled"

    # Click Door 30° again (repeatability)
    door_p._angle_buttons[30].click()
    assert mock_source.lines_sent[-1] == "DOOR:30\n"
    assert len(mock_source.lines_sent) == 2

    # Door 60° (twice)
    door_p._angle_buttons[60].click()
    assert mock_source.lines_sent[-1] == "DOOR:60\n"
    assert door_p.actuator.commanded == 60.0
    assert door_p._commanded.text() == "60°"
    door_p._angle_buttons[60].click()
    assert mock_source.lines_sent[-1] == "DOOR:60\n"

    # Door 90° (twice)
    door_p._angle_buttons[90].click()
    assert mock_source.lines_sent[-1] == "DOOR:90\n"
    assert door_p.actuator.commanded == 90.0
    assert door_p._commanded.text() == "90°"
    door_p._angle_buttons[90].click()
    assert mock_source.lines_sent[-1] == "DOOR:90\n"

    # Door Lock (twice)
    door_p._lock_btn.click()
    assert mock_source.lines_sent[-1] == "LOCK:DOOR\n"
    assert door_p.actuator.commanded == 0.0
    assert door_p._commanded.text() == "0°"
    assert door_p._last.text() == "LOCK:DOOR"
    door_p._lock_btn.click()
    assert mock_source.lines_sent[-1] == "LOCK:DOOR\n"

    # Door Send (TEST1)
    door_p._send_btn.click()
    assert mock_source.lines_sent[-1] == "TEST1\n"
    assert door_p._last.text() == "TEST1"

    # 3. Test CanSat Controls & Repeatability
    # CanSat 30° (twice)
    deploy_p._angle_buttons[30].click()
    assert mock_source.lines_sent[-1] == "CANSAT:30\n"
    assert deploy_p.actuator.commanded == 30.0
    assert deploy_p._commanded.text() == "30°"
    deploy_p._angle_buttons[30].click()
    assert mock_source.lines_sent[-1] == "CANSAT:30\n"

    # CanSat 60° (twice)
    deploy_p._angle_buttons[60].click()
    assert mock_source.lines_sent[-1] == "CANSAT:60\n"
    assert deploy_p.actuator.commanded == 60.0
    assert deploy_p._commanded.text() == "60°"
    deploy_p._angle_buttons[60].click()
    assert mock_source.lines_sent[-1] == "CANSAT:60\n"

    # CanSat 90° (twice)
    deploy_p._angle_buttons[90].click()
    assert mock_source.lines_sent[-1] == "CANSAT:90\n"
    assert deploy_p.actuator.commanded == 90.0
    assert deploy_p._commanded.text() == "90°"
    deploy_p._angle_buttons[90].click()
    assert mock_source.lines_sent[-1] == "CANSAT:90\n"

    # CanSat Lock (twice)
    deploy_p._lock_btn.click()
    assert mock_source.lines_sent[-1] == "LOCK:CANSAT\n"
    assert deploy_p.actuator.commanded == 0.0
    assert deploy_p._commanded.text() == "0°"
    assert deploy_p._last.text() == "LOCK:CANSAT"
    deploy_p._lock_btn.click()
    assert mock_source.lines_sent[-1] == "LOCK:CANSAT\n"

    # CanSat Send (TEST2)
    deploy_p._send_btn.click()
    assert mock_source.lines_sent[-1] == "TEST2\n"
    assert deploy_p._last.text() == "TEST2"

    # 4. Test Canister Separation Controls & Repeatability
    # Separation 30° (twice)
    separate_p._angle_buttons[30].click()
    assert mock_source.lines_sent[-1] == "SEPARATION:30\n"
    assert separate_p.actuator.commanded == 30.0
    assert separate_p._commanded.text() == "30°"
    separate_p._angle_buttons[30].click()
    assert mock_source.lines_sent[-1] == "SEPARATION:30\n"

    # Separation 60° (twice)
    separate_p._angle_buttons[60].click()
    assert mock_source.lines_sent[-1] == "SEPARATION:60\n"
    assert separate_p.actuator.commanded == 60.0
    assert separate_p._commanded.text() == "60°"
    separate_p._angle_buttons[60].click()
    assert mock_source.lines_sent[-1] == "SEPARATION:60\n"

    # Separation 90° (twice)
    separate_p._angle_buttons[90].click()
    assert mock_source.lines_sent[-1] == "SEPARATION:90\n"
    assert separate_p.actuator.commanded == 90.0
    assert separate_p._commanded.text() == "90°"
    separate_p._angle_buttons[90].click()
    assert mock_source.lines_sent[-1] == "SEPARATION:90\n"

    # Separation Lock (twice)
    separate_p._lock_btn.click()
    assert mock_source.lines_sent[-1] == "LOCK:SEPARATION\n"
    assert separate_p.actuator.commanded == 0.0
    assert separate_p._commanded.text() == "0°"
    assert separate_p._last.text() == "LOCK:SEPARATION"
    separate_p._lock_btn.click()
    assert mock_source.lines_sent[-1] == "LOCK:SEPARATION\n"

    # Separation Send (TEST3)
    separate_p._send_btn.click()
    assert mock_source.lines_sent[-1] == "TEST3\n"
    assert separate_p._last.text() == "TEST3"

    # Set some actuators to angles first
    door_p._angle_buttons[60].click()
    deploy_p._angle_buttons[90].click()
    separate_p._angle_buttons[30].click()
    assert door_p.actuator.commanded == 60.0
    assert deploy_p.actuator.commanded == 90.0
    assert separate_p.actuator.commanded == 30.0

    # 5. Test Global LOCK ALL
    page.sequence_panel._btn_lock_all.click()
    assert mock_source.lines_sent[-1] == "LOCK:ALL\n"
    assert door_p.actuator.commanded == 0.0
    assert deploy_p.actuator.commanded == 0.0
    assert separate_p.actuator.commanded == 0.0
    assert door_p._commanded.text() == "0°"
    assert deploy_p._commanded.text() == "0°"
    assert separate_p._commanded.text() == "0°"
    assert door_p._last.text() == "LOCK:ALL"
    assert deploy_p._last.text() == "LOCK:ALL"
    assert separate_p._last.text() == "LOCK:ALL"

    # Click LOCK ALL again (repeatability)
    page.sequence_panel._btn_lock_all.click()
    assert mock_source.lines_sent[-1] == "LOCK:ALL\n"

    # 6. Test RESET
    page.sequence_panel._btn_reset.click()
    assert mock_source.lines_sent[-1] == "RESET\n"
    assert door_p.actuator.commanded == 0.0
    assert deploy_p.actuator.commanded == 0.0
    assert separate_p.actuator.commanded == 0.0
    assert door_p._last.text() == "RESET"

    # 7. Check that actual position is NEVER faked without telemetry echo
    assert door_p.actuator.actual is None
    assert deploy_p.actuator.actual is None
    assert separate_p.actuator.actual is None
    assert door_p._actual.text() == "not reported"
    assert deploy_p._actual.text() == "not reported"
    assert separate_p._actual.text() == "not reported"

    # 8. Check command log
    page.log_panel.refresh()
    assert len(page.log_panel._rendered) == len(centre.records)
    for record in centre.records:
        assert record.command in mock_source.lines_sent[record.sequence - 1]

    print("ALL HARDWARE PROTOCOL INTEGRATION TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_hardware_protocol_integration()
