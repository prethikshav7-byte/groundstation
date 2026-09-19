import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication
from ground_station.app import GroundStationApp

def main():
    app = QApplication.instance() or QApplication(sys.argv)
    window = GroundStationApp(mode_label="LIVE", simulator_mode=False)
    window._navigate(3) # Command page
    window.resize(1560, 1100)
    window.show()

    app.processEvents()
    pixmap = window.grab()
    out_path = os.path.join(os.path.dirname(__file__), "full_command_page.png")
    pixmap.save(out_path)
    print(f"Saved {out_path}")

if __name__ == "__main__":
    main()
