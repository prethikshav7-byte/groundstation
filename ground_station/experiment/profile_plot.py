"""
ground_station/experiment/profile_plot.py
═════════════════════════════════════════
§11.10–§11.12 — the aerosol profile plot.

Explicitly NOT a time series, so §7's rules mostly do not apply and §11.10
says so directly. The differences that matter:

  §11.10  pinch zooms BOTH axes together about the pinch centre, not a
          time window. Scroll still never zooms.
  §11.10  at a time gap the line breaks — no interpolation, and never
          §7.5's ballistic reconciliation, "which is meaningless for a
          science profile".
  §11.11  the record crosses each altitude twice, so it is split at peak
          altitude into two visually distinct traces. One continuous line
          "implies a single profile that does not exist".
  §11.10  axis-swap toggle, defaulting to the specified orientation
          (aerosol on y, altitude on x).

The swap toggle is worth the twenty lines it costs. Atmospheric vertical
profiles are conventionally drawn with altitude on the y-axis so the plot
reads the way the vehicle flew, and a reviewer or report template may
expect that. Without the toggle someone re-plots it by hand later, from
the exported CSV, and the figure in the report stops matching the one in
the app.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ..theme import ThemeManager
from .parser import ExperimentDataset, split_on_time_gaps


class ProfileViewBox(pg.ViewBox):
    """§11.10 — pinch zooms both axes together; scroll never zooms."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.selected = False
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)

    def wheelEvent(self, ev, axis=None):
        # Same rule as the telemetry graphs: accepted rather than ignored
        # so it cannot bubble to a parent scroll area and move the page.
        ev.accept()


