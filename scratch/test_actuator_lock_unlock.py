import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication
from ground_station.actuators import BY_ID, Actuator, ArmingCentre, ACTUATORS
from ground_station.models import VehicleID, RocketSafetyState
from ground_station.commands import CommandCentre, CommandStatus
from ground_station.link import LinkSupervisor, SourceState
from ground_station.pages.command import CommandPage, ActuatorPanel

def test_lock_actuators():
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
    notices = []
    page.notice.connect(lambda msg: notices.append(msg))
    page.refresh()

    door_p = page.actuator_panels["ACT_DOOR"]
    deploy_p = page.actuator_panels["ACT_DEPLOY"]
    separate_p = page.actuator_panels["ACT_SEPARATE"]

    # Verify buttons exist and are enabled
    for p, name in [(door_p, "Rocket door"), (deploy_p, "Payload deploy"), (separate_p, "Separation")]:
        assert hasattr(p, "_lock_btn"), f"{name} panel must have _lock_btn"
        assert p._lock_btn.isEnabled(), f"{name} _lock_btn must be enabled"
        assert p._lock_btn.text() == "Lock"
        for a in (30, 60, 90):
            assert a in p._angle_buttons, f"{name} panel missing angle button {a}"
            assert p._angle_buttons[a].isEnabled()

    # 1. Rocket Door Lock (repeatability)
    for _ in range(2):
        door_p._lock_btn.click()
        assert mock_source.lines_sent[-1] == "LOCK:DOOR\n"
        assert door_p.actuator.locked is True
        assert door_p.actuator.commanded == 0.0
        assert door_p._commanded.text() == "0°"
        assert door_p._last.text() == "LOCK:DOOR"
        assert notices[-1] == "Door locked (0°)"
        assert door_p._lock_btn.isEnabled()

    # 2. Payload Deploy Lock (repeatability)
    for _ in range(2):
        deploy_p._lock_btn.click()
        assert mock_source.lines_sent[-1] == "LOCK:CANSAT\n"
        assert deploy_p.actuator.locked is True
        assert deploy_p.actuator.commanded == 0.0
        assert deploy_p._commanded.text() == "0°"
        assert deploy_p._last.text() == "LOCK:CANSAT"
        assert notices[-1] == "CanSat locked (0°)"
        assert deploy_p._lock_btn.isEnabled()

    # 3. Canister Separation Lock (repeatability)
    for _ in range(2):
        separate_p._lock_btn.click()
        assert mock_source.lines_sent[-1] == "LOCK:SEPARATION\n"
        assert separate_p.actuator.locked is True
        assert separate_p.actuator.commanded == 0.0
        assert separate_p._commanded.text() == "0°"
        assert separate_p._last.text() == "LOCK:SEPARATION"
        assert notices[-1] == "Separation locked (0°)"
        assert separate_p._lock_btn.isEnabled()

    # Check hardware confirmation was not faked (position remains None without echo)
    assert door_p.actuator.actual is None
    assert deploy_p.actuator.actual is None
    assert separate_p.actuator.actual is None

    print("ALL ACTUATOR LOCK TESTS PASSED!")

if __name__ == "__main__":
    test_lock_actuators()
