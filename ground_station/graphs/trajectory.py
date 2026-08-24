"""
ground_station/graphs/trajectory.py
═══════════════════════════════════
Piecewise constant-acceleration flight physics.

§7.5.5 requires the outage-reconciliation curve to "reuse the same physics
module as the flight simulator, so the two cannot disagree". This is that
module, and it is deliberately the *only* one: the Phase 4 simulator
imports from here rather than growing its own flight model.

That clause is doing real work. If the two drifted apart, the ballistic
segment drawn across an outage would stop matching the flight it claims
to reconstruct — and the simulator, which is the only way any of §7.5 gets
tested (§13.6), would be validating the reconstruction against a
different physics than the one the operator sees. The bug would be
invisible in exactly the test built to catch it.

Qt-free and pyqtgraph-free, so it can be tested headlessly.

WHAT "PARAMETERISED TO PASS EXACTLY THROUGH BOTH ENDPOINTS" MEANS HERE
─────────────────────────────────────────────────────────────────────
The endpoints are measurements; the curve between them is not. So the
fit is constrained to hit (x1,y1) and (x2,y2) exactly and the free
parameter is the initial velocity, not the endpoints. A least-squares
curve that missed both endpoints slightly would be a smoother lie: it
would put the drawn line somewhere no packet ever said the vehicle was.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

G = 9.80665             # m/s², standard gravity

Point = Tuple[float, float]


@dataclass(frozen=True)
class Arc:
    """A reconstructed segment.

    `exact` is False when the requested shape could not be fitted through
    both endpoints and a straight line was substituted. The caller must
    surface that rather than swallowing it — a straight line drawn where
    the operator expects a ballistic arc is a different claim about the
    flight, and §7.5.3's reasoning (a flat line reads as "altitude held
    constant", which is misleading telemetry) applies just as much to a
    silently downgraded curve.
    """
    points: List[Point]
    shape: str
    exact: bool = True
    apex: Optional[Point] = None
    note: str = ""


def straight(x1: float, y1: float, x2: float, y2: float,
             shape: str = "straight", note: str = "") -> Arc:
    return Arc(points=[(x1, y1), (x2, y2)], shape=shape, note=note)


# ─────────────────────────────────────────────────────────────────────────────
#  Gravity-only ballistic arc
# ─────────────────────────────────────────────────────────────────────────────

def ballistic(x1: float, y1: float, x2: float, y2: float,
              g: float = G, samples: int = 48) -> Arc:
    """Constant-acceleration (−g) arc through both endpoints.

        y(t) = y1 + v0·t − ½·g·t²,   t measured from x1

    Both endpoints fix v0 exactly:

        v0 = (y2 − y1 + ½·g·T²) / T,   T = x2 − x1

    The apex sits at t = v0/g. If that falls outside [0, T] the vehicle
    was not at apogee during this interval — the endpoints describe a
    purely rising or purely falling segment — and a parabola with its
    peak outside the window would be a worse reconstruction than a
    straight line, so one is substituted and flagged.
    """
    T = x2 - x1
    if T <= 0:
        return straight(x1, y1, x2, y2, "degenerate",
                        "non-positive time span")

    v0 = (y2 - y1 + 0.5 * g * T * T) / T
    t_apex = v0 / g

    if not (0.0 < t_apex < T):
        return Arc(
            points=[(x1, y1), (x2, y2)],
            shape="ballistic→straight",
            exact=False,
            note=("apex falls outside the outage window; endpoints "
                  "describe a monotonic segment, so a parabola would "
                  "invent a peak that did not occur"),
        )

    pts: List[Point] = []
    for i in range(samples + 1):
        t = T * i / samples
        pts.append((x1 + t, y1 + v0 * t - 0.5 * g * t * t))

    # Force the endpoints to be bit-exact rather than the result of
    # floating-point accumulation — they are measurements and must land
    # exactly where the packets said.
    pts[0] = (x1, y1)
    pts[-1] = (x2, y2)

    apex = (x1 + t_apex, y1 + v0 * t_apex - 0.5 * g * t_apex * t_apex)
    return Arc(points=pts, shape="ballistic", apex=apex)


# ─────────────────────────────────────────────────────────────────────────────
#  Powered ascent
# ─────────────────────────────────────────────────────────────────────────────

def powered(x1: float, y1: float, x2: float, y2: float,
            v_entry: Optional[float] = None, samples: int = 32) -> Arc:
    """Constant net upward acceleration through both endpoints (thrust).

    With an entry velocity known from the samples just before the outage,
    the acceleration is fixed by the endpoints:

        a = 2·(Δy − v_entry·T) / T²

    Without one, the segment is under-determined — infinitely many
    (v_entry, a) pairs pass through the same two points — so rather than
    inventing a plausible-looking one, this falls back to a straight line
    and says so.
    """
    T = x2 - x1
    if T <= 0:
        return straight(x1, y1, x2, y2, "degenerate",
                        "non-positive time span")

    if v_entry is None:
        return Arc(
            points=[(x1, y1), (x2, y2)],
            shape="powered→straight",
            exact=False,
            note=("no entry velocity available; a powered segment through "
                  "two points is under-determined"),
        )

    a = 2.0 * ((y2 - y1) - v_entry * T) / (T * T)
    pts = []
    for i in range(samples + 1):
        t = T * i / samples
        pts.append((x1 + t, y1 + v_entry * t + 0.5 * a * t * t))
    pts[0], pts[-1] = (x1, y1), (x2, y2)
    return Arc(points=pts, shape="powered")


# ─────────────────────────────────────────────────────────────────────────────
#  Parachute-limited descent
# ─────────────────────────────────────────────────────────────────────────────

def parachute(x1: float, y1: float, x2: float, y2: float,
              samples: int = 8) -> Arc:
    """Terminal-velocity descent — constant rate, so a straight line.

    Kept as a named shape rather than folded into straight() because the
    two mean different things: this asserts the vehicle was at terminal
    velocity under canopy, which is a physical claim the caller made by
    choosing it. Merging them would lose that distinction in the logs.
    """
    return straight(x1, y1, x2, y2, "parachute")


# ─────────────────────────────────────────────────────────────────────────────
#  Full profile — used by the Phase 4 simulator (§13.6)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FlightProfile:
    """A nominal 1 km flight, expressed as the same piecewise
    constant-acceleration segments §7.5.5 reconstructs with.

    ⚠️ §8's two altitude/velocity bases are not simultaneously satisfiable
    under a drag-free model, and this is worth knowing before anyone
    "corrects" the numbers here.

    §8 gives a 1 km apogee target and, separately, ~150–200 m/s at
    burnout. With gravity as the only force, a vehicle at 180 m/s coasts
    v²/2g ≈ 1,650 m *after* burnout, reaching roughly 1,900 m — nearly
    twice the target. The missing ~900 m is aerodynamic drag, which a
    piecewise constant-acceleration model does not represent and §7.5.5
    does not ask it to.

    Resolved by treating apogee as the input and deriving burnout
    velocity from it, because apogee is the mission requirement and is
    what §8's altitude extent (0–1,300 m) is actually built on. The
    derived figure lands near 127 m/s — below §8's band, and the gap is
    the drag the model omits.

    CONSEQUENCE FOR §7.5.5 RECONCILIATION, which is the part that matters
    operationally: the ballistic arc drawn across an outage is also
    drag-free, so it overestimates altitude in the middle of the span.
    Over a 2 s outage (the §7.5.2 OUTAGE threshold) the error is a metre
    or so and irrelevant. Over a 20 s outage during high-speed coast it
    could be tens of metres. The arc is dotted and never logged (§7.5.8),
    so it is presented as the estimate it is — but if long outages turn
    out to be common in practice, adding a quadratic drag term here is
    the fix, and it would automatically apply to both the reconstruction
    and the simulator because they share this module.
    """
    apogee: float = 1000.0          # m AGL — the mission target
    burn_time: float = 2.8          # s of thrust
    descent_rate: float = 12.0      # m/s under canopy

    @property
    def burnout_velocity(self) -> float:
        """Velocity at burnout implied by the apogee target.

        From  ½·v·T (powered rise) + v²/2g (coast rise) = apogee,
        solved for v.
        """
        a = 1.0 / (2.0 * G)
        b = 0.5 * self.burn_time
        c = -self.apogee
        return (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)

    def burnout_altitude(self) -> float:
        return 0.5 * self.burnout_velocity * self.burn_time

    def coast_time(self) -> float:
        return self.burnout_velocity / G

    def time_to_apogee(self) -> float:
        return self.burn_time + self.coast_time()

    def altitude_at(self, t: float) -> float:
        """Altitude in m AGL at mission time t."""
        if t <= 0:
            return 0.0
        if t < self.burn_time:
            a = self.burnout_velocity / self.burn_time
            return 0.5 * a * t * t
        t_apogee = self.time_to_apogee()
        if t < t_apogee:
            dt = t - self.burn_time
            return (self.burnout_altitude()
                    + self.burnout_velocity * dt - 0.5 * G * dt * dt)
        peak = self.altitude_at(t_apogee - 1e-9)
        return max(0.0, peak - self.descent_rate * (t - t_apogee))

    def velocity_at(self, t: float) -> float:
        if t <= 0:
            return 0.0
        if t < self.burn_time:
            return (self.burnout_velocity / self.burn_time) * t
        t_apogee = self.time_to_apogee()
        if t < t_apogee:
            return self.burnout_velocity - G * (t - self.burn_time)
        return 0.0 if self.altitude_at(t) <= 0.0 else -self.descent_rate

    def landing_time(self) -> float:
        peak = self.altitude_at(self.time_to_apogee())
        return self.time_to_apogee() + peak / self.descent_rate
