import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication
from ground_station.app import GroundStationApp
from ground_station.simulator.source import SimulatedSource

app = QApplication.instance() or QApplication(sys.argv)
win = GroundStationApp(mode_label='SIMULATOR', simulator_mode=True)
win.resize(1560, 960)
win.show()

source = SimulatedSource()
win.connect_supervisor(source)

for out in source.engine.run(28.0):
    for line in out.lines:
        r = source.demux.feed(line)
        if r is not None and r.accepted:
            if r.packet.vehicle_id.value == 'ROCKET':
                source.rocket_packet.emit(r.packet, r)
            else:
                source.cansat_packet.emit(r.packet, r)
    app.processEvents()

for _ in range(10):
    app.processEvents()
    time.sleep(0.02)

save_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "continuous_sim_overview.png")
pix = win.overview_page.grab()
pix.save(save_path)
print(f"Saved to: {save_path}, exists: {os.path.exists(save_path)}")
