"""
ground_station/derived.py
═════════════════════════
Ground-derived velocity / descent rate (§6.4).

This is the ONLY displayed quantity absent from the packet (§4.1 has no
velocity field), and it is fenced accordingly:

  §1.3   derived, display-only, never feeds a command decision
  §6.3   labelled "Velocity" for the Rocket, "Descent Rate" for the
         CanSat, per-vehicle constant, never state-dependent
  §6.4   tagged "derived" on the gauge and in the axis title
  §12.3  never written to the raw log

The window matters more than it looks. §6.4 requires differentiating over
a rolling ~0.5 s window rather than sample to sample, because at 100 ms
spacing barometric noise dominates the true signal: an MS5611 at ~0.1 m
RMS differenced over 0.1 s produces roughly ±1.4 m/s of pure noise, which
is the same order as the real descent rate under a parachute. Over 0.5 s
that falls by about the square root of the sample count and the signal
survives.

A least-squares slope is used rather than (last − first) / Δt. Both use
the same window, but the endpoint difference throws away the samples in
between and so keeps most of the noise it was meant to suppress.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple


class DerivedVelocity:
    """Rolling least-squares derivative of barometric altitude.

    Not thread-safe; own one per vehicle and feed it from the UI thread
    (§13.5 — no shared mutable state between the two streams).
    """

    def __init__(self, window_s: float = 0.5, min_samples: int = 5):
        #: §6.4 — "a rolling ~0.5 s window (5 samples at 10 Hz)". Both
        #: bounds are enforced: the window is a *duration*, so it stays
        #: correct if §3.11 option 3 drops the downlink rate, and the
        #: sample minimum stops a slope being fitted through two points.
        self.window_s = window_s
        self.min_samples = min_samples
        self._samples: Deque[Tuple[float, float]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def add(self, mission_time: float, altitude: float) -> Optional[float]:
        """Add one sample; return the current derived rate in m/s, or None
        while the window is still filling.

        None is deliberate and must be rendered as "no value yet", never
        as 0.0 — a zero descent rate on the pad and an unpopulated window
        look identical on a gauge, and only one of them is a measurement.
        """
        self._samples.append((mission_time, altitude))

        cutoff = mission_time - self.window_s
        while len(self._samples) > self.min_samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        if len(self._samples) < self.min_samples:
            return None

        return _lsq_slope(self._samples)

    def on_gap(self) -> None:
        """Call when the stream tiers a GAP or OUTAGE (§7.5.2).

        The window must be dropped rather than carried across the gap: a
        slope fitted from samples either side of a two-second outage is
        not a velocity, it is the average of whatever happened in between,
        and under a parachute it would read as a plausible number rather
        than an obvious error.
        """
        self._samples.clear()

    @property
    def ready(self) -> bool:
        return len(self._samples) >= self.min_samples


def _lsq_slope(samples) -> Optional[float]:
    n = len(samples)
    t0 = samples[0][0]                    # shift for conditioning
    sum_t = sum_v = sum_tt = sum_tv = 0.0
    for t, v in samples:
        t -= t0
        sum_t += t
        sum_v += v
        sum_tt += t * t
        sum_tv += t * v

    denom = n * sum_tt - sum_t * sum_t
    if denom == 0.0:
        # Every sample shares one timestamp — a stuck mission-time field.
        return None
    return (n * sum_tv - sum_t * sum_v) / denom
