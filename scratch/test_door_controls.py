import sys
from PyQt6.QtWidgets import QApplication
from ground_station.actuators import BY_ID, Actuator, ArmingCentre
from ground_station.pages.command import ActuatorPanel

def test_door_panel():
    app = QApplication.instance() or QApplication(sys.argv)

    spec = BY_ID["ACT_DOOR"]
    actuator = Actuator(spec)
    arming = ArmingCentre()

    panel = ActuatorPanel(spec, actuator, arming)
    panel.set_enabled_by_guard(True)

    # Check button existence
    assert hasattr(panel, "_lock_btn"), "Missing _lock_btn"
    assert hasattr(panel, "_send_btn"), "Missing _send_btn"
    for angle in (30, 60, 90):
        assert angle in panel._angle_buttons, f"Missing angle button {angle}"
        assert panel._angle_buttons[angle].text() == f"{angle}°"

    # Track emitted events
    angles_emitted = []
    locks_emitted = []
    sends_emitted = []

    panel.angle_requested.connect(lambda aid, a: angles_emitted.append((aid, a)))
    panel.lock_requested.connect(lambda aid: locks_emitted.append(aid))
    panel.send_command_requested.connect(lambda aid, cmd: sends_emitted.append((aid, cmd)))

    # 1. 30° click
    panel._angle_buttons[30].click()
    assert angles_emitted[-1] == ("ACT_DOOR", 30)
    assert panel.actuator.commanded == 30.0
    assert panel._commanded.text() == "30°"

    # 2. 60° click
    panel._angle_buttons[60].click()
    assert angles_emitted[-1] == ("ACT_DOOR", 60)
    assert panel.actuator.commanded == 60.0
    assert panel._commanded.text() == "60°"

    # 3. 90° click
    panel._angle_buttons[90].click()
    assert angles_emitted[-1] == ("ACT_DOOR", 90)
    assert panel.actuator.commanded == 90.0
    assert panel._commanded.text() == "90°"

    # 4. Lock click
    panel._lock_btn.click()
    assert locks_emitted[-1] == "ACT_DOOR"
    assert panel.actuator.commanded == 0.0
    assert panel._commanded.text() == "0°"

    # 5. Send click
    panel._send_btn.click()
    assert sends_emitted[-1] == ("ACT_DOOR", "TEST1")

    print("ALL DOOR PANEL TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_door_panel()