class AerosolProfilePlot(QWidget):
    """Aerosol count against altitude, split into ascent and descent."""

    #: §11.10 — max zoom is 5 m of altitude; min zoom is the data extent.
    MIN_ALTITUDE_SPAN_M = 5.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dataset: Optional[ExperimentDataset] = None
        self._swapped = False
        self._show_ascent = True
        self._show_descent = True
        self._selected = False

        c = ThemeManager.C()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        self.vb = ProfileViewBox()
        self.plot = pg.PlotWidget(viewBox=self.vb, background=c.BG_CARD)
        self.plot.setAntialiasing(True)
        self.plot.showGrid(x=True, y=True, alpha=0.12)

        for side in ("left", "bottom"):
            ax = self.plot.getAxis(side)
            ax.enableAutoSIPrefix(False)
            ax.setPen(pg.mkPen(c.BORDER))
            ax.setTextPen(pg.mkPen(c.TEXT_DIM))
            ax.setStyle(tickFont=QFont("monospace", 8))
        self.plot.getAxis("left").setWidth(74)
        self.plot.getAxis("bottom").setHeight(40)

        # §11.11 — two visually distinct traces, identified inline per
        # §7.4 rather than by a legend box.
        self._ascent = self.plot.plot(
            [], [], pen=pg.mkPen(QColor(c.CYAN), width=2),
            symbol="o", symbolSize=4, symbolBrush=QColor(c.CYAN),
            connect="finite")
        self._descent = self.plot.plot(
            [], [], pen=pg.mkPen(QColor(c.AMBER), width=2),
            symbol="t", symbolSize=4, symbolBrush=QColor(c.AMBER),
            connect="finite")

        self._ascent_label = pg.TextItem("ascent", color=c.CYAN, anchor=(0, 1))
        self._descent_label = pg.TextItem("descent", color=c.AMBER, anchor=(0, 0))
        self._ascent_label.setFont(QFont("monospace", 8))
        self._descent_label.setFont(QFont("monospace", 8))
        self.plot.addItem(self._ascent_label)
        self.plot.addItem(self._descent_label)

        # Graph control toolbar (Reset, Zoom Out, Zoom In)
        tb = QHBoxLayout()
        tb.setContentsMargins(0, 0, 2, 2)
        tb.setSpacing(3)
        tb.addStretch()

        btn_style = ThemeManager.graph_button_stylesheet()

        self.btn_reset = QPushButton("⟲")
        self.btn_reset.setToolTip("Reset view (auto-scale)")
        self.btn_reset.setStyleSheet(btn_style)
        self.btn_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reset.clicked.connect(lambda: self.reset_view())

        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.setToolTip("Zoom out (-)")
        self.btn_zoom_out.setStyleSheet(btn_style)
        self.btn_zoom_out.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_zoom_out.clicked.connect(lambda: self.zoom_out())

        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setToolTip("Zoom in (+)")
        self.btn_zoom_in.setStyleSheet(btn_style)
        self.btn_zoom_in.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_zoom_in.clicked.connect(lambda: self.zoom_in())

        tb.addWidget(self.btn_reset)
        tb.addWidget(self.btn_zoom_out)
        tb.addWidget(self.btn_zoom_in)
        lay.addLayout(tb)

        lay.addWidget(self.plot)
        self.grabGesture(Qt.GestureType.PinchGesture)
        self._apply_axis_titles()

    # ── configuration ────────────────────────────────────────────────────

    def set_dataset(self, dataset: Optional[ExperimentDataset]) -> None:
        # §11.8 — loading a new file fully replaces the previous dataset.
        # No merging: two flights on one plot is a figure nobody can read
        # and nobody notices is wrong.
        self.dataset = dataset
        self.redraw()

    def set_swapped(self, on: bool) -> None:
        self._swapped = on
        self._apply_axis_titles()
        self.redraw()

    def set_legs(self, ascent: bool, descent: bool) -> None:
        self._show_ascent = ascent
        self._show_descent = descent
        self.redraw()

    def _apply_axis_titles(self) -> None:
        # §11.10 / §7.4 — axis titles outside the plot area, no legend.
        c = ThemeManager.C()
        style = {"color": c.TEXT_DIM, "font-size": "9pt"}
        aerosol = "Aerosol Count (particles/cm³)"
        altitude = "Altitude (m AGL)"
        if self._swapped:
            self.plot.setLabel("left", altitude, **style)
            self.plot.setLabel("bottom", aerosol, **style)
        else:
            self.plot.setLabel("left", aerosol, **style)
            self.plot.setLabel("bottom", altitude, **style)

    # ── drawing ──────────────────────────────────────────────────────────

    def redraw(self) -> None:
        ds = self.dataset
        if ds is None or ds.empty:
            self._ascent.setData([], [])
            self._descent.setData([], [])
            return

        ascent_r, descent_r = ds.split_at_peak()
        self._draw_leg(self._ascent, self._ascent_label, ds, ascent_r,
                       self._show_ascent, "ascent")
        self._draw_leg(self._descent, self._descent_label, ds, descent_r,
                       self._show_descent, "descent")
        self._autoscale()

    def _draw_leg(self, curve, label, ds: ExperimentDataset, rows: range,
                  visible: bool, name: str) -> None:
        if not visible or len(rows) == 0:
            curve.setData([], [])
            label.setText("")
            return

        idx = list(rows)
        times = [ds.mission_time[i] for i in idx]
        alts = [ds.altitude[i] for i in idx]
        aero = [ds.aerosol[i] for i in idx]

        # §11.10 — break the line at a time gap. No interpolation, and
        # never a ballistic reconstruction: an estimated aerosol
        # concentration is a fabricated scientific result, which is a
        # different and worse thing than an estimated altitude.
        alts, aero = split_on_time_gaps(alts, aero, times)

        if self._swapped:
            curve.setData(aero, alts)
            anchor = (aero[-1], alts[-1]) if aero else None
        else:
            curve.setData(alts, aero)
            anchor = (alts[-1], aero[-1]) if alts else None

        label.setText(name)
        if anchor and anchor[0] == anchor[0] and anchor[1] == anchor[1]:
            label.setPos(*anchor)

    def _autoscale(self) -> None:
        ds = self.dataset
        if ds is None or ds.empty:
            return
        alt_lo, alt_hi = min(ds.altitude), max(ds.altitude)
        # §8 — aerosol has a hard 0 floor; negative concentration is
        # unphysical, so the axis never shows below zero even if a
        # flagged negative sample is present in the data.
        aero_lo = 0.0
        aero_hi = max(max(ds.aerosol), 10.0)

        if alt_hi - alt_lo < self.MIN_ALTITUDE_SPAN_M:
            mid = 0.5 * (alt_lo + alt_hi)
            alt_lo = mid - self.MIN_ALTITUDE_SPAN_M / 2
            alt_hi = mid + self.MIN_ALTITUDE_SPAN_M / 2

        if self._swapped:
            self.vb.setXRange(aero_lo, aero_hi * 1.05, padding=0)
            self.vb.setYRange(alt_lo, alt_hi, padding=0.02)
        else:
            self.vb.setXRange(alt_lo, alt_hi, padding=0.02)
            self.vb.setYRange(aero_lo, aero_hi * 1.05, padding=0)

    # ── §11.10 zoom ──────────────────────────────────────────────────────

    def set_selected(self, on: bool) -> None:
        self._selected = on
        self.vb.selected = on

    def event(self, ev):
        if ev.type() == ev.Type.NativeGesture:
            if ev.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                if self._selected:
                    self._zoom(1.0 - ev.value())
                ev.accept()
                return True
        return super().event(ev)

    def _zoom(self, factor: float) -> None:
        """§11.10 — both axes together, about the pinch centre, bounded by
        data extent and 5 m of altitude."""
        self.vb.scaleBy((factor, factor))

    def zoom_in(self, factor: Optional[float] = None) -> None:
        """Zoom in on profile plot."""
        f = 0.8 if factor is None or isinstance(factor, bool) else factor
        self._zoom(f)

    def zoom_out(self, factor: Optional[float] = None) -> None:
        """Zoom out on profile plot."""
        f = 1.25 if factor is None or isinstance(factor, bool) else factor
        self._zoom(f)

    def reset_view(self) -> None:
        self._autoscale()


