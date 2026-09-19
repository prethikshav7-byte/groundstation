"""
ground_station/pages/overview.py
════════════════════════════════
§6.2 — both vehicles' six channels side by side, plus the §6.5 health
strip.

§6.2 lists Altitude, Velocity/Descent Rate, Temperature, Acceleration,
Pressure and Orientation for each vehicle. That is twelve live graphs on
one page, which is why §13.2's decimation is not optional here: at full
mission zoom this page alone is drawing most of the 940,000 points the
spec warns about.

The graphs on this page are separate instances from the ones on the
vehicle dashboards, with their own buffers. Sharing a widget between two
pages would tie their zoom and selection state together, and §7.1's "one
selected graph at a time per dashboard" would then leak across
dashboards.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from .. import axes as axspec
from ..models import TelemetryPacket, VehicleID
from ..parser import IngestResult, LossTier
from ..graphs.plot import TelemetryGraph
from ..theme import ThemeManager
from ..widgets.health_strip import HealthStrip


class OverviewPage(QWidget):
    """§6.2 + §6.5."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.graphs: Dict[VehicleID, Dict[str, TelemetryGraph]] = {
            v: {} for v in VehicleID
        }
        self._selected: Optional[TelemetryGraph] = None
        self._build()

    def _build(self) -> None:
        c = ThemeManager.C()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # §6.5 — all three levels from §3.6 visible at once, pinned to the
        # top so they are never scrolled out of view during a flight.
        self.health = HealthStrip()
        outer.addWidget(self.health)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(12)

        for vid in VehicleID:
            lay.addWidget(self._vehicle_block(vid))
        lay.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll)

    def _vehicle_block(self, vid: VehicleID) -> QWidget:
        c = ThemeManager.C()
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        head = QLabel(vid.value)
        head.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        head.setStyleSheet(f"color: {c.CYAN};")
        lay.addWidget(head)

        grid = QGridLayout()
        grid.setSpacing(8)

        # §6.3 — the CanSat's is labelled Descent Rate for the entire
        # flight, the Rocket's Velocity for the entire flight.
        vspec = (axspec.DESCENT_RATE if vid is VehicleID.CANSAT
                 else axspec.VELOCITY)
        xyz = [("X", c.RED), ("Y", c.GREEN), ("Z", c.CYAN)]

        specs = [
            ("altitude", axspec.ALTITUDE, None),
            ("velocity", vspec, None),
            ("temperature", axspec.TEMPERATURE, None),
            ("acceleration", axspec.ACCELERATION, xyz),
            ("pressure", axspec.PRESSURE, None),
            ("orientation", axspec.ORIENTATION, None),
        ]
        for i, (key, spec, traces) in enumerate(specs):
            g = TelemetryGraph(key, spec=spec, traces=traces)
            # §20 — minimum height is now set by TelemetryGraph itself (230 px);
            # no override needed here.
            g.selection_changed.connect(self._on_selection)
            self.graphs[vid][key] = g
            grid.addWidget(g, i // 3, i % 3)

        holder = QWidget()
        holder.setLayout(grid)
        lay.addWidget(holder)
        return w

    # ── §7.1 ─────────────────────────────────────────────────────────────

    def _on_selection(self, graph: TelemetryGraph, on: bool) -> None:
        if on:
            if self._selected is not None and self._selected is not graph:
                self._selected.set_selected(False)
            self._selected = graph
        elif self._selected is graph:
            self._selected = None

    def all_graphs(self) -> List[TelemetryGraph]:
        out: List[TelemetryGraph] = []
        for d in self.graphs.values():
            out.extend(d.values())
        return out

    # ── data in ──────────────────────────────────────────────────────────

    def receive(self, packet: TelemetryPacket, result: IngestResult,
                derived_velocity: Optional[float]) -> None:
        g = self.graphs[packet.vehicle_id]
        t = packet.mission_time
        tier, state = result.tier, packet.state
        v_final = packet.velocity if packet.velocity is not None else derived_velocity

        g["altitude"].add_point(t, packet.altitude, tier=tier, state=state,
                                v_entry=v_final)
        g["temperature"].add_point(t, packet.temperature, tier=tier, state=state)
        g["pressure"].add_point(t, packet.pressure, tier=tier, state=state)
        accel_val = packet.accelerometer
        if accel_val is not None:
            g["acceleration"].add_point(t, accel_val, tier=tier, state=state)
        elif packet.accel_x != 0.0 or packet.accel_y != 0.0 or packet.accel_z != 0.0:
            for axis, val in (("X", packet.accel_x), ("Y", packet.accel_y),
                              ("Z", packet.accel_z)):
                g["acceleration"].add_point(t, val, trace=axis, tier=tier, state=state)
        spin_rate = packet.gyro_spin_rate if packet.gyro_spin_rate != 0.0 else packet.gyro_z
        g["orientation"].add_point(t, spin_rate, tier=tier, state=state)
        if v_final is not None:
            g["velocity"].add_point(t, v_final, tier=tier, state=state)

    def clear_graphs(self) -> None:
        for graph in self.all_graphs():
            graph.clear()
