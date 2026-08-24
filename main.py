#!/usr/bin/env python3
"""
main.py — Ground Station entry point
════════════════════════════════════
§2 — one entry point for both modes. The old split between main.py (live)
and main_sim.py (simulator) is gone: §2.1 puts the choice on a
mode-selection screen at startup, and §2.2 requires both modes to share
the same dashboard, rendering, parsing and reconciliation code.

Keeping two entry files was how the previous build ended up with a
simulator calling a method signature the simulator class did not have —
the two paths were only ever exercised separately. One path, chosen at
runtime, cannot drift like that.

Usage:
    python3 main.py                 # mode-selection screen (§2.1)
    python3 main.py --sim           # skip the screen, go straight to sim
    python3 main.py --port COM5     # skip the screen, connect directly

The two flags are a development convenience. They do not create a third
mode: they preselect one of the same two and take the same code path.
"""
from __future__ import annotations

import argparse
import sys

from PyQt6.QtWidgets import QApplication, QMessageBox

from ground_station.app import GroundStationApp
from ground_station.launcher import LaunchChoice, LaunchMode, LauncherDialog
from ground_station.link import LinkSupervisor, SERIAL_AVAILABLE
from ground_station.logs.raw_log import SessionLogs
from ground_station.simulator.source import SimulatedSource
from ground_station.theme import ThemeManager


def _choose(app: QApplication, args) -> LaunchChoice | None:
    if args.sim:
        return LaunchChoice(LaunchMode.SIMULATOR)
    if args.port:
        return LaunchChoice(LaunchMode.LIVE, stable_id=f"DEV:{args.port}",
                            device=args.port, baud=args.baud)

    dlg = LauncherDialog()
    if dlg.exec() != LauncherDialog.DialogCode.Accepted:
        return None
    return dlg.choice


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true",
                    help="skip the mode screen and run the simulator")
    ap.add_argument("--port", help="skip the mode screen and connect to a port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--log-dir", default="logs",
                    help="where session logs are written (§12)")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("Ground Station")
    app.setStyleSheet(ThemeManager.main_stylesheet())

    choice = _choose(app, args)
    if choice is None:
        return 0

    simulator = choice.mode is LaunchMode.SIMULATOR

    if not simulator and not SERIAL_AVAILABLE:
        QMessageBox.critical(
            None, "pyserial not installed",
            "Live mode needs pyserial.\n\nInstall it with:\n"
            "    pip install pyserial")
        return 1

    # §2.5 — "Simulator Mode" is the app-side thing. The vehicle-side
    # telecommand is "Flight Software Simulation" and never carries this
    # label; ENTER_SIMULATION is disabled while this flag is set.
    window = GroundStationApp(
        mode_label="SIMULATOR MODE" if simulator else "LIVE",
        simulator_mode=simulator)

    # §12 — logging starts before the source does, so the first line to
    # arrive is already being recorded. §12.2 makes the log the flight
    # record, and a record that starts a beat late is missing the moment
    # a bad connection usually announces itself.
    logs = SessionLogs(args.log_dir)

    # §2.2 — both objects expose the same signals, so nothing below this
    # line branches on which mode is running.
    source = SimulatedSource() if simulator else LinkSupervisor()
    _attach_logging(source, logs)
    window.connect_supervisor(source)

    if simulator:
        source.connect_to()
        source.fault_injected.connect(
            lambda kind, note: logs.raw.write_event(f"SIM:{kind}", note))
    else:
        source.connect_to(choice.stable_id, choice.baud, choice.device)

    window.statusBar().showMessage(f"Logging to {logs.directory}", 10000)
    window.show()
    window.activateWindow()
    window.raise_()

    code = app.exec()
    source.disconnect_source()
    logs.close()
    return code


def _attach_logging(source, logs: SessionLogs) -> None:
    """§12.1 — every received line, verbatim, before anything judges it.

    Wired to raw_line rather than to the per-vehicle packet signals on
    purpose: raw_line fires on receipt, ahead of the demultiplexer, so
    malformed lines reach the log even though they never become packets
    (§4.5). Wiring to the packet signals would produce a log containing
    only the lines that parsed, which cannot explain a flight where the
    parsing was the problem.
    """
    from ground_station.models import VehicleID

    def on_raw(line: str, received_at: float) -> None:
        vehicle = None
        # Cheap attribution for the log only. A wrong guess costs one
        # mis-filed line and never touches a displayed value.
        if ",ROCKET," in line:
            vehicle = VehicleID.ROCKET
        elif ",CANSAT," in line:
            vehicle = VehicleID.CANSAT
        logs.raw.write_line(line, received_at, vehicle)

    source.raw_line.connect(on_raw)
    source.line_rejected.connect(logs.raw.write_rejected)
    source.notice.connect(lambda msg: logs.raw.write_event("NOTICE", msg))
    source.source_state_changed.connect(
        lambda st: logs.raw.write_event("SOURCE", st.value))
    source.receiver_state_changed.connect(
        lambda st: logs.raw.write_event("RECEIVER", st.value))
    source.vehicle_state_changed.connect(
        lambda vid, st: logs.raw.write_event("VEHICLE", f"{vid.value} {st.value}"))


if __name__ == "__main__":
    sys.exit(main())
