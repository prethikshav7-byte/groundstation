"""
ground_station/axes.py
══════════════════════
§8 — y-axis auto-scale clamps.

§7.2 governs the x-axis (time window). This governs the y-axis:

    max extent   the widest the axis may grow. Data beyond it is still
                 plotted and still logged — the clamp bounds the *view*,
                 never the dataset. A packet reporting 1,400 m is real
                 data and is written to the log; the altitude axis simply
                 does not zoom out past 1,300 m to chase it.

    min span     the tightest the axis may shrink, so it never collapses
                 onto sensor noise. Without it, a barometer sitting on
                 the pad autoscales to its own ±0.1 m jitter and the plot
                 shows a dramatic-looking altitude trace of nothing at
                 all.

Every basis figure in §8 is preserved in the `basis` field rather than
being dropped once the number was copied — when someone later asks why
acceleration stops at 16 g, the answer ("ADXL345 at ±16 g") should be in
the same place as the number.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class AxisSpec:
    """One row of the §8 table."""
    key: str
    label: str                  # §7.4 y-axis title, full words
    unit: str
    extent_lo: float
    extent_hi: float
    min_span: float
    basis: str = ""
    #: True where the quantity cannot physically go below extent_lo, so
    #: the axis floor is hard rather than merely a clamp (§8: altitude is
    #: AGL zeroed at the pad; negative aerosol concentration is
    #: unphysical).
    hard_floor: bool = False
    #: §6.4 — tagged "derived" in the axis title. Only velocity/descent
    #: rate qualifies; it is the one displayed quantity absent from the
    #: packet.
    derived: bool = False

    def axis_title(self) -> str:
        """§7.4 — y-axis title text, placed outside the plot area."""
        base = f"{self.label} ({self.unit})" if self.unit else self.label
        return f"{base} — derived" if self.derived else base


# ─────────────────────────────────────────────────────────────────────────────
#  The §8 table
# ─────────────────────────────────────────────────────────────────────────────

ALTITUDE = AxisSpec(
    "altitude", "Altitude", "m AGL", 0.0, 1300.0, 5.0,
    "1 km target + ~30% overshoot; 0 floor since AGL is zeroed at the pad",
    hard_floor=True,
)
VELOCITY = AxisSpec(
    "velocity", "Velocity", "m/s", -50.0, 200.0, 2.0,
    "~150–200 m/s burnout; −5 to −15 m/s under chute; −50 margin for "
    "ballistic fallback",
    derived=False,
)
DESCENT_RATE = AxisSpec(
    # §6.3 — same computation as VELOCITY, different label, per-vehicle
    # constant and never state-dependent. Two specs rather than one with a
    # runtime label so nothing downstream can accidentally make the name
    # depend on flight phase.
    "velocity", "Descent Rate", "m/s", -50.0, 200.0, 2.0,
    "identical computation to Velocity; §6.3 labels it per vehicle",
    derived=False,
)
ACCELERATION = AxisSpec(
    "acceleration", "Acceleration", "m/s²", -16.0, 16.0, 0.5,
    "ADXL345 at ±16 g (confirmed)",
)
ACCEL_MAGNITUDE = AxisSpec(
    "accel_magnitude", "Accel Magnitude", "m/s²", 0.0, 30.0, 0.2,
    "||a|| = sqrt(ax²+ay²+az²); max is sqrt(3)×16 ≈ 27.7 g; 0 floor (magnitude)",
    hard_floor=True,
)
PRESSURE = AxisSpec(
    "pressure", "Pressure", "Pa", 90000.0, 140000.0, 500.0,
    "0.9–1.4 bar (90–140 kPa); covers sea level to above-ambient range",
)
TEMPERATURE = AxisSpec(
    "temperature", "Temperature", "°C", -20.0, 60.0, 1.0,
    "practical envelope",
)
BATTERY = AxisSpec(
    "battery_voltage", "Battery Voltage", "V", 6.0, 8.4, 0.1,
    "2S LiPo (confirmed)",
)
ORIENTATION = AxisSpec(
    "orientation", "Gyro Spin Rate", "deg/s", -2000.0, 2000.0, 10.0,
    "BNO055 gyro full scale",
)
AEROSOL = AxisSpec(
    "aerosol", "Aerosol Count", "particles/cm³", 0.0, float("inf"), 10.0,
    "§11; 0 floor, negative concentration is unphysical",
    hard_floor=True,
)
RSSI = AxisSpec(
    "rssi", "Signal Strength", "dBm", -130.0, 0.0, 10.0,
    "LoRa receive range",
)

BY_KEY: Dict[str, AxisSpec] = {
    "altitude":        ALTITUDE,
    "velocity":        VELOCITY,
    "descent_rate":    DESCENT_RATE,
    "acceleration":    ACCELERATION,
    "accel_magnitude": ACCEL_MAGNITUDE,
    "pressure":        PRESSURE,
    "temperature":     TEMPERATURE,
    "battery_voltage": BATTERY,
    "orientation":     ORIENTATION,
    "aerosol":         AEROSOL,
    "rssi":            RSSI,
}

#: §7.5.9 — reconciliation applies to the altitude family only. A
#: ballistic curve drawn through a temperature or battery gap is
#: meaningless, so everywhere else an outage is simply a line break.
RECONCILABLE_KEYS = frozenset({"altitude"})


# ─────────────────────────────────────────────────────────────────────────────
#  Clamping
# ─────────────────────────────────────────────────────────────────────────────

def clamp_range(spec: AxisSpec,
                data_lo: Optional[float],
                data_hi: Optional[float],
                pad_fraction: float = 0.05) -> Tuple[float, float]:
    """Return the y-range to display for a data range.

    Order of operations matters and is not arbitrary:

      1. no data          → a min_span window at the floor (or at zero)
      2. pad the data range slightly so the trace does not touch the frame
      3. widen to min_span if narrower, keeping the data centred
      4. clamp to the max extent
      5. re-widen if step 4 crushed it below min_span

    Step 5 exists because 3 and 4 can fight: a min_span wider than the
    remaining headroom at an extent edge would otherwise produce an axis
    narrower than min_span, i.e. the collapse-onto-noise that min_span
    exists to prevent.
    """
    lo_bound, hi_bound = spec.extent_lo, spec.extent_hi

    if data_lo is None or data_hi is None:
        lo = lo_bound if spec.hard_floor else 0.0
        if not _finite(lo):
            lo = 0.0
        return lo, lo + spec.min_span

    lo, hi = float(min(data_lo, data_hi)), float(max(data_lo, data_hi))

    pad = (hi - lo) * pad_fraction
    lo, hi = lo - pad, hi + pad

    if hi - lo < spec.min_span:
        mid = 0.5 * (lo + hi)
        lo, hi = mid - spec.min_span / 2, mid + spec.min_span / 2

    if _finite(lo_bound):
        lo = max(lo, lo_bound)
    if _finite(hi_bound):
        hi = min(hi, hi_bound)

    if hi - lo < spec.min_span:
        if _finite(lo_bound) and lo <= lo_bound:
            hi = lo + spec.min_span
            if _finite(hi_bound) and hi > hi_bound:
                hi = hi_bound
                lo = hi - spec.min_span
        else:
            lo = hi - spec.min_span

    return lo, hi


def _finite(v: float) -> bool:
    return v == v and abs(v) != float("inf")


# ─────────────────────────────────────────────────────────────────────────────
#  §7.2 — x-axis zoom bounds, per graph
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ZoomSpec:
    """Minimum visible time window in seconds at maximum zoom (§7.2).

    Minimum zoom is always "full mission so far" and therefore not a
    constant — it grows with the data (§7.3), so it is computed at use
    rather than stored.
    """
    max_zoom_span_s: float
    #: Samples visible at max zoom at the nominal 10 Hz, straight from the
    #: §7.2 table. Kept so the figure can be shown in a tooltip rather
    #: than the operator having to work it out.
    samples_at_max_zoom: int


ZOOM: Dict[str, ZoomSpec] = {
    "altitude":        ZoomSpec(5.0, 50),
    "velocity":        ZoomSpec(2.0, 20),
    "descent_rate":    ZoomSpec(5.0, 50),   # §7.2 groups it with Altitude
    "acceleration":    ZoomSpec(2.0, 20),
    "accel_magnitude": ZoomSpec(2.0, 20),
    "temperature":     ZoomSpec(10.0, 100),
    "pressure":        ZoomSpec(10.0, 100),
    "orientation":     ZoomSpec(5.0, 50),
    "battery_voltage": ZoomSpec(10.0, 100),
    "rssi":            ZoomSpec(10.0, 100),
}


def zoom_for(key: str) -> ZoomSpec:
    return ZOOM.get(key, ZoomSpec(5.0, 50))
