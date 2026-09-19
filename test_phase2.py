"""Phase 2 core tests — axes, trajectory, reconciliation, decimation.
No Qt, no pyqtgraph, no hardware.  Run: python3 test_phase2.py"""
import sys
sys.path.insert(0, ".")

from ground_station import axes
from ground_station.models import FlightState
from ground_station.graphs.trajectory import (
    ballistic, powered, parachute, FlightProfile, G,
)
from ground_station.graphs.reconcile import reconcile, OutageTracker
from ground_station.graphs.decimate import minmax_decimate, RingBuffer

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {extra}")
        FAILS.append(name)


print("\n§8 y-axis clamps")
lo, hi = axes.clamp_range(axes.ALTITUDE, 0.02, 0.05)
check("min span enforced on pad noise", abs((hi - lo) - 5.0) < 1e-9, (lo, hi))
check("hard floor respected", lo >= 0.0, lo)
lo, hi = axes.clamp_range(axes.ALTITUDE, 0.0, 2000.0)
check("max extent clamps view", hi <= 1300.0 + 1e-9, hi)
lo, hi = axes.clamp_range(axes.BATTERY, 8.39, 8.40)
check("battery min span at top edge", abs((hi - lo) - 0.1) < 1e-9, (lo, hi))
check("battery stays inside extent", hi <= 8.4 + 1e-9 and lo >= 6.0 - 1e-9, (lo, hi))
lo, hi = axes.clamp_range(axes.ALTITUDE, None, None)
check("no data -> min span at floor", (lo, hi) == (0.0, 5.0), (lo, hi))
lo, hi = axes.clamp_range(axes.PRESSURE, 89000.0, 89100.0)
check("pressure min span 500 Pa", abs((hi - lo) - 500.0) < 1e-9, (lo, hi))

print("\n§6.3/§6.4 axis titles")
check("velocity labelled properly", axes.VELOCITY.axis_title().startswith("Velocity"))
check("descent rate labelled per vehicle",
      axes.DESCENT_RATE.axis_title().startswith("Descent Rate"))
check("same underlying key", axes.VELOCITY.key == axes.DESCENT_RATE.key)
check("pressure not tagged derived", "derived" not in axes.PRESSURE.axis_title())

print("\n§7.5.9 reconcilable channels")
check("altitude reconcilable", "altitude" in axes.RECONCILABLE_KEYS)
check("temperature is not", "temperature" not in axes.RECONCILABLE_KEYS)
check("pressure is not", "pressure" not in axes.RECONCILABLE_KEYS)

print("\n§7.5.5 ballistic arc through both endpoints")
arc = ballistic(10.0, 900.0, 14.0, 880.0)
check("shape ballistic", arc.shape == "ballistic", arc.shape)
check("hits first endpoint exactly", arc.points[0] == (10.0, 900.0))
check("hits last endpoint exactly", arc.points[-1] == (14.0, 880.0))
check("apex inside window", arc.apex and 10.0 < arc.apex[0] < 14.0, arc.apex)
check("apex above both endpoints", arc.apex[1] > 900.0, arc.apex)
peak = max(y for _, y in arc.points)
check("apex is the maximum", abs(peak - arc.apex[1]) < 1.0, (peak, arc.apex))

print("\n§7.5.5 monotonic endpoints must not fabricate a peak")
arc = ballistic(0.0, 0.0, 4.0, 400.0)
check("falls back to straight", not arc.exact, arc.shape)
check("only two points", len(arc.points) == 2)
check("reason recorded", "apex" in arc.note, arc.note)

print("\ntrajectory: powered segment")
arc = powered(0.0, 0.0, 2.0, 100.0, v_entry=None)
check("under-determined -> straight", not arc.exact)
arc = powered(0.0, 0.0, 2.0, 100.0, v_entry=20.0)
check("determinate with v_entry", arc.exact and arc.shape == "powered")
check("endpoints exact", arc.points[0] == (0.0, 0.0) and arc.points[-1] == (2.0, 100.0))

print("\n§7.5.6 reconcile() is state-driven")
a = reconcile(FlightState.COAST, FlightState.COAST, 0, 500, 3, 560)
check("same phase -> straight", a.shape == "straight", a.shape)
a = reconcile(FlightState.DESCENT, FlightState.DESCENT, 0, 500, 3, 460)
check("descent -> parachute", a.shape == "parachute", a.shape)
a = reconcile(FlightState.COAST, FlightState.DESCENT, 20, 950, 26, 930)
check("spans apogee -> ballistic", a.shape == "ballistic", a.shape)
a = reconcile(FlightState.APOGEE, FlightState.DESCENT, 20, 1000, 24, 960)
check("includes apogee -> ballistic", a.shape == "ballistic", a.shape)
a = reconcile(FlightState.BOOST, FlightState.COAST, 2, 200, 4, 400, v_entry=90.0)
check("boost->coast powered", a.shape == "powered", a.shape)
a = reconcile(FlightState.DESCENT, FlightState.BOOST, 40, 400, 44, 380)
check("backward transition -> straight, flagged",
      a.shape == "straight" and "no physical model" in a.note, a.note)

