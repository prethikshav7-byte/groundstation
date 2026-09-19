import sys
import os
# pyrefly: ignore [missing-import]
from PyQt6.QtWidgets import QApplication
# pyrefly: ignore [missing-import]
from PyQt6.QtCore import Qt

# Ensure ground_station is importable
sys.path.insert(0, os.path.abspath("."))

from ground_station.app import GroundStationApp
from ground_station.graphs.plot import TelemetryGraph
from ground_station.experiment.profile_plot import AerosolProfilePlot, SecondaryPlot
from ground_station.theme import ThemeManager

def test_graph_controls():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setStyleSheet(ThemeManager.main_stylesheet())

    win = GroundStationApp(mode_label="SIMULATOR", simulator_mode=True)
    win.resize(1560, 960)
    win.show()

    # 1. Overview Page
    overview = win.overview_page
    overview_graphs = overview.all_graphs()
    print(f"Overview graphs count: {len(overview_graphs)}")
    assert len(overview_graphs) == 12, f"Expected 12 graphs on Overview, found {len(overview_graphs)}"
    for g in overview_graphs:
        assert hasattr(g, "btn_reset"), "Overview graph missing btn_reset"
        assert hasattr(g, "btn_zoom_out"), "Overview graph missing btn_zoom_out"
        assert hasattr(g, "btn_zoom_in"), "Overview graph missing btn_zoom_in"

    # Test zoom actions on first graph with data
    g0 = overview_graphs[0]
    for t in range(1, 100):
        g0.add_point(float(t), 10.0 + float(t))
    
    mission_span = max(g0._latest_x, g0.zoom_spec.max_zoom_span_s)
    print(f"Mission span with data (latest_x={g0._latest_x}): {mission_span}")
    
    # Zoom in
    g0.btn_zoom_in.click()
    print(f"After zoom in span: {g0._x_span}")
    assert g0._x_span < mission_span, f"Expected span to decrease from mission span, got {g0._x_span} >= {mission_span}"
    assert g0._follow is False

    # Zoom in again
    span_1 = g0._x_span
    g0.btn_zoom_in.click()
    print(f"After second zoom in span: {g0._x_span}")
    assert g0._x_span < span_1, f"Expected span to decrease further, got {g0._x_span} >= {span_1}"

    # Zoom out
    span_2 = g0._x_span
    g0.btn_zoom_out.click()
    print(f"After zoom out span: {g0._x_span}")
    assert g0._x_span > span_2, f"Expected span to increase, got {g0._x_span} <= {span_2}"

    # Reset
    g0.btn_reset.click()
    print(f"After reset span: {g0._x_span}, follow: {g0._follow}")
    assert g0._follow is True
    assert g0._x_span == mission_span

    # 2. Rocket Page
    rocket_graphs = win.rocket_page.all_graphs()
    print(f"Rocket graphs count: {len(rocket_graphs)}")
    assert len(rocket_graphs) == 6
    for g in rocket_graphs:
        assert hasattr(g, "btn_reset")
        assert hasattr(g, "btn_zoom_out")
        assert hasattr(g, "btn_zoom_in")

    # 3. CanSat Page
    cansat_graphs = win.cansat_page.all_graphs()
    print(f"CanSat graphs count: {len(cansat_graphs)}")
    assert len(cansat_graphs) == 6
    for g in cansat_graphs:
        assert hasattr(g, "btn_reset")
        assert hasattr(g, "btn_zoom_out")
        assert hasattr(g, "btn_zoom_in")

    # 4. Experiment Page
    exp = win.experiment_page
    assert hasattr(exp.profile, "btn_reset")
    assert hasattr(exp.profile, "btn_zoom_out")
    assert hasattr(exp.profile, "btn_zoom_in")

    assert hasattr(exp.alt_time, "btn_reset")
    assert hasattr(exp.alt_time, "btn_zoom_out")
    assert hasattr(exp.alt_time, "btn_zoom_in")

    assert hasattr(exp.aero_time, "btn_reset")
    assert hasattr(exp.aero_time, "btn_zoom_out")
    assert hasattr(exp.aero_time, "btn_zoom_in")

    # Test clicks on experiment plots
    exp.profile.btn_zoom_in.click()
    exp.profile.btn_zoom_out.click()
    exp.profile.btn_reset.click()

    exp.alt_time.btn_zoom_in.click()
    exp.alt_time.btn_zoom_out.click()
    exp.alt_time.btn_reset.click()

    exp.aero_time.btn_zoom_in.click()
    exp.aero_time.btn_zoom_out.click()
    exp.aero_time.btn_reset.click()

    os.makedirs("scratch", exist_ok=True)
    win.overview_page.grab().save("scratch/overview_page.png")
    win.rocket_page.grab().save("scratch/rocket_page.png")
    win.cansat_page.grab().save("scratch/cansat_page.png")
    win.experiment_page.grab().save("scratch/experiment_page.png")
    print("All UI tests passed successfully and screenshots saved!")

if __name__ == "__main__":
    test_graph_controls()
