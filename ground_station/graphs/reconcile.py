"""
ground_station/graphs/reconcile.py
══════════════════════════════════
§7.5.3–§7.5.9 — what a graph draws across an outage.

The lifecycle, in the spec's own order:

  §7.5.3  outage opens  → a dashed, greyed placeholder extends forward
                          from (x1,y1) at constant y. Explicitly NOT a
                          flat solid line, which reads as "altitude held
                          constant" and is misleading telemetry.
  §7.5.4  outage closes → delete the placeholder, replace with a single
                          reconciliation segment (x1,y1)→(x2,y2).
  §7.5.5  segment shape comes from the states either side.
  §7.5.6  signature is reconcile(state_before, state_after, x1,y1,x2,y2)
          — state-driven, not time-driven.
  §7.5.7  computed ONCE per outage-closure as a static point set, guarded
          by a gap_resolved flag cleared only when the next outage opens.
          Never recomputed per frame.
  §7.5.8  rendered dotted; never written to the raw log (§12.3).
  §7.5.9  altitude-family graphs only.

Qt-free so the rules are testable without a display.

WHY §7.5.7's FLAG IS A REAL REQUIREMENT AND NOT BOOKKEEPING
───────────────────────────────────────────────────────────
Reconciliation runs on outage *closure*, an event. A graph redraws tens
of times a second. Recomputing a 48-point arc every frame for every
outage in a long flight is the unbounded-growth failure §13.2 rules out —
and worse, it would be work that produces an identical answer each time,
so it would never look wrong, only slow. The flag makes the
compute-once property structural rather than something the render path
has to remember.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..models import FlightState
from .trajectory import Arc, ballistic, parachute, powered, straight

Point = Tuple[float, float]


# ─────────────────────────────────────────────────────────────────────────────
#  §7.5.5 / §7.5.6 — shape selection
# ─────────────────────────────────────────────────────────────────────────────

#: States between which the vehicle passes through apogee. Used to decide
#: whether the outage "spans or includes APOGEE" (§7.5.5).
_ASCENDING = (FlightState.BOOST, FlightState.COAST)
_DESCENDING = (FlightState.DESCENT, FlightState.LANDING, FlightState.RECOVERY)


def reconcile(state_before: FlightState,
              state_after: FlightState,
              x1: float, y1: float,
              x2: float, y2: float,
              v_entry: Optional[float] = None) -> Arc:
    """§7.5.6 — the required signature, exactly.

    State-driven, not time-driven: the duration of the outage never
    selects the shape. A four-second gap inside COAST and a four-second
    gap spanning apogee want completely different reconstructions, and
    only the states distinguish them.

    `v_entry` is an optional extension, not part of the §7.5.6 signature:
    the derived velocity (§6.4) immediately before the outage, used only
    to make a powered segment determinate. Everything works without it.
    """
    # ── same phase both sides → straight line (§7.5.5) ───────────────────
    if state_before is state_after:
        if state_before in _DESCENDING:
            # Under canopy the vehicle really is at a constant rate, so
            # the straight line is a physical claim rather than a
            # fallback. Named accordingly.
            return parachute(x1, y1, x2, y2)
        return straight(x1, y1, x2, y2)

    # ── outage spans or includes APOGEE → ballistic (§7.5.5) ─────────────
    spans_apogee = (
        state_before is FlightState.APOGEE
        or state_after is FlightState.APOGEE
        or (state_before in _ASCENDING and state_after in _DESCENDING)
    )
    if spans_apogee:
        return ballistic(x1, y1, x2, y2)

    # ── powered ascent into coast ────────────────────────────────────────
    if state_before is FlightState.BOOST and state_after is FlightState.COAST:
        return powered(x1, y1, x2, y2, v_entry=v_entry)

    # ── anything else ────────────────────────────────────────────────────
    # Includes backward transitions, which §5.2 requires be displayed
    # rather than corrected. A ballistic arc through a state regression
    # would assert a flight that did not happen, so the honest shape is a
    # straight line carrying the reason.
    return straight(
        x1, y1, x2, y2, "straight",
        f"no physical model for {state_before.value} → {state_after.value}",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  §7.5.3 / §7.5.4 / §7.5.7 — the per-graph state machine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ResolvedOutage:
    """One closed outage, computed once and then immutable (§7.5.7)."""
    arc: Arc
    x1: float
    y1: float
    x2: float
    y2: float
    state_before: FlightState
    state_after: FlightState
    #: §7.5.10 — outages caused by a refresh (§10.3) or a source drop
    #: (§3.9) are annotated as such rather than reconciled blindly.
    cause: str = "link"

    @property
    def annotated(self) -> bool:
        return self.cause != "link"


class OutageTracker:
    """Per-graph outage lifecycle.

    One instance per plotted channel. Holds no Qt objects, so the whole
    of §7.5 can be exercised in tests.
    """

    def __init__(self, reconcilable: bool = False):
        #: §7.5.9 — altitude-family graphs only. Elsewhere an outage is a
        #: line break and this tracker only records the break.
        self.reconcilable = reconcilable

        self._open: bool = False
        self._x1: float = 0.0
        self._y1: float = 0.0
        self._state_before: Optional[FlightState] = None
        self._v_entry: Optional[float] = None
        self._cause: str = "link"

        #: §7.5.7 — cleared only when the next outage opens.
        self.gap_resolved: bool = False

        self.resolved: List[ResolvedOutage] = []
        #: Time ranges where the line must simply break (§7.5.2 GAP tier,
        #: and every OUTAGE on a non-reconcilable channel).
        self.breaks: List[Tuple[float, float]] = []

    # ── §7.5.3 ───────────────────────────────────────────────────────────

    def open_outage(self, x1: float, y1: float,
                    state_before: Optional[FlightState],
                    v_entry: Optional[float] = None,
                    cause: str = "link") -> None:
        self._open = True
        self._x1, self._y1 = x1, y1
        self._state_before = state_before
        self._v_entry = v_entry
        self._cause = cause
        self.gap_resolved = False        # §7.5.7

    def placeholder(self, now_x: float) -> Optional[List[Point]]:
        """§7.5.3 — dashed, greyed segment extending forward at constant y.

        Returns the two points to draw; the *styling* (dashed, greyed) is
        the renderer's job, but it is not optional. A solid flat line here
        would read as a real altitude hold.
        """
        if not self._open:
            return None
        return [(self._x1, self._y1), (max(now_x, self._x1), self._y1)]

    @property
    def is_open(self) -> bool:
        return self._open

    # ── §7.5.4 / §7.5.7 ──────────────────────────────────────────────────

    def close_outage(self, x2: float, y2: float,
                     state_after: Optional[FlightState]
                     ) -> Optional[ResolvedOutage]:
        """Resolve the open outage exactly once.

        Returns the ResolvedOutage on the closing call and None on any
        repeat, so a caller that re-invokes this from a render path gets
        nothing rather than a fresh recomputation.
        """
        if not self._open:
            return None
        if self.gap_resolved:
            return None

        self._open = False
        self.gap_resolved = True

        if not self.reconcilable:
            # §7.5.9 — line break only.
            self.breaks.append((self._x1, x2))
            return None

        before = self._state_before or FlightState.BOOT
        after = state_after or before
        arc = reconcile(before, after, self._x1, self._y1, x2, y2,
                        v_entry=self._v_entry)

        out = ResolvedOutage(arc=arc, x1=self._x1, y1=self._y1, x2=x2, y2=y2,
                             state_before=before, state_after=after,
                             cause=self._cause)
        self.resolved.append(out)
        return out

    # ── §7.5.2 GAP tier ──────────────────────────────────────────────────

    def note_break(self, x1: float, x2: float) -> None:
        """A GAP (5–19 packets): break the line, no interpolation, no
        reconciliation, marked subtly by the renderer."""
        self.breaks.append((x1, x2))

    def reset(self) -> None:
        self._open = False
        self.gap_resolved = False
        self.resolved.clear()
        self.breaks.clear()