class SecondaryPlot(QWidget):
    """§11.12 — altitude vs time and aerosol vs time.

    Built from the same parsed dataset with no re-parse. These "separate
    genuine altitude-correlated structure from a sensor artefact that
    merely coincides with a flight phase" — a feature that appears at one
    altitude on the profile could be either, and only the time view
    distinguishes them.
    """

    def __init__(self, which: str, parent=None):
        super().__init__(parent)
        self.which = which          # "altitude" | "aerosol"
        c = ThemeManager.C()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        self.vb = ProfileViewBox()
        self.plot = pg.PlotWidget(viewBox=self.vb, background=c.BG_CARD)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        style = {"color": c.TEXT_DIM, "font-size": "9pt"}
        self.plot.setLabel("bottom", "Mission Time (s)", **style)
        self.plot.setLabel(
            "left",
            "Altitude (m AGL)" if which == "altitude"
            else "Aerosol Count (particles/cm³)", **style)
        self.plot.getAxis("left").setWidth(74)
        for side in ("left", "bottom"):
            self.plot.getAxis(side).enableAutoSIPrefix(False)

        colour = c.GREEN if which == "altitude" else c.CYAN
        self._curve = self.plot.plot([], [], pen=pg.mkPen(QColor(colour), width=2),
                                     connect="finite")

        # Graph control toolbar (Reset, Zoom Out, Zoom In)
        tb = QHBoxLayout()
        tb.setContentsMargins(0, 0, 2, 2)
        tb.setSpacing(3)
        tb.addStretch()

        btn_style = ThemeManager.graph_button_stylesheet()

        self.btn_reset = QPushButton("⟲")
        self.btn_reset.setToolTip("Reset view (fit data)")
        self.btn_reset.setStyleSheet(btn_style)
        self.btn_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reset.clicked.connect(lambda: self.reset_view())

        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.setToolTip("Zoom out (-)")
        self.btn_zoom_out.setStyleSheet(btn_style)
        self.btn_zoom_out.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_zoom_out.clicked.connect(lambda: self.zoom_out())

        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setToolTip("Zoom in (+)")
        self.btn_zoom_in.setStyleSheet(btn_style)
        self.btn_zoom_in.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_zoom_in.clicked.connect(lambda: self.zoom_in())

        tb.addWidget(self.btn_reset)
        tb.addWidget(self.btn_zoom_out)
        tb.addWidget(self.btn_zoom_in)
        lay.addLayout(tb)

        lay.addWidget(self.plot)

    def zoom_in(self, factor: Optional[float] = None) -> None:
        """Zoom in on secondary plot."""
        f = 0.8 if factor is None or isinstance(factor, bool) else factor
        self.vb.scaleBy((f, f))

    def zoom_out(self, factor: Optional[float] = None) -> None:
        """Zoom out on secondary plot."""
        f = 1.25 if factor is None or isinstance(factor, bool) else factor
        self.vb.scaleBy((f, f))

    def reset_view(self) -> None:
        """Reset view to auto-fit."""
        self.vb.autoRange()

    def set_dataset(self, ds: Optional[ExperimentDataset]) -> None:
        if ds is None or ds.empty:
            self._curve.setData([], [])
            return
        ys = ds.altitude if self.which == "altitude" else ds.aerosol
        xs, ys = split_on_time_gaps(ds.mission_time, ys, ds.mission_time)
        self._curve.setData(xs, ys)
