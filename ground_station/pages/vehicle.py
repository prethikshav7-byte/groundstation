"""
ground_station/pages/vehicle.py
═══════════════════════════════
§9 — the Rocket and CanSat dashboards.

One class for both. §9.2 makes them identical except that the CanSat
labels velocity as "Descent Rate" (§6.3), and §9.6 gives both the same 8
states, so two near-identical classes would be two places for the same
bug. The only per-vehicle input is the axis spec for that one channel.

§9.7 is a removal, and it is worth stating that it was honoured: there
are no actuator controls here, and no layout space reserved where they
used to be. All actuation lives on the Command dashboard (Phase 3).

§7.1's "one selected graph at a time per dashboard" is enforced here
rather than in the graph — a graph cannot know about its siblings, and
putting the rule in the page is what keeps it true when the grid changes.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import axes as axspec
from ..derived import DerivedVelocity
from ..models import (
    FlightState, StateObservation, TelemetryPacket, VehicleID, VehicleLinkState,
)
from ..parser import IngestResult, LossTier
from ..graphs.plot import TelemetryGraph
from ..theme import ThemeManager, FS_HEADING, FS_CAPTION
from ..timeline import StateTimeline
from ..widgets.gauges import GaugeField
from ..widgets.telemetry_panel import TelemetryPanel


class VehiclePage(QWidget):
    """§9 — gauges, six graphs, telemetry panel, state timeline."""

    #: Emitted when a backward state transition is detected (§5.2), carrying
    #: a human-readable description.  Connected in app.py to the status bar
    #: so the operator sees the anomaly without having to scroll to the
    #: timeline panel.  A signal with no receiver would be worse than
    #: nothing — it looks like the feature exists when it doesn’t.
    backward_transition = pyqtSignal(str)

    def __init__(self, vehicle_id: VehicleID, parent=None):
        super().__init__(parent)
        self.vehicle_id = vehicle_id
        self.velocity = DerivedVelocity()
        self.timeline = StateTimeline()

        # §6.3 — per-vehicle constant label, decided once at construction
        # and never state-dependent.
        self.velocity_spec = (axspec.DESCENT_RATE
                              if vehicle_id is VehicleID.CANSAT
                              else axspec.VELOCITY)

        self.graphs: Dict[str, TelemetryGraph] = {}
        self.gauges: Dict[str, GaugeField] = {}
        self._selected: Optional[TelemetryGraph] = None
        self._analog = False
        # §12 — latest mission time received; drives the header timer label.
        self._latest_mission_time: Optional[float] = None

        self._build()
        self.panel.set_replay_target(self._draw_sample)

    # ── construction ─────────────────────────────────────────────────────

    def _build(self) -> None:
        c = ThemeManager.C()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        outer.addLayout(self._header())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)

        body_layout.addWidget(self._gauge_grid())

        self.panel = TelemetryPanel(self.vehicle_id.value)
        body_layout.addWidget(self.panel)

        body_layout.addWidget(self._graph_grid())
        body_layout.addWidget(self._timeline_panel())
        body_layout.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll)

    def _header(self) -> QHBoxLayout:
        c = ThemeManager.C()
        row = QHBoxLayout()
        title = QLabel(f"{self.vehicle_id.value} Dashboard")
        title.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {c.CYAN};")
        row.addWidget(title)

        # §12 — mission time is the axis everything is indexed against;
        # show it in the header so it is visible without scrolling to graphs.
        self._mission_clock = QLabel("T+ — s")
        self._mission_clock.setFont(QFont("Segoe UI", FS_HEADING, QFont.Weight.Bold))
        self._mission_clock.setStyleSheet(
            f"color: {c.CYAN}; padding: 0 16px;")
        row.addWidget(self._mission_clock)

        self._gnss_label = QLabel("GNSS: —")
        self._gnss_label.setFont(QFont("monospace", FS_CAPTION, QFont.Weight.Bold))
        self._gnss_label.setStyleSheet(f"color: {c.TEXT_DIM}; padding: 0 12px;")
        row.addWidget(self._gnss_label)

        row.addStretch()

        self._mode_button = QPushButton("Show analog gauges")
        self._mode_button.setFont(QFont("monospace", FS_CAPTION))
        self._mode_button.setStyleSheet(
            f"QPushButton {{ background-color: {c.BG_INPUT}; color: {c.TEXT}; "
            f"border: 1px solid {c.BORDER}; border-radius: 4px; "
            f"padding: 4px 10px; }}")
        self._mode_button.clicked.connect(self._toggle_gauge_mode)
        row.addWidget(self._mode_button)

        reset = QPushButton("Reset graph view")
        reset.setFont(QFont("monospace", FS_CAPTION))
        reset.setStyleSheet(self._mode_button.styleSheet())
        reset.clicked.connect(self.reset_views)
        row.addWidget(reset)
        return row

    def _gauge_grid(self) -> QWidget:
        """§9.1 — every telemetry field from §4.1, units on every field."""
        w = QWidget()
        grid = QGridLayout(w)
        grid.setSpacing(8)
        grid.setContentsMargins(0, 0, 0, 0)

        fields = [
            ("altitude",        axspec.ALTITUDE,        2),
            ("velocity",        self.velocity_spec,     2),
            ("pressure",        axspec.PRESSURE,        1),
            ("temperature",     axspec.TEMPERATURE,     2),
            ("battery_voltage", axspec.BATTERY,         2),
            ("rssi",            axspec.RSSI,            0),
            ("accel_magnitude", axspec.ACCEL_MAGNITUDE, 2),
        ]
        for i, (key, spec, dp) in enumerate(fields):
            g = GaugeField(spec, decimals=dp, analog=self._analog)
            self.gauges[key] = g
            grid.addWidget(g, i // 3, i % 3)
        return w

    def _graph_grid(self) -> QWidget:
        """§9.2 — all six graphs per vehicle, per §7."""
        c = ThemeManager.C()
        w = QWidget()
        grid = QGridLayout(w)
        grid.setSpacing(8)
        grid.setContentsMargins(0, 0, 0, 0)

        xyz = [("X", c.RED), ("Y", c.GREEN), ("Z", c.CYAN)]
        specs = [
            ("altitude",        axspec.ALTITUDE,        None),
            ("velocity",        self.velocity_spec,     None),
            ("temperature",     axspec.TEMPERATURE,     None),
            ("accel_magnitude", axspec.ACCEL_MAGNITUDE, None),
            ("pressure",        axspec.PRESSURE,        None),
            ("orientation",     axspec.ORIENTATION,     None),
        ]
        for i, (key, spec, traces) in enumerate(specs):
            g = TelemetryGraph(key, spec=spec, traces=traces)
            # §20 — minimum height is now set by TelemetryGraph itself (230 px);
            # no override needed here.
            g.selection_changed.connect(self._on_selection)
            self.graphs[key] = g
            grid.addWidget(g, i // 2, i % 2)
        return w

    def _timeline_panel(self) -> QWidget:
        """§5.5 — current state plus when each was first observed.

        Replaced the \\n-joined QLabel with a QTableWidget so that
        observation type and anomaly flags can be colour-coded per row,
        making the post-flight debugging artefact actually readable.
        """
        c = ThemeManager.C()
        w = QWidget()
        w.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        head = QLabel("Flight State Timeline")
        head.setFont(QFont("monospace", FS_HEADING - 2, QFont.Weight.Bold))
        head.setStyleSheet(f"color: {c.CYAN};")
        lay.addWidget(head)

        # Four columns: T+ | State | How it was known | Anomaly flags
        self._timeline_table = QTableWidget(0, 4)
        self._timeline_table.setHorizontalHeaderLabels(
            ["T+ (s)", "State", "How", "Flags"])
        self._timeline_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed)
        self._timeline_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Fixed)
        self._timeline_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self._timeline_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        self._timeline_table.setColumnWidth(0, 80)
        self._timeline_table.setColumnWidth(1, 110)
        self._timeline_table.verticalHeader().setVisible(False)
        self._timeline_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._timeline_table.setSelectionMode(
            QTableWidget.SelectionMode.NoSelection)
        self._timeline_table.setAlternatingRowColors(False)
        self._timeline_table.setFont(QFont("monospace", FS_CAPTION))
        self._timeline_table.setStyleSheet(
            f"QTableWidget {{ background-color: {c.BG_CARD}; "
            f"color: {c.TEXT}; border: none; "
            f"gridline-color: {c.BORDER}; }}"
            f"QHeaderView::section {{ background-color: {c.BG_INPUT}; "
            f"color: {c.TEXT_DIM}; border: none; "
            f"padding: 3px; font-size: {FS_CAPTION}pt; }}")
        self._timeline_table.setMinimumHeight(140)
        lay.addWidget(self._timeline_table)
        return w

    # ── §7.1 one selection per dashboard ─────────────────────────────────

    def _on_selection(self, graph: TelemetryGraph, on: bool) -> None:
        if on:
            if self._selected is not None and self._selected is not graph:
                self._selected.set_selected(False)
            self._selected = graph
        elif self._selected is graph:
            self._selected = None

    def all_graphs(self) -> List[TelemetryGraph]:
        return list(self.graphs.values())

    def reset_views(self) -> None:
        for g in self.graphs.values():
            g.reset_view()

    def _toggle_gauge_mode(self) -> None:
        self._analog = not self._analog
        for g in self.gauges.values():
            g.set_analog(self._analog)
        self._mode_button.setText("Show digital gauges" if self._analog
                                  else "Show analog gauges")

    # ── data in ──────────────────────────────────────────────────────────

    def receive(self, packet: TelemetryPacket,
                result: IngestResult) -> Optional[float]:
        """One accepted packet for this vehicle.

        Returns the derived velocity (§6.4) for this sample, or None while
        the window is still filling. Returned rather than recomputed by
        the caller because there must be exactly one estimator per
        vehicle: a second one on the Overview page would hold a different
        window and could show a different number for the same instant,
        which is the sort of disagreement §1.3 exists to prevent.
        """
        # §12 — update mission timer before anything else so the header is
        # always in sync with the data arriving below.
        self._latest_mission_time = packet.mission_time
        self._mission_clock.setText(f"T+{packet.mission_time:,.1f} s")

        # §6.4 — the derived window must not span a gap.
        if result.tier in (LossTier.GAP, LossTier.OUTAGE):
            self.velocity.on_gap()
        v = self.velocity.add(packet.mission_time, packet.altitude)

        # §5.2 — backward transitions are displayed with an anomaly flag,
        # never suppressed.  observe() returns every entry it appended;
        # we surface backward ones immediately via the backward_transition
        # signal so the status bar catches them without the operator
        # having to scroll to the timeline panel.
        for entry in self.timeline.observe(packet.state, packet.mission_time):
            if entry.backward:
                self.backward_transition.emit(
                    f"⚠ BACKWARD TRANSITION: {packet.state.value} "
                    f"at T+{packet.mission_time:.2f} s — see timeline")
        self._refresh_timeline()

        # Gauges are throttled internally (§6.1) so they are fed every
        # packet; the widget decides when to repaint.
        self.gauges["altitude"].set_value(packet.altitude)
        v_final = packet.velocity if packet.velocity is not None else v
        self.gauges["velocity"].set_value(v_final)
        self.gauges["pressure"].set_value(packet.pressure)
        self.gauges["temperature"].set_value(packet.temperature)
        self.gauges["battery_voltage"].set_value(packet.battery_voltage)
        self.gauges["rssi"].set_value(packet.rssi)

        # GNSS status display (≥ 6 satellites = minimum reached, < 6 = insufficient)
        sats = packet.gnss_satellites
        fix_valid = packet.gnss_fix == 1 if hasattr(packet, "gnss_fix") else (sats > 0)
        sim_tag = " (SIM)" if getattr(packet, "is_simulation", False) else ""
        c = ThemeManager.C()
        if sats >= 6 and fix_valid and (packet.gnss_latitude != 0.0 or packet.gnss_longitude != 0.0):
            self._gnss_label.setText(
                f"GNSS{sim_tag}: {sats} SAT (LOCK) · {packet.gnss_latitude:.6f}°, {packet.gnss_longitude:.6f}° · {packet.gnss_altitude:.1f} m"
            )
            self._gnss_label.setStyleSheet(f"color: {c.GREEN}; font-weight: bold; padding: 0 12px;")
        elif sats > 0:
            status_text = f"INSUFFICIENT ({sats} SAT < 6)" if sats < 6 else "NO FIX"
            self._gnss_label.setText(
                f"GNSS{sim_tag}: {sats} SAT ({status_text})"
            )
            self._gnss_label.setStyleSheet(f"color: {c.AMBER}; font-weight: bold; padding: 0 12px;")
        else:
            self._gnss_label.setText(f"GNSS{sim_tag}: NO FIX (0 SAT)")
            self._gnss_label.setStyleSheet(f"color: {c.TEXT_DIM}; padding: 0 12px;")

        # Acceleration processing
        accel_val = packet.accelerometer
        if accel_val is None and packet.raw_accelerometer is None and (packet.accel_x != 0.0 or packet.accel_y != 0.0 or packet.accel_z != 0.0):
            accel_val = math.sqrt(
                packet.accel_x ** 2 + packet.accel_y ** 2 + packet.accel_z ** 2)
        if accel_val is not None:
            self.gauges["accel_magnitude"].set_value(accel_val)

        t = packet.mission_time
        spin_val = packet.gyro_spin_rate if packet.gyro_spin_rate != 0.0 else packet.gyro_z
        samples = [
            ("altitude",        "", t, packet.altitude),
            ("temperature",     "", t, packet.temperature),
            ("pressure",        "", t, packet.pressure),
            ("orientation",     "", t, spin_val),
        ]
        if accel_val is not None:
            samples.append(("accel_magnitude", "", t, accel_val))
        if v_final is not None:
            samples.append(("velocity", "", t, v_final))

        for key, trace, x, y in samples:
            sample = (key, trace, x, y, result.tier, packet.state, v)
            # §9.4 — pause is display-only. The packet is already parsed,
            # counted and logged; only the draw is deferred.
            if self.panel.accept(sample):
                self._draw_sample(sample)

        return v

    def _draw_sample(self, sample) -> None:
        key, trace, x, y, tier, state, v_entry = sample
        g = self.graphs.get(key)
        if g is not None:
            g.add_point(x, y, trace=trace, tier=tier, state=state,
                        v_entry=v_entry)

    def set_link_state(self, state: VehicleLinkState) -> None:
        self.panel.set_link_state(state)

    def _refresh_timeline(self) -> None:
        """Rebuild the timeline table from the full entry list.

        All entries are shown (not just the last 8) with auto-scroll to the
        bottom, because §5.5 calls this the primary post-flight debugging
        artefact — operators need the complete history, not a tail.

        Row colouring:
          backward=True      red-tinted background (#2a1515)
          INFERRED           muted text (TEXT_MUTED)
          RESTORED           amber text (AMBER)
          normal OBSERVED    default text (TEXT)
        """
        c = ThemeManager.C()
        t = self._timeline_table
        entries = self.timeline.entries

        # Grow the table if new rows have arrived; never shrink (entries are
        # append-only in StateTimeline).
        current_rows = t.rowCount()
        if len(entries) == current_rows:
            return   # nothing new

        t.setRowCount(len(entries))
        for row, e in enumerate(entries):
            if row < current_rows:
                continue   # already rendered

            # ── T+ ────────────────────────────────────────────────────────
            time_item = QTableWidgetItem(f"{e.mission_time:,.2f}")
            time_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            # ── State ─────────────────────────────────────────────────────
            state_item = QTableWidgetItem(e.state.value)

            # ── How ───────────────────────────────────────────────────────
            how_text = e.observation.value
            if e.time_uncertainty is not None:
                # §10.3 — worst-case timestamp error next to RESTORED marker.
                how_text += f"  (±{e.time_uncertainty:.2f} s)"
            how_item = QTableWidgetItem(how_text)

            # ── Flags ─────────────────────────────────────────────────────
            flags: list[str] = []
            if e.backward:
                flags.append("⚠ BACKWARD TRANSITION")
            if self.timeline.continuity_broken and e is entries[-1]:
                flags.append("⚠ CONTINUITY BROKEN")
            flag_item = QTableWidgetItem("  ".join(flags))

            # ── Per-row colour ────────────────────────────────────────────
            if e.backward:
                bg = QBrush(QColor("#2a1515"))   # red tint
                fg = QBrush(QColor(c.RED))
            elif e.observation is StateObservation.INFERRED:
                bg = QBrush(QColor(c.BG_CARD))
                fg = QBrush(QColor(c.TEXT_MUTED))
            elif e.observation is StateObservation.RESTORED:
                bg = QBrush(QColor(c.BG_CARD))
                fg = QBrush(QColor(c.AMBER))
            else:
                bg = QBrush(QColor(c.BG_CARD))
                fg = QBrush(QColor(c.TEXT))

            for item in (time_item, state_item, how_item, flag_item):
                item.setBackground(bg)
                item.setForeground(fg)

            t.setItem(row, 0, time_item)
            t.setItem(row, 1, state_item)
            t.setItem(row, 2, how_item)
            t.setItem(row, 3, flag_item)
            t.setRowHeight(row, 22)

        # Auto-scroll to the most recent entry.
        t.scrollToBottom()

    def clear_graphs(self) -> None:
        for g in self.graphs.values():
            g.clear()
