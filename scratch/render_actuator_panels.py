import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication
from ground_station.app import GroundStationApp

def main():
    app = QApplication.instance() or QApplication(sys.argv)
    window = GroundStationApp(mode_label="LIVE", simulator_mode=False)
    window._navigate(3) # Navigate to Command Page
    window.resize(1560, 960)
    window.show()

    # Process events to lay out widgets
    app.processEvents()

    # Capture each actuator panel
    for act_id, name in [("ACT_DOOR", "door"), ("ACT_DEPLOY", "deploy"), ("ACT_SEPARATE", "separate")]:
        panel = window.command_page.actuator_panels[act_id]
        pixmap = panel.grab()
        out_path = os.path.join(os.path.dirname(__file__), f"actuator_{name}_lock_unlock.png")
        pixmap.save(out_path)
        print(f"Saved {out_path}")

if __name__ == "__main__":
    main()
