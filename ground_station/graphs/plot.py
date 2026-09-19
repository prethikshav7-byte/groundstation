"""
ground_station/graphs/plot.py
═════════════════════════════
TelemetryGraph — replaces the old telemetry_plot.py entirely.

The old widget's model was wall-clock watchdog driven: a STALE state
after N seconds of silence, a "held" dashed segment, a dotted reconnect
bridge on resume. §7.5 replaces that with a packet-count-driven three-tier
model, so the two could not be reconciled and this is a rewrite rather
than a patch.

The other structural change: the old widget *derived* its own stale state
from a QTimer. This one does not derive anything. Tiering happens in
parser.py (one computation, §7.5.2), so the health strip and the graph
can never disagree about whether an outage occurred.

WHAT §7 REQUIRES, AND WHERE IT IS
─────────────────────────────────
  §7.1  selection      _set_selected, outline drawn outside the plot area
  §7.2  zoom           trackpad pinch only; wheel explicitly swallowed
  §7.3  x-axis growth  follow mode, broken by manual interaction
  §7.4  axis titles    no legends anywhere; inline end-of-line trace labels
  §7.5  loss           add_point(tier=...) drives the OutageTracker
  §8    y clamps       axes.clamp_range on every autoscale
  §13.2 decimation     min/max per pixel column at wide zoom
  §13.3 render rate    repaint on a timer, never per packet
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .. import axes as axspec
from ..models import FlightState
from ..theme import ThemeManager
from .decimate import RingBuffer, minmax_decimate
from .reconcile import OutageTracker
from ..parser import LossTier


# ─────────────────────────────────────────────────────────────────────────────
#  ViewBox — §7.2 input rules
# ─────────────────────────────────────────────────────────────────────────────

class TelemetryViewBox(pg.ViewBox):
    """A ViewBox that zooms on trackpad pinch and on nothing else.

    pyqtgraph's default is wheel-to-zoom, which §7.2 forbids outright
    ("scroll wheel / trackpad scroll must never zoom a graph, selected or
    not"). Disabling mouse interaction wholesale would also kill the
    pinch path, so the events are intercepted individually.
    """

    pinched = pyqtSignal(float, float)      # scale factor, centre x

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.selected = False
        # §7.2 — zoom affects the x-axis only; y autoscales within §8.
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)

    def wheelEvent(self, ev, axis=None):
        # §7.2, verbatim: scroll must never zoom, selected or not. Accept
        # the event so it does not bubble up to a parent scroll area and
        # move the page under the operator's cursor either.
        ev.accept()

    def mouseDragEvent(self, ev, axis=None):
        # Panning breaks follow mode (§7.3) and is only available while
        # selected, matching the pinch rule.
        if not self.selected:
            ev.ignore()
            return
        super().mouseDragEvent(ev, axis=axis)



# ─────────────────────────────────────────────────────────────────────────────
#  NaN-aware decimation helper
# ─────────────────────────────────────────────────────────────────────────────

def _decimate_with_nans(xs: List[float], ys: List[float],
                        columns: int) -> Tuple[List[float], List[float]]:
    """Decimate a NaN-gapped series while preserving gap boundaries.

    minmax_decimate assumes a contiguous numeric array. Feeding it an array
    that already contains NaN sentinels (inserted by _split_on_breaks) causes
    the decimator to misplace min/max representatives and corrupt the
    connect="finite" breaks. This wrapper splits on the sentinels first,
    decimates each clean segment independently, then rejoins them with NaN
    separators so pyqtgraph's connect="finite" still sees the right positions.
    """
    if not xs:
        return [], []

    out_x: List[float] = []
    out_y: List[float] = []
    seg_x: List[float] = []
    seg_y: List[float] = []

    for x, y in zip(xs, ys):
        if y != y:          # NaN check (NaN != NaN is always True)
            if seg_x:
                dx, dy = minmax_decimate(seg_x, seg_y, columns)
                if out_x:   # insert separator between segments
                    out_x.append(float("nan"))
                    out_y.append(float("nan"))
                out_x.extend(dx)
                out_y.extend(dy)
                seg_x, seg_y = [], []
        else:
            seg_x.append(x)
            seg_y.append(y)

    if seg_x:
        dx, dy = minmax_decimate(seg_x, seg_y, columns)
        if out_x:
            out_x.append(float("nan"))
            out_y.append(float("nan"))
        out_x.extend(dx)
        out_y.extend(dy)

    return out_x, out_y


# ─────────────────────────────────────────────────────────────────────────────
#  The graph
# ─────────────────────────────────────────────────────────────────────────────

class TelemetryGraph(QWidget):
    """One live time-series channel."""

    selection_changed = pyqtSignal(object, bool)     # self, selected

    #: §13.3 — repaint at a fixed rate rather than once per packet.
    #: Telemetry arrives at up to 20 lines/s across both vehicles; this
    #: batches whatever arrived between frames.
    REPAINT_MS = 40                                   # 25 fps

    #: §7.1 — outline thickness, drawn outside the plot area and inside
    #: the widget padding so it never overlaps axis labels or the trace.
    SELECT_BORDER_PX = 2
    SELECT_PADDING_PX = 3

    def __init__(self, axis_key: str, spec: Optional[axspec.AxisSpec] = None,
                 traces: Optional[Sequence[Tuple[str, str]]] = None,
                 parent=None):
        """
        axis_key  key into axes.BY_KEY / axes.ZOOM
        spec      override, e.g. DESCENT_RATE instead of VELOCITY (§6.3)
        traces    [(name, colour)] for multi-trace channels (§7.4).
                  Single-trace graphs pass None.
        """
        super().__init__(parent)
        c = ThemeManager.C()

        self.axis_key = axis_key
        self.spec = spec or axspec.BY_KEY[axis_key]
        self.zoom_spec = axspec.zoom_for(axis_key)

        self._traces = list(traces) if traces else [("", c.CYAN)]
        self._multi = bool(traces) and len(self._traces) > 1

        self._buffers: Dict[str, RingBuffer] = {
            name: RingBuffer() for name, _ in self._traces
        }
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._labels: Dict[str, pg.TextItem] = {}

        # §7.5.9 — only the altitude family reconciles.
        self._outage = OutageTracker(
            reconcilable=axis_key in axspec.RECONCILABLE_KEYS
        )
        self._placeholder_item: Optional[pg.PlotDataItem] = None
        self._resolved_items: List[pg.PlotDataItem] = []

        self._selected = False
        self._follow = True                  # §7.3
        self._x_span = self.zoom_spec.max_zoom_span_s
        self._latest_x = 0.0
        self._dirty = False
        self._state_now: Optional[FlightState] = None
        self._v_entry: Optional[float] = None

        self._build_ui()

        self._repaint = QTimer(self)
        self._repaint.setInterval(self.REPAINT_MS)
        self._repaint.timeout.connect(self._maybe_redraw)
        self._repaint.start()

    # ── construction ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        c = ThemeManager.C()
        layout = QVBoxLayout(self)
        pad = self.SELECT_BORDER_PX + self.SELECT_PADDING_PX
        layout.setContentsMargins(pad, pad, pad, pad)

        self.vb = TelemetryViewBox()
        self.plot = pg.PlotWidget(viewBox=self.vb, background=c.BG_CARD)
        self.plot.setAntialiasing(True)

        # §7.4 — axis titles outside the plot area, no legend anywhere.
        # Fixed label space reserved on both axes so the titles can never
        # overlap tick labels, the plot border, or the trace.
        label_style = {"color": c.TEXT_DIM, "font-size": "9pt"}
        self.plot.setLabel("bottom", "Time (s)", **label_style)
        self.plot.setLabel("left", self.spec.axis_title(), **label_style)
        self.plot.getAxis("left").setWidth(64)
        self.plot.getAxis("bottom").setHeight(38)
        for side in ("left", "bottom"):
            ax = self.plot.getAxis(side)
            ax.enableAutoSIPrefix(False)
            ax.setPen(pg.mkPen(c.BORDER))
            ax.setTextPen(pg.mkPen(c.TEXT_DIM))
            ax.setStyle(tickFont=QFont("monospace", 8))

        self.plot.showGrid(x=True, y=True, alpha=0.12)

        for name, colour in self._traces:
            pen = pg.mkPen(QColor(colour), width=2)
            self._curves[name] = self.plot.plot([], [], pen=pen)
            if self._multi:
                # §7.4 — multi-trace graphs still need trace
                # identification, but with inline end-of-line labels
                # rather than a floating legend box.
                lbl = pg.TextItem(name, color=colour, anchor=(0, 0.5))
                lbl.setFont(QFont("monospace", 8))
                self.plot.addItem(lbl)
                self._labels[name] = lbl

        # Graph control toolbar (Reset, Zoom Out, Zoom In)
        tb = QHBoxLayout()
        tb.setContentsMargins(0, 0, 2, 2)
        tb.setSpacing(3)
        tb.addStretch()

        btn_style = ThemeManager.graph_button_stylesheet()

        self.btn_reset = QPushButton("⟲")
        self.btn_reset.setToolTip("Reset view (auto-follow)")
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
        layout.addLayout(tb)

        layout.addWidget(self.plot)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        # §20 — 230 px floor: left-axis (64) + bottom-axis (38) +
        # selection padding (10) already consume 112 px, leaving 118 px of
        # actual plot area. That is enough for axis titles to clear tick
        # labels and the trace (§7.4) and avoids the §13.1 layout
        # collision that occurs at the old 160 px minimum.
        self.setMinimumHeight(230)
        # Needed for the trackpad pinch gesture on macOS/Windows; the
        # gesture arrives as a QNativeGestureEvent, not a wheel event.
        self.grabGesture(Qt.GestureType.PinchGesture)

    # ── §7.1 selection ───────────────────────────────────────────────────

    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, on: bool) -> None:
        if on == self._selected:
            return
        self._selected = on
        self.vb.selected = on
        self.update()
        self.selection_changed.emit(self, on)

    def mousePressEvent(self, ev) -> None:
        # §7.1 — tap toggles. One selected graph per dashboard is enforced
        # by the page, which listens to selection_changed.
        self.set_selected(not self._selected)
        ev.accept()

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if not self._selected:
            return
        # §7.1 — a thin outline along the plot border, drawn outside the
        # plot area and inside the widget's padding, so it cannot overlap
        # axis labels or the plot line.
        c = ThemeManager.C()
        p = QPainter(self)
        p.setPen(QPen(QColor(c.CYAN), self.SELECT_BORDER_PX))
        inset = self.SELECT_BORDER_PX // 2
        p.drawRect(self.rect().adjusted(inset, inset, -inset - 1, -inset - 1))
        p.end()

    # ── §7.2 zoom ────────────────────────────────────────────────────────

    def event(self, ev):
        # Trackpad pinch arrives as a native gesture on the platforms in
        # scope (desktop, trackpad). Handled here rather than in
        # wheelEvent precisely because §7.2 requires the two to behave
        # differently — pinch zooms, scroll must not.
        if ev.type() == ev.Type.NativeGesture:
            if ev.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                if self._selected:
                    self._apply_zoom(1.0 - ev.value())
                ev.accept()
                return True
        return super().event(ev)

    def _apply_zoom(self, factor: float) -> None:
        """Zoom the time window, bounded per §7.2.

        Bounded means it *stops* at the bound — no error, no extrapolation,
        no inversion. Clamping the span before applying it is what
        guarantees that; an unclamped multiply could pass through zero and
        invert the axis.
        """
        if self._follow:
            self._x_span = max(self._latest_x, self.zoom_spec.max_zoom_span_s)
        min_span = self.zoom_spec.max_zoom_span_s      # tightest view
        max_span = max(self._latest_x, min_span)       # §7.3 full mission
        new_span = self._x_span * max(0.1, factor)
        self._x_span = max(min_span, min(max_span, new_span))
        self._follow = False                           # §7.3 manual breaks follow
        self._dirty = True

    def zoom_in(self, factor: Optional[float] = None) -> None:
        """Zoom in on the time window."""
        f = 0.75 if factor is None or isinstance(factor, bool) else factor
        self._apply_zoom(f)
        self._redraw()

    def zoom_out(self, factor: Optional[float] = None) -> None:
        """Zoom out on the time window."""
        f = 1.33 if factor is None or isinstance(factor, bool) else factor
        self._apply_zoom(f)
        self._redraw()

    def reset_view(self) -> None:
        """§7.3 — Reset returns to min zoom following live data, not to a
        fixed default range."""
        self._x_span = max(self._latest_x, self.zoom_spec.max_zoom_span_s)
        self._follow = True
        self._dirty = True
        self._redraw()

    # ── data in ──────────────────────────────────────────────────────────

    def add_point(self, x: float, y: float, trace: str = "",
                  tier: LossTier = LossTier.NONE,
                  state: Optional[FlightState] = None,
                  v_entry: Optional[float] = None) -> None:
        """Add one sample.

        `tier` comes from the parser (§7.5.2) rather than being derived
        here — see the module docstring. `state` is the vehicle's reported
        state, needed by §7.5.6's state-driven reconciliation.
        """
        name = trace or self._traces[0][0]
        buf = self._buffers.get(name)
        if buf is None:
            return

        if state is not None:
            self._state_now = state
        if v_entry is not None:
            self._v_entry = v_entry

        # ── close an open outage, if any (§7.5.4) ────────────────────────
        if self._outage.is_open:
            resolved = self._outage.close_outage(x, y, self._state_now)
            self._clear_placeholder()
            if resolved is not None:
                self._draw_resolved(resolved)

        # ── open a new one, or note a break (§7.5.2) ─────────────────────
        if buf.xs:
            last_x, last_y = buf.xs[-1], buf.ys[-1]
            if tier is LossTier.OUTAGE:
                self._outage.open_outage(last_x, last_y, self._state_now,
                                         v_entry=self._v_entry)
            elif tier is LossTier.GAP:
                # §7.5.2 — break the line, no interpolation, marked
                # subtly. Recorded so the renderer can split the curve.
                self._outage.note_break(last_x, x)
            # DROPOUT connects straight across as a normal line with no
            # annotation, so there is deliberately nothing to do here.

        buf.append(x, y)
        self._latest_x = max(self._latest_x, x)
        self._dirty = True

    def mark_annotated_outage(self, x: float, y: float, cause: str) -> None:
        """§7.5.10 — a refresh (§10.3) or source drop (§3.9) dropout is
        annotated as such rather than reconciled blindly."""
        self._outage.open_outage(x, y, self._state_now,
                                 v_entry=self._v_entry, cause=cause)

    def clear(self) -> None:
        for b in self._buffers.values():
            b.clear()
        self._outage.reset()
        self._clear_placeholder()
        for item in self._resolved_items:
            self.plot.removeItem(item)
        self._resolved_items.clear()
        self._latest_x = 0.0
        self.reset_view()

    # ── rendering ────────────────────────────────────────────────────────

    def _maybe_redraw(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        self._redraw()

    def _redraw(self) -> None:
        # ── x window (§7.3) ──────────────────────────────────────────────
        if self._follow:
            self._x_span = max(self._latest_x, self.zoom_spec.max_zoom_span_s)
        x_hi = self._latest_x
        x_lo = x_hi - self._x_span
        self.vb.setXRange(x_lo, x_hi, padding=0)

        # ── traces, decimated per pixel column (§13.2) ───────────────────
        columns = max(64, int(self.plot.width()))
        lo_all: Optional[float] = None
        hi_all: Optional[float] = None

        # §13.2 / Fix 2 — trim break records that are older than the
        # buffer's oldest retained sample; they can never be drawn and
        # their accumulation makes _split_on_breaks() O(N) on every frame.
        oldest_xs = [buf.xs[0] for buf in self._buffers.values() if buf.xs]
        if oldest_xs:
            self._outage.trim_breaks(min(oldest_xs))

        for name, _ in self._traces:
            buf = self._buffers[name]
            xs, ys = buf.window(x_lo, x_hi)
            if not xs:
                continue

            xs, ys = self._split_on_breaks(xs, ys)
            # §13.2 — decimate each NaN-separated segment independently so
            # that gap boundaries (NaN sentinels) survive the decimation
            # step. Passing the NaN-marked arrays straight to minmax_decimate
            # corrupts them: the decimator treats NaN as a numeric value and
            # the connect="finite" breaks no longer land where they should.
            dx, dy = _decimate_with_nans(xs, ys, columns)
            self._curves[name].setData(dx, dy, connect="finite")

            b_lo, b_hi = min(v for v in ys if v == v), max(v for v in ys if v == v)
            lo_all = b_lo if lo_all is None else min(lo_all, b_lo)
            hi_all = b_hi if hi_all is None else max(hi_all, b_hi)

            if name in self._labels:
                # Walk back to the last real sample — ys[-1] can be a NaN
                # sentinel inserted by _split_on_breaks().  Positioning a
                # TextItem at NaN silently drops it off-screen.
                label_x, label_y = xs[-1], ys[-1]
                for lx, ly in zip(reversed(xs), reversed(ys)):
                    if ly == ly:   # NaN != NaN is always True
                        label_x, label_y = lx, ly
                        break
                self._labels[name].setPos(label_x, label_y)

        # ── y range, clamped per §8 ──────────────────────────────────────
        y_lo, y_hi = axspec.clamp_range(self.spec, lo_all, hi_all)
        self.vb.setYRange(y_lo, y_hi, padding=0)

        # ── §7.5.3 placeholder ───────────────────────────────────────────
        ph = self._outage.placeholder(self._latest_x)
        if ph is not None:
            self._draw_placeholder(ph)

    def _split_on_breaks(self, xs: List[float], ys: List[float]
                         ) -> Tuple[List[float], List[float]]:
        """Insert NaN at GAP boundaries so the curve breaks.

        NaN with connect="finite" is how pyqtgraph draws a discontinuity.
        The alternative — one PlotDataItem per fragment — would allocate a
        new item per gap and leak them over a lossy flight.
        """
        if not self._outage.breaks:
            return xs, ys
        out_x, out_y = [], []
        breaks = [b for b in self._outage.breaks if xs[0] <= b[1] and b[0] <= xs[-1]]
        if not breaks:
            return xs, ys
        bi = 0
        for i, (x, y) in enumerate(zip(xs, ys)):
            while bi < len(breaks) and x > breaks[bi][1]:
                bi += 1
            if bi < len(breaks) and breaks[bi][0] < x < breaks[bi][1]:
                if out_x and out_x[-1] == out_x[-1]:
                    out_x.append(0.5 * (breaks[bi][0] + breaks[bi][1]))
                    out_y.append(float("nan"))
                continue
            out_x.append(x)
            out_y.append(y)
        return out_x, out_y

    def _draw_placeholder(self, pts: List[Tuple[float, float]]) -> None:
        c = ThemeManager.C()
        if self._placeholder_item is None:
            # §7.5.3 — dashed and greyed. Not a solid flat line, which
            # reads as "altitude held constant" and is misleading
            # telemetry.
            pen = pg.mkPen(QColor(c.TEXT_MUTED), width=2,
                           style=Qt.PenStyle.DashLine)
            self._placeholder_item = self.plot.plot([], [], pen=pen)
        self._placeholder_item.setData([p[0] for p in pts], [p[1] for p in pts])

    def _clear_placeholder(self) -> None:
        # §7.5.4 — delete the placeholder, do not leave it under the
        # reconciliation segment.
        if self._placeholder_item is not None:
            self.plot.removeItem(self._placeholder_item)
            self._placeholder_item = None

    def _draw_resolved(self, resolved) -> None:
        c = ThemeManager.C()
        # §7.5.8 — reconciled segments render dotted. They are display
        # aids, never data, and are never written to the raw log (§12.3).
        colour = c.AMBER if resolved.annotated else c.TEXT_DIM
        pen = pg.mkPen(QColor(colour), width=2, style=Qt.PenStyle.DotLine)
        item = self.plot.plot([p[0] for p in resolved.arc.points],
                              [p[1] for p in resolved.arc.points], pen=pen)
        self._resolved_items.append(item)

    # ── introspection, used by the page and the health strip ─────────────

    @property
    def outage_count(self) -> int:
        return len(self._outage.resolved)

    def latest(self, trace: str = "") -> Optional[Tuple[float, float]]:
        buf = self._buffers[trace or self._traces[0][0]]
        if not buf.xs:
            return None
        return buf.xs[-1], buf.ys[-1]
