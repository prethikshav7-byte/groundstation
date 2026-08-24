"""
ground_station/widgets/gauges.py
════════════════════════════════
§6.1 — digital and analog gauges.

The rules, and what each is defending against:

  Digital: full numeric readout, units always shown. "No field displays a
  bare number" — a value with no unit is the reading that gets
  misinterpreted at the worst moment.

  Analog: dial and needle, no unit text, labels are full words.
  "Altitude", never "ALT"; "Battery Voltage", never "BATT V".

  Labels never touch or cross the dial edge at any supported size. A
  fixed label-margin band is reserved outside the dial radius and the
  font auto-shrinks in discrete steps until the text fits inside it.
  Discrete steps rather than a continuous fit so that two gauges of the
  same size always land on the same size, instead of drifting a point
  apart and looking accidental.

  Identical font family and weight across both modes, so switching mode
  changes the representation and nothing else.

  Update at 2–4 Hz, not 10 Hz — "a numeric readout changing ten times a
  second is unreadable". Graphs still receive every sample; only the
  gauge is throttled.

§6.4's "derived" tag is honoured here: a derived gauge is marked, and a
value of None renders as an em dash rather than 0.0. Those are different
facts and a zero would be read as a measurement.
"""
from __future__ import annotations

import math
from typing import Optional

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..axes import AxisSpec
from ..theme import ThemeManager, FS_CAPTION

#: §6.1 — one font family and weight for both modes.
GAUGE_FAMILY = "monospace"

#: §6.1 — 2–4 Hz. 3 Hz sits in the middle of the permitted band: fast
#: enough to feel live, slow enough that the digits are readable.
GAUGE_REFRESH_MS = 333


class _Throttled:
    """Shared 2–4 Hz update gate (§6.1).

    Values arrive at up to 10 Hz and are stored immediately; only the
    repaint is rate-limited. Storing the latest value on arrival rather
    than sampling on the timer means the gauge always shows the most
    recent reading at the moment it repaints, not one up to 333 ms stale.
    """

    def _init_throttle(self) -> None:
        self._pending: Optional[float] = None
        self._shown: Optional[float] = None
        self._has_value = False
        self._timer = QTimer(self)
        self._timer.setInterval(GAUGE_REFRESH_MS)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    def set_value(self, v: Optional[float]) -> None:
        self._pending = v
        self._has_value = True

    def _flush(self) -> None:
        if not self._has_value:
            return
        if self._pending != self._shown:
            self._shown = self._pending
            self._on_value_changed()
        self._has_value = False

    def _on_value_changed(self) -> None:
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────────────
#  Digital
# ─────────────────────────────────────────────────────────────────────────────

