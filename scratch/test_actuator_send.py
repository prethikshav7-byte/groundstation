import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PyQt6.QtWidgets import QApplication
from ground_station.actuators import BY_ID, Actuator, ArmingCentre, ACTUATORS
from ground_station.models import VehicleID, RocketSafetyState
from ground_station.commands import CommandCentre
from ground_station.link import LinkSupervisor, SourceState
from ground_station.pages.command import CommandPage, ActuatorPanel

def test_actuator_send_flow():
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

    page = CommandPage(centre)
    page.refresh()

    door_panel = page.actuator_panels["ACT_DOOR"]
    deploy_panel = page.actuator_panels["ACT_DEPLOY"]
    separate_panel = page.actuator_panels["ACT_SEPARATE"]

    # Initial state: Last command is "—"
    assert door_panel._last.text() == "—"
    assert deploy_panel._last.text() == "—"
    assert separate_panel._last.text() == "—"

    # 1. Rocket door Send -> TEST1\n
    door_panel._send_btn.click()
    assert mock_source.lines_sent[-1] == "TEST1\n", f"Expected 'TEST1\\n', got {mock_source.lines_sent[-1]!r}"
    assert door_panel._last.text() == "TEST1"
    assert deploy_panel._last.text() == "—"
    assert separate_panel._last.text() == "—"

    # 2. Payload deploy Send -> TEST2\n
    deploy_panel._send_btn.click()
    assert mock_source.lines_sent[-1] == "TEST2\n", f"Expected 'TEST2\\n', got {mock_source.lines_sent[-1]!r}"
    assert door_panel._last.text() == "TEST1"
    assert deploy_panel._last.text() == "TEST2"
    assert separate_panel._last.text() == "—"

    # 3. Canister separation Send -> TEST3\n
    separate_panel._send_btn.click()
    assert mock_source.lines_sent[-1] == "TEST3\n", f"Expected 'TEST3\\n', got {mock_source.lines_sent[-1]!r}"
    assert door_panel._last.text() == "TEST1"
    assert deploy_panel._last.text() == "TEST2"
    assert separate_panel._last.text() == "TEST3"

    # 4. Verify CommandLogPanel contains 3 real command records
    page.log_panel.refresh()
    assert len(page.log_panel._rendered) == 3, f"Expected 3 rendered records in CommandLogPanel, got {len(page.log_panel._rendered)}"

    # 5. Verify disconnected source does not crash
    sup_no_source = LinkSupervisor()
    centre_no_source = CommandCentre(sup_no_source.send_command)
    page_no_source = CommandPage(centre_no_source)
    page_no_source.actuator_panels["ACT_DOOR"]._send_btn.click()
    assert page_no_source.actuator_panels["ACT_DOOR"]._last.text() == "TEST1"

    print("ALL ACTUATOR SEND FLOW TESTS PASSED!")

if __name__ == "__main__":
    test_actuator_send_flow()
