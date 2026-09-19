import sys
import os
sys.path.insert(0, os.path.abspath("."))

from PyQt6.QtWidgets import QApplication
from ground_station.actuators import BY_ID, Actuator, ArmingCentre
from ground_station.pages.command import ActuatorPanel

def test_all_panels():
    app = QApplication.instance() or QApplication(sys.argv)

    arming = ArmingCentre()

    for act_id in ("ACT_DOOR", "ACT_DEPLOY", "ACT_SEPARATE"):
        spec = BY_ID[act_id]
        actuator = Actuator(spec)
        panel = ActuatorPanel(spec, actuator, arming)
        panel.set_enabled_by_guard(True)

        assert hasattr(panel, "_lock_btn"), f"{act_id} missing _lock_btn"
        assert hasattr(panel, "_send_btn"), f"{act_id} missing _send_btn"
        for angle in (30, 60, 90):
            assert angle in panel._angle_buttons, f"{act_id} missing angle button {angle}"
            assert panel._angle_buttons[angle].text() == f"{angle}°"

    print("ALL PANELS (DOOR + DEPLOY + SEPARATE) VERIFIED SUCCESSFULLY!")

if __name__ == "__main__":
    test_all_panels()