same_states_long = reconcile(FlightState.COAST, FlightState.COAST, 0, 500, 30, 560)
same_states_short = reconcile(FlightState.COAST, FlightState.COAST, 0, 500, 1, 560)
check("duration does not select shape",
      same_states_long.shape == same_states_short.shape)

print("\n§7.5.3/§7.5.4/§7.5.7 outage lifecycle")
t = OutageTracker(reconcilable=True)
t.open_outage(20.0, 950.0, FlightState.COAST)
ph = t.placeholder(23.0)
check("placeholder is flat at y1", ph == [(20.0, 950.0), (23.0, 950.0)], ph)
check("gap_resolved cleared on open", not t.gap_resolved)
r1 = t.close_outage(26.0, 930.0, FlightState.DESCENT)
check("resolved once", r1 is not None and r1.arc.shape == "ballistic")
check("gap_resolved latched", t.gap_resolved)
r2 = t.close_outage(26.0, 930.0, FlightState.DESCENT)
check("second close returns None (§7.5.7)", r2 is None)
check("only one resolved outage stored", len(t.resolved) == 1)
check("placeholder gone after close", t.placeholder(30.0) is None)
t.open_outage(40.0, 500.0, FlightState.DESCENT)
check("gap_resolved cleared by next outage", not t.gap_resolved)

print("\n§7.5.9 non-reconcilable channel breaks instead")
t2 = OutageTracker(reconcilable=False)
t2.open_outage(10.0, 20.0, FlightState.COAST)
r = t2.close_outage(14.0, 19.0, FlightState.DESCENT)
check("no arc computed", r is None)
check("break recorded", t2.breaks == [(10.0, 14.0)], t2.breaks)

print("\n§13.2 min/max decimation")
xs = [i * 0.1 for i in range(10000)]
ys = [0.0] * 10000
ys[5000] = 15.9          # a 1-sample spike, as at burnout
dx, dy = minmax_decimate(xs, ys, 200)
check("output bounded by columns", len(dx) <= 400, len(dx))
check("spike survives decimation", max(dy) == 15.9, max(dy))
check("x stays ordered", all(dx[i] <= dx[i + 1] for i in range(len(dx) - 1)))
check("short series passes through", minmax_decimate(xs[:50], ys[:50], 200)[0] == xs[:50])
check("empty series safe", minmax_decimate([], [], 100) == ([], []))
dx, dy = minmax_decimate([1.0, 1.0, 1.0], [1.0, 5.0, 2.0], 10)
check("zero span safe", len(dx) == 3)

print("\n§13.2 bounded ring buffer")
rb = RingBuffer(capacity=1000)
for i in range(5000):
    rb.append(i * 0.1, float(i))
check("bounded", len(rb) <= 1000, len(rb))
check("keeps newest", rb.ys[-1] == 4999.0)
w_x, w_y = rb.window(450.0, 460.0)
check("window non-empty", len(w_x) > 0)
check("window includes edges", w_x[0] <= 450.0 and w_x[-1] >= 460.0, (w_x[0], w_x[-1]))
lo, hi = rb.y_range(450.0, 460.0)
check("windowed y range", lo is not None and hi > lo)
rb2 = RingBuffer()
check("empty y range is None", rb2.y_range() == (None, None))

print("\n§7.5.5 profile consistent with a 1 km apogee")
p = FlightProfile()
apogee = p.altitude_at(p.time_to_apogee())
check("apogee ~1 km", 900 < apogee < 1300, apogee)
check("burnout velocity derived from apogee", 120 < p.burnout_velocity < 135,
      p.burnout_velocity)
check("apogee is the input, velocity the output",
      abs(FlightProfile(apogee=600.0).altitude_at(
          FlightProfile(apogee=600.0).time_to_apogee()) - 600.0) < 1.0)
check("descent rate in §8 range", 5 <= p.descent_rate <= 15)
check("altitude zero at t=0", p.altitude_at(0.0) == 0.0)
check("lands back at zero", p.altitude_at(p.landing_time() + 1) == 0.0)
check("velocity negative under canopy",
      p.velocity_at(p.time_to_apogee() + 5) < 0)
check("altitude never negative",
      all(p.altitude_at(t * 0.5) >= 0 for t in range(400)))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("all checks passed")