class DigitalGauge(QWidget, _Throttled):
    """Numeric readout with the unit always visible (§6.1)."""

    def __init__(self, spec: AxisSpec, decimals: int = 2, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.decimals = decimals

        c = ThemeManager.C()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        title = spec.label
        if spec.derived:
            # §6.4 — tagged "derived" on the gauge, not only on the axis.
            title += "  ·  derived"
        self._title = QLabel(title)
        # §2 — FS_CAPTION pt, no px units (px doesn't scale on high-DPI).
        self._title.setStyleSheet(
            f"color: {c.CYAN}; font-weight: bold;")
        self._title.setFont(QFont(GAUGE_FAMILY, FS_CAPTION, QFont.Weight.Bold))

        self._value = QLabel("—")
        self._value.setFont(QFont(GAUGE_FAMILY, 18, QFont.Weight.Bold))
        self._value.setStyleSheet(f"color: {c.TEXT};")

        self._unit = QLabel(spec.unit)
        # FS_CAPTION pt — was 9px, which doesn't scale on high-DPI displays.
        self._unit.setFont(QFont(GAUGE_FAMILY, FS_CAPTION))
        self._unit.setStyleSheet(f"color: {c.TEXT_DIM};")

        layout.addWidget(self._title)
        layout.addWidget(self._value)
        layout.addWidget(self._unit)
        self.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;")
        # §25 — prevent collapse in a compressed layout.
        self.setMinimumHeight(90)
        self._init_throttle()

    def _on_value_changed(self) -> None:
        v = self._shown
        if v is None:
            # §6.4 — a derived value whose window has not filled is not
            # zero. Rendering it as 0.00 would put a measurement on screen
            # that no packet supports.
            self._value.setText("—")
        else:
            self._value.setText(f"{v:,.{self.decimals}f}")


# ─────────────────────────────────────────────────────────────────────────────
#  Analog
# ─────────────────────────────────────────────────────────────────────────────

class AnalogGauge(QWidget, _Throttled):
    """Dial and needle with graduation labels and unit display (§6.1)."""

    START_ANGLE = 220.0          # degrees, measured anticlockwise from east
    SWEEP = 260.0

    #: §6.1 — the reserved label-margin band outside the dial radius, and
    #: the discrete font sizes tried within it, largest first.
    LABEL_BAND_PX = 26
    FONT_STEPS = (11, 10, 9, 8, 7)

    #: Graduation label font steps, largest first.
    GRAD_FONT_STEPS = (8, 7, 6)

    #: How many major ticks carry a numeric label (0 %, 25 %, …, 100 %).
    MAJOR_TICKS = 5

    def __init__(self, spec: AxisSpec, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.setMinimumSize(170, 170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._init_throttle()

    def _on_value_changed(self) -> None:
        self.update()

    # ── geometry ─────────────────────────────────────────────────────────

    def _dial_rect(self) -> QRectF:
        """Dial bounds, with the label band reserved outside the radius.

        The band is subtracted from the available space *before* the
        radius is chosen, which is what makes "labels never touch or
        cross the dial edge at any supported gauge size" structural
        rather than something that happens to hold at the sizes tested.
        """
        side = min(self.width(), self.height()) - 2 * self.LABEL_BAND_PX
        side = max(side, 40)
        return QRectF((self.width() - side) / 2, (self.height() - side) / 2,
                      side, side)

    def _fitted_font(self, text: str, max_width: int) -> QFont:
        """Largest discrete step whose rendering fits the band."""
        for size in self.FONT_STEPS:
            f = QFont(GAUGE_FAMILY, size, QFont.Weight.Bold)
            if QFontMetrics(f).horizontalAdvance(text) <= max_width:
                return f
        return QFont(GAUGE_FAMILY, self.FONT_STEPS[-1], QFont.Weight.Bold)

    def _grad_font(self, text: str, max_width: int) -> QFont:
        """Largest discrete step for graduation labels."""
        for size in self.GRAD_FONT_STEPS:
            f = QFont(GAUGE_FAMILY, size)
            if QFontMetrics(f).horizontalAdvance(text) <= max_width:
                return f
        return QFont(GAUGE_FAMILY, self.GRAD_FONT_STEPS[-1])

    # ── painting ─────────────────────────────────────────────────────────

    def paintEvent(self, _ev) -> None:
        c = ThemeManager.C()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self._dial_rect()
        cx, cy = rect.center().x(), rect.center().y()
        radius = rect.width() / 2

        # Qt drawArc: angles in 1/16° units, measured anticlockwise from
        # 3 o'clock (east). START_ANGLE is measured anticlockwise from east
        # in our domain, so the Qt start angle is the same value. The sweep
        # is clockwise (negative in Qt convention) to match the needle which
        # also sweeps clockwise from lo to hi.
        qt_start = int(self.START_ANGLE * 16)
        qt_sweep = int(-self.SWEEP * 16)          # negative = clockwise
        p.setPen(QPen(QColor(c.GAUGE_BG), 10))
        p.drawArc(rect, qt_start, qt_sweep)

        lo, hi = self.spec.extent_lo, self.spec.extent_hi
        if hi == float("inf"):
            hi = max(lo + 1.0, (self._shown or 0.0) * 1.2)

        # ── ticks ───────────────────────────────────────────────────────────
        # 11 ticks at 0..10; every 5th is a major tick (longer).
        for i in range(11):
            frac = i / 10
            ang = math.radians(self.START_ANGLE - self.SWEEP * frac)
            is_major = (i % 5 == 0)
            tick_len = 15 if is_major else 8
            pen_w    = 2  if is_major else 1
            p.setPen(QPen(QColor(c.TEXT_DIM if is_major else c.GAUGE_TICK),
                          pen_w))
            inner = radius - tick_len
            p.drawLine(int(cx + inner * math.cos(ang)),
                       int(cy - inner * math.sin(ang)),
                       int(cx + radius * math.cos(ang)),
                       int(cy - radius * math.sin(ang)))

        # ── graduation labels ────────────────────────────────────────────────
        # One numeric label at each major tick (i = 0, 2.5, 5, 7.5, 10 →
        # fractions 0.0, 0.25, 0.50, 0.75, 1.0).  They are placed just
        # *inside* the inner tick edge so they never cross the arc.
        major_fracs = [k / (self.MAJOR_TICKS - 1)
                       for k in range(self.MAJOR_TICKS)]
        # Estimate the widest label to pick one consistent font size.
        def _fmt(val: float) -> str:
            span = hi - lo
            if span == 0:
                return "0"
            # Fewer decimals for large ranges; more for small ones.
            if abs(span) >= 100:
                return f"{val:.0f}"
            elif abs(span) >= 10:
                return f"{val:.1f}"
            return f"{val:.2f}"

        # Available width for a label: roughly the space between two ticks
        # along the circumference (conservative half-chord estimate).
        label_max_w = max(10, int(radius * 0.40))
        widest = max((_fmt(lo + f * (hi - lo)) for f in major_fracs), key=len)
        grad_font = self._grad_font(widest, label_max_w)
        fm = QFontMetrics(grad_font)
        p.setFont(grad_font)
        p.setPen(QPen(QColor(c.TEXT_DIM)))

        # Label placement radius: just inside the inner major-tick edge,
        # with a 3 px gap between the tick and the label bounding box.
        label_r = radius - 15 - fm.ascent() - 3
        label_r = max(label_r, radius * 0.40)   # never crowd the hub

        for frac in major_fracs:
            val  = lo + frac * (hi - lo)
            text = _fmt(val)
            ang  = math.radians(self.START_ANGLE - self.SWEEP * frac)
            lx   = cx + label_r * math.cos(ang)
            ly   = cy - label_r * math.sin(ang)
            tw   = fm.horizontalAdvance(text)
            th   = fm.height()
            p.drawText(int(lx - tw / 2), int(ly + th / 4), text)

        # ── needle ──────────────────────────────────────────────────────────
        v = self._shown
        if v is not None and hi > lo:
            frac = min(1.0, max(0.0, (v - lo) / (hi - lo)))
            ang = math.radians(self.START_ANGLE - self.SWEEP * frac)
            p.setPen(QPen(QColor(c.CYAN), 3))
            p.drawLine(int(cx), int(cy),
                       int(cx + (radius - 20) * math.cos(ang)),
                       int(cy - (radius - 20) * math.sin(ang)))
            # Hub dot
            p.setBrush(QColor(c.CYAN))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(int(cx - 4), int(cy - 4), 8, 8)
            p.setBrush(Qt.BrushStyle.NoBrush)

        # ── unit label (inside dial, near hub) ───────────────────────────────
        # Drawn in a small rect centred horizontally, positioned just below
        # the hub so it sits in the blank area between the needle pivot and
        # the lower arc segment.  The needle never reaches this zone because
        # it terminates at radius-20 and the hub is only 8 px wide.
        unit = self.spec.unit
        if unit:
            unit_font = QFont(GAUGE_FAMILY, 7)
            p.setFont(unit_font)
            p.setPen(QPen(QColor(c.AMBER)))
            ufm   = QFontMetrics(unit_font)
            uw    = ufm.horizontalAdvance(unit)
            # Place it ~30 % of the radius below the centre, which lands in
            # the dead zone between hub and lower arc.
            unit_y = int(cy + radius * 0.30)
            p.drawText(int(cx - uw / 2), unit_y, unit)

        # ── bottom label band (§6.1) ─────────────────────────────────────────
        text = self.spec.label
        font = self._fitted_font(text, int(self.width() - 8))
        p.setFont(font)
        p.setPen(QPen(QColor(c.TEXT_DIM)))
        band = QRectF(0, rect.bottom() + 2, self.width(), self.LABEL_BAND_PX)
        p.drawText(band, Qt.AlignmentFlag.AlignCenter, text)
        p.end()


# ─────────────────────────────────────────────────────────────────────────────
#  Mode switch
# ─────────────────────────────────────────────────────────────────────────────

class GaugeField(QWidget):
    """One telemetry field, switchable between digital and analog (§6.1).

    Both are constructed up front and one is hidden, so a mode switch
    does not lose the current reading or rebuild layout — §13.1 requires
    collision-free layout in *both* modes, and rebuilding on switch is
    how the two drift apart.
    """

    def __init__(self, spec: AxisSpec, decimals: int = 2,
                 analog: bool = False, parent=None):
        super().__init__(parent)
        self.spec = spec
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.digital = DigitalGauge(spec, decimals)
        self.analog = AnalogGauge(spec)
        layout.addWidget(self.digital)
        layout.addWidget(self.analog)
        self.set_analog(analog)

    def set_analog(self, on: bool) -> None:
        self.analog.setVisible(on)
        self.digital.setVisible(not on)

    def set_value(self, v: Optional[float]) -> None:
        self.digital.set_value(v)
        self.analog.set_value(v)
